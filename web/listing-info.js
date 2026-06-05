// Shared listing detail panel renderer used by platform pages and favorites.
//
// Exposes ``window.ListingInfo`` with:
//   esc(value)              — HTML-escape any value
//   typeLabel(d, source)    — Daangn enum → Korean label, otherwise raw
//   buildSection(d, source) — full "매물 정보" HTML block (may be empty)
//   attachSparklines(root, source)
//       Find any sparkline placeholder buildSection planted inside ``root``,
//       fetch its price history from /api/listings/{source}/{id}/price-history,
//       and render an SVG. Idempotent — already-loaded placeholders are
//       skipped, so calling this twice (e.g. on a re-render that re-attaches
//       the detail row) is safe.
(function () {
  const DAANGN_TYPE_LABEL = {
    ONE_ROOM: "원룸",
    OPEN_ONE_ROOM: "오픈형 원룸",
    SPLIT_ONE_ROOM: "분리형 원룸",
    TWO_ROOM: "투룸",
    THREE_ROOM: "쓰리룸 이상",
    OFFICETEL: "오피스텔",
    APARTMENT: "아파트",
    VILLA: "빌라/연립",
    HOUSE: "단독/다가구",
  };

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ───────── Naver Land URL rewrite ─────────
  // /rooms?articleNo=N alone gets redirected to a viewport-only URL that
  // drops articleNo (Naver assumes the visitor wants the map at their last
  // viewport, not "show me this listing"). Including ms=lat,lng,zoom + the
  // default listing filter blocks that redirect, so the side panel opens
  // with the article selected. Mirrors encode_coord() in scripts/rentmap.py.
  const NAVER_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const NAVER_LINK_ZOOM = 17;
  const NAVER_LINK_PARAMS =
    "a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL" +
    "&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM";

  function encodeNaverCoord(value) {
    if (value == null || !isFinite(value)) return null;
    let n = Math.round(value * 10000000) + 2000000000;
    if (n <= 0) return "0";
    let out = "";
    while (n > 0) {
      out = NAVER_BASE62[n % 62] + out;
      n = Math.floor(n / 62);
    }
    return out || "0";
  }

  function naverArticleUrl(fallback, listing) {
    if (!listing) return fallback || "";
    const id = listing.id != null ? String(listing.id) : "";
    const lat = Number(listing.lat != null ? listing.lat : listing.latitude);
    const lon = Number(
      listing.lon != null ? listing.lon :
      (listing.lng != null ? listing.lng : listing.longitude)
    );
    if (!id || !isFinite(lat) || !isFinite(lon) || lat === 0 || lon === 0) {
      return fallback || "";
    }
    const ms = encodeNaverCoord(lat) + "," + encodeNaverCoord(lon) + "," + NAVER_LINK_ZOOM;
    return "https://new.land.naver.com/rooms?ms=" + ms +
           "&" + NAVER_LINK_PARAMS +
           "&articleNo=" + encodeURIComponent(id);
  }

  function resolveListingUrl(fallback, source, listing) {
    if (source === "naver") return naverArticleUrl(fallback, listing);
    return fallback || "";
  }

  // 주소에서 식별력 높은 꼬리(동/리 + 번지)만 뽑아낸다. 전체 주소를 그대로
  // 보여주면 "경기도 수원시 영통구 …" 광역 prefix가 매물마다 똑같이 반복돼
  // 정작 동/번지가 잘려 보이지 않는다.
  function shortAddress(addr) {
    if (addr == null) return "";
    const s = String(addr).trim();
    if (!s) return "";
    // 지번 주소: "...매탄동 508-13" → "매탄동 508-13"
    const lot = s.match(/([가-힣\d]+(?:동|리|가))(?:\s+([\d]+(?:[-\d]+)?))?\s*$/);
    if (lot) return lot[1] + (lot[2] ? " " + lot[2] : "");
    // 도로명 주소: 끝 두 토큰만 남긴다 ("…광교중앙로42번길 12" → "광교중앙로42번길 12")
    const parts = s.split(/\s+/);
    return parts.slice(-2).join(" ");
  }

  function typeLabel(d, source) {
    const raw = d.type || d.room_type || "";
    if (source === "daangn" && DAANGN_TYPE_LABEL[raw]) return DAANGN_TYPE_LABEL[raw];
    return raw;
  }

  // ───────── 옵션·보안 태그 정규화 ─────────
  // 데이터 파이프라인이 가끔 망가진 문자열을 흘려서 - python list literal이
  // 통째로 들어오거나, 광고 텍스트가 한 태그로 들어오거나, 원시 영문 enum이
  // 노출되는 경우가 있다. 프런트에서 방어적으로 한 번 더 닦아준다.
  // 진짜 fix는 scripts/rentmap.py 의 normal_common / normal_daangn 단에서
  // 정리하는 게 맞지만, 이미 빌드된 정적 데이터에도 효과가 나게 여기서 처리.
  const DAANGN_OPTION_LABEL = {
    MOVE_IN_REGISTRATION: "전입신고 가능",
    ROOFTOP: "옥상/옥탑",
  };
  // 양 끝 따옴표/대괄호/공백/쉼표 정리
  function stripBracketsQuotes(s) {
    let v = String(s || "").trim();
    // 반복 strip — [' x ']' 같은 경우도 처리
    while (v && /^[\[\]"'`(),\s]/.test(v)) v = v.slice(1);
    while (v && /[\[\]"'`(),\s]$/.test(v)) v = v.slice(0, -1);
    return v.trim();
  }
  // 한 태그가 점(.)으로 이어진 광고 텍스트(예: "월세20만원별도.전입가능.보증보험가능")
  // 인지 추정. 점이 2개 이상 있고, 각 segment가 모두 한글/숫자 위주의 짧은
  // 토큰이면 점 단위로 분해해서 별도 태그로 만든다. 소수점이 들어간
  // "세대당 1.5대이상" 같은 정상 태그는 길이가 짧고 공백을 포함해 걸러진다.
  function maybeSplitDotSeparated(t) {
    if (!t) return [t];
    const dots = (t.match(/\./g) || []).length;
    if (dots < 2) return [t];
    if (/\s/.test(t)) return [t];  // 공백 포함은 정상 태그로 간주
    const parts = t.split(".").map(p => p.trim()).filter(Boolean);
    if (parts.length < 3) return [t];
    if (parts.some(p => p.length > 18)) return [t];  // 한 segment가 너무 길면 의심
    return parts;
  }
  function normalizeTag(raw, source) {
    let t = stripBracketsQuotes(raw);
    if (!t) return [];
    // 원시 영문 enum (ALL_CAPS_UNDERSCORE) — 짧은 약어(TV, CCTV 등)는 통과,
    // 그 외엔 한국어 라벨로 치환하거나 매핑 없으면 숨김 (대문자만으로 된
    // 시스템 코드는 사용자에게 의미 없음)
    if (/^[A-Z][A-Z0-9_]+$/.test(t)) {
      if (t.length <= 4) {
        // TV / CCTV / IPTV 같은 약어는 그대로
      } else if (DAANGN_OPTION_LABEL[t]) {
        t = DAANGN_OPTION_LABEL[t];
      } else {
        return [];  // 미매핑 enum은 노출하지 않음
      }
    }
    return maybeSplitDotSeparated(t);
  }
  // 외부 진입점: raw 문자열(";" / "," 구분)을 받아 정규화된 태그 배열을
  // 반환. 빈 태그 / 중복 제거까지 책임.
  function normalizeTagList(raw, source) {
    if (!raw) return [];
    const out = [];
    const seen = new Set();
    String(raw).split(/[;,]\s*/).forEach(piece => {
      normalizeTag(piece, source).forEach(t => {
        if (!t) return;
        if (seen.has(t)) return;
        seen.add(t);
        out.push(t);
      });
    });
    return out;
  }

  // ───────── value gating ─────────
  // Numeric fields where 0 means "missing", not "literally zero". Dabang's
  // provision_size is 0 for ~22% of its inventory; displaying "공급면적: 0.00"
  // is just noise.
  const NUMERIC_PAIR_KEYS = new Set(["공급면적", "전용면적", "방수", "욕실"]);
  function isMeaningful(key, value) {
    if (value == null) return false;
    const s = String(value).trim();
    if (s === "") return false;
    if (NUMERIC_PAIR_KEYS.has(key)) {
      const n = parseFloat(s);
      return !isNaN(n) && n > 0;
    }
    return true;
  }

  // ───────── 관리비 표시 ─────────
  // Each platform ships maintenance metadata in a very different shape:
  //   dabang  → "detail_code: E06; detail_cost: 50000; detail_include_types:
  //              WATER_RATES; PUBLIC_USE_RATES; ETC_USE_RATES"
  //              + basis "FIXED_FEE_CHARGE"
  //   zigbang → items "수도; excluded: 전기; 가스; ..."  (already Korean,
  //              just needs label-splitting)
  //              + detail "amount: 1; includes: ...; include: code: 03; ..."
  //              (noisy dict dump — we drop it)
  //   daangn  → detail "관리비 8만원" (already a one-liner; nothing else known)
  //
  // humanizeMaintenance() consolidates all three into a structured object,
  // and renderMaintenanceBlock() turns that into the panel HTML.
  const MAINT_ENUM_LABEL = {
    PUBLIC_USE_RATES: "공용관리",
    WATER_RATES: "수도",
    HOT_WATER_RATES: "온수",
    GAS_RATES: "가스",
    ELECTRICITY_RATES: "전기",
    HEATING_RATES: "난방",
    INTERNET_RATES: "인터넷",
    TV_RATES: "TV",
    CLEANING_RATES: "청소",
    SECURITY_RATES: "보안",
    PARKING_RATES: "주차",
    ELEVATOR_RATES: "엘리베이터",
    ETC_USE_RATES: "기타",
    FIXED_FEE_CHARGE: "정액 부과",
    ETC_FEE_CHARGE: "실비 부과",
    UNABLE_CHECK_FEE_CHARGE: "확인 불가",
  };

  function fmtWonNumber(n) {
    return n.toLocaleString("ko-KR") + "원";
  }

  function labelEnum(s) {
    const t = (s || "").trim();
    if (!t) return "";
    if (/^[A-Z_]+$/.test(t)) return MAINT_ENUM_LABEL[t] || t;
    return t;
  }

  // Returns {cost, basis, includes:[], excludes:[]}. Missing fields are
  // omitted; the renderer hides empty rows.
  function humanizeMaintenance(d) {
    const out = { cost: null, basis: null, includes: [], excludes: [] };
    const rawDetail = (d.maintenance_detail || "").trim();
    const rawBasis  = (d.maintenance_basis  || "").trim();
    const rawItems  = (d.maintenance_items  || "").trim();

    // ── dabang: detail_code / detail_cost / detail_include_types ────────
    if (/detail_(cost|code|include_types)\s*:/.test(rawDetail)) {
      const cost = rawDetail.match(/detail_cost\s*:\s*([\d,]+)/);
      if (cost) {
        const n = parseInt(cost[1].replace(/,/g, ""), 10);
        if (!isNaN(n) && n > 0) out.cost = fmtWonNumber(n);
      }
      const inc = rawDetail.match(/detail_include_types\s*:\s*([A-Z_][A-Z_;\s]*)/);
      if (inc) {
        out.includes = inc[1].split(/[;\s]+/).map(s => s.trim()).filter(Boolean).map(labelEnum);
      }
      if (rawBasis) out.basis = labelEnum(rawBasis);
    }
    // ── zigbang: items already say "포함; excluded: 미포함" in Korean ───
    else if (/excluded\s*:/i.test(rawItems)) {
      const parts = rawItems.split(/excluded\s*:/i);
      out.includes = (parts[0] || "").split(/;\s*/).map(s => s.trim()).filter(Boolean);
      out.excludes = (parts[1] || "").split(/;\s*/).map(s => s.trim()).filter(Boolean);
    }
    // ── daangn / other: detail is already a one-liner like "관리비 8만원" ──
    else if (rawDetail) {
      out.cost = rawDetail;
    }
    // ── fallback: nothing matched; surface whatever items hold, raw ──
    else if (rawItems) {
      out.includes = rawItems.split(/;\s*/).map(s => s.trim()).filter(Boolean).map(labelEnum);
    }
    return out;
  }

  function renderMaintenanceBlock(d) {
    const h = humanizeMaintenance(d);
    const lines = [];
    if (h.cost) lines.push(h.cost);
    if (h.basis) lines.push("부과방식: " + h.basis);
    if (h.includes.length) lines.push("포함: " + h.includes.join(", "));
    if (h.excludes.length) lines.push("미포함: " + h.excludes.join(", "));
    if (!lines.length) return "";
    return '<div class="info-long"><div class="info-long-key">관리비 상세</div>' +
           '<div class="info-long-val">' + esc(lines.join("\n")) + "</div></div>";
  }

  // ───────── price sparkline ─────────
  // Total monthly cost = deposit/100 (rough opportunity-cost normalization,
  // ~1%/yr deposit interest) + rent + maint. Mostly useful as a single curve
  // that summarizes "did the listing get cheaper or pricier overall."
  function combinedCost(pt) {
    const d = pt.deposit || 0;
    const r = pt.rent || 0;
    const m = pt.maint || 0;
    return d * 0.01 + r + m;
  }

  function buildSparkSvg(points) {
    if (!points || points.length < 2) return "";  // need at least 2 to draw a line
    const w = 240, h = 36, pad = 2;
    const ys = points.map(combinedCost);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const span = maxY - minY || 1;
    const step = (w - pad * 2) / (points.length - 1);
    const coords = points.map((_, i) => {
      const x = pad + i * step;
      const y = h - pad - ((ys[i] - minY) / span) * (h - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const first = ys[0], last = ys[ys.length - 1];
    const trend = last > first ? "up" : (last < first ? "down" : "flat");
    return (
      '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h +
      '" class="price-spark price-spark--' + trend + '" preserveAspectRatio="none">' +
      '<polyline points="' + coords + '" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
      '</svg>'
    );
  }

  function describeChange(points) {
    if (!points || points.length < 2) return "";
    const first = points[0], last = points[points.length - 1];
    const delta = (last.rent || 0) - (first.rent || 0);
    if (delta === 0 && (last.deposit || 0) === (first.deposit || 0)) {
      return points.length + "회 기록 · 변동 없음";
    }
    const arrow = delta > 0 ? "▲" : (delta < 0 ? "▼" : "·");
    return points.length + "회 기록 · 월세 " + arrow + " " + Math.abs(delta) + "만";
  }

  async function loadOnePlaceholder(el, source) {
    if (el.dataset.ptLoaded === "1") return;
    el.dataset.ptLoaded = "1";  // mark optimistically so concurrent calls don't double-fetch
    const listingNo = el.dataset.listingNo;
    if (!listingNo) return;
    try {
      const resp = await fetch("/api/listings/" + encodeURIComponent(source) +
                               "/" + encodeURIComponent(listingNo) + "/price-history");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      const points = (data && data.points) || [];
      if (points.length < 2) {
        // One data point isn't a trend — hide the placeholder rather than
        // show a sad empty box. Listings with only the discovery snapshot
        // will start showing a chart after their first price change.
        el.remove();
        return;
      }
      el.innerHTML =
        '<div class="price-trend-meta">📈 가격 추이 · ' + esc(describeChange(points)) + '</div>' +
        buildSparkSvg(points);
    } catch (err) {
      // API failure shouldn't break the panel. Drop the placeholder silently.
      el.remove();
    }
  }

  function attachSparklines(root, source) {
    if (!root) return;
    const placeholders = root.querySelectorAll(".price-trend[data-listing-no]");
    placeholders.forEach(el => loadOnePlaceholder(el, source));
  }

  // ───────── main panel ─────────
  function buildSection(d, source) {
    const pairs = [
      ["용도", d.building_use],
      ["방유형", typeLabel(d, source)],
      ["공급면적", d.supply_area],
      ["전용면적", d.exclusive_area],
      ["구조", d.room_structure],
      ["층구조", d.duplex],
      ["방향", d.direction],
      ["방수", d.room_count],
      ["욕실", d.bathroom_count],
      ["주차", d.parking],
      ["엘리베이터", d.elevator],
      ["반려동물", d.pet_allowed],
      ["대출 가능", d.loan_available],
      ["입주", d.move_in],
      ["등록일", d.published_at],
      ["확인일", d.confirmed_at],
      ["게시 경과", d.listing_age_text],
      ["사용승인", d.approval_date],
    ].filter(([k, v]) => isMeaningful(k, v));

    const description = (d.description || "").trim();
    const options = (d.options || "").trim();
    const security = (d.security_options || "").trim();
    // Build the structured maintenance block via humanizeMaintenance.
    // Empty string when the source has no maintenance metadata at all.
    const maintenanceHtml = renderMaintenanceBlock(d);

    if (!pairs.length && !description && !options && !security && !maintenanceHtml && !d.id) return "";

    const gridHtml = pairs.length
      ? `<div class="info-grid">${pairs.map(([k, v]) =>
          `<div class="info-item"><span class="info-key">${k}</span><span class="info-val">${esc(v)}</span></div>`
        ).join("")}</div>`
      : "";

    const tagsHtml = (raw, cls = "") => {
      const items = normalizeTagList(raw, source);
      if (!items.length) return "";
      return `<div class="info-tags">${items.map((t) =>
        `<span class="info-tag${cls ? " " + cls : ""}">${esc(t)}</span>`
      ).join("")}</div>`;
    };
    const descHtml = description
      ? `<div class="info-long"><div class="info-long-key">소개</div><div class="info-long-val">${esc(description)}</div></div>`
      : "";
    const optsHtml = options
      ? `<div class="info-long"><div class="info-long-key">옵션</div>${tagsHtml(options)}</div>`
      : "";
    const secHtml = security
      ? `<div class="info-long"><div class="info-long-key">보안</div>${tagsHtml(security, "sec")}</div>`
      : "";

    // Sparkline placeholder — only emitted when we know the listing id, so
    // attachSparklines() has something to look up. Empty content; loadOnePlaceholder
    // will replace it (or remove it if there's no trend to show).
    const sparkHtml = d.id
      ? `<div class="price-trend" data-listing-no="${esc(d.id)}"></div>`
      : "";

    // Listing number — needed when the user calls the agent ("아주 12345번 매물").
    // Lives in the detail panel so the table row stays compact; copy button
    // wiring is attached by the host via wireCopyButtons() below.
    const listingIdHtml = d.id
      ? `<div class="listing-id-row">
           <span class="listing-id-label">매물번호</span>
           <span class="listing-id-text" title="${esc(d.id)}">${esc(d.id)}</span>
           <button type="button" class="listing-id-copy" title="번호 복사" data-copy-text="${esc(d.id)}" data-listing-id="${esc(d.id)}">📋</button>
         </div>`
      : "";

    // 전체 주소 — 표/카드에서는 너비를 좁혀 뒷부분(동/번지)만 보이게 잘리므로
    // 광역 prefix까지 한 번에 확인하고 싶을 땐 여기서 복사 가능하게 노출한다.
    const fullAddr = (d.address || d.region || "").trim();
    const addrRowHtml = fullAddr
      ? `<div class="listing-id-row addr-detail-row">
           <span class="listing-id-label">주소</span>
           <span class="listing-id-text addr-detail-text" title="${esc(fullAddr)}">${esc(fullAddr)}</span>
           <button type="button" class="listing-id-copy addr-copy" title="주소 복사" data-copy-text="${esc(fullAddr)}">📋</button>
         </div>`
      : "";

    return `
      <div class="sec">
        <div class="sec-title">매물 정보</div>
        ${listingIdHtml}
        ${addrRowHtml}
        ${gridHtml}
        ${sparkHtml}
        ${maintenanceHtml}${descHtml}${optsHtml}${secHtml}
      </div>
    `;
  }

  // 매물 상세 패널의 모든 복사 버튼(.listing-id-copy)을 한 번에 wire-up.
  // 버튼은 data-copy-text 속성을 갖고 있어야 하며, 클릭 시 클립보드에 쓰고
  // 살짝 ✓ 피드백을 준다. platform-common.js / favorites.html / bookmarks.html
  // 어디에서 패널을 그리든 동일한 헬퍼를 호출하면 된다.
  async function copyTextToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {
        // 권한 거부 / 비-secure context 등 — execCommand fallback로 진행
      }
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (_) {
      return false;
    }
  }

  function wireCopyButtons(root) {
    if (!root) return;
    const btns = root.querySelectorAll(".listing-id-copy");
    btns.forEach(btn => {
      if (btn.dataset.copyWired === "1") return;
      btn.dataset.copyWired = "1";
      btn.addEventListener("click", async e => {
        e.stopPropagation();
        const text = btn.dataset.copyText || btn.dataset.listingId || "";
        if (!text) return;
        const ok = await copyTextToClipboard(text);
        const orig = btn.textContent;
        btn.textContent = ok ? "✓" : "✗";
        btn.classList.add("copied");
        setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1200);
      });
    });
  }

  window.ListingInfo = { esc, typeLabel, buildSection, attachSparklines, resolveListingUrl, shortAddress, wireCopyButtons, normalizeTagList };
})();
