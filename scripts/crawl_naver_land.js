const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const DEFAULT_URLS = [
  "https://new.land.naver.com/rooms?cortarNo=4111710200&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
  "https://new.land.naver.com/rooms?cortarNo=4111514000&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
  "https://new.land.naver.com/rooms?cortarNo=4111710100&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
];

function parseArgs(argv) {
  const args = {
    urls: [],
    outputCsv: path.join("data", "naver_land_ajou_2026-05-22.csv"),
    rawJson: "",
    maxPages: 5,
    chromePath: "",
    headed: false,
    cortarNo: "",
    skipHome: false,
    minLat: 37.265,
    maxLat: 37.285,
    minLng: 127.030,
    maxLng: 127.055,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    if (arg === "--url" && next) { args.urls.push(next); i += 1; }
    else if (arg === "--output-csv" && next) { args.outputCsv = next; i += 1; }
    else if (arg === "--raw-json" && next) { args.rawJson = next; i += 1; }
    else if (arg === "--max-pages" && next) { args.maxPages = Number(next); i += 1; }
    else if (arg === "--chrome-path" && next) { args.chromePath = next; i += 1; }
    else if (arg === "--headed") args.headed = true;
    else if (arg === "--cortar-no" && next) { args.cortarNo = next; i += 1; }
    else if (arg === "--skip-home") args.skipHome = true;
    else if (arg === "--min-lat" && next) { args.minLat = Number(next); i += 1; }
    else if (arg === "--max-lat" && next) { args.maxLat = Number(next); i += 1; }
    else if (arg === "--min-lng" && next) { args.minLng = Number(next); i += 1; }
    else if (arg === "--max-lng" && next) { args.maxLng = Number(next); i += 1; }
  }

  if (args.urls.length === 0) args.urls.push(...DEFAULT_URLS);
  return args;
}

function isInBbox(record, args) {
  const lat = Number(record.latitude);
  const lng = Number(record.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return true;
  return lat >= args.minLat && lat <= args.maxLat && lng >= args.minLng && lng <= args.maxLng;
}

function findChrome(explicitPath) {
  const candidates = [
    explicitPath,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  ].filter(Boolean);

  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) {
    throw new Error("Chrome or Edge executable was not found. Pass --chrome-path.");
  }
  return found;
}

function decodeBase62(value) {
  const chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  if (!value || !/^[0-9a-zA-Z]+$/.test(value)) return null;
  let number = 0;
  for (const char of value) {
    const index = chars.indexOf(char);
    if (index < 0) return null;
    number = number * 62 + index;
  }
  return number;
}

function decodeCoord(value) {
  const decoded = decodeBase62(value);
  return decoded == null ? null : (decoded - 2000000000) / 10000000;
}

function getMapCenter(url) {
  const parsed = new URL(url);
  const ms = parsed.searchParams.get("ms") || "";
  const parts = ms.split(",");
  if (parts.length < 2) return { latitude: 37.280, longitude: 127.043, zoom: "16" };
  return {
    latitude: decodeCoord(parts[0]) ?? 37.280,
    longitude: decodeCoord(parts[1]) ?? 127.043,
    zoom: parts[2] || "16",
  };
}

function getFirst(obj, names) {
  for (const name of names) {
    if (obj && obj[name] !== undefined && obj[name] !== null && obj[name] !== "") {
      return obj[name];
    }
  }
  return "";
}

function parseManwon(text) {
  if (!text) return "";
  const normalized = String(text).replace(/\s+/g, "");
  const match = normalized.match(/(?:월세|단기임대)?(.+?)\/([0-9,]+)/);
  if (!match) return "";

  const toManwon = (value) => {
    const cleaned = value.replace(/,/g, "");
    const eok = cleaned.match(/([0-9.]+)억/);
    const rest = cleaned.replace(/[0-9.]+억/, "").replace(/[^0-9.]/g, "");
    return (eok ? Number(eok[1]) * 10000 : 0) + (rest ? Number(rest) : 0);
  };

  return {
    deposit: toManwon(match[1]),
    rent: Number(match[2].replace(/,/g, "")),
  };
}

function parseAmountManwon(value) {
  if (value === undefined || value === null || value === "") return "";
  const cleaned = String(value).replace(/\s+/g, "").replace(/,/g, "");
  const eok = cleaned.match(/([0-9.]+)억/);
  const rest = cleaned.replace(/[0-9.]+억/, "").replace(/[^0-9.]/g, "");
  const amount = (eok ? Number(eok[1]) * 10000 : 0) + (rest ? Number(rest) : 0);
  return Number.isFinite(amount) && amount > 0 ? amount : "";
}

function csvEscape(value) {
  const text = value === undefined || value === null ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function writeCsv(records, filePath) {
  const columns = [
    "source",
    "listing_no",
    "room_id",
    "url",
    "agency",
    "agent_name",
    "agent_phone",
    "region",
    "address",
    "latitude",
    "longitude",
    "address_public_level",
    "title",
    "deposit_manwon",
    "rent_manwon",
    "maintenance_manwon",
    "total_monthly_manwon",
    "room_type",
    "area_m2",
    "floor",
    "direction",
    "parking",
    "move_in",
    "approval_date",
    "building_use",
    "options",
    "security_options",
    "image_1",
    "image_2",
    "crawl_note",
  ];

  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const lines = [columns.map(csvEscape).join(",")];
  for (const record of records) {
    lines.push(columns.map((column) => csvEscape(record[column])).join(","));
  }
  fs.writeFileSync(filePath, `${lines.join("\n")}\n`, "utf8");
}

function cleanRequestHeaders(headers) {
  if (!headers) return undefined;
  const blocked = new Set([
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "host",
  ]);
  return Object.fromEntries(
    Object.entries(headers).filter(([name]) => !name.startsWith(":") && !blocked.has(name.toLowerCase())),
  );
}

async function readNaverJson(response) {
  const body = await response.body();
  const text = new TextDecoder("utf-8").decode(body);
  return JSON.parse(text);
}

function formatYmd(value) {
  const text = String(value || "");
  const match = text.match(/^(\d{4})(\d{2})(\d{2})$/);
  return match ? `${match[1]}.${match[2]}.${match[3]}` : text;
}

function imageUrl(value) {
  if (!value) return "";
  return String(value).startsWith("/") ? `https://landthumb-phinf.pstatic.net${value}` : String(value);
}

function normalizeArticle(article, sourceUrl, center) {
  const depositText = getFirst(article, ["dealOrWarrantPrc", "priceText"]);
  const parsedPrice = parseManwon(`${getFirst(article, ["tradeTypeName"])}${depositText}/${getFirst(article, ["rentPrc"])}`);
  const rent = getFirst(article, ["rentPrc"]);
  const maintenanceWon = Number(getFirst(article, ["monthlyManagementCost", "managementCost"]) || 0);
  const maintenance = maintenanceWon ? Math.round((maintenanceWon / 10000) * 10) / 10 : "";
  const latitude = getFirst(article, ["latitude"]) || center.latitude;
  const longitude = getFirst(article, ["longitude"]) || center.longitude;
  const articleNo = getFirst(article, ["articleNo"]);
  const detailUrl = articleNo ? `https://new.land.naver.com/rooms?articleNo=${articleNo}` : sourceUrl;
  const rentManwon = parsedPrice.rent || Number(String(rent).replace(/,/g, "")) || "";

  return {
    source: "naver_land",
    listing_no: articleNo,
    room_id: articleNo,
    url: detailUrl,
    agency: getFirst(article, ["realtorName", "cpName"]),
    agent_name: "",
    agent_phone: "",
    region: getFirst(article, ["cityName", "divisionName", "sectionName"]),
    address: getFirst(article, ["articleName", "buildingName"]),
    latitude,
    longitude,
    address_public_level: "naver_public_listing_level",
    title: getFirst(article, ["articleFeatureDesc", "articleName"]),
    deposit_manwon: parsedPrice.deposit || parseAmountManwon(depositText),
    rent_manwon: rentManwon,
    maintenance_manwon: maintenance,
    total_monthly_manwon: rentManwon === "" ? "" : Math.round((Number(rentManwon) + (Number(maintenance) || 0)) * 10) / 10,
    room_type: getFirst(article, ["realEstateTypeName", "articleName"]),
    area_m2: [getFirst(article, ["supplySpace", "area1"]), getFirst(article, ["exclusiveSpace", "area2"])].filter(Boolean).join("/"),
    floor: [getFirst(article, ["floorInfo"]), getFirst(article, ["floorLayerName"])].filter(Boolean).join(" "),
    direction: getFirst(article, ["direction"]),
    parking: "",
    move_in: "",
    approval_date: formatYmd(getFirst(article, ["articleConfirmYmd", "confirmYmd"])),
    building_use: getFirst(article, ["articleRealEstateTypeName"]),
    options: [getFirst(article, ["tagList"]), getFirst(article, ["articleFeatureDesc"])].flat().filter(Boolean).join("; "),
    security_options: "",
    image_1: imageUrl(getFirst(article, ["representativeImgUrl"])),
    image_2: "",
    crawl_note: "Captured from Naver Land article list API.",
  };
}

async function crawlOneUrl(page, context, targetUrl, getHeaders, args) {
  const center = getMapCenter(targetUrl);

  const firstResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/articles?") && response.status() === 200,
    { timeout: 45000 },
  );
  await page.goto(targetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  const firstResponse = await firstResponsePromise;
  let firstUrl = firstResponse.url();

  const capturedParams = new URL(firstUrl).searchParams;
  console.log(`  cortarNo: ${capturedParams.get("cortarNo")}, zoom: ${capturedParams.get("zoom")}`);

  let firstJson;
  if (args.cortarNo) {
    const u = new URL(firstUrl);
    console.log(`  Overriding cortarNo: ${u.searchParams.get("cortarNo")} -> ${args.cortarNo}`);
    u.searchParams.set("cortarNo", args.cortarNo);
    firstUrl = u.toString();
    const corrResp = await context.request.get(firstUrl, {
      headers: cleanRequestHeaders(getHeaders()),
      timeout: 30000,
    });
    firstJson = corrResp.ok() ? await readNaverJson(corrResp) : await readNaverJson(firstResponse);
  } else {
    firstJson = await readNaverJson(firstResponse);
  }

  const payloads = [firstJson];
  let isMoreData = Boolean(firstJson.isMoreData);

  for (let pageNo = 2; pageNo <= args.maxPages && isMoreData; pageNo += 1) {
    const nextUrl = new URL(firstUrl);
    nextUrl.searchParams.set("page", String(pageNo));
    const response = await context.request.get(nextUrl.toString(), {
      headers: cleanRequestHeaders(getHeaders()),
      timeout: 30000,
    });
    if (!response.ok()) break;
    const json = await readNaverJson(response);
    payloads.push(json);
    isMoreData = Boolean(json.isMoreData);
    await page.waitForTimeout(250);
  }

  const records = [];
  for (const payload of payloads) {
    for (const article of payload.articleList || []) {
      const record = normalizeArticle(article, targetUrl, center);
      if (isInBbox(record, args)) records.push(record);
    }
  }

  return { records, payloads };
}

async function main() {
  const args = parseArgs(process.argv);
  const chromePath = findChrome(args.chromePath);

  const browser = await chromium.launch({
    headless: !args.headed,
    executablePath: chromePath,
    args: ["--disable-blink-features=AutomationControlled"],
  });

  const context = await browser.newContext({
    locale: "ko-KR",
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  });
  const page = await context.newPage();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });

  let articleHeaders = null;
  page.on("request", async (request) => {
    if (request.url().includes("/api/articles?")) {
      articleHeaders = await request.allHeaders();
    }
  });

  try {
    if (!args.skipHome) {
      await page.goto("https://new.land.naver.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
      await page.waitForTimeout(1200);
    }

    const seen = new Set();
    const allRecords = [];
    const allPayloads = [];

    for (let urlIdx = 0; urlIdx < args.urls.length; urlIdx += 1) {
      const targetUrl = args.urls[urlIdx];
      console.log(`\nCrawling URL ${urlIdx + 1}/${args.urls.length}: ${targetUrl}`);

      const { records, payloads } = await crawlOneUrl(page, context, targetUrl, () => articleHeaders, args);
      allPayloads.push(...payloads);

      let newCount = 0;
      for (const record of records) {
        const articleNo = record.listing_no;
        if (articleNo && seen.has(articleNo)) continue;
        if (articleNo) seen.add(articleNo);
        allRecords.push(record);
        newCount += 1;
      }
      console.log(`  Found ${records.length} in bbox, ${newCount} new after dedup`);
    }

    allRecords.sort((a, b) =>
      String(a.agency).localeCompare(String(b.agency), "ko") ||
      Number(a.total_monthly_manwon || 999999) - Number(b.total_monthly_manwon || 999999),
    );

    writeCsv(allRecords, args.outputCsv);

    if (args.rawJson) {
      fs.mkdirSync(path.dirname(args.rawJson), { recursive: true });
      fs.writeFileSync(args.rawJson, JSON.stringify(allPayloads, null, 2), "utf8");
    }

    console.log(`\nWrote ${allRecords.length} rows to ${args.outputCsv}`);
    if (args.rawJson) console.log(`Wrote raw payloads to ${args.rawJson}`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
