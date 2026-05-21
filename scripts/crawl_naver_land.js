const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const DEFAULT_URL =
  "https://new.land.naver.com/rooms?ms=2AzVQ9,3zkrDJ,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT";

function parseArgs(argv) {
  const args = {
    url: DEFAULT_URL,
    outputCsv: path.join("data", "naver_land_ajou_2026-05-22.csv"),
    rawJson: "",
    maxPages: 5,
    chromePath: "",
    headed: false,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    if (arg === "--url" && next) args.url = next, i += 1;
    else if (arg === "--output-csv" && next) args.outputCsv = next, i += 1;
    else if (arg === "--raw-json" && next) args.rawJson = next, i += 1;
    else if (arg === "--max-pages" && next) args.maxPages = Number(next), i += 1;
    else if (arg === "--chrome-path" && next) args.chromePath = next, i += 1;
    else if (arg === "--headed") args.headed = true;
  }

  return args;
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
  if (parts.length < 2) return { latitude: "", longitude: "", zoom: "" };
  return {
    latitude: decodeCoord(parts[0]) ?? "",
    longitude: decodeCoord(parts[1]) ?? "",
    zoom: parts[2] || "",
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

async function main() {
  const args = parseArgs(process.argv);
  const chromePath = findChrome(args.chromePath);
  const center = getMapCenter(args.url);

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
    await page.goto("https://new.land.naver.com/", { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(1200);

    const firstResponsePromise = page.waitForResponse(
      (response) => response.url().includes("/api/articles?") && response.status() === 200,
      { timeout: 45000 },
    );
    await page.goto(args.url, { waitUntil: "domcontentloaded", timeout: 45000 });
    const firstResponse = await firstResponsePromise;
    const firstUrl = firstResponse.url();
    const firstJson = await readNaverJson(firstResponse);

    const payloads = [firstJson];
    let isMoreData = Boolean(firstJson.isMoreData);

    for (let pageNo = 2; pageNo <= args.maxPages && isMoreData; pageNo += 1) {
      const nextUrl = new URL(firstUrl);
      nextUrl.searchParams.set("page", String(pageNo));
      const response = await context.request.get(nextUrl.toString(), {
        headers: cleanRequestHeaders(articleHeaders),
        timeout: 30000,
      });
      if (!response.ok()) break;
      const json = await readNaverJson(response);
      payloads.push(json);
      isMoreData = Boolean(json.isMoreData);
      await page.waitForTimeout(250);
    }

    const seen = new Set();
    const records = [];
    for (const payload of payloads) {
      for (const article of payload.articleList || []) {
        const articleNo = getFirst(article, ["articleNo"]);
        if (articleNo && seen.has(articleNo)) continue;
        if (articleNo) seen.add(articleNo);
        records.push(normalizeArticle(article, args.url, center));
      }
    }

    records.sort((a, b) =>
      String(a.agency).localeCompare(String(b.agency), "ko") ||
      Number(a.total_monthly_manwon || 999999) - Number(b.total_monthly_manwon || 999999),
    );

    writeCsv(records, args.outputCsv);

    if (args.rawJson) {
      fs.mkdirSync(path.dirname(args.rawJson), { recursive: true });
      fs.writeFileSync(args.rawJson, JSON.stringify(payloads, null, 2), "utf8");
    }

    console.log(`Wrote ${records.length} rows to ${args.outputCsv}`);
    if (args.rawJson) console.log(`Wrote raw payloads to ${args.rawJson}`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
