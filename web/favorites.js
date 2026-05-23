(function () {
  'use strict';

  const KEY = 'rentmap_favorites';

  function fk(id, source) { return String(source) + '::' + String(id); }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (_) { return []; } }
  function entryTime(entry) {
    const t = Date.parse(entry && entry.savedAt);
    return Number.isFinite(t) ? t : 0;
  }
  function mergeFavorites(a, b) {
    const byKey = new Map();
    [...(a || []), ...(b || [])].forEach(entry => {
      if (!entry || !entry.key) return;
      const prev = byKey.get(entry.key);
      if (!prev || entryTime(entry) >= entryTime(prev)) byKey.set(entry.key, entry);
    });
    return [...byKey.values()].sort((x, y) => entryTime(y) - entryTime(x));
  }
  
  function save(favs) { 
    localStorage.setItem(KEY, JSON.stringify(favs)); 
    syncToServer(favs);
  }

  function syncToServer(favs) {
    return fetch('/api/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(favs)
    }).catch(err => console.error('Failed to sync favorites to server:', err));
  }

  const ready = fetch('/api/favorites')
    .then(r => r.ok ? r.json() : [])
    .then(serverFavs => {
      const localFavs = load();
      const merged = mergeFavorites(localFavs, Array.isArray(serverFavs) ? serverFavs : []);
      const serverJson = JSON.stringify(serverFavs || []);
      const mergedJson = JSON.stringify(merged);
      localStorage.setItem(KEY, JSON.stringify(merged));
      console.log('Favorites synced:', merged.length, 'items');
      window.dispatchEvent(new CustomEvent('favoritesSynced'));

      if (mergedJson !== serverJson) {
        syncToServer(merged);
      }
      return merged;
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
    const k = fk(listing.id, listing.source);
    const i = favs.findIndex(f => f.key === k);
    const entry = {
      key: k, id: listing.id, source: listing.source,
      data: listing, savedAt: new Date().toISOString(), rating: null, notes: '',
    };
    if (i >= 0) { entry.rating = favs[i].rating; entry.notes = favs[i].notes; favs[i] = entry; }
    else favs.push(entry);
    save(favs);
  }

  function remove(id, source) { save(load().filter(f => f.key !== fk(id, source))); }

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
    favs.push({ key: fk(id, 'manual'), id, source: 'manual', data: listing, savedAt: new Date().toISOString(), rating: null, notes: data.notes || '' });
    save(favs);
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
