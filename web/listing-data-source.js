(function () {
  const CSV_PATHS = {
    dabang: ["data/dabang_ajou_2026-05-22.csv", "../data/dabang_ajou_2026-05-22.csv"],
    daangn: ["data/daangn_ajou_2026-05-22.csv", "../data/daangn_ajou_2026-05-22.csv"],
    zigbang: ["data/zigbang_ajou_2026-05-22.csv", "../data/zigbang_ajou_2026-05-22.csv"],
    naver: ["data/naver_land_ajou_2026-05-22.csv", "../data/naver_land_ajou_2026-05-22.csv"],
  };

  function parseCsv(text) {
    const rows = [];
    let row = [];
    let value = "";
    let inQuotes = false;

    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      const next = text[i + 1];

      if (inQuotes) {
        if (ch === '"' && next === '"') {
          value += '"';
          i += 1;
        } else if (ch === '"') {
          inQuotes = false;
        } else {
          value += ch;
        }
        continue;
      }

      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        row.push(value);
        value = "";
      } else if (ch === "\n") {
        row.push(value);
        rows.push(row);
        row = [];
        value = "";
      } else if (ch !== "\r") {
        value += ch;
      }
    }

    if (value !== "" || row.length) {
      row.push(value);
      rows.push(row);
    }

    if (!rows.length) return [];
    const headers = rows[0].map((header) => header.replace(/^\uFEFF/, ""));
    return rows.slice(1).filter((cells) => cells.some((cell) => cell !== "")).map((cells) => {
      const record = {};
      headers.forEach((header, index) => {
        record[header] = cells[index] ?? "";
      });
      return record;
    });
  }

  function toNumber(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    const parsed = Number(String(value).replace(/,/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function firstValue(record, names) {
    for (const name of names) {
      const value = record[name];
      if (value !== undefined && value !== null && String(value).trim() !== "") return value;
    }
    return "";
  }

  function normalizeImageUrl(source, value) {
    if (!value) return "";
    if (source !== "zigbang") return value;

    try {
      const url = new URL(value);
      if (url.hostname !== "ic.zigbang.com") return value;
      if (!url.search) {
        url.searchParams.set("w", "400");
        url.searchParams.set("h", "300");
        url.searchParams.set("q", "70");
      }
      return url.toString();
    } catch (_) {
      return value;
    }
  }

  function normalizeListing(source, record) {
    const listingId = firstValue(record, ["listing_no", "room_id", "item_id"]);
    const region = source === "daangn"
      ? [record.region_depth2, record.region_depth3].filter(Boolean).join(" ")
      : firstValue(record, ["region"]);

    const agency = source === "daangn"
      ? (record.writer_type === "DIRECT_USER" || record.writer_type === "DIRECT" ? "DIRECT" : "BROKER")
      : firstValue(record, ["agency", "realtor_name"]);

    return {
      id: toNumber(listingId) ?? listingId,
      source,
      url: firstValue(record, ["url"]),
      agency,
      phone: firstValue(record, ["agent_phone", "realtor_phone"]),
      region,
      address: firstValue(record, ["address"]),
      lat: toNumber(firstValue(record, ["latitude"])),
      lon: toNumber(firstValue(record, ["longitude"])),
      title: firstValue(record, ["title"]),
      deposit: toNumber(firstValue(record, ["deposit_manwon"])),
      rent: toNumber(firstValue(record, ["rent_manwon"])),
      maint: toNumber(firstValue(record, ["maintenance_manwon"])),
      total: toNumber(firstValue(record, ["total_monthly_manwon"])),
      type: firstValue(record, ["room_type", "residence_type"]),
      area: firstValue(record, ["area_m2"]),
      floor: firstValue(record, ["floor"]),
      img1: normalizeImageUrl(source, firstValue(record, ["image_1"])),
      img2: normalizeImageUrl(source, firstValue(record, ["image_2"])),
    };
  }

  async function loadPlatformListings(source) {
    const csvPaths = CSV_PATHS[source];
    if (!csvPaths) throw new Error(`Unknown listing source: ${source}`);

    let response = null;
    let loadedPath = "";
    for (const csvPath of csvPaths) {
      response = await fetch(csvPath, { cache: "no-store" });
      if (response.ok) {
        loadedPath = csvPath;
        break;
      }
    }
    if (!response || !response.ok) {
      throw new Error(`Failed to load CSV for ${source}`);
    }
    window.currentListingCsvPath = loadedPath;

    const csvText = await response.text();
    return parseCsv(csvText).map((record) => normalizeListing(source, record));
  }

  window.loadPlatformListings = loadPlatformListings;
})();
