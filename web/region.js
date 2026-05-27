(function () {
  'use strict';

  // Region selector — a dropdown that lives in the top nav of every page
  // and lets the user switch which approved region's data they're looking
  // at. Active selection is persisted in localStorage (same key
  // region-data-loader.js reads at boot), and changing the dropdown
  // reloads the page so every cached map/marker/list rebuilds against
  // the new data files.
  //
  // Auto-injected from auth.js after the user is authenticated. The
  // /api/regions endpoint already filters to approved regions for
  // non-admin callers, so the selector only ever shows real choices.

  var ALLOWED_SLUG = /^[a-z0-9][a-z0-9_-]*$/;
  var STORAGE_KEY =
    (window.RegionData && window.RegionData.STORAGE_KEY) || 'rentmap.region.v1';

  function injectStyle() {
    if (document.getElementById('region-selector-css')) return;
    var s = document.createElement('style');
    s.id = 'region-selector-css';
    s.textContent = [
      '.region-selector{display:inline-flex;align-items:center;gap:6px;font-size:12px;',
      'background:#f8fafc;border:1px solid #e2e8f0;color:#1f2937;padding:0 8px;',
      "border-radius:16px;height:28px;font-family:'Noto Sans KR',sans-serif;margin-left:6px}",
      '.region-selector select{border:none;background:transparent;font-size:12px;',
      'color:inherit;font-family:inherit;cursor:pointer;outline:none;padding:0 2px}',
      '.region-selector .label{font-weight:700;font-size:11px;color:#6b7280}',
      '@media (max-width:480px){.region-selector{font-size:11px;height:26px}}',
    ].join('');
    document.head.appendChild(s);
  }

  function currentSlug() {
    if (window.RegionData && typeof window.RegionData.currentSlug === 'function') {
      return window.RegionData.currentSlug();
    }
    try {
      var raw = localStorage.getItem(STORAGE_KEY) || 'ajou';
      return String(raw).toLowerCase().replace(/[^a-z0-9_-]/g, '') || 'ajou';
    } catch (_e) {
      return 'ajou';
    }
  }

  function setSlug(slug) {
    if (!ALLOWED_SLUG.test(slug)) return false;
    if (window.RegionData && typeof window.RegionData.setSlug === 'function') {
      return window.RegionData.setSlug(slug);
    }
    try {
      localStorage.setItem(STORAGE_KEY, slug);
      return true;
    } catch (_e) {
      return false;
    }
  }

  function buildSelector(regions, active) {
    var wrap = document.createElement('span');
    wrap.className = 'region-selector';

    var label = document.createElement('span');
    label.className = 'label';
    label.textContent = '지역';
    wrap.appendChild(label);

    var sel = document.createElement('select');
    var matched = false;
    regions.forEach(function (r) {
      var opt = document.createElement('option');
      opt.value = r.slug;
      opt.textContent = r.name || r.slug;
      if (r.slug === active) {
        opt.selected = true;
        matched = true;
      }
      sel.appendChild(opt);
    });
    // If the stored slug isn't in the approved set (e.g. region got
    // disabled), default to the first available so the user isn't stuck
    // looking at a non-existent dataset.
    if (!matched && regions.length) {
      sel.value = regions[0].slug;
      setSlug(regions[0].slug);
    }
    sel.addEventListener('change', function () {
      var slug = sel.value;
      if (!ALLOWED_SLUG.test(slug)) return;
      setSlug(slug);
      // Reload rather than swap-in: Leaflet markers, favorite caches,
      // platform tables and the area-filter overlay all build state off
      // the global DATA_*, and untangling that incrementally is more
      // complex than a clean reload.
      location.reload();
    });
    wrap.appendChild(sel);
    return wrap;
  }

  function ensureSelector() {
    var nav = document.querySelector('nav');
    if (!nav || nav.querySelector('.region-selector')) return;
    fetch('/api/regions', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (payload) {
        var regions = (payload && payload.regions) || [];
        if (!regions.length) return;
        injectStyle();
        var sel = buildSelector(regions, currentSlug());
        var slot = document.getElementById('userInfo');
        nav.insertBefore(sel, slot || null);
      })
      .catch(function (err) {
        // 401 (not logged in), network blip, or 5xx — silently skip. The
        // page still renders with whatever localStorage / fallback slug
        // region-data-loader.js picked.
        if (typeof err !== 'number' || err !== 401) {
          console.warn('region-selector: failed to load regions', err);
        }
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureSelector, { once: true });
  } else {
    ensureSelector();
  }
})();
