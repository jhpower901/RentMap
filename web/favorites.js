(function () {
  'use strict';

  const KEY = 'rentmap_favorites';
  const DELETED_KEY = 'rentmap_favorites_deleted';

  function fk(id, source) { return String(source) + '::' + String(id); }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (_) { return []; } }
  function loadDeleted() { try { return JSON.parse(localStorage.getItem(DELETED_KEY) || '{}'); } catch (_) { return {}; } }
  function saveDeleted(deleted) { localStorage.setItem(DELETED_KEY, JSON.stringify(deleted || {})); }
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
    localStorage.setItem(KEY, JSON.stringify(merged));
    saveDeleted(deleted);
    window.dispatchEvent(new CustomEvent('favoritesSynced'));
    return { favorites: merged, deleted };
  }
  
  function save(favs, deleted = loadDeleted()) { 
    localStorage.setItem(KEY, JSON.stringify(favs)); 
    saveDeleted(deleted);
    syncToServer(favs, deleted);
  }

  function syncToServer(favs, deleted = loadDeleted()) {
    return fetch('/api/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favorites: favs, deleted })
    })
      .then(r => r.ok ? r.json() : null)
      .then(payload => {
        if (payload) applyServerState(payload);
      })
      .catch(err => console.error('Failed to sync favorites to server:', err));
  }

  function fetchServerState() {
    return fetch('/api/favorites/state', { cache: 'no-store' })
      .then(r => {
        if (r.ok) return r.json();
        return fetch('/api/favorites', { cache: 'no-store' }).then(fallback => fallback.ok ? fallback.json() : []);
      });
  }

  const ready = fetchServerState()
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

  function getAll() { return load(); }
  function isFav(id, source) { const k = fk(id, source); return load().some(f => f.key === k); }

  function add(listing) {
    const favs = load();
    const deleted = loadDeleted();
    const k = fk(listing.id, listing.source);
    delete deleted[k];
    const i = favs.findIndex(f => f.key === k);
    const entry = {
      key: k, id: listing.id, source: listing.source,
      data: listing, savedAt: new Date().toISOString(), rating: null, notes: '',
    };
    if (i >= 0) { entry.rating = favs[i].rating; entry.notes = favs[i].notes; favs[i] = entry; }
    else favs.push(entry);
    save(favs, deleted);
  }

  function remove(id, source) {
    const k = fk(id, source);
    const deleted = loadDeleted();
    deleted[k] = new Date().toISOString();
    save(load().filter(f => f.key !== k), deleted);
  }

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
    favs.push({ key: fk(id, 'manual'), id, source: 'manual', data: listing, savedAt: new Date().toISOString(), rating: null, notes: data.notes || '' });
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

  window.Favorites = { getAll, isFav, add, remove, updateRating, updateNotes, addManual, addPhoto, getPhotos, deletePhoto, ready };
})();
