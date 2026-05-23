// Shared listing detail panel renderer used by platform pages and favorites.
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

    if (!pairs.length && !description && !options && !security && !maintenanceDetail && !maintenanceBasis && !maintenanceItems) return "";

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

    return `
      <div class="sec">
        <div class="sec-title">매물 정보</div>
        ${gridHtml}
        ${maintenanceHtml}${descHtml}${optsHtml}${secHtml}
      </div>
    `;
  }

  window.ListingInfo = { esc, typeLabel, buildSection };
})();
