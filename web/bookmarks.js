(function () {
  'use strict';

  // bookmarks.js — companion to favorites.js. Same offline-merge-via-savedAt
  // model, same key shape ("{source}::{id}"), same localStorage-then-server
  // sync loop. The differences:
  //   - There's no like/dislike axis. A listing is bookmarked or it isn't.
  //   - Each entry carries a sortOrder for the "첫 번째 본 방 / 두 번째 본 방"
  //     sequence the user manages by hand. add() auto-assigns max+1.
  //   - Each entry carries `note` (free text) and `checklist` (array of
  //     {label, done}) — those are bookmark-specific (not on favorites).
  //   - Photos are uploaded through the same /api/photos endpoint favorites
  //     uses (file path is user+source+listing_no keyed) so a photo taken
  //     while bookmarking shows up on the favorites detail panel and vice
  //     versa. Bookmarks.addPhoto is a thin alias for Favorites.addPhoto.

  const LS_KEY = 'rentmap_bookmarks';
  const LS_DELETED_KEY = 'rentmap_bookmarks_deleted';
  let storageKeys = null;
  let currentUserScope = null;

  function fk(id, source) { return String(source) + '::' + String(id); }
  function scopedKey(base, user) {
    if (user && user.id !== undefined && user.id !== null) return base + ':user:' + String(user.id);
    return base + ':anonymous';
  }
  function configureStorage(user) {
    const nextScope = user && user.id !== undefined && user.id !== null ? String(user.id) : 'anonymous';
    const changed = currentUserScope !== null && currentUserScope !== nextScope;
    currentUserScope = nextScope;
    storageKeys = {
      bookmarks: scopedKey(LS_KEY, user),
      deleted: scopedKey(LS_DELETED_KEY, user),
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
    try { return JSON.parse(localStorage.getItem(storageKeys.bookmarks) || '[]'); } catch (_) { return []; }
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
    if (Array.isArray(payload)) return { bookmarks: payload, deleted: {} };
    if (payload && typeof payload === 'object') {
      return {
        bookmarks: Array.isArray(payload.bookmarks) ? payload.bookmarks : [],
        deleted: payload.deleted && typeof payload.deleted === 'object' ? payload.deleted : {},
      };
    }
    return { bookmarks: [], deleted: {} };
  }
  function mergeDeleted(a, b) {
    const out = { ...(a || {}) };
    Object.entries(b || {}).forEach(([key, value]) => {
      if (!out[key] || deletedTime({ [key]: value }, key) > deletedTime(out, key)) out[key] = value;
    });
    return out;
  }
  function mergeBookmarks(a, b, deleted) {
    const byKey = new Map();
    [...(a || []), ...(b || [])].forEach(entry => {
      if (!entry || !entry.key) return;
      if (deletedTime(deleted, entry.key) >= entryTime(entry)) return;
      const prev = byKey.get(entry.key);
      if (!prev || entryTime(entry) >= entryTime(prev)) byKey.set(entry.key, entry);
    });
    return [...byKey.values()].sort((x, y) => {
      const ax = Number(x.sortOrder) || 0;
      const bx = Number(y.sortOrder) || 0;
      if (ax !== bx) return ax - bx;
      // Ties resolve to add-order so the newest "I just visited" lands at the
      // bottom rather than colliding with an older row of the same number.
      return entryTime(x) - entryTime(y);
    });
  }
  function applyServerState(payload) {
    const serverState = normalizePayload(payload);
    const deleted = mergeDeleted(loadDeleted(), serverState.deleted);
    const merged = mergeBookmarks(load(), serverState.bookmarks, deleted);
    if (storageKeys) localStorage.setItem(storageKeys.bookmarks, JSON.stringify(merged));
    saveDeleted(deleted);
    window.dispatchEvent(new CustomEvent('bookmarksSynced'));
    return { bookmarks: merged, deleted };
  }

  function save(bookmarks, deleted = loadDeleted()) {
    if (storageKeys) localStorage.setItem(storageKeys.bookmarks, JSON.stringify(bookmarks));
    saveDeleted(deleted);
    // Fire immediately on every local change so a listener (e.g. the platform
    // pages' chip refresh, or the bookmarks page list) re-renders without
    // waiting for the server round-trip — useful when offline / server down.
    window.dispatchEvent(new CustomEvent('bookmarksSynced'));
    syncToServer(bookmarks, deleted);
  }

  function syncToServer(bookmarks, deleted = loadDeleted()) {
    return ensureStorageFresh()
      .then(changed => {
        const payloadBookmarks = changed ? load() : bookmarks;
        const payloadDeleted = changed ? loadDeleted() : deleted;
        return fetch('/api/bookmarks', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Rentmap-User-Id': currentUserScope || '',
          },
          credentials: 'same-origin',
          body: JSON.stringify({ bookmarks: payloadBookmarks, deleted: payloadDeleted })
        });
      })
      .then(r => r.ok ? r.json() : null)
      .then(payload => {
        if (payload) applyServerState(payload);
      })
      .catch(err => console.error('Failed to sync bookmarks to server:', err));
  }

  function fetchServerState() {
    return fetch('/api/bookmarks/state', { cache: 'no-store', credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : { bookmarks: [], deleted: {} })
      .catch(() => ({ bookmarks: [], deleted: {} }));
  }

  function refresh() {
    return ensureStorageFresh()
      .then(() => fetchServerState())
      .then(payload => applyServerState(payload))
      .catch(err => {
        console.warn('Bookmarks refresh failed:', err);
        return null;
      });
  }

  const ready = ensureStorageFresh()
    .then(() => fetchServerState())
    .then(payload => {
      const before = normalizePayload(payload);
      const serverJson = JSON.stringify(before);
      const merged = applyServerState(payload);
      const mergedJson = JSON.stringify(merged);
      if (mergedJson !== serverJson) syncToServer(merged.bookmarks, merged.deleted);
      return merged.bookmarks;
    })
    .catch(err => {
      console.warn('Bookmarks server sync failed, using local only:', err);
      window.dispatchEvent(new CustomEvent('bookmarksSynced'));
      return load();
    });

  // ── Queries ─────────────────────────────────────────────────────────────
  function getAll() {
    return load().slice().sort((x, y) => {
      const ax = Number(x.sortOrder) || 0;
      const bx = Number(y.sortOrder) || 0;
      if (ax !== bx) return ax - bx;
      return entryTime(x) - entryTime(y);
    });
  }
  function isBookmarked(id, source) {
    const k = fk(id, source);
    return load().some(b => b.key === k);
  }
  function get(id, source) {
    const k = fk(id, source);
    return load().find(b => b.key === k) || null;
  }
  function nextSortOrder() {
    const all = load();
    if (!all.length) return 1;
    return all.reduce((max, b) => Math.max(max, Number(b.sortOrder) || 0), 0) + 1;
  }

  // ── Mutations ───────────────────────────────────────────────────────────
  function add(listing) {
    const bms = load();
    const deleted = loadDeleted();
    const k = fk(listing.id, listing.source);
    delete deleted[k];
    const i = bms.findIndex(b => b.key === k);
    if (i >= 0) {
      // Already bookmarked — keep position and metadata, just bump savedAt
      // so the server merge sees this as a fresh save.
      bms[i] = { ...bms[i], data: listing, savedAt: new Date().toISOString() };
    } else {
      bms.push({
        key: k,
        id: listing.id,
        source: listing.source,
        data: listing,
        sortOrder: nextSortOrder(),
        note: '',
        checklist: [],
        savedAt: new Date().toISOString(),
      });
    }
    save(bms, deleted);
  }

  function remove(id, source) {
    const k = fk(id, source);
    const deleted = loadDeleted();
    deleted[k] = new Date().toISOString();
    // Compact sort orders so the remaining list reads 1, 2, 3 without gaps.
    const remaining = load()
      .filter(b => b.key !== k)
      .sort((x, y) => (Number(x.sortOrder) || 0) - (Number(y.sortOrder) || 0))
      .map((b, idx) => ({ ...b, sortOrder: idx + 1, savedAt: new Date().toISOString() }));
    save(remaining, deleted);
  }

  function toggle(listing) {
    if (isBookmarked(listing.id, listing.source)) {
      remove(listing.id, listing.source);
      return false;
    }
    add(listing);
    return true;
  }

  function updateNote(id, source, note) {
    const bms = load();
    const i = bms.findIndex(b => b.key === fk(id, source));
    if (i < 0) return;
    bms[i] = { ...bms[i], note: String(note || ''), savedAt: new Date().toISOString() };
    save(bms);
  }

  function updateChecklist(id, source, checklist) {
    const bms = load();
    const i = bms.findIndex(b => b.key === fk(id, source));
    if (i < 0) return;
    // Normalize: trim labels, coerce done to bool. Reject empty labels so a
    // half-typed item doesn't get persisted on a sync race.
    const clean = (Array.isArray(checklist) ? checklist : [])
      .map(item => ({
        label: String((item && item.label) || '').trim(),
        done: !!(item && item.done),
      }))
      .filter(item => item.label.length > 0);
    bms[i] = { ...bms[i], checklist: clean, savedAt: new Date().toISOString() };
    save(bms);
  }

  // Move the bookmark at `index` (0-based in sortOrder-asc order) by `delta`
  // positions. Other rows shift to fill the gap. Used by the up/down arrow
  // buttons on the bookmark list page.
  function move(id, source, delta) {
    const sorted = getAll();
    const idx = sorted.findIndex(b => b.key === fk(id, source));
    if (idx < 0) return;
    const target = idx + delta;
    if (target < 0 || target >= sorted.length) return;
    // Swap the two adjacent entries (delta = ±1 only — UI gives that, but we
    // tolerate any delta by stepping through).
    const swapped = sorted.slice();
    const [item] = swapped.splice(idx, 1);
    swapped.splice(target, 0, item);
    const now = new Date().toISOString();
    const renumbered = swapped.map((b, i) => ({ ...b, sortOrder: i + 1, savedAt: now }));
    save(renumbered);
  }

  // Whole-list reorder by an array of keys (used for drag-drop if ever added).
  // Any keys not in the input keep their existing sortOrder relative to each
  // other and slot in after the explicit ones.
  function reorder(keysInOrder) {
    const sorted = getAll();
    const byKey = new Map(sorted.map(b => [b.key, b]));
    const out = [];
    keysInOrder.forEach(k => {
      const b = byKey.get(k);
      if (b) { out.push(b); byKey.delete(k); }
    });
    byKey.forEach(b => out.push(b));
    const now = new Date().toISOString();
    save(out.map((b, i) => ({ ...b, sortOrder: i + 1, savedAt: now })));
  }

  // Photos: piggyback on the favorites endpoints. Each entry is identified by
  // (user_id, source, listing_no) regardless of like/bookmark — the same
  // photos appear on either side.
  function addPhoto(id, source, file) {
    if (window.Favorites && window.Favorites.addPhoto) {
      return window.Favorites.addPhoto(id, source, file);
    }
    return Promise.reject(new Error('Favorites module not loaded'));
  }
  function getPhotos(id, source) {
    if (window.Favorites && window.Favorites.getPhotos) {
      return window.Favorites.getPhotos(id, source);
    }
    return Promise.resolve([]);
  }
  function deletePhoto(id, source, photoKey) {
    if (window.Favorites && window.Favorites.deletePhoto) {
      return window.Favorites.deletePhoto(id, source, photoKey);
    }
    return Promise.reject(new Error('Favorites module not loaded'));
  }

  window.Bookmarks = {
    ready, refresh,
    getAll, get, isBookmarked,
    add, remove, toggle,
    updateNote, updateChecklist,
    move, reorder,
    addPhoto, getPhotos, deletePhoto,
  };
})();
