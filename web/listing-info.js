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
    ].filter(([, v]) => v != null && String(v).trim() !== "");

    const description = (d.description || "").trim();
    const options = (d.options || "").trim();
    const security = (d.security_options || "").trim();
    const maintenanceDetail = (d.maintenance_detail || "").trim();
    const maintenanceBasis = (d.maintenance_basis || "").trim();
    const maintenanceItems = (d.maintenance_items || "").trim();

    if (!pairs.length && !description && !options && !security && !maintenanceDetail && !maintenanceBasis && !maintenanceItems && !d.id) return "";

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

    const maintenanceHtml = (maintenanceDetail || maintenanceBasis || maintenanceItems)
      ? `<div class="info-long"><div class="info-long-key">관리비 상세</div><div class="info-long-val">${esc([maintenanceDetail, maintenanceBasis, maintenanceItems].filter(Boolean).join("\n"))}</div></div>`
      : "";
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
