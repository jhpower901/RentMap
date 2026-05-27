(function () {
  'use strict';

  const KEY_PREFIX = 'rentmap.userFilters.v1';
  const PUSH_DEBOUNCE_MS = 500;

  function clone(value) {
    try { return JSON.parse(JSON.stringify(value || {})); }
    catch (_) { return {}; }
  }

  function isObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function mergeDefaults(defaults, value) {
    return Object.assign({}, clone(defaults), isObject(value) ? clone(value) : {});
  }

  function contextName(context) {
    const cleaned = String(context || 'default').replace(/[^A-Za-z0-9_.:-]/g, '_').slice(0, 80);
    return cleaned || 'default';
  }

  function readJson(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : {};
    } catch (_) {
      return {};
    }
  }

  function getScope() {
    if (window.Auth && window.Auth.me) {
      return window.Auth.me()
        .then(user => user && user.id != null ? 'user:' + String(user.id) : 'anonymous')
        .catch(() => 'anonymous');
    }
    return Promise.resolve('anonymous');
  }

  function create(context, defaults) {
    const name = contextName(context);
    const base = clone(defaults || {});
    const listeners = new Set();
    let state = clone(base);
    let storageKey = null;
    let dirtyKey = null;
    let initialized = false;
    let changedBeforeReady = false;
    let pushTimer = null;
    let suppressPush = false;

    function notify() {
      listeners.forEach(fn => {
        try { fn(clone(state)); } catch (err) { console.error(err); }
      });
    }

    function saveLocal() {
      if (!storageKey) return;
      try { localStorage.setItem(storageKey, JSON.stringify(state)); }
      catch (_) {}
    }

    function setDirty(on) {
      if (!dirtyKey) return;
      try {
        if (on) localStorage.setItem(dirtyKey, '1');
        else localStorage.removeItem(dirtyKey);
      } catch (_) {}
    }

    function isDirty() {
      if (!dirtyKey) return false;
      try { return localStorage.getItem(dirtyKey) === '1'; }
      catch (_) { return false; }
    }

    function endpoint() {
      return '/api/user-filters/' + encodeURIComponent(name);
    }

    function pushNow() {
      if (pushTimer) {
        clearTimeout(pushTimer);
        pushTimer = null;
      }
      if (!initialized) {
        changedBeforeReady = true;
        return Promise.resolve(null);
      }
      return fetch(endpoint(), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        keepalive: true,
        body: JSON.stringify({ state }),
      })
        .then(r => {
          if (r && r.ok) setDirty(false);
          else console.warn('user-filter push got non-OK:', name, r && r.status);
        })
        .catch(err => console.warn('user-filter push failed:', name, err));
    }

    function schedulePush() {
      if (suppressPush) return;
      if (!initialized) {
        changedBeforeReady = true;
        return;
      }
      setDirty(true);
      if (pushTimer) clearTimeout(pushTimer);
      pushTimer = setTimeout(pushNow, PUSH_DEBOUNCE_MS);
    }

    function pullFromServer() {
      if (isDirty()) return pushNow();
      return fetch(endpoint(), { cache: 'no-store', credentials: 'same-origin' })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data || !isObject(data.state)) return;
          suppressPush = true;
          try {
            state = mergeDefaults(base, data.state);
            saveLocal();
            notify();
          } finally {
            suppressPush = false;
          }
        })
        .catch(err => console.warn('user-filter pull failed:', name, err));
    }

    function set(next) {
      state = mergeDefaults(base, next);
      if (!initialized) changedBeforeReady = true;
      saveLocal();
      notify();
      schedulePush();
    }

    function patch(partial) {
      set(Object.assign({}, state, isObject(partial) ? partial : {}));
    }

    const ready = getScope()
      .then(scope => {
        storageKey = KEY_PREFIX + ':' + scope + ':' + name;
        dirtyKey = storageKey + '.dirty';
        const localState = mergeDefaults(base, readJson(storageKey));
        state = changedBeforeReady ? Object.assign(localState, clone(state)) : localState;
        initialized = true;
        saveLocal();
        notify();
        if (changedBeforeReady || isDirty()) {
          setDirty(true);
          return pushNow();
        }
        return pullFromServer();
      })
      .catch(err => {
        console.warn('user-filter init failed:', name, err);
        initialized = true;
        notify();
      });

    window.addEventListener('storage', e => {
      if (!storageKey || e.key !== storageKey) return;
      state = mergeDefaults(base, readJson(storageKey));
      notify();
    });

    function flushPending() {
      if (pushTimer) pushNow();
    }
    window.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flushPending();
    });
    window.addEventListener('pagehide', flushPending);

    return {
      ready,
      get: () => clone(state),
      set,
      patch,
      subscribe(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
    };
  }

  window.UserFilters = { create };
})();
