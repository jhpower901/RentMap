(function () {
  const CSV_PATHS = {
    dabang: ["data/dabang_ajou_2026-05-22.csv", "../data/dabang_ajou_2026-05-22.csv"],
    daangn: ["data/daangn_ajou_2026-05-22.csv", "../data/daangn_ajou_2026-05-22.csv"],
    zigbang: ["data/zigbang_ajou_2026-05-22.csv", "../data/zigbang_ajou_2026-05-22.csv"],
    naver: ["data/naver_land_ajou_2026-05-22.csv", "../data/naver_land_ajou_2026-05-22.csv"],
  };

  function parseCsv(text) {
    if (!text) return [];
    const lines = text.split(/\r?\n/).filter(line => line.trim());
    if (lines.length === 0) return [];

    const parseLine = (line) => {
      const result = [];
      let current = '';
      let inQuotes = false;
      for (let i = 0; i < line.length; i++) {
        const char = line[i];
        if (char === '"') {
          if (inQuotes && line[i + 1] === '"') {
            current += '"';
            i++;
          } else {
            inQuotes = !inQuotes;
          }
        } else if (char === ',' && !inQuotes) {
          result.push(current.trim());
          current = '';
        } else {
          current += char;
        }
      }
      result.push(current.trim());
      return result;
    };

    const rawHeaders = parseLine(lines[0]);
    const headers = rawHeaders.map(h => h.replace(/^\uFEFF/, '').replace(/^["']|["']$/g, '').trim());

    return lines.slice(1).map(line => {
      const cells = parseLine(line);
      const record = {};
      headers.forEach((h, i) => {
        let val = cells[i] || '';
        record[h] = val.replace(/^["']|["']$/g, '').trim();
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
      ? (record.writer_type === "DIRECT_USER" || record.writer_type === "DIRECT"
          ? "DIRECT"
          : (firstValue(record, ["agency"]) || "BROKER"))
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

    const globalKey = 'DATA_' + source.toUpperCase();
    if (Array.isArray(window[globalKey])) return window[globalKey];

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
