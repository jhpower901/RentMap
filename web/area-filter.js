(function () {
  const KEY = 'rentmap.areaFilter.v1';
  // localStorage flag: "dirty edit not yet acked by the server". Set when
  // schedulePush() queues a push, cleared when fetch succeeds. Survives a
  // page reload so an unsynced edit from the previous visit can re-attempt
  // on next boot before pullFromServer would otherwise overwrite it.
  const DIRTY_KEY = 'rentmap.areaFilter.dirty.v1';
  const SERVER_URL = '/api/area-filter';
  const PUSH_DEBOUNCE_MS = 500;
  const DEFAULT_POINTS = [
    [37.282812, 127.038062],
    [37.282812, 127.051938],
    [37.273313, 127.051938],
    [37.273313, 127.038062],
  ];
  const STYLE = {
    color: '#6366F1', weight: 2, fillColor: '#6366F1',
    fillOpacity: 0.08, dashArray: '6,4', interactive: false,
    className: 'area-filter-shape',
  };

  function clonePoints(pts) { return pts.map(p => [p[0], p[1]]); }

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return { points: clonePoints(DEFAULT_POINTS), enabled: true };
      const parsed = JSON.parse(raw);
      const points = Array.isArray(parsed.points) && parsed.points.length >= 3
        ? parsed.points.map(p => [Number(p[0]), Number(p[1])])
        : clonePoints(DEFAULT_POINTS);
      return { points, enabled: parsed.enabled !== false };
    } catch (_) {
      return { points: clonePoints(DEFAULT_POINTS), enabled: true };
    }
  }

  function saveLocal() {
    try { localStorage.setItem(KEY, JSON.stringify({ points: state.points, enabled: state.enabled })); }
    catch (_) {}
  }

  // Debounced PUT to /api/area-filter. We don't gate UI updates on the
  // network round-trip; localStorage is the source of truth for instant reads
  // and the server PUT is fire-and-forget. ``suppressPush`` is set while we
  // apply a server fetch into local state so we don't bounce it right back.
  let pushTimer = null;
  let suppressPush = false;
  function setDirty(on) {
    try {
      if (on) localStorage.setItem(DIRTY_KEY, '1');
      else localStorage.removeItem(DIRTY_KEY);
    } catch (_) {}
  }
  function isDirty() {
    try { return localStorage.getItem(DIRTY_KEY) === '1'; } catch (_) { return false; }
  }
  function schedulePush() {
    if (suppressPush) return;
    setDirty(true);
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(pushNow, PUSH_DEBOUNCE_MS);
  }
  function pushNow() {
    pushTimer = null;
    const payload = JSON.stringify({ points: state.points, enabled: state.enabled });
    return fetch(SERVER_URL, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      // ``keepalive`` lets a push that's still in flight survive the page
      // unloading — important when the user edits and immediately closes
      // the tab. Browsers cap payload at 64 KB which is far more than the
      // few KB our polygon JSON ever needs.
      keepalive: true,
      body: payload,
    })
      .then(r => {
        if (r && r.ok) setDirty(false);
        else console.warn('area-filter push got non-OK:', r && r.status);
      })
      .catch(err => console.warn('area-filter push failed (will retry on next edit / next boot):', err));
  }
  // Synchronous best-effort flush for pagehide. sendBeacon ignores
  // Content-Type but the server's Pydantic body parser tolerates it for
  // JSON because we still set Content-Type via Blob — sendBeacon picks up
  // the Blob's type. Used when there's still a pending debounce timer.
  function flushBeacon() {
    if (!pushTimer) return;
    clearTimeout(pushTimer);
    pushTimer = null;
    try {
      const blob = new Blob(
        [JSON.stringify({ points: state.points, enabled: state.enabled })],
        { type: 'application/json' }
      );
      // sendBeacon is POST-only, but the server accepts the same payload at
      // POST /api/area-filter? No — only PUT is registered. Fall back to
      // fetch+keepalive when sendBeacon is unavailable or for PUT semantics.
      if (navigator.sendBeacon) {
        // The endpoint takes PUT only; we still call pushNow() (fetch with
        // keepalive=true) which the browser keeps alive past unload.
      }
    } catch (_) {}
    pushNow();
  }
  function save() {
    saveLocal();
    schedulePush();
  }

  const state = load();
  const listeners = new Set();
  function notify() { listeners.forEach(fn => { try { fn(); } catch (e) { console.error(e); } }); }

  // Pull authoritative state from the server on boot. Same-tab updates after
  // this come through setPoints/setEnabled below; cross-tab via the storage
  // event listener at the bottom of this file.
  //
  // Special case: if we have a *dirty* localStorage edit that never made it
  // to the server (page closed inside the debounce window, or last push
  // errored), retry the push instead of overwriting local state from the
  // server. This avoids the data-loss scenario where the server's stale
  // copy would otherwise win on the next boot.
  function pullFromServer() {
    if (isDirty()) {
      return pushNow();
    }
    return fetch(SERVER_URL, { cache: 'no-store', credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !Array.isArray(data.points) || data.points.length < 3) return;
        suppressPush = true;
        try {
          state.points = data.points.map(p => [Number(p[0]), Number(p[1])]);
          state.enabled = data.enabled !== false;
          saveLocal();
          notify();
        } finally {
          suppressPush = false;
        }
      })
      .catch(err => console.warn('area-filter pull failed:', err));
  }
  // Defer until the page is interactive so we don't compete with the initial
  // marker render for network bandwidth; the localStorage value is already
  // applied at this point.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', pullFromServer, { once: true });
  } else {
    pullFromServer();
  }

  // Flush pending push on tab hide / pagehide. ``visibilitychange`` covers
  // backgrounding (e.g. mobile app switch) and ``pagehide`` catches the
  // bfcache eviction / actual unload. Both can fire so we no-op when no
  // timer is pending.
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushBeacon();
  });
  window.addEventListener('pagehide', flushBeacon);

  function isDefault() {
    if (state.points.length !== DEFAULT_POINTS.length) return false;
    return state.points.every((p, i) =>
      p[0] === DEFAULT_POINTS[i][0] && p[1] === DEFAULT_POINTS[i][1]);
  }

  function setPoints(points) {
    if (!Array.isArray(points) || points.length < 3) return;
    state.points = points.map(p => [Number(p[0]), Number(p[1])]);
    save(); notify();
  }
  function setEnabled(on) { state.enabled = !!on; save(); notify(); }
  function reset() { setPoints(clonePoints(DEFAULT_POINTS)); }

  function contains(lat, lon) {
    const poly = state.points;
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const yi = poly[i][0], xi = poly[i][1];
      const yj = poly[j][0], xj = poly[j][1];
      const hit = ((yi > lat) !== (yj > lat))
        && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi);
      if (hit) inside = !inside;
    }
    return inside;
  }

  function applies(rec) {
    if (!state.enabled) return true;
    if (!rec) return true;
    const lat = Number(rec.lat != null ? rec.lat : rec.latitude);
    const lon = Number(rec.lon != null ? rec.lon : (rec.lng != null ? rec.lng : rec.longitude));
    if (!isFinite(lat) || !isFinite(lon) || lat === 0 || lon === 0) return true;
    return contains(lat, lon);
  }

  function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

  window.addEventListener('storage', e => {
    if (e.key !== KEY) return;
    const next = load();
    state.points = next.points;
    state.enabled = next.enabled;
    notify();
  });

  function injectCss() {
    if (document.getElementById('area-filter-css')) return;
    const style = document.createElement('style');
    style.id = 'area-filter-css';
    style.textContent = `
.leaflet-container.area-drawing{cursor:crosshair !important}
.area-filter-shape{pointer-events:none !important}
.area-draw-banner{position:absolute;top:10px;left:50%;transform:translateX(-50%);z-index:1100;background:#6366F1;color:#fff;padding:7px 12px;border-radius:18px;box-shadow:0 2px 8px rgba(0,0,0,.25);font-size:12px;display:none;align-items:center;gap:8px;font-family:'Noto Sans KR',sans-serif;white-space:nowrap}
.area-draw-banner.on{display:flex}
.area-draw-banner button{background:rgba(255,255,255,.2);border:none;color:#fff;padding:3px 9px;border-radius:11px;cursor:pointer;font-size:11px;font-family:inherit}
.area-draw-banner button:hover{background:rgba(255,255,255,.35)}
.area-draw-banner button.primary{background:#fff;color:#6366F1;font-weight:700}
.area-draw-banner button.primary:disabled{opacity:.5;cursor:not-allowed}
.area-ctl{display:inline-flex;align-items:center;gap:6px;font-size:12px;background:#eef2ff;border:1px solid #c7d2fe;padding:3px 9px;border-radius:16px;color:#3730a3;font-family:inherit}
.area-ctl input[type=checkbox]{accent-color:#6366F1;cursor:pointer;width:13px;height:13px;margin:0}
.area-ctl label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.area-ctl small{color:#6366F1;font-weight:600}
.area-ctl a{color:#4338ca;text-decoration:underline;cursor:pointer}
`;
    document.head.appendChild(style);
  }
  injectCss();

  function mountMapOverlay(map, opts) {
    opts = opts || {};
    if (typeof L === 'undefined' || !map) return null;
    let layer = null;
    function refresh() {
      if (layer) { map.removeLayer(layer); layer = null; }
      if (state.enabled) layer = L.polygon(state.points, STYLE).addTo(map);
    }
    refresh();
    const unsub = subscribe(refresh);
    if (!opts.drawing) return { refresh, unsubscribe: unsub };

    let draft = null;
    let banner = null;
    let onClick = null;

    function ensureBanner() {
      if (banner) return banner;
      banner = document.createElement('div');
      banner.className = 'area-draw-banner';
      banner.innerHTML =
        '<span>지도를 클릭해 꼭짓점 추가 (<b class="vcount">0</b>개)</span>' +
        '<button type="button" data-act="undo">↶ 한 점 취소</button>' +
        '<button type="button" data-act="finish" class="primary" disabled>완료</button>' +
        '<button type="button" data-act="cancel">취소</button>';
      banner.addEventListener('click', e => {
        const act = e.target && e.target.dataset && e.target.dataset.act;
        if (act === 'undo') undoVertex();
        else if (act === 'finish') endDraw(true);
        else if (act === 'cancel') endDraw(false);
      });
      map.getContainer().appendChild(banner);
      return banner;
    }
    function updateBanner() {
      const b = ensureBanner();
      const n = draft ? draft.points.length : 0;
      b.querySelector('.vcount').textContent = n;
      b.querySelector('[data-act=finish]').disabled = n < 3;
      b.classList.toggle('on', !!draft);
    }
    function renderDraft() {
      draft.group.clearLayers();
      draft.points.forEach((p, i) => {
        L.circleMarker(p, { radius: 5, color: '#fff', weight: 2, fillColor: '#6366F1', fillOpacity: 1, interactive: false, className: 'area-filter-shape' })
          .bindTooltip(String(i + 1), { permanent: true, direction: 'top', offset: [0, -6] })
          .addTo(draft.group);
      });
      if (draft.points.length >= 2) {
        L.polyline(draft.points, { color: '#6366F1', weight: 2, dashArray: '4,4', interactive: false, className: 'area-filter-shape' }).addTo(draft.group);
      }
      if (draft.points.length >= 3) {
        L.polygon(draft.points, Object.assign({}, STYLE, { weight: 0, interactive: false })).addTo(draft.group);
      }
      updateBanner();
    }
    function startDraw() {
      if (draft) return;
      draft = { points: [], group: L.layerGroup().addTo(map) };
      if (layer) { map.removeLayer(layer); layer = null; }
      map.getContainer().classList.add('area-drawing');
      onClick = e => {
        // Don't swallow clicks that landed on a marker or popup — let
        // Leaflet's own handler open the popup. Only count clicks on the
        // empty map as polygon vertices.
        const t = e.originalEvent && e.originalEvent.target;
        if (t && t.closest && t.closest('.leaflet-interactive, .leaflet-popup, .leaflet-marker-icon')) return;
        draft.points.push([e.latlng.lat, e.latlng.lng]);
        renderDraft();
      };
      map.on('click', onClick);
      renderDraft();
    }
    function endDraw(commit) {
      if (!draft) return;
      const pts = draft.points;
      if (onClick) { map.off('click', onClick); onClick = null; }
      map.removeLayer(draft.group);
      draft = null;
      map.getContainer().classList.remove('area-drawing');
      updateBanner();
      if (commit && pts.length >= 3) setPoints(pts);
      else refresh();
    }
    function undoVertex() {
      if (draft && draft.points.length) { draft.points.pop(); renderDraft(); }
    }

    return {
      refresh,
      unsubscribe: unsub,
      startDraw, endDraw, undoVertex,
      isDrawing: () => !!draft,
    };
  }

  function mountControl(container, opts) {
    opts = opts || {};
    const el = document.createElement('span');
    el.className = 'area-ctl';
    function render() {
      el.innerHTML = '';
      const lbl = document.createElement('label');
      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.checked = state.enabled;
      chk.addEventListener('change', () => setEnabled(chk.checked));
      lbl.appendChild(chk);
      lbl.appendChild(document.createTextNode(' 영역 안만'));
      el.appendChild(lbl);

      const info = document.createElement('small');
      info.textContent = '꼭짓점 ' + state.points.length + (isDefault() ? ' · 기본' : '');
      el.appendChild(info);

      if (opts.editHref !== false) {
        const link = document.createElement('a');
        link.href = opts.editHref || 'index.html';
        link.textContent = '지도에서 편집';
        el.appendChild(link);
      }
    }
    render();
    subscribe(render);
    container.appendChild(el);
    return el;
  }

  window.AreaFilter = {
    DEFAULT_POINTS: clonePoints(DEFAULT_POINTS),
    get points() { return clonePoints(state.points); },
    get enabled() { return state.enabled; },
    isDefault,
    setPoints, setEnabled, reset,
    contains, applies, subscribe,
    mountMapOverlay, mountControl,
  };
})();
