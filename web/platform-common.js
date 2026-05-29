(function () {
  const PALETTE = [
    '#e6194B','#3cb44b','#4363d8','#f58231','#911eb4',
    '#42d4f4','#f032e6','#469990','#9a6324','#800000',
    '#808000','#000075','#6F4E37','#C71585','#556B2F',
  ];
  const PLATFORM_NAMES = {
    dabang: '다방', daangn: '당근부동산', zigbang: '직방', naver: '네이버부동산',
  };

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function safeUrl(v) {
    const raw = String(v || '').trim();
    if (!raw) return '';
    try {
      const url = new URL(raw, window.location.href);
      if (url.protocol === 'http:' || url.protocol === 'https:') return url.href;
    } catch (_) {}
    return '';
  }

  function fmtDeposit(v) {
    if (v === null || v === undefined) return '-';
    if (v >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, '') + '억';
    return v + '만';
  }
  function fmtRent(v) {
    if (v === null || v === undefined) return '-';
    return v + '만';
  }
  function fmtArea(v) {
    if (!v) return '-';
    return String(v).split('/').map(p => {
      const n = parseFloat(p);
      return isNaN(n) ? p : n.toFixed(1);
    }).join('/') + 'm²';
  }
  function imageSrc(src, source) {
    if (!src) return '';
    if (source !== 'zigbang' || !src.startsWith('https://ic.zigbang.com/') || src.includes('?')) return src;
    return src + '?w=400&h=300&q=70';
  }
  function agencyLabel(agency, source) {
    if (source === 'daangn') {
      if (agency === 'DIRECT') return '직거래';
      if (agency === 'BROKER') return '중개사';
    }
    return agency || '-';
  }
  function scheduleImageLoad(container) {
    const load = () => {
      container.querySelectorAll('img[data-src]').forEach(img => {
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
      });
    };
    if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 800 });
    else setTimeout(load, 0);
  }
  function sortData(data, col, asc) {
    return [...data].sort((a, b) => {
      let va = a[col], vb = b[col];
      if (va === null || va === undefined) va = asc ? Infinity : -Infinity;
      if (vb === null || vb === undefined) vb = asc ? Infinity : -Infinity;
      if (typeof va === 'string') return asc ? va.localeCompare(vb, 'ko') : vb.localeCompare(va, 'ko');
      return asc ? va - vb : vb - va;
    });
  }

  function init({ source, accent, raw }) {
    const RAW = raw || [];

    const agencyColors = (() => {
      const keys = [...new Set(RAW.map(r => r.agency).filter(Boolean))]
        .sort((a, b) => String(a).localeCompare(String(b), 'ko'));
      const out = {};
      keys.forEach((k, i) => { out[k] = PALETTE[i % PALETTE.length]; });
      return out;
    })();
    const agencyColor = agency => agencyColors[agency] || accent;

    // Branding
    document.documentElement.style.setProperty('--accent', accent);
    const pName = PLATFORM_NAMES[source] || source;
    document.title = pName + ' - 아주대 월세 매물';
    const badgeEl = document.getElementById('page-badge');
    if (badgeEl) { badgeEl.textContent = pName; badgeEl.style.background = accent; }
    const titleEl = document.getElementById('page-title');
    if (titleEl) titleEl.textContent = '매물 목록';
    const navEl = document.getElementById('nav-' + source);
    if (navEl) navEl.classList.add('active-nav');

    // Populate type filter
    const types = [...new Set(RAW.map(r => r.type).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, 'ko'));
    const sel = document.getElementById('typeFilter');
    types.forEach(t => {
      const o = document.createElement('option');
      o.value = t; o.textContent = t;
      sel.appendChild(o);
    });

    // Map
    const map = L.map('map').setView([37.2779, 127.0438], 15);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors', maxZoom: 19,
    }).addTo(map);
    const centerMarker = L.marker([37.280062, 127.043688]).addTo(map).bindPopup('<b>아주대학교 정문</b>');
    window.addEventListener('resize', () => map.invalidateSize());

    // Recenter on the active region so switching regions moves the viewport.
    fetch('/api/regions', { credentials: 'same-origin', cache: 'no-store' })
      .then(r => r.ok ? r.json() : { regions: [] })
      .then(payload => {
        const slug = (window.RegionData && RegionData.currentSlug && RegionData.currentSlug()) || 'ajou';
        const region = (payload.regions || []).find(r => r.slug === slug);
        if (!region) return;
        map.setView([region.centerLat, region.centerLng], 15);
        centerMarker.setLatLng([region.centerLat, region.centerLng]);
        centerMarker.setPopupContent('<b>' + (region.name || region.slug).replace(/[<>&"']/g, '') + ' 중심</b>');
      })
      .catch(err => console.warn('Failed to fetch region center', err));

    const markerMap = new Map();
    RAW.forEach(r => {
      // Rewrite Naver URLs to include ms= viewport + articleNo so the link
      // opens the article instead of redirecting to a viewport-only page.
      // Non-naver sources pass through unchanged.
      const resolvedUrl = window.ListingInfo
        ? window.ListingInfo.resolveListingUrl(r.url, source, r)
        : r.url;
      r.url = safeUrl(resolvedUrl);
      if (!r.lat || !r.lon) return;
      const m = L.circleMarker([r.lat, r.lon], {
        radius: 7, fillColor: agencyColor(r.agency), color: '#fff', weight: 2, fillOpacity: 0.85,
      }).addTo(map);
      const imgHtml = r.img1
        ? '<img class="popup-img" src="' + esc(safeUrl(imageSrc(r.img1, source))) + '" onerror="this.style.display=\'none\'">'
        : '';
      m.bindPopup(
        '<div class="popup-title">' + esc(r.title || '') + '</div>' +
        imgHtml +
        '<div class="popup-price">보증금 ' + fmtDeposit(r.deposit) + ' / 월세 ' + fmtRent(r.rent) + '</div>' +
        '<div class="popup-meta">관리비 ' + fmtRent(r.maint) + ' | 총 ' + fmtRent(r.total) + '</div>' +
        '<div class="popup-meta"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + agencyColor(r.agency) + ';vertical-align:middle;margin-right:4px"></span>' + esc(agencyLabel(r.agency, source)) + (r.phone ? ' ' + esc(r.phone) : '') + '</div>' +
        '<div class="popup-meta">' + esc(r.address || r.region || '') + '</div>' +
        '<div style="margin-top:6px"><a href="' + r.url + '" target="_blank" class="link-btn">매물 보기</a></div>'
      );
      // Marker click ⇒ scroll the table to the matching row. We defer to a
      // microtask so Leaflet's own popup-open handler fires first; otherwise
      // the smooth scroll competes with the popup focus jump and feels jumpy.
      m.on('click', () => {
        setTimeout(() => scrollRowIntoView(r.id), 0);
      });
      markerMap.set(r.id, { marker: m, data: r });
    });

    let sortCol = 'total', sortAsc = true;

    const PRICE_FILTER_DEFAULTS = {
      maxDeposit: '3000',
      maxRent: '60',
    };
    const PLATFORM_FILTER_DEFAULTS = {
      typeFilter: '',
      search: '',
    };
    const priceFilterPrefs = window.UserFilters
      ? UserFilters.create('price', PRICE_FILTER_DEFAULTS)
      : null;
    const filterPrefs = window.UserFilters
      ? UserFilters.create('platform.' + source, PLATFORM_FILTER_DEFAULTS)
      : null;

    function setControlValue(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      const next = value === null || value === undefined ? '' : String(value);
      if (el.value !== next) el.value = next;
    }

    function applySavedPriceFilterState(saved) {
      const state = saved || PRICE_FILTER_DEFAULTS;
      setControlValue('maxDeposit', state.maxDeposit);
      setControlValue('maxRent', state.maxRent);
    }

    function applySavedFilterState(saved) {
      const state = saved || PLATFORM_FILTER_DEFAULTS;
      setControlValue('typeFilter', state.typeFilter);
      setControlValue('search', state.search);
    }

    function currentPriceFilterState() {
      return {
        maxDeposit: document.getElementById('maxDeposit').value,
        maxRent: document.getElementById('maxRent').value,
      };
    }

    function currentFilterState() {
      return {
        typeFilter: document.getElementById('typeFilter').value,
        search: document.getElementById('search').value,
      };
    }

    if (priceFilterPrefs) {
      priceFilterPrefs.subscribe(state => {
        applySavedPriceFilterState(state);
        render();
      });
    }

    if (filterPrefs) {
      filterPrefs.subscribe(state => {
        applySavedFilterState(state);
        render();
      });
    }

    function readLimit(id, fallback) {
      const n = parseFloat(document.getElementById(id).value);
      return Number.isFinite(n) ? n : fallback;
    }

    function getFiltered() {
      const maxDep  = readLimit('maxDeposit', 9999);
      const maxRent = readLimit('maxRent', 999);
      const type    = document.getElementById('typeFilter').value;
      const q       = (document.getElementById('search').value || '').toLowerCase();
      return RAW.filter(r => {
        if (r.deposit !== null && r.deposit > maxDep) return false;
        if (r.rent    !== null && r.rent    > maxRent) return false;
        if (type && r.type !== type) return false;
        if (q) {
          const hay = [r.title, r.address, r.region, r.agency, r.type].join(' ').toLowerCase();
          if (!hay.includes(q)) return false;
        }
        if (window.AreaFilter && !window.AreaFilter.applies(r)) return false;
        return true;
      });
    }

    // Track which row IDs are currently expanded so render() can preserve
    // their open state across re-renders (filter changes, sort clicks, etc.).
    const openIds = new Set();

    function detailRowFor(r, colspan) {
      const html = window.ListingInfo ? window.ListingInfo.buildSection(r, source) : '';
      if (!html) return null;
      const tr = document.createElement('tr');
      tr.className = 'detail-row';
      // The trailing .detail-collapse-wrap mirrors the favorites-page button so
      // a long detail panel can be closed without scrolling back up to the row.
      tr.innerHTML =
        '<td colspan="' + colspan + '" class="detail-cell">' + html +
        '<div class="detail-collapse-wrap">' +
          '<button class="btn detail-collapse" type="button">접기</button>' +
        '</div>' +
        '</td>';
      // Lazy-load the price sparkline from /api/listings/.../price-history.
      // The placeholder inside the panel is already in the DOM, so we just
      // hand the cell to ListingInfo to find and fill it. Idempotent —
      // re-rendering an already-open row won't trigger a second fetch.
      if (window.ListingInfo && window.ListingInfo.attachSparklines) {
        window.ListingInfo.attachSparklines(tr, source);
      }
      return tr;
    }

    // Scroll the table row matching `id` into view + flash a highlight. Called
    // when a map marker is clicked so the side-table jumps to the listing the
    // user just inspected on the map.
    function scrollRowIntoView(id) {
      const tbody = document.getElementById('tbody');
      if (!tbody) return;
      const row = tbody.querySelector('tr[data-row-id="' + CSS.escape(String(id)) + '"]');
      if (!row) return;
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      row.classList.add('row-flash');
      setTimeout(() => row.classList.remove('row-flash'), 1400);
    }

    // Helper for the reaction-cell render path. Likes (`isFav`) and dislikes
    // (`isDislike`) share a key namespace via Favorites.upsert — only one can
    // be active at a time, so the two `.on` classes are also mutually
    // exclusive. The .reaction-btn CSS in platform-common.css handles the
    // muted/active styling.
    function applyReactionState(tr, id, src) {
      const like = tr.querySelector('.fav-like-btn');
      const dis  = tr.querySelector('.fav-dislike-btn');
      if (!like || !dis || !window.Favorites) return;
      const isLike = window.Favorites.isFav(id, src);
      const isDis  = window.Favorites.isDislike(id, src);
      like.textContent = isLike ? '❤️' : '🤍';
      like.classList.toggle('on', isLike);
      dis.classList.toggle('on', isDis);
      tr.classList.toggle('row-disliked', isDis);
    }

    function render() {
      const filtered = sortData(getFiltered(), sortCol, sortAsc);
      document.getElementById('countLabel').textContent = filtered.length + '건';
      const visibleIds = new Set(filtered.map(r => r.id));

      markerMap.forEach(({ marker, data }) => {
        marker.setStyle(
          visibleIds.has(data.id)
            ? { fillOpacity: 0.85, opacity: 1 }
            : { fillOpacity: 0.12, opacity: 0.3 }
        );
      });

      const tbody = document.getElementById('tbody');
      const colspan = (document.querySelectorAll('#tbl thead th') || []).length || 13;
      tbody.innerHTML = '';
      filtered.forEach(r => {
        const tr = document.createElement('tr');
        tr.dataset.rowId = String(r.id || '');
        r.img1 = safeUrl(imageSrc(r.img1, source));
        const favId = esc(r.id);
        const favSource = esc(r.source);
        const imgCell = r.img1
          ? '<img class="img-thumb" data-src="' + imageSrc(r.img1, source) + '" loading="lazy" decoding="async" alt="" onerror="this.outerHTML=\'<div class=\\\'no-img\\\'>사진없음</div>\'">'
          : '<div class="no-img">사진없음</div>';
        tr.innerHTML =
          '<td style="padding:0;width:6px;background:' + agencyColor(r.agency) + '"></td>' +
          '<td>' + imgCell + '</td>' +
          '<td class="price">' + fmtDeposit(r.deposit) + '</td>' +
          '<td class="price">' + fmtRent(r.rent) + '</td>' +
          '<td>' + fmtRent(r.maint) + '</td>' +
          '<td class="total-price">' + fmtRent(r.total) + '</td>' +
          '<td>' + esc(r.type || '-') + '</td>' +
          '<td>' + esc(fmtArea(r.area)) + '</td>' +
          '<td>' + esc(r.floor || '-') + '</td>' +
          '<td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.address || r.region || '-') + '</td>' +
          '<td class="agency-cell">' + esc(agencyLabel(r.agency, source)) + (r.phone ? '<br><span class="phone-small">' + esc(r.phone) + '</span>' : '') + '</td>' +
          '<td><a href="' + r.url + '" target="_blank" class="link-btn">보기</a></td>' +
          '<td class="reaction-cell">' +
            '<button class="heart-btn reaction-btn fav-like-btn" type="button" title="좋아요" ' +
              'data-fav-id="' + favId + '" data-fav-source="' + favSource + '">🤍</button>' +
            '<button class="heart-btn reaction-btn fav-dislike-btn" type="button" title="싫어요" ' +
              'data-fav-id="' + favId + '" data-fav-source="' + favSource + '">👎</button>' +
          '</td>';
        applyReactionState(tr, r.id, r.source);
        tr.addEventListener('click', e => {
          // Clicks on the heart, links, or anything inside an already-open
          // detail row shouldn't trigger fly/toggle on the parent.
          if (e.target.closest('.reaction-btn, a, .detail-row')) return;
          const entry = markerMap.get(r.id);
          if (entry && r.lat && r.lon) {
            map.flyTo([r.lat, r.lon], 17, { duration: 0.5 });
            entry.marker.openPopup();
          }
          // Toggle the detail sub-row (information from normal_common's
          // optional fields: description / options / parking / etc.).
          const existing = tr.nextElementSibling;
          if (existing && existing.classList.contains('detail-row')) {
            existing.remove();
            openIds.delete(r.id);
            tr.classList.remove('row-open');
            return;
          }
          const detail = detailRowFor(r, colspan);
          if (detail) {
            tr.insertAdjacentElement('afterend', detail);
            openIds.add(r.id);
            tr.classList.add('row-open');
            // Close button at the bottom of the detail panel — clicking it
            // mirrors clicking the row header (collapses + scrolls the row
            // back into view). Without this the user has to scroll back up
            // to the row to close a long detail panel.
            const closeBtn = detail.querySelector('.detail-collapse');
            if (closeBtn) {
              closeBtn.addEventListener('click', evt => {
                evt.stopPropagation();
                detail.remove();
                openIds.delete(r.id);
                tr.classList.remove('row-open');
                tr.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
              });
            }
          }
        });
        const like = tr.querySelector('.fav-like-btn');
        const dis  = tr.querySelector('.fav-dislike-btn');
        like.addEventListener('click', e => {
          e.stopPropagation();
          if (!window.Favorites) return;
          const id = like.dataset.favId, src = like.dataset.favSource;
          if (window.Favorites.isFav(id, src)) {
            window.Favorites.remove(id, src);
          } else {
            window.Favorites.add(r);
          }
          applyReactionState(tr, r.id, r.source);
        });
        dis.addEventListener('click', e => {
          e.stopPropagation();
          if (!window.Favorites) return;
          const id = dis.dataset.favId, src = dis.dataset.favSource;
          if (window.Favorites.isDislike(id, src)) {
            window.Favorites.removeDislike(id, src);
          } else {
            window.Favorites.addDislike(r);
          }
          applyReactionState(tr, r.id, r.source);
        });
        tbody.appendChild(tr);
        // Re-attach the detail row if the user had this one open before the
        // re-render (filter/sort cycle).
        if (openIds.has(r.id)) {
          const detail = detailRowFor(r, colspan);
          if (detail) {
            tbody.appendChild(detail);
            tr.classList.add('row-open');
            const closeBtn = detail.querySelector('.detail-collapse');
            if (closeBtn) {
              closeBtn.addEventListener('click', evt => {
                evt.stopPropagation();
                detail.remove();
                openIds.delete(r.id);
                tr.classList.remove('row-open');
                tr.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
              });
            }
          }
        }
      });
      scheduleImageLoad(tbody);
    }

    // Sort headers
    document.querySelectorAll('th[data-col]').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (sortCol === col) sortAsc = !sortAsc;
        else { sortCol = col; sortAsc = true; }
        document.querySelectorAll('th[data-col]').forEach(t => {
          t.classList.toggle('sorted', t.dataset.col === sortCol);
          t.textContent = t.textContent.replace(/ [▲▼]$/, '') + (t.dataset.col === sortCol ? (sortAsc ? ' ▲' : ' ▼') : '');
        });
        render();
      });
    });

    ['maxDeposit', 'maxRent', 'typeFilter', 'search'].forEach(id => {
      const onChange = () => {
        if (id === 'maxDeposit' || id === 'maxRent') {
          if (priceFilterPrefs) priceFilterPrefs.set(currentPriceFilterState());
          else render();
          return;
        }
        if (filterPrefs) filterPrefs.set(currentFilterState());
        else render();
      };
      const el = document.getElementById(id);
      el.addEventListener('change', onChange);
      if (el.tagName !== 'SELECT') el.addEventListener('input', onChange);
    });

    // Shared area filter: overlay polygon on side map, mount chip, re-render on change
    if (window.AreaFilter) {
      AreaFilter.mountMapOverlay(map);
      AreaFilter.mountControl(document.getElementById('areaCtl'));
      AreaFilter.subscribe(render);
    }

    // Cross-device favorites sync — re-render heart icons when localStorage
    // gets refreshed from the server (fires once on initial load + on every
    // subsequent /api/favorites POST response). Without this listener the
    // initial render uses the stale localStorage snapshot and never updates,
    // so a heart toggled on another device stays invisible until full reload.
    window.addEventListener('favoritesSynced', render);

    // Refetch server state when the tab becomes visible again. The page may
    // have been backgrounded for hours while a phone added/removed a
    // favorite — visibilitychange is the cheapest cross-device convergence
    // signal that doesn't require polling. Favorites.refresh() handles the
    // GET, merge, and favoritesSynced dispatch which re-runs render() above.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && window.Favorites && window.Favorites.refresh) {
        window.Favorites.refresh();
      }
    });

    render();
  }

  window.PlatformPage = { init };
})();
