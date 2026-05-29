(function () {
  'use strict';

  const LEGACY_KEY = 'rentmap_favorites';
  const LEGACY_DELETED_KEY = 'rentmap_favorites_deleted';
  let storageKeys = null;
  let currentUserScope = null;

  function fk(id, source) { return String(source) + '::' + String(id); }
  function entryKind(entry) {
    return entry && entry.kind === 'dislike' ? 'dislike' : 'like';
  }
  function scopedKey(base, user) {
    if (user && user.id !== undefined && user.id !== null) return base + ':user:' + String(user.id);
    return base + ':anonymous';
  }
  function configureStorage(user) {
    const nextScope = user && user.id !== undefined && user.id !== null ? String(user.id) : 'anonymous';
    const changed = currentUserScope !== null && currentUserScope !== nextScope;
    currentUserScope = nextScope;
    storageKeys = {
      favorites: scopedKey(LEGACY_KEY, user),
      deleted: scopedKey(LEGACY_DELETED_KEY, user),
    };
    return changed;
  }
  function ensureStorageFresh() {
    if (!window.Auth || !window.Auth.me) {
      if (!storageKeys) configureStorage(null);
      return Promise.resolve(false);
    }
    return window.Auth.me().then(user => configureStorage(user));
  }
  function load() {
    if (!storageKeys) return [];
    try { return JSON.parse(localStorage.getItem(storageKeys.favorites) || '[]'); } catch (_) { return []; }
  }
  function loadDeleted() {
    if (!storageKeys) return {};
    try { return JSON.parse(localStorage.getItem(storageKeys.deleted) || '{}'); } catch (_) { return {}; }
  }
  function saveDeleted(deleted) {
    if (!storageKeys) return;
    localStorage.setItem(storageKeys.deleted, JSON.stringify(deleted || {}));
  }
  function entryTime(entry) {
    const t = Date.parse(entry && entry.savedAt);
    return Number.isFinite(t) ? t : 0;
  }
  function deletedTime(deleted, key) {
    const t = Date.parse(deleted && deleted[key]);
    return Number.isFinite(t) ? t : 0;
  }
  function normalizePayload(payload) {
    if (Array.isArray(payload)) return { favorites: payload, deleted: {} };
    if (payload && typeof payload === 'object') {
      return {
        favorites: Array.isArray(payload.favorites) ? payload.favorites : [],
        deleted: payload.deleted && typeof payload.deleted === 'object' ? payload.deleted : {},
      };
    }
    return { favorites: [], deleted: {} };
  }
  function mergeDeleted(a, b) {
    const out = { ...(a || {}) };
    Object.entries(b || {}).forEach(([key, value]) => {
      if (!out[key] || deletedTime({ [key]: value }, key) > deletedTime(out, key)) out[key] = value;
    });
    return out;
  }
  function mergeFavorites(a, b, deleted) {
    const byKey = new Map();
    [...(a || []), ...(b || [])].forEach(entry => {
      if (!entry || !entry.key) return;
      if (deletedTime(deleted, entry.key) >= entryTime(entry)) return;
      const prev = byKey.get(entry.key);
      if (!prev || entryTime(entry) >= entryTime(prev)) byKey.set(entry.key, entry);
    });
    return [...byKey.values()].sort((x, y) => entryTime(y) - entryTime(x));
  }
  function applyServerState(payload) {
    const serverState = normalizePayload(payload);
    const deleted = mergeDeleted(loadDeleted(), serverState.deleted);
    const merged = mergeFavorites(load(), serverState.favorites, deleted);
    if (storageKeys) localStorage.setItem(storageKeys.favorites, JSON.stringify(merged));
    saveDeleted(deleted);
    window.dispatchEvent(new CustomEvent('favoritesSynced'));
    return { favorites: merged, deleted };
  }

  function save(favs, deleted = loadDeleted()) {
    if (storageKeys) localStorage.setItem(storageKeys.favorites, JSON.stringify(favs));
    saveDeleted(deleted);
    syncToServer(favs, deleted);
  }

  function syncToServer(favs, deleted = loadDeleted()) {
    return ensureStorageFresh()
      .then(changed => {
        const payloadFavs = changed ? load() : favs;
        const payloadDeleted = changed ? loadDeleted() : deleted;
        return fetch('/api/favorites', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Rentmap-User-Id': currentUserScope || '',
          },
          credentials: 'same-origin',
          body: JSON.stringify({ favorites: payloadFavs, deleted: payloadDeleted })
        });
      })
      .then(r => r.ok ? r.json() : null)
      .then(payload => {
        if (payload) applyServerState(payload);
      })
      .catch(err => console.error('Failed to sync favorites to server:', err));
  }

  function fetchServerState() {
    return fetch('/api/favorites/state', { cache: 'no-store', credentials: 'same-origin' })
      .then(r => {
        if (r.ok) return r.json();
        return fetch('/api/favorites', { cache: 'no-store', credentials: 'same-origin' }).then(fallback => fallback.ok ? fallback.json() : []);
      });
  }

  // Public refresh — pull server state and merge into local, then dispatch
  // favoritesSynced so any page listening can re-render. Used by platform
  // pages on visibilitychange so a tab that's been backgrounded for hours
  // catches up to changes another device made in the meantime.
  function refresh() {
    return ensureStorageFresh()
      .then(() => fetchServerState())
      .then(payload => applyServerState(payload))
      .catch(err => {
        console.warn('Favorites refresh failed:', err);
        return null;
      });
  }

  const ready = ensureStorageFresh()
    .then(() => fetchServerState())
    .then(serverPayload => {
      const before = normalizePayload(serverPayload);
      const serverJson = JSON.stringify(before);
      const mergedState = applyServerState(serverPayload);
      const mergedJson = JSON.stringify(mergedState);
      console.log('Favorites synced:', mergedState.favorites.length, 'items');

      if (mergedJson !== serverJson) {
        syncToServer(mergedState.favorites, mergedState.deleted);
      }
      return mergedState.favorites;
    })
    .catch(err => {
      console.warn('Server sync failed, using local storage only:', err);
      window.dispatchEvent(new CustomEvent('favoritesSynced'));
      return load();
    });

  // ── Public queries ──────────────────────────────────────────────────────
  // getAll() preserves the historical "likes only" semantics so callers that
  // pre-date the dislike feature keep behaving the same way. New callers use
  // getDislikes() or getAllEntries() when they want the wider view.
  function getAll() { return load().filter(f => entryKind(f) === 'like'); }
  function getDislikes() { return load().filter(f => entryKind(f) === 'dislike'); }
  function getAllEntries() { return load(); }
  function isFav(id, source) {
    const k = fk(id, source);
    return load().some(f => f.key === k && entryKind(f) === 'like');
  }
  function isDislike(id, source) {
    const k = fk(id, source);
    return load().some(f => f.key === k && entryKind(f) === 'dislike');
  }

  // ── Mutations ───────────────────────────────────────────────────────────
  // Likes and dislikes share the same key namespace ({source}::{id}) so a
  // listing can only be in one bucket at a time. Toggling from like to
  // dislike upserts the entry with kind='dislike'; the server merge sees a
  // newer savedAt for the same key and replaces the older one.
  function upsert(listing, kind) {
    const favs = load();
    const deleted = loadDeleted();
    const k = fk(listing.id, listing.source);
    delete deleted[k];
    const i = favs.findIndex(f => f.key === k);
    const entry = {
      key: k, id: listing.id, source: listing.source,
      data: listing, savedAt: new Date().toISOString(),
      kind,
      rating: null, notes: '',
    };
    if (i >= 0) {
      // Preserve user-entered metadata (rating/notes) across like<->dislike toggles.
      entry.rating = favs[i].rating;
      entry.notes = favs[i].notes;
      favs[i] = entry;
    } else {
      favs.push(entry);
    }
    save(favs, deleted);
  }

  function add(listing) { upsert(listing, 'like'); }
  function addDislike(listing) { upsert(listing, 'dislike'); }

  function remove(id, source) {
    const k = fk(id, source);
    const deleted = loadDeleted();
    deleted[k] = new Date().toISOString();
    save(load().filter(f => f.key !== k), deleted);
  }
  // Removing a dislike is identical to remove() — tombstone the key. Kept as a
  // distinct name so UI code reads as the user's intent ("undo the 👎") rather
  // than the underlying storage mechanic.
  function removeDislike(id, source) { remove(id, source); }

  function updateRating(id, source, rating) {
    const favs = load();
    const i = favs.findIndex(f => f.key === fk(id, source));
    if (i >= 0) { favs[i].rating = rating; save(favs); }
  }

  function updateNotes(id, source, notes) {
    const favs = load();
    const i = favs.findIndex(f => f.key === fk(id, source));
    if (i >= 0) { favs[i].notes = notes; save(favs); }
  }

  function addManual(data) {
    const id = 'manual_' + Date.now();
    const listing = {
      id, source: 'manual',
      url: data.url || '', agency: data.agency || '', phone: '',
      region: '', address: data.address || '',
      lat: data.lat || null, lon: data.lon || null,
      title: data.title || '', deposit: data.deposit || null,
      rent: data.rent || null, maint: data.maint || null,
      total: (data.rent && data.maint) ? data.rent + data.maint : (data.rent || null),
      type: data.type || '', area: data.area || '', floor: data.floor || '',
      img1: '', img2: '',
    };
    const favs = load();
    const deleted = loadDeleted();
    delete deleted[fk(id, 'manual')];
    favs.push({
      key: fk(id, 'manual'), id, source: 'manual', data: listing,
      savedAt: new Date().toISOString(),
      kind: 'like',
      rating: null, notes: data.notes || '',
    });
    save(favs, deleted);
    return id;
  }

  function addPhoto(id, source, file) {
    const formData = new FormData();
    formData.append('file', file);
    return fetch(`/api/photos?id=${encodeURIComponent(id)}&source=${encodeURIComponent(source)}`, {
      method: 'POST',
      body: formData
    }).then(r => r.json());
  }

  function getPhotos(id, source) {
    return fetch(`/api/photos?id=${encodeURIComponent(id)}&source=${encodeURIComponent(source)}`)
      .then(r => r.ok ? r.json() : []);
  }

  function deletePhoto(id, source, photoKey) {
    return fetch(`/api/photos?id=${encodeURIComponent(id)}&source=${encodeURIComponent(source)}&photoKey=${encodeURIComponent(photoKey)}`, {
      method: 'DELETE'
    }).then(r => r.json());
  }

  window.Favorites = {
    getAll, getDislikes, getAllEntries,
    isFav, isDislike,
    add, addDislike, remove, removeDislike,
    updateRating, updateNotes, addManual,
    addPhoto, getPhotos, deletePhoto,
    ready, refresh,
  };
})();
