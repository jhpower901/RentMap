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

  function typeLabel(d, source) {
    const raw = d.type || d.room_type || "";
    if (source === "daangn" && DAANGN_TYPE_LABEL[raw]) return DAANGN_TYPE_LABEL[raw];
    return raw;
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
      const items = raw.split(/[;,]\s*/).map((s) => s.trim()).filter(Boolean);
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

    return `
      <div class="sec">
        <div class="sec-title">매물 정보</div>
        ${gridHtml}
        ${sparkHtml}
        ${maintenanceHtml}${descHtml}${optsHtml}${secHtml}
      </div>
    `;
  }

  window.ListingInfo = { esc, typeLabel, buildSection, attachSparklines };
})();
