(function () {
  'use strict';

  const KEY = 'rentmap_favorites';

  function fk(id, source) { return String(source) + '::' + String(id); }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (_) { return []; } }
  function save(favs) { localStorage.setItem(KEY, JSON.stringify(favs)); }

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

  // IndexedDB for photos
  let _db = null;
  function openDB() {
    if (_db) return Promise.resolve(_db);
    return new Promise((res, rej) => {
      const req = indexedDB.open('rentmap_photos', 1);
      req.onupgradeneeded = e => {
        const s = e.target.result.createObjectStore('photos', { keyPath: 'photoKey' });
        s.createIndex('favKey', 'favKey', { unique: false });
      };
      req.onsuccess = e => { _db = e.target.result; res(_db); };
      req.onerror = e => rej(e.target.error);
    });
  }

  function addPhoto(id, source, blob) {
    return openDB().then(db => new Promise((res, rej) => {
      const photoKey = fk(id, source) + '::' + Date.now();
      const tx = db.transaction('photos', 'readwrite');
      tx.objectStore('photos').put({ photoKey, favKey: fk(id, source), blob, addedAt: Date.now() });
      tx.oncomplete = () => res(photoKey);
      tx.onerror = e => rej(e.target.error);
    }));
  }

  function getPhotos(id, source) {
    return openDB().then(db => new Promise((res, rej) => {
      const tx = db.transaction('photos', 'readonly');
      const req = tx.objectStore('photos').index('favKey').getAll(fk(id, source));
      req.onsuccess = e => res(e.target.result.sort((a, b) => a.addedAt - b.addedAt));
      req.onerror = e => rej(e.target.error);
    }));
  }

  function deletePhoto(photoKey) {
    return openDB().then(db => new Promise((res, rej) => {
      const tx = db.transaction('photos', 'readwrite');
      tx.objectStore('photos').delete(photoKey);
      tx.oncomplete = res;
      tx.onerror = e => rej(e.target.error);
    }));
  }

  window.Favorites = { getAll, isFav, add, remove, updateRating, updateNotes, addManual, addPhoto, getPhotos, deletePhoto };
})();
