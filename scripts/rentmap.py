#!/usr/bin/env python
"""Python crawler and web generator for the RentMap workspace."""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import math
import os
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse, parse_qs, urlunparse

import requests


ROOT = Path(__file__).resolve().parents[1]
# Today's date in the container/host local timezone (the rentmap-server image
# sets TZ=Asia/Seoul). Schedulers always pass --date explicitly, so this only
# affects manual CLI invocations — and there "today" is the expected default.
DEFAULT_DATE = datetime.now().strftime("%Y-%m-%d")
# CSV / web file names include the area slug so different regions don't
# overwrite each other. ``RENTMAP_AREA_NAME`` is set by region_runner to the
# region's slug for every scheduled crawl; manual CLI invocations fall back
# to "ajou" so existing operator habits keep working.
DEFAULT_AREA = (os.environ.get("RENTMAP_AREA_NAME") or "ajou").strip() or "ajou"
# Legacy hardcoded Ajou bbox — kept as documentation of the original area;
# only DEFAULT_CENTER_LAT/LNG/RADIUS_KM are still used by the runtime.
DEFAULT_MIN_LAT = 37.260
DEFAULT_MAX_LAT = 37.290
DEFAULT_MIN_LNG = 127.025
DEFAULT_MAX_LNG = 127.095
DEFAULT_CENTER_LAT = 37.280062
DEFAULT_CENTER_LNG = 127.043688
DEFAULT_RADIUS_KM = 3.0
NO_PRICE_LIMIT_MANWON = 999999
DEFAULT_ZIGBANG_GEOHASHES = ["wyd7f", "wyd7g", "wyd7u", "wydk4", "wydk5", "wydkh"]
DEFAULT_DAANGN_REGION_IDS = [1289, 1290, 1298, 1294, 1295, 1296, 1297, 1291, 1302, 1303]
# Naver Land ms= grid parameters
# At zoom 16 each tile covers ~1.5km. Step 1.2km gives ~50% overlap → solid coverage.
NAVER_TILE_STEP_KM = 1.2
NAVER_ZOOM = 16
NAVER_DEFAULT_PARAMS = (
    "a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL"
    "&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM"
)
# Naver-specific timings/limits used in the crawl loop and detail enrichment.
NAVER_PAGE_DELAY_MS = 250          # gap between list-API page requests
NAVER_DETAIL_DELAY_MS = 250        # gap between detail-API requests
NAVER_DETAIL_RETRIES = 2           # retry count for 429/503
NAVER_PROGRESS_EVERY = 50          # how often to log "detail: i/N"
NAVER_DEFAULT_MAX_PAGES = 20       # list-API pages per cortarNo (100 articles/page)
NAVER_LIST_RETRIES = 2
NAVER_RATE_LIMIT_STATUS = 429
NAVER_TRANSIENT_STATUS_CODES = {429, 503}
NAVER_LIST_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
NAVER_DETAIL_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
NAVER_RATE_POLICY_STREAK_THRESHOLD = 3
DABANG_DEFAULT_DELAY_MS = 120      # gap between Dabang detail requests
DABANG_DEFAULT_ZOOM = 18
CRAWL_DETAIL_PROGRESS_EVERY = 20
# Missing confirmation policy: a listing can recover on any successful probe.
# If the retry probe cannot get usable data after these attempts, that probe is
# treated as absent and the DB miss counter decides whether to remove it.
MISSING_PROBE_ATTEMPTS = 3
MISSING_PROBE_DELAY_SECONDS = 2.0
NAVER_MISSING_PROBE_DELAY_SECONDS = 15.0
NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RETRY_DEFERRED_EXIT = 75
# Trig clamp so cos(lat) for the longitude conversion never hits 0 near the poles.
COS_LAT_FLOOR = 0.01
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
DAANGN_BASE_URL = "https://realty.daangn.com"
DAANGN_GRAPHQL_URL = "https://realty.kr.karrotmarket.com/graphql"
DAANGN_ARTICLE_DETAIL_QUERY_HASH = "a6ca947b00f51b71850abb5757a9bf66e73dd50524352b78aed4138bc82b9ae0"
_daangn_article_detail_query_hash_cache = ""

DABANG_COLUMNS = [
    "source", "listing_no", "room_id", "url", "agency", "agent_name", "agent_phone",
    "region", "address", "latitude", "longitude", "address_public_level", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "area_m2", "supply_area_m2", "exclusive_area_m2", "floor", "direction",
    "parking", "move_in", "published_at", "confirmed_at", "listing_age_text", "approval_date",
    "maintenance_detail", "maintenance_basis", "maintenance_items",
    "building_use", "options", "security_options", "description",
    "image_1", "image_2", "crawl_note",
]

ZIGBANG_COLUMNS = [
    "source", "listing_no", "item_id", "url", "agency", "agent_name", "agent_phone",
    "realtor_name", "realtor_phone", "agency_address", "agency_reg_no", "region", "address",
    "latitude", "longitude", "address_public_level", "title", "deposit_manwon", "rent_manwon",
    "maintenance_manwon", "total_monthly_manwon", "room_type", "bathroom_count", "service_type", "area_m2",
    "supply_area_m2", "exclusive_area_m2", "floor", "direction", "parking", "elevator",
    "move_in", "published_at", "confirmed_at", "listing_age_text", "approval_date", "residence_type",
    "maintenance_detail", "maintenance_basis", "maintenance_items",
    "non_compliant_building", "options", "description",
    "image_1", "image_2", "crawl_note",
]

DAANGN_COLUMNS = [
    "source", "listing_no", "url", "writer_type", "agency", "region_depth1",
    "region_depth2", "region_depth3", "address", "latitude", "longitude", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "room_count", "bathroom_count", "area_m2", "supply_area_m2", "exclusive_area_m2",
    "floor", "direction", "parking", "elevator", "pet_allowed", "loan_available", "move_in",
    "published_at", "confirmed_at", "listing_age_text", "approval_date",
    "maintenance_detail", "maintenance_basis", "maintenance_items", "building_use",
    "options", "description",
    "image_1", "image_2", "crawl_note",
]

# Facility tokens we look for inside the Daangn description body, since the
# proper "시설 정보" grid that the user sees on the rendered page is React-
# rendered from a separate fetch we don't see in the SSR HTML. Most agents
# repeat the same vocabulary in the description so a keyword scan recovers
# the majority of the signal. Update this list if you spot a token Daangn
# uses that doesn't appear here.
DAANGN_FACILITY_KEYWORDS = [
    "세탁기", "건조기", "드럼세탁기", "냉장고", "에어컨", "천장형에어컨", "벽걸이에어컨",
    "인덕션", "가스레인지", "가스렌지", "전자레인지", "오븐", "식기세척기",
    "TV", "와이파이", "비데", "샤워부스", "욕조",
    "침대", "책상", "옷장", "신발장", "붙박이장", "싱크대", "화장대",
    "엘리베이터", "주차", "오토바이주차", "베란다", "발코니", "테라스",
]

NAVER_COLUMNS = [
    "source", "listing_no", "room_id", "url", "agency", "agent_name", "agent_phone",
    "region", "address", "latitude", "longitude", "address_public_level", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "room_count", "bathroom_count", "area_m2", "supply_area_m2", "exclusive_area_m2",
    "floor", "direction", "room_structure", "duplex", "parking", "move_in", "approval_date",
    "published_at", "confirmed_at", "listing_age_text",
    "maintenance_detail", "maintenance_basis", "maintenance_items",
    "building_use", "description", "options", "security_options",
    "image_1", "image_2", "crawl_note",
]


def first(obj: Any, names: list[str], default: Any = "") -> Any:
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None and value != "":
            return value
    return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[config] ignoring invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] ignoring invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def default_max_deposit() -> int:
    return env_int("RENTMAP_MAX_DEPOSIT", NO_PRICE_LIMIT_MANWON)


def default_max_rent() -> int:
    return env_int("RENTMAP_MAX_RENT", NO_PRICE_LIMIT_MANWON)


def bbox_from_center_radius(center_lat: float, center_lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """Convert (center, radius) to (min_lat, max_lat, min_lng, max_lng).

    1° latitude ≈ 111 km. Longitude shrinks by cos(lat) toward the poles;
    we clamp the cosine to ``COS_LAT_FLOOR`` so the divisor never approaches
    zero (lat ≥ 89.4°).
    """
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * max(math.cos(math.radians(center_lat)), COS_LAT_FLOOR))
    return (
        center_lat - lat_delta,
        center_lat + lat_delta,
        center_lng - lng_delta,
        center_lng + lng_delta,
    )


def default_bbox_from_env() -> tuple[float, float, float, float]:
    center_lat = env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT)
    center_lng = env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG)
    radius_km = env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM)
    return bbox_from_center_radius(center_lat, center_lng, radius_km)


def nested(obj: dict[str, Any] | None, path: list[str], default: Any = "") -> Any:
    cur: Any = obj
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    text = re.sub(r"[^0-9.]", "", str(value))
    return float(text) if text else None


def round1(value: float) -> float:
    return round(value + 1e-9, 1)


def has_address_detail(value: Any) -> bool:
    return bool(value and re.search(r"\s\d+(?:-\d+)?(?:\s|$)", str(value)))


def join_text_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        value = [value]
    items: list[str] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, str):
            label = item
        elif isinstance(item, dict):
            label = to_text(first(item, ["name", "title", "label", "option_name", "optionName", "value"]))
        else:
            label = to_text(item)
        if label and label not in items:
            items.append(label)
    return "; ".join(items)


def join_nested_text(value: Any) -> str:
    """Compact nested API values into a readable semicolon-separated string."""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return to_text(value)
    if isinstance(value, list):
        return "; ".join([x for x in (join_nested_text(v) for v in value) if x])
    if isinstance(value, dict):
        parts: list[str] = []
        for key, val in value.items():
            if val in (None, "", [], {}):
                continue
            text = join_nested_text(val)
            if text:
                parts.append(f"{key}: {text}")
        return "; ".join(parts)
    return to_text(value)


def first_deep(obj: Any, names: list[str]) -> Any:
    """Find the first non-empty value for any key name in a nested payload."""
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) not in (None, "", [], {}):
                return obj.get(name)
        for value in obj.values():
            found = first_deep(value, names)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = first_deep(value, names)
            if found not in (None, "", [], {}):
                return found
    return ""


def parse_manwon_from_text(value: Any) -> float | None:
    text = to_text(value).replace(",", "")
    if not text:
        return None
    eok = re.search(r"([0-9.]+)\s*억", text)
    man = re.search(r"([0-9.]+)\s*만", text)
    if eok or man:
        amount = (float(eok.group(1)) * 10000 if eok else 0) + (float(man.group(1)) if man else 0)
        return round1(amount)
    number = to_number(text)
    return round1(number / 10000) if number and number >= 10000 else number


def to_iso_date(value: Any, now: datetime | None = None) -> str:
    """Coerce any plausible date input to ISO ``YYYY-MM-DD``. Returns ``""`` on failure.

    Accepts:
      - ISO datetime (with/without TZ suffix, microseconds, or space separator):
        ``2026-05-21T11:35:04.346421Z``, ``2026-05-21 11:35:04``
      - ISO date: ``2026-05-21``, ``2026/05/21``
      - Korean dotted: ``2026.05.21`` (with optional trailing dot) or ``26.05.21``
        (2-digit year → 20YY)
      - 8-digit packed: ``20260521``
      - Korean relative expressions: ``오늘``, ``어제``, ``그제`` / ``그저께``,
        ``N일 전``, ``N개월 전`` (approx 30d), ``N{시간|분|초} 전`` → 오늘

    Time-of-day component is intentionally dropped — every consumer column is
    ``DATE`` in the DB schema. Caller can keep the raw string alongside if needed
    for audit.
    """
    text = to_text(value).strip()
    if not text:
        return ""

    now = now or datetime.now()
    today = now.date()

    # Korean relative expressions — match before any digit-based parsing so
    # the digit prefix in "5일 전" doesn't get caught by YYYYMMDD.
    if text in ("오늘", "방금", "방금 전", "방금전", "지금"):
        return today.isoformat()
    if text == "어제":
        return (today - timedelta(days=1)).isoformat()
    if text in ("그제", "그저께"):
        return (today - timedelta(days=2)).isoformat()
    m = re.match(r"^\s*(\d+)\s*일\s*전\s*$", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.match(r"^\s*(\d+)\s*개월\s*전\s*$", text)
    if m:
        # Approximate — exact month math doesn't add value at day granularity.
        return (today - timedelta(days=int(m.group(1)) * 30)).isoformat()
    if re.match(r"^\s*\d+\s*(시간|분|초)\s*전\s*$", text):
        return today.isoformat()

    # 8-digit packed (YYYYMMDD), no separator. Common in naver payloads.
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""

    # Korean dotted: YYYY.MM.DD or YY.MM.DD (sometimes trailing dot).
    m = re.match(r"^(\d{2,4})\.(\d{1,2})\.(\d{1,2})\.?$", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000  # YY → 20YY (every site we crawl is post-2000)
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return ""

    # ISO / slash. Trim TZ suffix and microsecond tail so strptime stays simple.
    trimmed = text.rstrip("Z").strip()[:26]
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(trimmed, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def days_ago_text(iso_date: str, now: datetime | None = None) -> str:
    """Render an ISO ``YYYY-MM-DD`` as ``오늘`` / ``N일 전``. Empty/invalid → ``""``.

    Future dates return ``""`` rather than a negative count — they usually mean
    the source feed had a malformed date, not an actual future event.
    """
    if not iso_date:
        return ""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return ""
    now = now or datetime.now()
    days = (now.date() - d).days
    if days < 0:
        return ""
    return "오늘" if days == 0 else f"{days}일 전"


def split_area_pair(value: Any) -> tuple[str, str]:
    text = to_text(value).strip()
    if not text:
        return "", ""
    parts = [p for p in re.split(r"\s*/\s*", text) if p]
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", text)


def text_has_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def image_url(images: Any, index: int) -> str:
    if images is None:
        return ""
    arr = images if isinstance(images, list) else [images]
    if len(arr) <= index:
        return ""
    image = arr[index]
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        if image.get("prefix_url") and image.get("id"):
            return f"{image['prefix_url']}{image['id']}"
        return to_text(first(image, ["url", "image_url", "imageUrl", "src", "origin", "large", "medium", "img_url"]))
    return ""


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: to_text(row.get(col, "")) for col in columns})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def request_json(session: requests.Session, url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.json()


def _fmt_bbox(args: argparse.Namespace) -> str:
    return (
        f"lat={args.min_lat:.6f}..{args.max_lat:.6f} "
        f"lng={args.min_lng:.6f}..{args.max_lng:.6f}"
    )


def _fmt_limit(value: int | float | str | None) -> str:
    if value in (None, "", NO_PRICE_LIMIT_MANWON):
        return "none"
    return str(value)


def _target_area() -> str:
    return os.environ.get("RENTMAP_AREA_NAME") or "unspecified"


def _log_crawl_start(source: str, args: argparse.Namespace, *, extra: str = "") -> None:
    parts = [
        f"[crawl:{source}] START",
        f"area={_target_area()}",
        f"bbox=({_fmt_bbox(args)})",
    ]
    if extra:
        parts.append(extra)
    print(" ".join(parts), flush=True)


def _log_crawl_done(source: str, rows: int, output_csv: str, elapsed_s: float) -> None:
    print(f"[crawl:{source}] DONE rows={rows} output={output_csv} elapsed={elapsed_s:.1f}s", flush=True)


def crawl_dabang(args: argparse.Namespace) -> None:
    started = time.monotonic()
    _log_crawl_start(
        "dabang",
        args,
        extra=(
            f"source=dabang-api zoom={args.zoom} "
            f"max_deposit={_fmt_limit(args.max_deposit)} max_rent={_fmt_limit(args.max_rent)}"
        ),
    )
    session = requests.Session()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "D-Api-Version": "5.0.0",
        "D-App-Version": "1",
        "D-Call-Type": "web",
        "csrf": "token",
        "Referer": "https://www.dabangapp.com/map/onetwo",
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Content-Type": "application/json",
        "Origin": "https://www.dabangapp.com",
    }
    filters = {
        "sellingTypeList": ["MONTHLY_RENT"],
        "depositRange": {"min": 0, "max": args.max_deposit},
        "priceRange": {"min": 0, "max": args.max_rent},
        "isIncludeMaintenance": False,
        "pyeongRange": {"min": 0, "max": 999999},
        "useApprovalDateRange": {"min": 0, "max": 999999},
        "roomFloorList": ["GROUND_FIRST", "GROUND_SECOND_OVER", "SEMI_BASEMENT", "ROOFTOP"],
        "roomTypeList": ["ONE_ROOM", "TWO_ROOM"],
        "dealTypeList": ["AGENT"],
        "canParking": False,
        "isShortLease": False,
        "hasElevator": False,
        "hasPano": False,
        "isDivision": False,
        "isDuplex": False,
    }
    bbox = {"sw": {"lat": args.min_lat, "lng": args.min_lng}, "ne": {"lat": args.max_lat, "lng": args.max_lng}}
    encoded_filters = quote(json.dumps(filters, ensure_ascii=False, separators=(",", ":")))
    encoded_bbox = quote(json.dumps(bbox, ensure_ascii=False, separators=(",", ":")))

    print(f"[crawl:dabang] fetching list pages", flush=True)
    rooms: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"https://www.dabangapp.com/api/v5/room-list/category/one-two/bbox?filters={encoded_filters}&bbox={encoded_bbox}&zoom={args.zoom}&useMap=naver&page={page}"
        payload = request_json(session, url, headers=headers)
        result = payload.get("result", payload)
        rooms.extend(result.get("roomList") or [])
        if not result.get("hasMore"):
            break
        page += 1
    if not rooms:
        raise RuntimeError("No Dabang rooms found.")
    print(f"[crawl:dabang] list_rows={len(rooms)} detail_fetch=yes", flush=True)

    detail_headers = dict(headers)
    detail_headers["D-Api-Version"] = "3.0.1"
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    raw_details: list[Any] = []

    for idx, room in enumerate(rooms, 1):
        if idx % CRAWL_DETAIL_PROGRESS_EVERY == 0:
            print(f"[crawl:dabang] detail_progress={idx}/{len(rooms)}", flush=True)
        room_id = to_text(first(room, ["id", "room_id", "roomId", "seq", "hash"]))
        if not room_id or room_id in seen:
            continue
        seen.add(room_id)
        detail_url = f"https://www.dabangapp.com/api/3/new-room/detail?room_id={quote(room_id)}&api_version=3.0.1&call_type=web&version=1"
        try:
            detail_payload = request_json(session, detail_url, headers=detail_headers)
        except Exception as exc:
            print(f"WARNING: Detail fetch failed for room_id={room_id}: {exc}", file=sys.stderr)
            continue
        detail = detail_payload.get("result", detail_payload)
        raw_details.append(detail)
        room_data = first(detail, ["room"], detail)
        agent = first(detail, ["agent", "agency", "agent_info", "agentInfo", "office"], {})
        region = first(detail, ["region"], {})
        listing_no = first(room_data, ["seq", "room_seq", "roomSeq", "room_no", "roomNo", "id"])
        public_room_id = to_text(first(room_data, ["id", "room_id", "roomId"], room_id))

        price_title = to_text(first(room_data, ["price_title", "priceTitle"]))
        deposit = rent = None
        match = re.search(r"([0-9,]+)\s*/\s*([0-9,]+)", price_title)
        if match:
            deposit = to_number(match.group(1))
            rent = to_number(match.group(2))
        maintenance_won = to_number(first(room_data, ["maintenance_cost", "maintenanceCost"]))
        maintenance = round1(maintenance_won / 10000) if maintenance_won is not None else None
        if maintenance is None:
            maintenance = to_number(first(room_data, ["maintenance_cost_str", "maintenanceCostStr"]))
        if maintenance is None:
            maintenance = 0
        maintenance_detail = join_nested_text(first(room_data, [
            "maintenance_etc_fee_charge_detail", "maintenanceEtcFeeChargeDetail",
            "maintenance_fixed_fee_charge_detail_list", "maintenanceFixedFeeChargeDetailList",
            "maintenance_unable_check_detail", "maintenanceUnableCheckDetail",
        ]))
        maintenance_basis = join_nested_text(first(room_data, [
            "maintenance_charge_type", "maintenanceChargeType",
            "maintenance_standard_type", "maintenanceStandardType",
            "maintenance_charge_detail_type", "maintenanceChargeDetailType",
        ]))
        maintenance_items = join_text_list(first(room_data, [
            "maintenance_items_str", "maintenanceItemsStr",
            "personal_maintenance_items_str", "personalMaintenanceItemsStr",
        ]))

        location = first(room_data, ["location"], [])
        lng = lat = ""
        if isinstance(location, list) and len(location) >= 2:
            lng, lat = location[0], location[1]

        address = best_address(room_data, [
            "full_jibun_address2_str", "fullJibunAddress2Str", "full_road_address2_str",
            "fullRoadAddress2Str", "full_jibun_address_str", "fullJibunAddressStr",
            "full_road_address_str", "fullRoadAddressStr", "address",
        ])
        if not has_address_detail(address):
            near_url = f"https://www.dabangapp.com/api/v5/room/{quote(public_room_id)}/near"
            try:
                near_payload = request_json(session, near_url, headers=headers)
                near = near_payload.get("result", near_payload)
                near_addr = first(near, ["address"])
                if near_addr:
                    address = near_addr
                near_loc = first(near, ["location"], {})
                if isinstance(near_loc, dict) and near_loc.get("lat") is not None and near_loc.get("lng") is not None:
                    lat, lng = near_loc["lat"], near_loc["lng"]
            except Exception as exc:
                print(f"WARNING: Near fetch failed for room_id={public_room_id}: {exc}", file=sys.stderr)

        show = first(room_data, ["is_show_detail_address", "isShowDetailAddress"], None)
        toggle = first(room_data, ["is_toggle_detail_address", "isToggleDetailAddress"], None)
        if has_address_detail(address):
            address_level = "exact_address_visible"
        elif show is True or toggle is True:
            address_level = "detail_address_field_visible_but_no_jibun_number"
        else:
            address_level = "dong_only_ask_agency_for_exact_jibun"

        images = first(detail, ["image_list", "imageList", "images", "photos", "room_images", "roomImages"])
        options = first(room_data, ["room_options", "roomOptions", "options", "option"])
        security = first(room_data, ["safeties", "safety_options", "safetyOptions", "security_options", "securityOptions"])
        published_at = to_iso_date(first(room_data, ["saved_time_str", "savedTimeStr", "created_at", "createdAt"]))
        confirmed_at = to_iso_date(first(room_data, ["confirm_date_str", "confirmDateStr", "naver_verify_date_str", "naverVerifyDateStr"]))
        records.append({
            "source": "dabang",
            "listing_no": listing_no,
            "room_id": public_room_id,
            "url": f"https://www.dabangapp.com/room/{public_room_id}",
            "agency": first(agent, ["name", "office_name", "officeName", "agent_name", "agentName"]),
            "agent_name": first(agent, ["facename", "representative_name", "representativeName", "owner_name", "ownerName"]),
            "agent_phone": first(agent, ["agent_tel", "phone", "tel", "telephone", "cell_phone", "cellPhone"]),
            "region": first(region, ["full_name", "name"]),
            "address": address,
            "latitude": lat,
            "longitude": lng,
            "address_public_level": address_level,
            "title": first(room_data, ["title", "name", "description_title", "descriptionTitle"]),
            "deposit_manwon": deposit if deposit is not None else "",
            "rent_manwon": rent if rent is not None else "",
            "maintenance_manwon": maintenance,
            "total_monthly_manwon": round1(rent + maintenance) if rent is not None else "",
            "room_type": first(room_data, ["room_type_str", "roomTypeStr", "room_type_main_str", "roomTypeMainStr"]),
            "area_m2": first(room_data, ["room_size", "roomSize", "provision_size", "provisionSize"]),
            "supply_area_m2": first(room_data, ["provision_size", "provisionSize", "contract_size", "contractSize"]),
            "exclusive_area_m2": first(room_data, ["room_size", "roomSize"]),
            "floor": f"{first(room_data, ['room_floor_str', 'roomFloorStr'])}/{first(room_data, ['building_floor_str', 'buildingFloorStr'])}",
            "direction": first(room_data, ["direction_str", "directionStr", "direction"]),
            "parking": first(room_data, ["parking_str", "parkingStr", "parking"]),
            "move_in": first(room_data, ["moving_date", "movingDate"]),
            "published_at": published_at,
            "confirmed_at": confirmed_at,
            "listing_age_text": days_ago_text(published_at),
            "approval_date": to_iso_date(first(room_data, ["building_approval_date_str", "buildingApprovalDateStr"])),
            "maintenance_detail": maintenance_detail,
            "maintenance_basis": maintenance_basis,
            "maintenance_items": maintenance_items,
            "building_use": join_text_list(first(room_data, ["building_use_types_str", "buildingUseTypesStr"])),
            "options": join_text_list(options),
            "security_options": join_text_list(security),
            "description": to_text(first(room_data, ["memo", "description"])),
            "image_1": image_url(images, 0),
            "image_2": image_url(images, 1),
            "crawl_note": "",
        })
        time.sleep(args.delay_ms / 1000)

    records.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["total_monthly_manwon"]), float_or_inf(r["rent_manwon"])))
    write_csv(Path(args.output_csv), records, DABANG_COLUMNS)
    _reconcile_after_crawl("dabang", records, "dabang")
    if args.raw_json:
        Path(args.raw_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.raw_json).write_text(json.dumps(raw_details, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_crawl_done("dabang", len(records), args.output_csv, time.monotonic() - started)


def best_address(obj: dict[str, Any], names: list[str]) -> str:
    fallback = ""
    for name in names:
        value = first(obj, [name])
        if not value:
            continue
        if not fallback:
            fallback = to_text(value)
        if has_address_detail(value):
            return to_text(value)
    return fallback


def float_or_inf(value: Any) -> float:
    try:
        if value == "":
            return math.inf
        return float(value)
    except Exception:
        return math.inf


def normalize_phone(phone: Any) -> str:
    text = to_text(phone)
    if not text:
        return ""
    digits = re.sub(r"[^0-9]", "", text)
    if digits.startswith("02") and len(digits) == 8:
        return f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
    if digits.startswith("02") and len(digits) == 9:
        return f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
    if digits.startswith("02") and len(digits) == 10:
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return text


def crawl_zigbang(args: argparse.Namespace) -> None:
    started = time.monotonic()
    _log_crawl_start(
        "zigbang",
        args,
        extra=(
            f"source=zigbang-api geohashes={','.join(args.geohashes)} "
            f"max_deposit={_fmt_limit(args.max_deposit_manwon)} max_rent={_fmt_limit(args.max_rent_manwon)}"
        ),
    )
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*", "Origin": "https://www.zigbang.com", "Referer": "https://www.zigbang.com/"}
    items_by_id: dict[str, dict[str, Any]] = {}
    for geohash in args.geohashes:
        print(f"[crawl:zigbang] fetching list geohash={geohash}", flush=True)
        url = f"https://apis.zigbang.com/v2/items/oneroom?geohash={quote(geohash)}&depositMin=0&rentMin=0&salesTypes%5B0%5D=%EC%9B%94%EC%84%B8&domain=zigbang&checkAnyItemWithoutFilter=true"
        payload = request_json(session, url, headers=headers)
        for item in payload.get("items", []):
            lat, lng = float(item.get("lat", 0)), float(item.get("lng", 0))
            if args.min_lat <= lat <= args.max_lat and args.min_lng <= lng <= args.max_lng:
                items_by_id[to_text(item.get("itemId"))] = item
    print(f"[crawl:zigbang] detail_candidates_in_bbox={len(items_by_id)}", flush=True)

    rows: list[dict[str, Any]] = []
    for idx, item_id in enumerate(sorted(items_by_id), 1):
        if idx % CRAWL_DETAIL_PROGRESS_EVERY == 0:
            print(f"[crawl:zigbang] detail_progress={idx}/{len(items_by_id)}", flush=True)
        try:
            detail = request_json(session, f"https://apis.zigbang.com/v3/items/{quote(item_id)}", headers=headers)
            item = detail.get("item")
            if not item:
                continue
            deposit = int(nested(item, ["price", "deposit"], 0))
            rent = int(nested(item, ["price", "rent"], 0))
            if rent <= 0 or deposit > args.max_deposit_manwon or rent > args.max_rent_manwon:
                continue
            manage_cost = nested(item, ["manageCost", "amount"], "")
            total = int(rent) + int(manage_cost) if manage_cost != "" else ""
            manage_payload = item.get("manageCost") or {}
            maintenance_items = join_text_list(first(manage_payload, ["includes", "include"]))
            excluded_maintenance_items = join_text_list(first(manage_payload, ["notIncludes", "exclude"]))
            if excluded_maintenance_items:
                maintenance_items = "; ".join([x for x in [maintenance_items, f"excluded: {excluded_maintenance_items}"] if x])
            images = item.get("images") or []
            updated_at = to_iso_date(item.get("updatedAt", ""))
            area_m2 = get_area_m2(item.get("area"))
            rows.append({
                "source": "zigbang",
                "listing_no": item.get("itemId"),
                "item_id": item.get("itemId"),
                "url": f"https://www.zigbang.com/home/oneroom/items/{item.get('itemId')}?itemDetailType=ZIGBANG",
                "agency": nested(detail, ["agent", "agentTitle"]),
                "agent_name": nested(detail, ["agent", "agentName"]),
                "agent_phone": normalize_phone(nested(detail, ["agent", "agentPhone"])),
                "realtor_name": nested(detail, ["realtor", "name"]),
                "realtor_phone": normalize_phone(nested(detail, ["realtor", "phone"])),
                "agency_address": nested(detail, ["agent", "agentAddress"]),
                "agency_reg_no": nested(detail, ["realtor", "officeRegNumber"]),
                "region": nested(item, ["addressOrigin", "fullText"]),
                "address": item.get("jibunAddress", ""),
                "latitude": nested(item, ["location", "lat"]),
                "longitude": nested(item, ["location", "lng"]),
                "address_public_level": "exact_jibun_from_api",
                "title": item.get("title", ""),
                "deposit_manwon": deposit,
                "rent_manwon": rent,
                "maintenance_manwon": manage_cost,
                "total_monthly_manwon": total,
                "room_type": item.get("roomType", ""),
                "bathroom_count": item.get("bathroomCount", ""),
                "service_type": item.get("serviceType", ""),
                "area_m2": area_m2,
                "supply_area_m2": "",
                "exclusive_area_m2": area_m2,
                "floor": get_floor_text(item.get("floor")),
                "direction": item.get("roomDirection", ""),
                "parking": item.get("parkingAvailableText", ""),
                "elevator": item.get("elevator", ""),
                "move_in": to_iso_date(item.get("moveinDate", "")),
                "published_at": "",
                "confirmed_at": updated_at,
                "listing_age_text": days_ago_text(updated_at),
                "approval_date": to_iso_date(item.get("approveDate", "")),
                "residence_type": item.get("residenceType", ""),
                # maintenance_detail intentionally empty for zigbang — the
                # full manageCost dict is just code/name pairs that duplicate
                # ``maintenance_items``. The client builds the panel from items
                # alone (includes vs excludes split).
                "maintenance_detail": "",
                "maintenance_basis": "",
                "maintenance_items": maintenance_items,
                "non_compliant_building": item.get("nonCompliantBuilding", ""),
                "options": join_text_list(item.get("options")),
                "description": to_text(item.get("description", "")),
                "image_1": images[0] if len(images) > 0 else "",
                "image_2": images[1] if len(images) > 1 else "",
                "crawl_note": "",
            })
        except Exception as exc:
            print(f"WARNING: Failed detail {item_id}: {exc}", file=sys.stderr)
    rows.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["rent_manwon"]), float_or_inf(r["deposit_manwon"])))
    write_csv(Path(args.output_csv), rows, ZIGBANG_COLUMNS)
    _log_crawl_done("zigbang", len(rows), args.output_csv, time.monotonic() - started)
    _reconcile_after_crawl("zigbang", rows, "zigbang")


def get_floor_text(floor: Any) -> str:
    if not isinstance(floor, dict):
        return ""
    cur, total = floor.get("floor"), floor.get("allFloors")
    return f"{cur}/{total}" if cur is not None and total is not None else to_text(cur)


def get_area_m2(area: Any) -> str:
    if not isinstance(area, dict):
        return ""
    for key, value in area.items():
        if "M2" in key and value is not None:
            return to_text(value)
    return ""


def get_utf8(session: requests.Session, url: str, delay_ms: int = 0) -> str:
    resp = session.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "ko-KR,ko;q=0.9"}, timeout=20)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    if delay_ms:
        time.sleep(delay_ms / 1000)
    return resp.text


def crawl_daangn(args: argparse.Namespace) -> None:
    started = time.monotonic()
    _log_crawl_start(
        "daangn",
        args,
        extra=(
            f"source=daangn-region-pages region_ids={','.join(str(x) for x in args.region_ids)} "
            f"max_deposit={_fmt_limit(args.max_deposit)} max_rent={_fmt_limit(args.max_rent)} "
            f"detail_fetch={not args.skip_detail}"
        ),
    )
    valid_types = {"SPLIT_ONE_ROOM", "OPEN_ONE_ROOM", "TWO_ROOM", "OFFICETEL"}
    session = requests.Session()
    all_raw: list[dict[str, Any]] = []
    seen: set[str] = set()
    print(f"[crawl:daangn] fetching regions={len(args.region_ids)}", flush=True)
    for region_id in args.region_ids:
        listings = get_daangn_listings(session, region_id, args.max_deposit, args.max_rent, valid_types)
        print(f"[crawl:daangn] region={region_id} listings_within_budget={len(listings)}", flush=True)
        for listing in listings:
            article_id = article_id_from_url(listing.get("webUrl", ""))
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            all_raw.append(listing)
    print(f"[crawl:daangn] unique_listings={len(all_raw)}", flush=True)

    records: list[dict[str, Any]] = []
    for idx, listing in enumerate(all_raw, 1):
        article_id = article_id_from_url(listing.get("webUrl", ""))
        if idx % CRAWL_DETAIL_PROGRESS_EVERY == 0:
            print(f"[crawl:daangn] detail_progress={idx}/{len(all_raw)}", flush=True)
        trades = listing.get("trades") or []
        trade = next((t for t in trades if t.get("type") == "MONTH"), {})
        detail = {} if args.skip_detail else get_daangn_article_detail(session, article_id)
        region = listing.get("_regionInfo") or {}
        lat, lon = detail.get("lat", ""), detail.get("lon", "")
        public_addr = detail.get("publicAddress") or listing.get("address", "")
        approval = to_iso_date(detail.get("approvalDate") or listing.get("buildingApprovalDate", ""))
        writer_type = detail.get("writerType") or listing.get("writerType", "")
        maintenance = float(listing.get("manageCost") or 0)
        rent = float(trade.get("monthlyPay") or 0)
        title = re.sub(r"\s*\|\s*[^\|]+$", "", to_text(listing.get("title", "")))
        published_at = to_iso_date(detail.get("publishedAt", ""))
        confirmed_at = to_iso_date(detail.get("updatedAt", ""))
        records.append({
            "source": "daangn",
            "listing_no": article_id,
            "url": f"https://realty.daangn.com/articles/{article_id}",
            "writer_type": writer_type,
            "agency": detail.get("agencyName", ""),
            "region_depth1": region.get("depth1RegionName", ""),
            "region_depth2": region.get("depth2RegionName", ""),
            "region_depth3": region.get("depth3RegionName", ""),
            "address": public_addr,
            "latitude": lat,
            "longitude": lon,
            "title": title,
            "deposit_manwon": float(trade.get("deposit") or 0),
            "rent_manwon": rent,
            "maintenance_manwon": maintenance,
            "total_monthly_manwon": round1(rent + maintenance),
            "room_type": listing.get("salesType", ""),
            "room_count": detail.get("roomCnt", ""),
            "bathroom_count": detail.get("bathroomCnt", ""),
            "area_m2": listing.get("area", ""),
            "supply_area_m2": "",
            "exclusive_area_m2": listing.get("area", ""),
            "floor": listing.get("floor", ""),
            "direction": detail.get("direction", ""),
            "parking": detail.get("parking", ""),
            "elevator": detail.get("elevator", ""),
            "pet_allowed": detail.get("petAllowed", ""),
            "loan_available": detail.get("loanAvailable", ""),
            "move_in": to_iso_date(detail.get("moveIn", "")),
            "published_at": published_at,
            "confirmed_at": confirmed_at,
            "listing_age_text": days_ago_text(published_at or confirmed_at),
            "approval_date": approval,
            "maintenance_detail": detail.get("maintenanceDetail", ""),
            "maintenance_basis": detail.get("maintenanceBasis", ""),
            "maintenance_items": detail.get("maintenanceItems", ""),
            "building_use": detail.get("buildingUse", ""),
            "options": detail.get("options", ""),
            "description": detail.get("description", ""),
            "image_1": image_url(listing.get("images"), 0),
            "image_2": image_url(listing.get("images"), 1),
            "crawl_note": "",
        })
    if all(v != 0 for v in [args.min_lat, args.max_lat, args.min_lng, args.max_lng]):
        before = len(records)
        records = [r for r in records if bbox_ok(r.get("latitude"), r.get("longitude"), args)]
        print(f"[crawl:daangn] bbox_filter rows_before={before} rows_after={len(records)}", flush=True)
    records.sort(key=lambda r: (to_text(r["region_depth3"]), float_or_inf(r["total_monthly_manwon"]), float_or_inf(r["rent_manwon"])))
    write_csv(Path(args.output_csv), records, DAANGN_COLUMNS)
    _log_crawl_done("daangn", len(records), args.output_csv, time.monotonic() - started)
    _reconcile_after_crawl("daangn", records, "daangn")


def get_daangn_listings(session: requests.Session, region_id: int, max_deposit: int, max_rent: int, valid_types: set[str]) -> list[dict[str, Any]]:
    try:
        html_text = get_utf8(session, f"https://www.daangn.com/kr/realty/?in=x-{region_id}")
    except Exception as exc:
        print(f"WARNING: Region {region_id} fetch failed: {exc}", file=sys.stderr)
        return []
    marker = "window.__remixContext = "
    start = html_text.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = html_text.find("</script>", start)
    if end < 0:
        return []
    try:
        ctx = json.loads(html_text[start:end].strip().rstrip(";"))
        data = ctx["state"]["loaderData"]["routes/kr.realty._index"]
    except Exception as exc:
        print(f"WARNING: Region {region_id} JSON parse failed: {exc}", file=sys.stderr)
        return []
    region = data.get("searchRegion") or {}
    filtered = []
    for listing in data.get("realtyPosts", {}).get("realtyPosts", []) or []:
        if listing.get("salesType") not in valid_types:
            continue
        ok_trade = next((t for t in listing.get("trades", []) if t.get("type") == "MONTH" and t.get("deposit", 10**9) <= max_deposit and t.get("monthlyPay", 10**9) <= max_rent), None)
        if ok_trade:
            listing = dict(listing)
            listing["_regionInfo"] = region
            filtered.append(listing)
    return filtered


def article_id_from_url(url: str) -> str:
    match = re.search(r"/articles/(\d+)", url)
    return match.group(1) if match else ""


def extract_daangn_relay_store(text: str) -> dict[str, Any]:
    match = re.search(r'window\.RELAY_STORE\s*=\s*("(?:\\.|[^"\\])*")\s*;', text, re.S)
    if not match:
        return {}
    try:
        return json.loads(json.loads(match.group(1)))
    except Exception:
        return {}


def daangn_ref_node(store: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ref = value.get("__ref")
    node = store.get(ref) if ref else None
    return node if isinstance(node, dict) else {}


def find_daangn_article_node(store: dict[str, Any], article_id: str) -> dict[str, Any]:
    root = store.get("client:root")
    if isinstance(root, dict):
        ref_key = f'articleByOriginalArticleIdForSeo(originalArticleId:"{article_id}")'
        node = daangn_ref_node(store, root.get(ref_key))
        if node:
            return node
    for value in store.values():
        if isinstance(value, dict) and to_text(value.get("originalId")) == article_id:
            return value
    return {}


def find_daangn_article_detail_query_hash(session: requests.Session, detail_html: str) -> str:
    global _daangn_article_detail_query_hash_cache
    if _daangn_article_detail_query_hash_cache:
        return _daangn_article_detail_query_hash_cache

    scan_text = detail_html.replace("\\/", "/")
    asset_paths = sorted(set(re.findall(
        r'(?:https://realty\.daangn\.com)?/?assets/ArticleDetail-[^"\'<>]+\.js',
        scan_text,
    )))
    for asset_path in asset_paths:
        url = asset_path if asset_path.startswith("http") else f"{DAANGN_BASE_URL}/{asset_path.lstrip('/')}"
        try:
            resp = session.get(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/javascript,*/*",
                    "Accept-Language": "ko-KR,ko;q=0.9",
                    "Referer": f"{DAANGN_BASE_URL}/",
                },
                timeout=20,
            )
            resp.raise_for_status()
            resp.encoding = "utf-8"
        except Exception:
            continue
        match = re.search(
            r'id:[`"]([0-9a-f]{64})[`"].{0,240}?name:[`"]ArticleDetailQuery[`"]',
            resp.text,
            re.S,
        )
        if match:
            _daangn_article_detail_query_hash_cache = match.group(1)
            return _daangn_article_detail_query_hash_cache

    _daangn_article_detail_query_hash_cache = DAANGN_ARTICLE_DETAIL_QUERY_HASH
    return _daangn_article_detail_query_hash_cache


def get_daangn_graphql_article_detail(
    session: requests.Session,
    article_id: str,
    detail_html: str,
) -> dict[str, Any]:
    query_hash = find_daangn_article_detail_query_hash(session, detail_html)
    payload = {
        "operationName": "ArticleDetailQuery",
        "variables": {"articleId": article_id},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": query_hash,
            },
        },
    }
    try:
        resp = session.post(
            DAANGN_GRAPHQL_URL,
            headers={
                "User-Agent": UA,
                "Accept": "application/graphql-response+json, application/json",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Content-Type": "application/json",
                "Origin": DAANGN_BASE_URL,
                "Referer": f"{DAANGN_BASE_URL}/articles/{article_id}",
            },
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"WARNING: Article {article_id} GraphQL detail failed: {exc}", file=sys.stderr)
        return {}

    article = nested(data, ["data", "articleByOriginalArticleId"], {})
    if isinstance(article, dict) and article:
        return article
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        first_error = errors[0] if isinstance(errors, list) and errors else {}
        message = to_text(first(first_error, ["message"], "unknown error"))
        print(f"WARNING: Article {article_id} GraphQL detail returned no article: {message}", file=sys.stderr)
    return {}


def apply_daangn_graphql_article_detail(detail: dict[str, str], article: dict[str, Any]) -> None:
    coord = article.get("publicCoordinate") if isinstance(article.get("publicCoordinate"), dict) else {}
    updates = {
        "lat": coord.get("lat", ""),
        "lon": coord.get("lon", ""),
        "publicAddress": article.get("publicAddress", ""),
        "roomCnt": article.get("roomCnt", ""),
        "bathroomCnt": article.get("bathroomCnt", ""),
        "approvalDate": article.get("buildingApprovalDate", ""),
        "writerType": article.get("writerTypeV2", ""),
        "publishedAt": article.get("publishedAt", ""),
        "updatedAt": article.get("updatedAt", ""),
        "description": article.get("content", ""),
    }
    for key, value in updates.items():
        text = to_text(value).strip()
        if text:
            detail[key] = text

    biz_profile = article.get("bizProfile") if isinstance(article.get("bizProfile"), dict) else {}
    agency_name = to_text(first(biz_profile, ["name", "businessCompanyName"])).strip()
    if agency_name:
        detail["agencyName"] = agency_name

    if detail["description"] and not detail["options"]:
        facs = [fac for fac in DAANGN_FACILITY_KEYWORDS if fac in detail["description"]]
        if facs:
            detail["options"] = "; ".join(facs)


def get_daangn_article_detail(session: requests.Session, article_id: str) -> dict[str, str]:
    try:
        text = get_utf8(session, f"https://realty.daangn.com/articles/{article_id}", delay_ms=80)
    except Exception as exc:
        print(f"WARNING: Article {article_id} fetch failed: {exc}", file=sys.stderr)
        return {}
    detail = {
        "lat": "", "lon": "", "publicAddress": "", "roomCnt": "",
        "bathroomCnt": "", "approvalDate": "", "writerType": "", "agencyName": "",
        "publishedAt": "", "updatedAt": "", "direction": "", "parking": "",
        "elevator": "", "petAllowed": "", "loanAvailable": "", "moveIn": "",
        "maintenanceDetail": "", "maintenanceBasis": "", "maintenanceItems": "",
        "buildingUse": "", "description": "", "options": "",
    }
    coord_ref = re.search(r'originalId\\":\\"' + re.escape(article_id) + r'\\".*?publicCoordinate\\":\{\\"__ref\\":\\"([^\\"]+)', text)
    if coord_ref:
        coord = re.search(re.escape(coord_ref.group(1)) + r'\\":\{\\"__id\\":\\"[^\\"]+\\",\\"__typename\\":\\"Coordinate\\",\\"lat\\":\\"([^\\"]+)\\",\\"lon\\":\\"([^\\"]+)', text)
        if coord:
            detail["lat"], detail["lon"] = coord.group(1), coord.group(2)
    store = extract_daangn_relay_store(text)
    article = find_daangn_article_node(store, article_id)
    if article:
        coord = daangn_ref_node(store, article.get("publicCoordinate"))
        detail.update({
            "lat": to_text(coord.get("lat", "")),
            "lon": to_text(coord.get("lon", "")),
            "publicAddress": to_text(article.get("publicAddress", "")),
            "roomCnt": to_text(article.get("roomCnt", "")),
            "bathroomCnt": to_text(article.get("bathroomCnt", "")),
            "approvalDate": to_text(article.get("buildingApprovalDate", "")),
            "writerType": to_text(article.get("writerTypeV2", "")),
            "publishedAt": to_text(article.get("publishedAt", "")),
            "updatedAt": to_text(article.get("updatedAt", "")),
            "description": to_text(article.get("content", "")).strip(),
        })
        facs = [fac for fac in DAANGN_FACILITY_KEYWORDS if fac in detail["description"]]
        if facs:
            detail["options"] = "; ".join(facs)

    # Description body: the page may inline multiple articles' content (related
    # listings, recommendations). Anchor to THIS article's originalId and grab
    # the first `content` field that follows.
    #
    # Two subtleties:
    # - Lazy quantifier (`{n,m}?`) so we stop at the FIRST escaped quote that
    #   closes the value — the greedy form happily ate past the closing `\"`
    #   and grabbed the next field (`","publishedAt":"..."`).
    # - The string is double-escaped in the SSR payload. One pass of
    #   `json.loads('"' + raw + '"')` unescapes the outer layer (turning
    #   raw `\\n` → literal `\n`); a second targeted pass collapses any
    #   inner-layer escapes that remain. Doing both keeps the body readable
    #   regardless of which Daangn template emitted it.
    oid_match = None if detail["description"] else re.search(r'originalId\\":\\"' + re.escape(article_id) + r'\\"', text)
    if oid_match:
        window = text[oid_match.start(): oid_match.start() + 12000]
        cm = re.search(r'content\\":\\"((?:[^"\\]|\\.){10,5000}?)\\"', window)
        if cm:
            raw = cm.group(1)
            try:
                desc = json.loads('"' + raw + '"')
            except Exception:
                desc = raw
            # Collapse the second escape layer if it's still present
            # (real newlines stay real; literal backslash-n becomes newline).
            desc = desc.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            detail["description"] = desc.strip()
            # Daangn's structured "시설 정보" grid is rendered client-side from
            # a separate fetch we don't see here. Recover most of the signal by
            # scanning the description body for known facility tokens — agents
            # usually repeat them in the body.
            facs = [fac for fac in DAANGN_FACILITY_KEYWORDS if fac in desc]
            if facs:
                detail["options"] = "; ".join(facs)
    if not detail["agencyName"] and detail.get("writerType") != "DIRECT_USER":
        api_article = get_daangn_graphql_article_detail(session, article_id, text)
        if api_article:
            apply_daangn_graphql_article_detail(detail, api_article)
    body = detail["description"]
    if body:
        if text_has_any(body, ["주차 가능", "주차가능", "주차 가능합니다", "주차공간"]):
            detail["parking"] = "가능"
        elif text_has_any(body, ["주차 불가", "주차불가", "주차 안"]):
            detail["parking"] = "불가능"
        if text_has_any(body, ["엘리베이터", "엘베"]):
            detail["elevator"] = "있음"
        if text_has_any(body, ["반려동물 불가", "반려동물 안", "애완동물 불가", "애완동물 안"]):
            detail["petAllowed"] = "불가능"
        elif text_has_any(body, ["반려동물 가능", "반려동물가능", "애완동물 가능"]):
            detail["petAllowed"] = "가능"
        if text_has_any(body, ["대출 가능", "대출가능"]):
            detail["loanAvailable"] = "가능"
        elif text_has_any(body, ["대출 불가", "대출불가"]):
            detail["loanAvailable"] = "불가능"
        for direction in ["남향", "남동향", "남서향", "동향", "서향", "북향", "북동향", "북서향"]:
            if direction in body:
                detail["direction"] = direction
                break
        if text_has_any(body, ["공동주택"]):
            detail["buildingUse"] = "공동주택"
        elif text_has_any(body, ["단독주택"]):
            detail["buildingUse"] = "단독주택"
        elif text_has_any(body, ["제2근생", "제2종근린생활시설"]):
            detail["buildingUse"] = "제2종근린생활시설"
        maint = re.search(r"관리비\s*[:：]?\s*([0-9.,]+\s*만?원)", body)
        if maint:
            detail["maintenanceDetail"] = maint.group(0)
    meta = ""
    m1 = re.search(r'name="description"\s+content="([^"]+)"', text)
    m2 = re.search(r'content="([^"]+)"\s+name="description"', text)
    if m1:
        meta = html.unescape(m1.group(1))
    elif m2:
        meta = html.unescape(m2.group(1))
    parts = meta.split("\u2014", 1)
    if not detail["agencyName"] and len(parts) == 2:
        after = parts[1].strip()
        phone = re.search(r"\s[0-9]{2,3}-[0-9]", after)
        candidate = after[: phone.start()] if phone else after[:35]
        candidate = re.sub(r"^[\W]+|[\W]+$", "", candidate.strip()).strip()
        if 2 <= len(candidate) <= 30 and re.search(r"부동산|공인중개|중개사|사무소", candidate):
            detail["agencyName"] = candidate
    return detail


def bbox_ok(lat: Any, lon: Any, args: argparse.Namespace) -> bool:
    """True iff (lat, lon) falls inside the bbox declared on ``args``.

    Two pass-through cases:
    - Bbox is the "no-op" sentinel (all four edges 0 — what legacy callers
      use to mean "skip filtering"). Hemisphere users with negative coords
      will never hit this exactly, but the equator/Greenwich corner is also
      not a realistic centre for this app.
    - The record itself has no coordinates yet (unenriched). Better to let
      it through than to silently drop it; a downstream enrichment may fill
      the coords later.
    """
    if args.min_lat == args.max_lat == args.min_lng == args.max_lng == 0:
        return True
    if lat in (None, "") or lon in (None, ""):
        return True
    try:
        return args.min_lat <= float(lat) <= args.max_lat and args.min_lng <= float(lon) <= args.max_lng
    except Exception:
        return True


def crawl_naver(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Python Playwright is required for Naver crawling. Install with: python -m pip install playwright && python -m playwright install chromium") from exc
    asyncio.run(crawl_naver_async(args, async_playwright))


def _retry_after_seconds(value: str | None, default_s: float) -> float:
    if not value:
        return max(0.0, default_s)
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return max(0.0, default_s)


def _new_naver_rate_stats() -> dict[str, Any]:
    return {
        "requests": Counter(),
        "http_errors": Counter(),
        "rate_limit_events": [],
        "consecutive_rate_limit": 0,
        "max_consecutive_rate_limit": 0,
        "cooldowns": 0,
    }


def _naver_record_response(stats: dict[str, Any] | None, phase: str, status: int, item: str = "") -> None:
    if stats is None:
        return
    stats["requests"][phase] += 1
    if status >= 400:
        stats["http_errors"][(phase, status)] += 1
    if status == NAVER_RATE_LIMIT_STATUS:
        stats["consecutive_rate_limit"] += 1
        stats["max_consecutive_rate_limit"] = max(
            stats["max_consecutive_rate_limit"],
            stats["consecutive_rate_limit"],
        )
        events = stats["rate_limit_events"]
        if len(events) < 12:
            events.append({"phase": phase, "item": item})
    else:
        stats["consecutive_rate_limit"] = 0


def _format_naver_counter(counter: Counter) -> str:
    if not counter:
        return "{}"
    parts = []
    for key, value in sorted(counter.items(), key=lambda kv: str(kv[0])):
        if isinstance(key, tuple):
            parts.append(f"{key[0]}:{key[1]}={value}")
        else:
            parts.append(f"{key}={value}")
    return "{" + ", ".join(parts) + "}"


def _naver_rate_policy(stats: dict[str, Any]) -> tuple[str, str]:
    requests = sum(stats["requests"].values())
    rate_limited = sum(
        count
        for (phase, status), count in stats["http_errors"].items()
        if status == NAVER_RATE_LIMIT_STATUS
    )
    max_streak = int(stats["max_consecutive_rate_limit"] or 0)
    if rate_limited == 0:
        return "normal", "429 not observed during this crawl"
    ratio = rate_limited / max(1, requests)
    if max_streak >= NAVER_RATE_POLICY_STREAK_THRESHOLD:
        return "cool_down_on_429", f"consecutive_429={max_streak}"
    if ratio >= 0.05:
        return "slow_request_rate", f"429_ratio={ratio:.1%}"
    return "batch_retry_later", f"429_ratio={ratio:.1%} max_streak={max_streak}"


def _log_naver_rate_summary(stats: dict[str, Any]) -> None:
    policy, reason = _naver_rate_policy(stats)
    rate_limited = sum(
        count
        for (_phase, status), count in stats["http_errors"].items()
        if status == NAVER_RATE_LIMIT_STATUS
    )
    print(
        "[naver-rate] summary "
        f"requests={_format_naver_counter(stats['requests'])} "
        f"http_errors={_format_naver_counter(stats['http_errors'])} "
        f"429={rate_limited} max_consecutive_429={stats['max_consecutive_rate_limit']} "
        f"cooldowns={stats['cooldowns']} policy={policy} reason={reason}",
        flush=True,
    )
    events = stats["rate_limit_events"]
    if events:
        print(f"[naver-rate] first_429_events={events}", flush=True)


def _naver_retry_wait_seconds(response: Any, attempt: int, default_s: float) -> float:
    header = None
    try:
        headers = response.headers
        header = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        header = None
    fallback = default_s if response.status == NAVER_RATE_LIMIT_STATUS else 1.5 * (attempt + 1)
    return _retry_after_seconds(header, fallback)


async def _naver_wait_for_retry(
    response: Any,
    *,
    phase: str,
    item: str,
    attempt: int,
    retries: int,
    default_cooldown_s: float,
    stats: dict[str, Any] | None,
) -> bool:
    if response.status not in NAVER_TRANSIENT_STATUS_CODES or attempt >= retries:
        return False
    wait_s = _naver_retry_wait_seconds(response, attempt, default_cooldown_s)
    if stats is not None:
        stats["cooldowns"] += 1
    print(
        f"[naver-rate] phase={phase} item={item} status={response.status} "
        f"attempt={attempt + 1}/{retries + 1} wait={wait_s:.1f}s",
        flush=True,
    )
    if wait_s:
        await asyncio.sleep(wait_s)
    return True


async def _paginate_naver_cortarno(
    context: Any,
    template_url: str,
    cortarno: str,
    headers: dict[str, str] | None,
    args: argparse.Namespace,
    stats: dict[str, Any] | None = None,
) -> list[Any]:
    """Walk pages 1..max_pages for an explicit cortarNo via direct list-API calls.

    Used when the env-driven cortarNo list (RENTMAP_NAVER_CORTARNOS) contains a
    dong the auto-grid never resolved to. Builds the URL by swapping the
    cortarNo on a captured template, so all other query params (filters,
    pageSize, tag, etc.) match what the browser would have sent.
    """
    payloads: list[Any] = []
    cleaned = clean_headers(headers)
    url_with_cn = set_query_param(template_url, "cortarNo", cortarno)
    for pg in range(1, args.max_pages + 1):
        next_url = set_query_param(url_with_cn, "page", str(pg))
        response = None
        for attempt in range(NAVER_LIST_RETRIES + 1):
            response = await context.request.get(next_url, headers=cleaned, timeout=30000)
            _naver_record_response(stats, "direct-list", response.status, f"cortarNo={cortarno} page={pg}")
            if response.ok:
                break
            if await _naver_wait_for_retry(
                response,
                phase="direct-list",
                item=f"cortarNo={cortarno} page={pg}",
                attempt=attempt,
                retries=NAVER_LIST_RETRIES,
                default_cooldown_s=NAVER_LIST_RATE_LIMIT_COOLDOWN_SECONDS,
                stats=stats,
            ):
                continue
            break
        if response is None:
            break
        if not response.ok:
            print(f"  [direct cortarNo={cortarno}] page {pg}: HTTP {response.status}", file=sys.stderr)
            break
        payload = await response.json()
        payloads.append(payload)
        if not payload.get("isMoreData"):
            break
        await asyncio.sleep(NAVER_PAGE_DELAY_MS / 1000)
    return payloads


async def crawl_naver_async(args: argparse.Namespace, async_playwright: Any) -> None:
    started = time.monotonic()
    urls = args.urls or default_naver_urls()
    explicit_cortarnos = default_naver_cortarnos()
    _log_crawl_start(
        "naver",
        args,
        extra=(
            f"source=naver-land urls={len(urls)} explicit_cortarnos={len(explicit_cortarnos)} "
            f"max_pages={args.max_pages} detail_fetch={not getattr(args, 'skip_detail', False)}"
        ),
    )
    chrome = find_chrome(args.chrome_path)
    async with async_playwright() as p:
        launch_options: dict[str, Any] = {
            "headless": not args.headed,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if chrome:
            launch_options["executable_path"] = chrome
        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context(locale="ko-KR", user_agent=UA, ignore_https_errors=True)
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        article_headers: dict[str, str] | None = None
        naver_rate_stats = _new_naver_rate_stats()
        # Captured list-API URL from the first successful navigation — used as
        # a template for direct cortarNo paginate calls (we only swap cortarNo).
        first_list_url: str | None = None

        async def on_request(request: Any) -> None:
            nonlocal article_headers, first_list_url
            if "/api/articles?" in request.url:
                try:
                    article_headers = await request.all_headers()
                    if first_list_url is None:
                        first_list_url = request.url
                except Exception:
                    pass

        page.on("request", on_request)
        try:
            if not args.skip_home:
                await page.goto("https://new.land.naver.com/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1200)
            seen: set[str] = set()
            # Naver's list API is cortarNo-scoped (dong-level), not viewport-scoped,
            # so multiple ms= tiles often resolve to the same cortarNo. We track
            # which cortarNos have already been fully paginated and skip pagination
            # for subsequent duplicates (page 1 still arrives from the navigation
            # but the article dedup below filters it out).
            seen_cortarnos: set[str] = set()
            records: list[dict[str, Any]] = []
            raw_payloads: list[Any] = []
            for idx, url in enumerate(urls, 1):
                print(f"[crawl:naver] url_progress={idx}/{len(urls)} url={url}", flush=True)
                one_records, payloads, cortarno = await crawl_naver_one(
                    page,
                    context,
                    url,
                    article_headers,
                    args,
                    seen_cortarnos,
                    naver_rate_stats,
                )
                raw_payloads.extend(payloads)
                new_count = 0
                for record in one_records:
                    key = to_text(record.get("listing_no"))
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    records.append(record)
                    new_count += 1
                print(f"[crawl:naver] url_result in_bbox={len(one_records)} new_after_dedup={new_count} cortarNo={cortarno or '?'}", flush=True)
            print(f"[crawl:naver] grid_pass unique_articles={len(records)} cortarNos={len(seen_cortarnos)} payload_pages={len(raw_payloads)}", flush=True)

            # Coverage backstop: paginate every cortarNo from RENTMAP_NAVER_CORTARNOS
            # that the grid didn't already cover. Defends against Naver's
            # non-deterministic ms= → cortarNo mapping (the same tile can flip
            # between dongs across requests, so grid-only coverage can silently
            # drop entire dongs of listings).
            template = first_list_url
            missing_cortarnos = [cn for cn in explicit_cortarnos if cn not in seen_cortarnos]
            if missing_cortarnos and template and article_headers:
                center = get_map_center(urls[0]) if urls else {"latitude": 0, "longitude": 0, "zoom": "16"}
                print(f"[crawl:naver] direct_pass missing_cortarnos={len(missing_cortarnos)} cortarNos={missing_cortarnos}", flush=True)
                for cn in missing_cortarnos:
                    payloads = await _paginate_naver_cortarno(context, template, cn, article_headers, args, naver_rate_stats)
                    raw_payloads.extend(payloads)
                    seen_cortarnos.add(cn)
                    new_count = 0
                    for payload in payloads:
                        for article in payload.get("articleList") or []:
                            record = normalize_naver_article(article, template, center)
                            if not bbox_ok(record.get("latitude"), record.get("longitude"), args):
                                continue
                            key = to_text(record.get("listing_no"))
                            if key and key in seen:
                                continue
                            if key:
                                seen.add(key)
                            records.append(record)
                            new_count += 1
                    print(f"[crawl:naver] direct_cortarNo={cn} pages={len(payloads)} new_in_bbox={new_count}", flush=True)
            elif missing_cortarnos and not template:
                print(f"[naver] {len(missing_cortarnos)} explicit cortarNos requested but no list URL captured; skipping direct pass", file=sys.stderr)

            print(f"[crawl:naver] list_total unique_articles={len(records)} cortarNos={len(seen_cortarnos)} payload_pages={len(raw_payloads)}", flush=True)

            # Detail-API enrichment: list API never returns the exact address or
            # room/parking/move-in/description fields. We call /api/articles/{no}
            # for every bbox article and merge the extra fields in place. Reuses
            # the session cookies captured by the request listener above.
            skip_detail = getattr(args, "skip_detail", False)
            if not skip_detail and records:
                detail_source = article_headers
                if detail_source is None:
                    print("[naver-detail] no captured headers; skipping detail enrichment", file=sys.stderr)
                else:
                    print(f"[crawl:naver] fetching_details articles={len(records)}", flush=True)
                    detail_ok = 0
                    for i, record in enumerate(records, 1):
                        article_no = to_text(record.get("listing_no"))
                        if not article_no:
                            continue
                        if i % NAVER_PROGRESS_EVERY == 0:
                            print(f"[crawl:naver] detail_progress={i}/{len(records)} enriched={detail_ok}", flush=True)
                        detail = await fetch_naver_article_detail(
                            context,
                            article_no,
                            detail_source,
                            stats=naver_rate_stats,
                            position=i,
                            total=len(records),
                        )
                        if detail:
                            enrich_from_naver_detail(record, detail)
                            detail_ok += 1
                    print(f"[crawl:naver] detail_done={len(records)}/{len(records)} enriched={detail_ok}", flush=True)
            elif skip_detail:
                print("[naver-detail] --skip-detail set; leaving list-API placeholders in place")
            _log_naver_rate_summary(naver_rate_stats)

            records.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["total_monthly_manwon"])))
            write_csv(Path(args.output_csv), records, NAVER_COLUMNS)
            _reconcile_after_crawl("naver_land", records, "naver")
            if args.raw_json:
                Path(args.raw_json).parent.mkdir(parents=True, exist_ok=True)
                Path(args.raw_json).write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
            # Dump discovered cortarNos so region_runner can UNION-merge them
            # back into the region row. Written even when empty so the caller
            # can distinguish "crawl skipped writing" from "crawl found nothing".
            cortarnos_out = getattr(args, "cortarnos_out", "") or ""
            if cortarnos_out:
                try:
                    Path(cortarnos_out).parent.mkdir(parents=True, exist_ok=True)
                    Path(cortarnos_out).write_text(
                        json.dumps(sorted(seen_cortarnos)), encoding="utf-8"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[crawl:naver] cortarnos-out write failed: {exc}", file=sys.stderr)
            _log_crawl_done("naver", len(records), args.output_csv, time.monotonic() - started)
        finally:
            await browser.close()


def find_chrome(explicit: str = "") -> str | None:
    candidates = [
        explicit,
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    return None


async def crawl_naver_one(
    page: Any,
    context: Any,
    target_url: str,
    article_headers: dict[str, str] | None,
    args: argparse.Namespace,
    seen_cortarnos: set[str],
    stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[Any], str]:
    center = get_map_center(target_url)
    first_response = None
    for attempt in range(NAVER_LIST_RETRIES + 1):
        async with page.expect_response(lambda r: "/api/articles?" in r.url, timeout=45000) as response_info:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        first_response = await response_info.value
        _naver_record_response(stats, "grid-list", first_response.status, target_url)
        if first_response.ok:
            break
        if await _naver_wait_for_retry(
            first_response,
            phase="grid-list",
            item=target_url,
            attempt=attempt,
            retries=NAVER_LIST_RETRIES,
            default_cooldown_s=NAVER_LIST_RATE_LIMIT_COOLDOWN_SECONDS,
            stats=stats,
        ):
            continue
        raise RuntimeError(f"Naver article API request failed: {first_response.status}")
    if first_response is None:
        raise RuntimeError("Naver article API request failed: no response captured")
    first_url = first_response.url
    request_headers = await first_response.request.all_headers()
    print(f"  captured: {first_url}")
    try:
        first_json = await first_response.json()
    except Exception:
        response = await context.request.get(first_url, headers=clean_headers(request_headers or article_headers), timeout=30000)
        _naver_record_response(stats, "grid-list-json-retry", response.status, first_url)
        if not response.ok:
            raise RuntimeError(f"Naver article API request failed: {response.status}")
        first_json = await response.json()
    payloads = [first_json]
    # Extract cortarNo from the captured first_url. If we've already paginated
    # this cortarNo from a previous tile, skip pages 2..N — page 1 was already
    # delivered by the navigation above and all articles will be filtered out
    # by the listing_no dedup in the caller.
    qs = parse_qs(urlparse(first_url).query)
    cortarno = (qs.get("cortarNo") or [""])[0]
    if cortarno and cortarno in seen_cortarnos:
        print(f"  cortarNo {cortarno} already paginated — skipping pages 2..{args.max_pages}")
    else:
        if cortarno:
            seen_cortarnos.add(cortarno)
        page_no = 2
        while page_no <= args.max_pages and first_json.get("isMoreData"):
            next_url = set_query_param(first_url, "page", str(page_no))
            response = None
            for attempt in range(NAVER_LIST_RETRIES + 1):
                response = await context.request.get(next_url, headers=clean_headers(request_headers or article_headers), timeout=30000)
                _naver_record_response(stats, "grid-list-page", response.status, f"cortarNo={cortarno} page={page_no}")
                if response.ok:
                    break
                if await _naver_wait_for_retry(
                    response,
                    phase="grid-list-page",
                    item=f"cortarNo={cortarno} page={page_no}",
                    attempt=attempt,
                    retries=NAVER_LIST_RETRIES,
                    default_cooldown_s=NAVER_LIST_RATE_LIMIT_COOLDOWN_SECONDS,
                    stats=stats,
                ):
                    continue
                break
            if response is None:
                break
            if not response.ok:
                break
            payload = await response.json()
            payloads.append(payload)
            first_json = payload
            page_no += 1
            await page.wait_for_timeout(NAVER_PAGE_DELAY_MS)
    records = []
    for payload in payloads:
        for article in payload.get("articleList") or []:
            record = normalize_naver_article(article, target_url, center)
            if bbox_ok(record.get("latitude"), record.get("longitude"), args):
                records.append(record)
    return records, payloads, cortarno


def clean_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    blocked = {"accept-encoding", "connection", "content-length", "cookie", "host"}
    return {k: v for k, v in headers.items() if not k.startswith(":") and k.lower() not in blocked}


def set_query_param(url: str, key: str, value: str) -> str:
    """Replace ``key`` in ``url``'s query string, preserving the rest.

    ``keep_blank_values=True`` is critical: Naver's list-API URL ends with
    parameters like ``&articleState`` that have no value. The default
    ``parse_qs`` behaviour silently drops those, so pages 2..N would lose
    them after round-tripping through this function — works today because
    Naver tolerates the omission, but defending against the day it stops.
    """
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query[key] = [value]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def decode_base62(value: str) -> int | None:
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if not value or not re.match(r"^[0-9a-zA-Z]+$", value):
        return None
    number = 0
    for char in value:
        idx = chars.find(char)
        if idx < 0:
            return None
        number = number * 62 + idx
    return number


def decode_coord(value: str) -> float | None:
    decoded = decode_base62(value)
    return None if decoded is None else (decoded - 2000000000) / 10000000


def encode_coord(value: float) -> str:
    """Encode a lat/lng float to Naver Land's base62 ms= format (inverse of decode_coord)."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    number = int(round(value * 10000000)) + 2000000000
    if number <= 0:
        return "0"
    result = ""
    while number > 0:
        number, rem = divmod(number, 62)
        result = chars[rem] + result
    return result or "0"


def gen_naver_grid_urls(center_lat: float, center_lng: float, radius_km: float) -> list[str]:
    """Generate a grid of Naver Land ms= viewport URLs covering the given radius.

    Each tile at zoom 16 covers roughly 1.5 km. Tiles are spaced NAVER_TILE_STEP_KM
    apart (with ~50% overlap) so no area is missed between tile edges.
    """
    step_lat = NAVER_TILE_STEP_KM / 111.0
    step_lng = NAVER_TILE_STEP_KM / (111.0 * max(math.cos(math.radians(center_lat)), COS_LAT_FLOOR))
    n = max(1, math.ceil(radius_km / NAVER_TILE_STEP_KM))
    urls: list[str] = []
    seen: set[str] = set()
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            # Skip tiles whose centres are more than one step beyond the radius
            dist_km = math.sqrt((i * NAVER_TILE_STEP_KM) ** 2 + (j * NAVER_TILE_STEP_KM) ** 2)
            if dist_km > radius_km + NAVER_TILE_STEP_KM:
                continue
            lat = center_lat + i * step_lat
            lng = center_lng + j * step_lng
            ms = f"{encode_coord(lat)},{encode_coord(lng)},{NAVER_ZOOM}"
            if ms not in seen:
                seen.add(ms)
                urls.append(f"https://new.land.naver.com/rooms?ms={ms}&{NAVER_DEFAULT_PARAMS}")
    return urls


def default_naver_urls() -> list[str]:
    """Return Naver crawl URLs.

    Priority:
    1. RENTMAP_NAVER_URLS env var (comma-separated list of full URLs).
    2. Auto-generated grid from RENTMAP_CENTER_LAT/LNG + RENTMAP_RADIUS_KM.
    """
    raw = os.environ.get("RENTMAP_NAVER_URLS", "").strip()
    if raw:
        # Use "|" as separator — Naver ms= URLs contain commas (ms=lat,lng,zoom)
        # so comma-splitting would corrupt them.
        urls = [u.strip() for u in raw.split("|") if u.strip()]
        if urls:
            print(f"[naver] using {len(urls)} URLs from RENTMAP_NAVER_URLS", file=sys.stderr)
            return urls
    center_lat = env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT)
    center_lng = env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG)
    radius_km = env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM)
    urls = gen_naver_grid_urls(center_lat, center_lng, radius_km)
    print(f"[naver] generated {len(urls)} grid URLs (center={center_lat},{center_lng} r={radius_km}km)", file=sys.stderr)
    return urls


def default_naver_cortarnos() -> list[str]:
    """Explicit cortarNos (dong-level admin codes) the crawler must paginate.

    Naver's ``ms=`` → ``cortarNo`` resolution is non-deterministic: the same
    viewport URL can resolve to different dong codes across requests (we've
    observed a single tile flipping between 4111710200 원천동 and 4113510300
    분당). The auto-generated coordinate grid alone therefore can't guarantee
    coverage of any particular dong — listings in skipped dongs vanish from
    the CSV silently.

    The mitigation is to feed the crawler the cortarNos we KNOW we want
    covered (find them by visiting new.land.naver.com, navigating the map,
    and reading the ``cortarNo=`` digits in the Network tab's request URL).
    The crawler paginates every cortarNo in this list using the headers it
    captured from the first grid tile. Returns an empty list when the env
    var is unset — grid is then the sole coverage source (legacy behaviour).
    """
    raw = os.environ.get("RENTMAP_NAVER_CORTARNOS", "").strip()
    if not raw:
        return []
    cns = [x.strip() for x in raw.split(",") if x.strip()]
    if cns:
        print(f"[naver] forcing pagination of {len(cns)} explicit cortarNos from RENTMAP_NAVER_CORTARNOS", file=sys.stderr)
    return cns


def default_daangn_region_ids() -> list[int]:
    """Return Daangn region IDs.

    Priority:
    1. RENTMAP_DAANGN_REGION_IDS env var (comma-separated integers).
    2. DEFAULT_DAANGN_REGION_IDS (Ajou University / Suwon Gwonseon-gu).

    To find region IDs for a different city: browse daangn.com/kr/realty/, navigate
    to the target neighbourhood and read the `in=x-XXXX` value from the URL.
    """
    raw = os.environ.get("RENTMAP_DAANGN_REGION_IDS", "").strip()
    if raw:
        try:
            ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if ids:
                print(f"[daangn] using {len(ids)} region IDs from RENTMAP_DAANGN_REGION_IDS", file=sys.stderr)
                return ids
        except ValueError as exc:
            print(f"[config] ignoring invalid RENTMAP_DAANGN_REGION_IDS: {exc}", file=sys.stderr)
    return list(DEFAULT_DAANGN_REGION_IDS)


def get_map_center(url: str) -> dict[str, Any]:
    qs = parse_qs(urlparse(url).query)
    ms = (qs.get("ms") or [""])[0].split(",")
    if len(ms) < 2:
        return {"latitude": 37.280, "longitude": 127.043, "zoom": "16"}
    return {"latitude": decode_coord(ms[0]) or 37.280, "longitude": decode_coord(ms[1]) or 127.043, "zoom": ms[2] if len(ms) > 2 else "16"}


def parse_manwon(text: str) -> dict[str, float] | None:
    match = re.search(r"(?:월세|단기임대)?(.+?)/([0-9,]+)", re.sub(r"\s+", "", text or ""))
    if not match:
        return None

    def amount(value: str) -> float:
        cleaned = value.replace(",", "")
        eok = re.search(r"([0-9.]+)억", cleaned)
        rest = re.sub(r"[^0-9.]", "", re.sub(r"[0-9.]+억", "", cleaned))
        return (float(eok.group(1)) * 10000 if eok else 0) + (float(rest) if rest else 0)

    return {"deposit": amount(match.group(1)), "rent": float(match.group(2).replace(",", ""))}


def parse_amount_manwon(value: Any) -> Any:
    text = re.sub(r"\s+", "", to_text(value)).replace(",", "")
    if not text:
        return ""
    eok = re.search(r"([0-9.]+)억", text)
    rest = re.sub(r"[^0-9.]", "", re.sub(r"[0-9.]+억", "", text))
    amount = (float(eok.group(1)) * 10000 if eok else 0) + (float(rest) if rest else 0)
    return amount if amount > 0 else ""


def normalize_naver_article(article: dict[str, Any], source_url: str, center: dict[str, Any]) -> dict[str, Any]:
    deposit_text = first(article, ["dealOrWarrantPrc", "priceText"])
    parsed = parse_manwon(f"{first(article, ['tradeTypeName'])}{deposit_text}/{first(article, ['rentPrc'])}") or {}
    rent = parsed.get("rent") or float_or_empty(str(first(article, ["rentPrc"])).replace(",", ""))
    maintenance_won = float_or_empty(first(article, ["monthlyManagementCost", "managementCost"])) or 0
    maintenance = round1(float(maintenance_won) / 10000) if maintenance_won else ""
    article_no = first(article, ["articleNo"])
    lat = first(article, ["latitude"], center["latitude"])
    lon = first(article, ["longitude"], center["longitude"])
    img = first(article, ["representativeImgUrl"])
    if img and to_text(img).startswith("/"):
        img = f"https://landthumb-phinf.pstatic.net{img}"
    # The list API never returns the actual jibun/road address (detailAddressYn=N
    # for almost every listing). Use the dong-level region as a sane placeholder;
    # the detail-API enrichment step will overwrite this with the exact address
    # (e.g. "경기도 수원시 영통구 원천동 90-15").
    region_parts = [to_text(first(article, [k])) for k in ("cityName", "divisionName", "sectionName")]
    region_addr = " ".join([p for p in region_parts if p])
    supply_area, exclusive_area = split_area_pair("/".join([to_text(x) for x in [first(article, ["supplySpace", "area1"]), first(article, ["exclusiveSpace", "area2"])] if x]))
    confirmed_at = to_iso_date(first(article, ["articleConfirmYmd", "confirmYmd"]))
    return {
        "source": "naver_land",
        "listing_no": article_no,
        "room_id": article_no,
        "url": f"https://new.land.naver.com/rooms?articleNo={article_no}" if article_no else source_url,
        "agency": first(article, ["realtorName", "cpName"]),
        "agent_name": "",
        "agent_phone": "",
        "region": first(article, ["cityName", "divisionName", "sectionName"]),
        "address": region_addr or first(article, ["articleName", "buildingName"]),
        "latitude": lat,
        "longitude": lon,
        "address_public_level": "naver_dong_level_until_detail_enrichment",
        "title": first(article, ["articleFeatureDesc", "articleName"]),
        "deposit_manwon": parsed.get("deposit") or parse_amount_manwon(deposit_text),
        "rent_manwon": rent,
        "maintenance_manwon": maintenance,
        "total_monthly_manwon": "" if rent == "" else round1(float(rent) + (float(maintenance) if maintenance != "" else 0)),
        "room_type": first(article, ["realEstateTypeName", "articleName"]),
        "room_count": "",
        "bathroom_count": "",
        "area_m2": "/".join([x for x in [supply_area, exclusive_area] if x]) or exclusive_area,
        "supply_area_m2": supply_area,
        "exclusive_area_m2": exclusive_area,
        "floor": " ".join([to_text(x) for x in [first(article, ["floorInfo"]), first(article, ["floorLayerName"])] if x]),
        "direction": first(article, ["direction"]),
        "room_structure": "",
        "duplex": "",
        "parking": "",
        "move_in": "",
        "approval_date": confirmed_at,
        "published_at": "",
        "confirmed_at": confirmed_at,
        "listing_age_text": days_ago_text(confirmed_at),
        "maintenance_detail": "",
        "maintenance_basis": "",
        "maintenance_items": "",
        "building_use": first(article, ["articleRealEstateTypeName"]),
        "description": "",
        "options": join_text_list([first(article, ["tagList"]), first(article, ["articleFeatureDesc"])]),
        "security_options": "",
        "image_1": img,
        "image_2": "",
        "crawl_note": "Captured from Naver Land article list API.",
    }


async def fetch_naver_article_detail(
    context: Any,
    article_no: str,
    headers: dict[str, str] | None,
    delay_ms: int = NAVER_DETAIL_DELAY_MS,
    retries: int = NAVER_DETAIL_RETRIES,
    stats: dict[str, Any] | None = None,
    position: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """Fetch one Naver Land article's detail-API payload, with light retry.

    Naver returns the full ``articleDetail``/``articleOneroom``/``articleFacility``/
    ``articleRealtor``/``articleSpace``/``articlePhotos`` tree at
    ``/api/articles/{articleNo}``. The request reuses the captured browser-session
    headers (same cookies as the list-API call).
    """
    url = f"https://new.land.naver.com/api/articles/{article_no}"
    cleaned = clean_headers(headers)
    item = f"{article_no} {position}/{total}" if position and total else article_no
    for attempt in range(retries + 1):
        try:
            response = await context.request.get(url, headers=cleaned, timeout=20000)
            _naver_record_response(stats, "detail", response.status, item)
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            if response.ok:
                return await response.json()
            if await _naver_wait_for_retry(
                response,
                phase="detail",
                item=item,
                attempt=attempt,
                retries=retries,
                default_cooldown_s=NAVER_DETAIL_RATE_LIMIT_COOLDOWN_SECONDS,
                stats=stats,
            ):
                continue
            print(f"  [naver-detail] {article_no}: HTTP {response.status}", file=sys.stderr)
            return {}
        except Exception as exc:
            if attempt < retries:
                await asyncio.sleep(1)
                continue
            print(f"  [naver-detail] {article_no}: {exc}", file=sys.stderr)
            return {}
    return {}


def _is_positive_float(value: Any) -> bool:
    """True iff ``value`` coerces to a non-zero float. Handles int/float/str
    uniformly so a stringified ``'0.0'`` is treated the same as a numeric 0.
    """
    if value in (None, ""):
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return False


def enrich_from_naver_detail(record: dict[str, Any], detail: dict[str, Any]) -> None:
    """Merge ``/api/articles/{articleNo}`` fields into a list-API record in place.

    Overwrites placeholders (region-only address, blank phone/parking/move-in/etc.)
    with the real values from the detail payload. Safe to call with an empty
    ``detail`` dict — the record is left untouched in that case.

    The ``crawl_note`` audit string is only rewritten when at least one field
    actually changed, so a 200-OK response with empty inner blocks doesn't
    falsely advertise enrichment in the CSV.
    """
    if not detail:
        return
    ad = detail.get("articleDetail") or {}
    ao = detail.get("articleOneroom") or {}
    af = detail.get("articleFacility") or {}
    ar = detail.get("articleRealtor") or {}
    asp = detail.get("articleSpace") or {}
    photos = detail.get("articlePhotos") or []

    touched = False

    def _set(key: str, value: Any) -> None:
        nonlocal touched
        record[key] = value
        touched = True

    # Real address: ``exposureAddress`` is the jibun shown to logged-out users
    # (e.g. "경기도 수원시 영통구 원천동 90-15"); fall back to the dong region
    # if for some reason it's empty.
    exposure_addr = first(ad, ["exposureAddress"])
    if exposure_addr:
        _set("address", exposure_addr)
        record["address_public_level"] = "naver_exposure_address_from_detail_api"

    # Agency contact (overrides empty list-API values)
    rep_name = first(ar, ["representativeName"])
    if rep_name:
        _set("agent_name", rep_name)
    cell = first(ar, ["cellPhoneNo"])
    tel = first(ar, ["representativeTelNo"])
    phone = normalize_phone(cell or tel)
    if phone:
        _set("agent_phone", phone)

    confirm_iso = to_iso_date(first(ad, ["articleConfirmYmd", "confirmYmd"]))
    if confirm_iso:
        _set("confirmed_at", confirm_iso)
        _set("listing_age_text", days_ago_text(confirm_iso))

    # Room / bathroom counts
    room_cnt = first(ad, ["roomCount"])
    if room_cnt not in (None, ""):
        _set("room_count", room_cnt)
    bath_cnt = first(ad, ["bathroomCount"])
    if bath_cnt not in (None, ""):
        _set("bathroom_count", bath_cnt)

    # Room structure (분리형 / 일자형 / etc.) from articleOneroom
    room_structure = first(ao, ["roomType"])
    if room_structure:
        _set("room_structure", room_structure)

    # Parking
    parking_yn = to_text(first(ad, ["parkingPossibleYN"]))
    parking_cnt = first(ad, ["parkingCount"])
    if parking_yn == "Y":
        _set("parking", f"가능 ({parking_cnt}대)" if parking_cnt not in (None, "", 0) else "가능")
    elif parking_yn == "N":
        _set("parking", "불가")

    # Move-in: prefer the actual date when present, otherwise the human label
    move_in_name = first(ad, ["moveInTypeName"])
    move_in_ymd = to_text(first(ad, ["moveInPossibleYmd"]))
    move_in_iso = to_iso_date(move_in_ymd) if move_in_ymd and move_in_ymd != "NOW" else ""
    if move_in_iso:
        _set("move_in", move_in_iso)
    elif move_in_name:
        _set("move_in", move_in_name)

    # Duplex / floor structure (e.g. 단층 / 복층)
    duplex_yn = to_text(first(ad, ["duplexYN"]))
    floor_layer = first(ad, ["floorLayerName"])
    if floor_layer:
        _set("duplex", floor_layer)
    elif duplex_yn:
        _set("duplex", "복층" if duplex_yn == "Y" else "단층")

    # Approval date — articleFacility has the precise YYYYMMDD; better than the
    # confirmYmd we already pulled from the list API. NOTE: this is the
    # *building*'s use-approval date (construction-era), semantically different
    # from the list API's articleConfirmYmd (last-verified date). The column
    # name is intentionally generic; if you need both, split the schema.
    aprv_iso = to_iso_date(first(af, ["buildingUseAprvYmd"]))
    if aprv_iso:
        _set("approval_date", aprv_iso)

    building_use = first_deep(detail, ["buildingUseName", "buildingUse", "principalUse", "principalUseName"])
    if building_use:
        _set("building_use", join_nested_text(building_use))

    # Management fee: Naver's list API often omits this for one-room articles
    # even when the detail table shows it. Detail payload field names have
    # varied over time, so look across the full response before giving up.
    maintenance_raw = first_deep(detail, [
        "monthlyManagementCost", "managementCost", "maintenanceCost",
        "monthlyManageCost", "manageCost", "managementFee",
    ])
    maintenance_value = parse_manwon_from_text(maintenance_raw)
    if maintenance_value is not None:
        _set("maintenance_manwon", maintenance_value)
        rent_value = float_or_empty(record.get("rent_manwon"))
        if rent_value != "":
            _set("total_monthly_manwon", round1(float(rent_value) + float(maintenance_value)))
    maintenance_detail = first_deep(detail, [
        "managementCostInfo", "managementFeeInfo", "maintenanceCostInfo",
        "monthlyManagementCostInfo", "managementCostDetail", "maintenanceCostDetail",
    ])
    if maintenance_detail:
        _set("maintenance_detail", join_nested_text(maintenance_detail))
    maintenance_basis = first_deep(detail, [
        "managementCostBasis", "managementFeeBasis", "maintenanceCostBasis",
        "managementCostType", "maintenanceCostType", "managementFeeType",
    ])
    if maintenance_basis:
        _set("maintenance_basis", join_nested_text(maintenance_basis))
    maintenance_items = first_deep(detail, [
        "managementCostIncludeItemName", "maintenanceIncludeItemName",
        "managementCostIncludes", "maintenanceIncludes", "includeItems",
    ])
    if maintenance_items:
        _set("maintenance_items", join_nested_text(maintenance_items))

    # Description (full listing body)
    desc = first(ad, ["detailDescription"])
    if desc:
        _set("description", desc)

    # Options: union of lifeFacilities, airconFacilities, roomFacilities, tagList
    tag_list = ad.get("tagList") or []
    life_fac = af.get("lifeFacilities") or []
    aircon_fac = af.get("airconFacilities") or []
    room_fac = ao.get("roomFacilities") or []
    seen_opts: list[str] = []
    for lst in (tag_list, life_fac, aircon_fac, room_fac):
        for item in lst:
            label = to_text(item).strip()
            if label and label not in seen_opts:
                seen_opts.append(label)
    if seen_opts:
        _set("options", "; ".join(seen_opts))

    # Security options: union of securityFacilities (facility) + buildingFacilities (oneroom)
    sec_fac = af.get("securityFacilities") or []
    bld_fac = ao.get("buildingFacilities") or []
    sec_seen: list[str] = []
    for lst in (sec_fac, bld_fac):
        for item in lst:
            label = to_text(item).strip()
            if label and label not in sec_seen:
                sec_seen.append(label)
    if sec_seen:
        _set("security_options", "; ".join(sec_seen))

    # Areas: articleSpace gives the canonical supply/exclusive sizes (㎡).
    # Coerce-to-float check catches stringified zeros (`"0.0"`) as well as
    # numeric ones.
    excl_space = asp.get("exclusiveSpace")
    supp_space = asp.get("supplySpace")
    space_parts = [to_text(s) for s in (supp_space, excl_space) if _is_positive_float(s)]
    if space_parts:
        _set("area_m2", "/".join(space_parts))
        if _is_positive_float(supp_space):
            _set("supply_area_m2", to_text(supp_space))
        if _is_positive_float(excl_space):
            _set("exclusive_area_m2", to_text(excl_space))

    # Photos: prefix relative imageSrc with the static thumbnail host. Both
    # slots fall back to whatever the list API already gave us when the detail
    # payload's imageSrc is empty — keeps behaviour symmetric.
    if photos:
        def _photo_url(p: dict[str, Any]) -> str:
            src = to_text(p.get("imageSrc", ""))
            return f"https://landthumb-phinf.pstatic.net{src}" if src.startswith("/") else src
        new_img1 = _photo_url(photos[0])
        if new_img1:
            _set("image_1", new_img1)
        if len(photos) > 1:
            new_img2 = _photo_url(photos[1])
            if new_img2:
                _set("image_2", new_img2)

    if touched:
        record["crawl_note"] = "Enriched from Naver Land /api/articles/{articleNo} detail API."


def float_or_empty(value: Any) -> Any:
    try:
        if value in (None, ""):
            return ""
        return float(value)
    except Exception:
        return ""


def _latest_csv(data_dir: Path, prefix: str, target_date: str) -> Path | None:
    """Return the dated CSV for target_date, or the most recent prior file
    matching `<prefix>_<YYYY-MM-DD>.csv` as fallback. Returns None if no
    candidate exists at all."""
    target = data_dir / f"{prefix}_{target_date}.csv"
    if target.exists():
        return target
    candidates = sorted(data_dir.glob(f"{prefix}_*.csv"))
    return candidates[-1] if candidates else None


def _reconcile_after_crawl(platform_code: str, rows: list[dict[str, Any]], label: str) -> None:
    """Hand a freshly-crawled record list to the DB reconcile engine.

    All four crawlers call this immediately after ``write_csv`` so the CSV
    remains the canonical "what we saw this run" snapshot **and** the DB
    accumulates the incremental price/detail history that powers webhooks
    and the sparkline API.

    Robustness contract: this MUST NOT throw. reconcile is best-effort —
    if Postgres is down, migrations aren't applied, or the module isn't
    importable, the crawl keeps producing CSVs as before. Errors are logged
    in a single line that's easy to grep for in the scheduler log.
    """
    try:
        from reconcile import reconcile_csv_rows_safely  # late import
    except ImportError as exc:
        print(f"[reconcile] {label}: skipped — reconcile module unavailable ({exc})", file=sys.stderr)
        return
    target_area = os.environ.get("RENTMAP_AREA_NAME") or None
    try:
        reconcile_csv_rows_safely(platform_code, rows, label=label, target_area=target_area)
    except Exception as exc:  # noqa: BLE001 — defensive
        print(f"[reconcile] {label}: outer guard caught {type(exc).__name__}: {exc}", file=sys.stderr)


def finalize_missing(args: argparse.Namespace) -> None:
    """Finalize the in-schedule missing retry queue for selected platforms."""
    try:
        from db import session
        from reconcile import finalize_missing_queue
    except ImportError as exc:
        raise RuntimeError(f"finalize-missing unavailable: {exc}") from exc
    finalized_at = datetime.now(timezone.utc)
    with session() as conn:
        count = finalize_missing_queue(
            conn,
            args.platform,
            finalized_at,
            dry_run_webhooks=args.dry_run_webhooks,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(
        f"[reconcile] finalize-missing platforms={','.join(args.platform)} "
        f"removed={count} dry_run={args.dry_run}",
        flush=True,
    )


def _read_db_missing_candidates(platform_codes: list[str]) -> list[dict[str, Any]]:
    from db import session  # type: ignore

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.code AS platform_code,
                l.platform_listing_id, l.source_url, l.current_status, l.miss_count,
                s.title, s.description, s.room_type_raw, s.address_raw,
                s.lat, s.lng,
                s.deposit_won, s.monthly_rent_won, s.maintenance_fee_won,
                s.expected_monthly_cost_won,
                s.supply_area_m2, s.exclusive_area_m2, s.area_raw,
                s.floor_raw, s.room_count, s.bathroom_count,
                s.direction, s.parking_raw, s.move_in_raw,
                s.approval_date, s.building_usage, s.structure_type,
                s.raw_normalized_json
            FROM listings l
            JOIN platforms p ON p.id = l.platform_id
            JOIN LATERAL (
                SELECT *
                FROM listing_snapshots
                WHERE listing_id = l.id
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
            ) s ON TRUE
            WHERE p.code = ANY(%s)
              AND l.current_status = 'missing'
            ORDER BY p.code, l.id
            """,
            (platform_codes,),
        )
        candidates: list[dict[str, Any]] = []
        for row in cur.fetchall():
            csv_row = _db_row_to_csv_shape(row)
            csv_row["_platform_code"] = row["platform_code"]
            csv_row["_miss_count"] = row["miss_count"]
            candidates.append(csv_row)
        return candidates


def _dabang_room_id(row: dict[str, Any]) -> str:
    url = str(row.get("url") or "")
    match = re.search(r"/room/([^/?#]+)", url)
    if match:
        return match.group(1)
    return str(row.get("room_id") or row.get("listing_no") or "").strip()


def _probe_dabang_missing(session: requests.Session, row: dict[str, Any]) -> bool | None:
    room_id = _dabang_room_id(row)
    if not room_id:
        return False
    headers = {
        "Accept": "application/json, text/plain, */*",
        "D-Api-Version": "3.0.1",
        "D-App-Version": "1",
        "D-Call-Type": "web",
        "csrf": "token",
        "Referer": "https://www.dabangapp.com/map/onetwo",
        "User-Agent": UA,
        "Origin": "https://www.dabangapp.com",
    }
    url = f"https://www.dabangapp.com/api/3/new-room/detail?room_id={quote(room_id)}&api_version=3.0.1&call_type=web&version=1"
    try:
        payload = request_json(session, url, headers=headers, timeout=20)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        return False if status in {400, 404, 410} else None
    except Exception:
        return None
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    return bool(first(result, ["room"], result))


def _probe_zigbang_missing(session: requests.Session, row: dict[str, Any]) -> bool | None:
    item_id = str(row.get("listing_no") or row.get("item_id") or "").strip()
    if not item_id:
        return False
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.zigbang.com",
        "Referer": "https://www.zigbang.com/",
    }
    try:
        payload = request_json(session, f"https://apis.zigbang.com/v3/items/{quote(item_id)}", headers=headers, timeout=20)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        return False if status in {400, 404, 410} else None
    except Exception:
        return None
    return bool(isinstance(payload, dict) and payload.get("item"))


def _probe_daangn_missing(session: requests.Session, row: dict[str, Any]) -> bool | None:
    article_id = str(row.get("listing_no") or "").strip()
    if not article_id:
        return False
    try:
        resp = session.get(
            f"https://realty.daangn.com/articles/{quote(article_id)}",
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=20,
        )
    except Exception:
        return None
    if resp.status_code in {404, 410}:
        return False
    if resp.status_code >= 400:
        return None
    resp.encoding = "utf-8"
    return article_id in resp.text


class ProbeRateLimited(RuntimeError):
    def __init__(self, platform: str, listing_no: str, status: int, retry_after_s: float | None = None):
        self.platform = platform
        self.listing_no = listing_no
        self.status = status
        self.retry_after_s = retry_after_s
        super().__init__(f"{platform}:{listing_no} rate limited with HTTP {status}")


def _probe_naver_missing(session: requests.Session, row: dict[str, Any]) -> bool | None:
    article_no = str(row.get("listing_no") or row.get("room_id") or "").strip()
    if not article_no:
        return False
    try:
        resp = session.get(
            f"https://new.land.naver.com/api/articles/{quote(article_no)}",
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://new.land.naver.com/rooms?articleNo={quote(article_no)}",
            },
            timeout=20,
        )
    except Exception as exc:
        print(f"[reconcile] retry-missing naver_land:{article_no} api_error={exc}", flush=True)
        return None
    if resp.status_code in {400, 404, 410}:
        return False
    if resp.status_code == NAVER_RATE_LIMIT_STATUS:
        retry_after = resp.headers.get("Retry-After")
        retry_after_s = _retry_after_seconds(retry_after, NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS) if retry_after else None
        cooldown_text = f"{retry_after_s:.1f}s" if retry_after_s is not None else "default"
        print(
            f"[reconcile] retry-missing naver_land:{article_no} "
            f"api_status={resp.status_code} retry_after={retry_after or '-'} cooldown={cooldown_text}",
            flush=True,
        )
        raise ProbeRateLimited("naver_land", article_no, resp.status_code, retry_after_s)
    if resp.status_code in {500, 502, 503, 504}:
        retry_after = resp.headers.get("Retry-After")
        suffix = f" retry_after={retry_after}s" if retry_after else ""
        print(
            f"[reconcile] retry-missing naver_land:{article_no} "
            f"api_status={resp.status_code}{suffix}",
            flush=True,
        )
        return None
    if resp.status_code >= 400:
        print(
            f"[reconcile] retry-missing naver_land:{article_no} "
            f"api_status={resp.status_code}",
            flush=True,
        )
        return None
    try:
        payload = resp.json()
    except ValueError as exc:
        print(f"[reconcile] retry-missing naver_land:{article_no} bad_json={exc}", flush=True)
        return None
    return bool(isinstance(payload, dict) and payload.get("articleDetail"))


def _probe_missing_row_once(session: requests.Session, row: dict[str, Any]) -> bool | None:
    platform = row.get("_platform_code")
    if platform == "dabang":
        return _probe_dabang_missing(session, row)
    if platform == "zigbang":
        return _probe_zigbang_missing(session, row)
    if platform == "daangn":
        return _probe_daangn_missing(session, row)
    if platform == "naver_land":
        return _probe_naver_missing(session, row)
    return None


def _missing_probe_delay(platform: str, attempt: int, default_delay_s: float, naver_delay_s: float) -> float:
    base = naver_delay_s if platform == "naver_land" else default_delay_s
    return max(0.0, base * attempt)


def _probe_missing_row(
    session: requests.Session,
    row: dict[str, Any],
    attempts: int,
    default_delay_s: float,
    naver_delay_s: float,
    rate_limit_cooldown_s: float,
) -> bool:
    platform = str(row.get("_platform_code") or "")
    listing_no = str(row.get("listing_no") or row.get("room_id") or "").strip()
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            result = _probe_missing_row_once(session, row)
        except ProbeRateLimited as exc:
            wait_s = exc.retry_after_s if exc.retry_after_s is not None else rate_limit_cooldown_s
            if attempt < max_attempts:
                print(
                    f"[reconcile] retry-missing {platform}:{listing_no} "
                    f"probe=rate-limited attempt={attempt}/{max_attempts} wait={wait_s:.1f}s",
                    flush=True,
                )
                if wait_s:
                    time.sleep(wait_s)
                continue
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                f"probe=rate-limited attempts={max_attempts}; deferring batch",
                flush=True,
            )
            raise
        if result is not None:
            return result
        if attempt < max_attempts:
            wait_s = _missing_probe_delay(platform, attempt, default_delay_s, naver_delay_s)
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                f"probe=unknown attempt={attempt}/{max_attempts} wait={wait_s:.1f}s",
                flush=True,
            )
            if wait_s:
                time.sleep(wait_s)
    print(
        f"[reconcile] retry-missing {platform}:{listing_no} "
        f"probe=no-data attempts={max_attempts}; treating as absent",
        flush=True,
    )
    return False


def retry_missing(args: argparse.Namespace) -> int:
    """Probe only listings already marked missing and update that retry queue."""
    try:
        from db import session
        from reconcile import reconcile_missing_probe
    except ImportError as exc:
        raise RuntimeError(f"retry-missing unavailable: {exc}") from exc

    platform_codes = list(dict.fromkeys(args.platform))
    candidates = _read_db_missing_candidates(platform_codes)
    print(
        f"[reconcile] retry-missing platforms={','.join(platform_codes)} "
        f"candidates={len(candidates)} dry_run={args.dry_run}",
        flush=True,
    )
    by_platform: dict[str, dict[str, Any]] = {
        code: {"found": [], "probed": [], "unknown": 0, "rate_limited": 0}
        for code in platform_codes
    }
    http = requests.Session()
    probe_attempts = max(1, int(args.probe_attempts))
    probe_delay_s = max(0.0, float(args.probe_delay_seconds))
    naver_probe_delay_s = max(0.0, float(args.naver_probe_delay_seconds))
    naver_rate_limit_cooldown_s = max(0.0, float(args.naver_rate_limit_cooldown_seconds))
    deferred_by_rate_limit = False
    for idx, row in enumerate(candidates, 1):
        platform = str(row.get("_platform_code") or "")
        listing_no = str(row.get("listing_no") or "")
        bucket = by_platform.setdefault(platform, {"found": [], "probed": [], "unknown": 0, "rate_limited": 0})
        try:
            result = _probe_missing_row(
                http,
                row,
                attempts=probe_attempts,
                default_delay_s=probe_delay_s,
                naver_delay_s=naver_probe_delay_s,
                rate_limit_cooldown_s=naver_rate_limit_cooldown_s,
            )
        except ProbeRateLimited:
            bucket["unknown"] += 1
            bucket["rate_limited"] += 1
            deferred_by_rate_limit = True
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                "batch_deferred=rate_limited; leaving unresolved missing rows queued",
                flush=True,
            )
            break
        bucket["probed"].append(listing_no)
        if result:
            bucket["found"].append(row)
        if idx % 25 == 0:
            print(f"[reconcile] retry-missing progress={idx}/{len(candidates)}", flush=True)

    retry_at = datetime.now(timezone.utc)
    with session() as conn:
        for platform_code in platform_codes:
            bucket = by_platform.get(platform_code) or {"found": [], "probed": [], "unknown": 0, "rate_limited": 0}
            if not bucket["probed"]:
                print(
                    f"[reconcile] retry-missing {platform_code}: probed=0 "
                    f"found=0 unknown={bucket['unknown']} rate_limited={bucket['rate_limited']}",
                    flush=True,
                )
                continue
            summary = reconcile_missing_probe(
                conn,
                platform_code,
                bucket["found"],
                bucket["probed"],
                retry_at,
                target_area=os.environ.get("RENTMAP_AREA_NAME") or None,
                dry_run_webhooks=args.dry_run_webhooks,
            )
            print(
                f"[reconcile] retry-missing {platform_code}: "
                f"probed={len(bucket['probed'])} found={len(bucket['found'])} "
                f"missing={summary.missing} removed={summary.removed} "
                f"unchanged={summary.unchanged} price={summary.price_changed} "
                f"detail={summary.detail_changed} unknown={bucket['unknown']} "
                f"rate_limited={bucket['rate_limited']} "
                f"errors={len(summary.errors)}",
                flush=True,
            )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    return RETRY_DEFERRED_EXIT if deferred_by_rate_limit else 0


def _read_csv_lenient(data_dir: Path, prefix: str, target_date: str, label: str) -> list[dict[str, str]]:
    path = _latest_csv(data_dir, prefix, target_date)
    if path is None:
        print(f"  [gen-web] {label}: no CSV found (using empty)")
        return []
    if path.name != f"{prefix}_{target_date}.csv":
        print(f"  [gen-web] {label}: today's CSV missing, falling back to {path.name}")
    return read_csv(path)


def _won_to_manwon_str(value: Any) -> str:
    """Reverse the manwon→won conversion ingestion did, for CSV-shape output.

    normal_common does its own float parsing on the result, so emitting a
    plain string keeps the path identical to the CSV-fed gen-web.
    """
    if value is None:
        return ""
    try:
        return str(int(value) // 10000)
    except (TypeError, ValueError):
        return ""


def _date_to_iso_str(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _db_row_to_csv_shape(row: dict[str, Any]) -> dict[str, str]:
    """Project a (listings ⋈ latest snapshot) row into the CSV-shape dict
    ``normal_common`` already understands, so the rest of gen_web is unchanged.

    Anything that lived in ``raw_normalized_json`` (options, security,
    images, agency contact, daangn region depth1/2/3, etc.) flows through
    untouched — the CSV-shape we used to ingest is also the shape we re-emit.
    """
    raw = row.get("raw_normalized_json") or {}
    if isinstance(raw, str):
        # psycopg returns JSONB as a dict already, but defend against a stray
        # string in case a future driver/version round-trips it as text.
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}

    out: dict[str, str] = {
        "listing_no": str(row.get("platform_listing_id") or ""),
        "url": row.get("source_url") or "",
        "title": row.get("title") or "",
        "address": row.get("address_raw") or "",
        "latitude": "" if row.get("lat") is None else str(row["lat"]),
        "longitude": "" if row.get("lng") is None else str(row["lng"]),
        "deposit_manwon": _won_to_manwon_str(row.get("deposit_won")),
        "rent_manwon": _won_to_manwon_str(row.get("monthly_rent_won")),
        "maintenance_manwon": _won_to_manwon_str(row.get("maintenance_fee_won")),
        "total_monthly_manwon": _won_to_manwon_str(row.get("expected_monthly_cost_won")),
        "room_type": row.get("room_type_raw") or "",
        "area_m2": row.get("area_raw") or "",
        "supply_area_m2": "" if row.get("supply_area_m2") is None else str(row["supply_area_m2"]),
        "exclusive_area_m2": "" if row.get("exclusive_area_m2") is None else str(row["exclusive_area_m2"]),
        "floor": row.get("floor_raw") or "",
        "direction": row.get("direction") or "",
        "room_count": "" if row.get("room_count") is None else str(row["room_count"]),
        "bathroom_count": "" if row.get("bathroom_count") is None else str(row["bathroom_count"]),
        "parking": row.get("parking_raw") or "",
        "move_in": row.get("move_in_raw") or "",
        "approval_date": _date_to_iso_str(row.get("approval_date")),
        "building_use": row.get("building_usage") or "",
        "room_structure": row.get("structure_type") or "",
        "description": row.get("description") or "",
    }
    # Merge raw_normalized_json AFTER core fields — raw never overrides a
    # normalized column, only adds the ones we don't have a home for.
    for key, value in raw.items():
        if key not in out and value not in (None, ""):
            out[key] = str(value)
    return out


def _read_db_active(platform_code: str, label: str) -> list[dict[str, str]]:
    """Pull active listings + their most recent snapshot from Postgres.

    Returns a list of CSV-shape dicts so the rest of gen_web doesn't care
    where the data came from. Returns [] (with a warning) when the DB is
    unreachable — the caller's CSV fallback kicks in.
    """
    # Late import so the CSV-only path (--source csv) doesn't pay for psycopg.
    from db import session, DBConfigError  # type: ignore

    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    l.platform_listing_id, l.source_url, l.current_status,
                    s.title, s.description, s.room_type_raw, s.address_raw,
                    s.lat, s.lng,
                    s.deposit_won, s.monthly_rent_won, s.maintenance_fee_won,
                    s.expected_monthly_cost_won,
                    s.supply_area_m2, s.exclusive_area_m2, s.area_raw,
                    s.floor_raw, s.room_count, s.bathroom_count,
                    s.direction, s.parking_raw, s.move_in_raw,
                    s.approval_date, s.building_usage, s.structure_type,
                    s.raw_normalized_json
                FROM listings l
                JOIN platforms p ON p.id = l.platform_id
                JOIN LATERAL (
                    SELECT * FROM listing_snapshots
                    WHERE listing_id = l.id
                    ORDER BY captured_at DESC LIMIT 1
                ) s ON TRUE
                WHERE p.code = %s
                  AND l.current_status = 'active'
                ORDER BY l.id
                """,
                (platform_code,),
            )
            rows = [_db_row_to_csv_shape(r) for r in cur.fetchall()]
            return rows
    except (DBConfigError, Exception) as exc:
        print(f"  [gen-web] {label}: DB unavailable ({exc!s}); will try CSV fallback")
        return []


def _read_for_gen_web(source: str, data_dir: Path, prefix: str, target_date: str,
                       label: str, platform_code: str) -> list[dict[str, str]]:
    """Source selector for gen_web. ``source`` is 'db', 'csv', or 'auto'.

    auto = DB first, fall back to CSV if DB returns empty (cold start, or DB
    intentionally not provisioned). Default operating mode after the DB is in
    place; lets a freshly cloned repo still render pages from the seed CSVs.
    """
    if source == "csv":
        return _read_csv_lenient(data_dir, prefix, target_date, label)
    db_rows = _read_db_active(platform_code, label)
    if source == "db":
        return db_rows
    # auto
    if db_rows:
        return db_rows
    print(f"  [gen-web] {label}: DB empty, falling back to CSV")
    return _read_csv_lenient(data_dir, prefix, target_date, label)


def gen_web(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl_dir = Path(__file__).resolve().parent
    tpl_platform = (tpl_dir / "_tpl_platform.html").read_text(encoding="utf-8")
    tpl_index = (tpl_dir / "_tpl_index.html").read_text(encoding="utf-8")

    src = args.source
    # Prefix tracks the same DEFAULT_AREA the crawlers wrote to so a region
    # crawl + gen-web in the same env (region_runner injects RENTMAP_AREA_NAME)
    # round-trips through the right files. Manual CLI invocations still get
    # "ajou" so legacy operator habits keep working.
    dabang = _read_for_gen_web(src, data_dir, f"dabang_{DEFAULT_AREA}", args.date, "dabang", "dabang")
    daangn = _read_for_gen_web(src, data_dir, f"daangn_{DEFAULT_AREA}", args.date, "daangn", "daangn")
    zigbang = _read_for_gen_web(src, data_dir, f"zigbang_{DEFAULT_AREA}", args.date, "zigbang", "zigbang")
    naver = _read_for_gen_web(src, data_dir, f"naver_land_{DEFAULT_AREA}", args.date, "naver", "naver_land")
    print(f"Loaded ({src}): dabang={len(dabang)} daangn={len(daangn)} zigbang={len(zigbang)} naver={len(naver)}")

    js_dabang = js_array([normal_common(r, "dabang") for r in dabang])
    js_daangn = js_array([normal_daangn(r) for r in daangn])
    js_zigbang = js_array([normal_common(r, "zigbang") for r in zigbang])
    js_naver = js_array([normal_common(r, "naver") for r in naver])

    # Platform templates no longer bake the data inline — they load
    # ``data_<source>_<slug>.js`` at boot via region-data-loader.js so a
    # single HTML page can render any approved region. We still pass "[]"
    # to write_platform for backward compatibility with the legacy
    # __DATA__ placeholder (template doesn't reference it anymore, so this
    # is a no-op string replace).
    write_platform(out_dir / "dabang.html", tpl_platform, "dabang", "#FF5C38", "[]")
    write_platform(out_dir / "daangn.html", tpl_platform, "daangn", "#FF6F00", "[]")
    write_platform(out_dir / "zigbang.html", tpl_platform, "zigbang", "#6366F1", "[]")
    write_platform(out_dir / "naver.html", tpl_platform, "naver", "#03C75A", "[]")

    # Data files are per-region; the slug suffix matches the one
    # region-data-loader.js asks for at runtime. region_runner sets
    # RENTMAP_AREA_NAME to the region's slug before invoking gen-web, so
    # one gen-web call per region writes one set of data files.
    slug = DEFAULT_AREA
    (out_dir / f"data_dabang_{slug}.js").write_text(f"window.DATA_DABANG = {js_dabang};", encoding="utf-8")
    (out_dir / f"data_daangn_{slug}.js").write_text(f"window.DATA_DAANGN = {js_daangn};", encoding="utf-8")
    (out_dir / f"data_zigbang_{slug}.js").write_text(f"window.DATA_ZIGBANG = {js_zigbang};", encoding="utf-8")
    (out_dir / f"data_naver_{slug}.js").write_text(f"window.DATA_NAVER = {js_naver};", encoding="utf-8")
    (out_dir / "index.html").write_text(tpl_index, encoding="utf-8")
    print(f"Wrote web files to {out_dir} (region={slug})")


def normal_common(r: dict[str, str], source: str) -> dict[str, Any]:
    out = {
        "source": source,
        "id": r.get("listing_no", ""),
        "url": r.get("url", ""),
        "agency": r.get("agency", ""),
        "phone": r.get("agent_phone", ""),
        "region": r.get("region", ""),
        "address": r.get("address", ""),
        "lat": num_or_none(r.get("latitude")),
        "lon": num_or_none(r.get("longitude")),
        "title": r.get("title", ""),
        "deposit": num_or_none(r.get("deposit_manwon")),
        "rent": num_or_none(r.get("rent_manwon")),
        "maint": num_or_none(r.get("maintenance_manwon")),
        "total": num_or_none(r.get("total_monthly_manwon")),
        "type": r.get("room_type", ""),
        "area": r.get("area_m2", ""),
        "supply_area": r.get("supply_area_m2", ""),
        "exclusive_area": r.get("exclusive_area_m2", ""),
        "floor": r.get("floor", ""),
        "img1": r.get("image_1", ""),
        "img2": r.get("image_2", ""),
    }
    # Optional detail fields (favorites page renders them when present).
    # Zigbang uses ``residence_type`` for the same concept Naver/Dabang call
    # ``building_use``; expose it under the same key.
    optional_fields = {
        "direction": r.get("direction", ""),
        "room_count": r.get("room_count", ""),
        "bathroom_count": r.get("bathroom_count", ""),
        "room_structure": r.get("room_structure", ""),
        "duplex": r.get("duplex", ""),
        "parking": r.get("parking", ""),
        "elevator": r.get("elevator", ""),
        "pet_allowed": r.get("pet_allowed", ""),
        "loan_available": r.get("loan_available", ""),
        "move_in": r.get("move_in", ""),
        "published_at": r.get("published_at", ""),
        "confirmed_at": r.get("confirmed_at", ""),
        "listing_age_text": r.get("listing_age_text", ""),
        "approval_date": r.get("approval_date", ""),
        "maintenance_detail": r.get("maintenance_detail", ""),
        "maintenance_basis": r.get("maintenance_basis", ""),
        "maintenance_items": r.get("maintenance_items", ""),
        "building_use": r.get("building_use", "") or r.get("residence_type", ""),
        "description": r.get("description", ""),
        "options": r.get("options", ""),
        "security_options": r.get("security_options", ""),
    }
    for key, value in optional_fields.items():
        if value not in (None, ""):
            out[key] = value
    return out


def normal_daangn(r: dict[str, str]) -> dict[str, Any]:
    """Daangn needs source-specific tweaks: writer-type → agency mapping, no
    phone (Daangn never exposes contact via the listing), and depth2/depth3
    composed into a single region string. Other sources use ``normal_common``
    directly.
    """
    agency = "DIRECT" if r.get("writer_type") == "DIRECT_USER" else (r.get("agency") or "BROKER")
    out = normal_common(r, "daangn")
    out["agency"] = agency
    out["phone"] = ""
    out["region"] = " ".join([x for x in [r.get("region_depth2", ""), r.get("region_depth3", "")] if x])
    return out


def num_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        return None


def js_array(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[\n\n]"
    objects = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows]
    return "[\n" + ",\n".join(objects) + "\n]"


def write_platform(path: Path, template: str, source: str, accent: str, data: str, note: str = "") -> None:
    html_text = template.replace("__SOURCE__", source).replace("__ACCENT__", accent).replace("__EXTRA_NOTE__", note).replace("__DATA__", data)
    path.write_text(html_text, encoding="utf-8")
    print(f"Wrote {path}")


def add_common_bbox(parser: argparse.ArgumentParser) -> None:
    """Register --center-{lat,lng} / --radius-km / --{min,max}-{lat,lng}.

    Bbox defaults are derived from the ``RENTMAP_CENTER_*`` env vars. Callers
    can override either by passing the explicit bbox flags directly, or by
    passing --center-lat/--center-lng/--radius-km (which ``apply_center_radius``
    later converts into a fresh bbox).
    """
    min_lat, max_lat, min_lng, max_lng = default_bbox_from_env()
    parser.add_argument("--center-lat", type=float, default=None)
    parser.add_argument("--center-lng", type=float, default=None)
    parser.add_argument("--radius-km", type=float, default=None)
    parser.add_argument("--min-lat", type=float, default=min_lat)
    parser.add_argument("--max-lat", type=float, default=max_lat)
    parser.add_argument("--min-lng", type=float, default=min_lng)
    parser.add_argument("--max-lng", type=float, default=max_lng)


def _resolve_center_radius(args: argparse.Namespace) -> tuple[float, float, float] | None:
    """Return (center_lat, center_lng, radius_km) if any --center/--radius flag
    was supplied, otherwise ``None``. Missing flags fall back to env vars.

    Reads via ``getattr`` throughout so a partial Namespace (one centre attr
    missing) can never AttributeError — current parsers always register all
    three together, but a future caller building a hand-rolled Namespace would
    otherwise be a footgun.
    """
    center_lat = getattr(args, "center_lat", None)
    center_lng = getattr(args, "center_lng", None)
    radius_km = getattr(args, "radius_km", None)
    if center_lat is None and center_lng is None and radius_km is None:
        return None
    return (
        center_lat if center_lat is not None else env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT),
        center_lng if center_lng is not None else env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG),
        radius_km if radius_km is not None else env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM),
    )


def apply_center_radius(args: argparse.Namespace) -> argparse.Namespace:
    """If the caller passed --center-{lat,lng}/--radius-km, recompute the bbox.

    No-op when the parser doesn't expose ``min_lat`` (e.g. ``crawl-all``, which
    derives its bbox internally) or when none of the centre flags were given.
    """
    if not all(hasattr(args, name) for name in ("min_lat", "max_lat", "min_lng", "max_lng")):
        return args
    cr = _resolve_center_radius(args)
    if cr is not None:
        args.min_lat, args.max_lat, args.min_lng, args.max_lng = bbox_from_center_radius(*cr)
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RentMap Python crawler and web generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("crawl-dabang")
    add_common_bbox(p)
    p.add_argument("--zoom", type=int, default=DABANG_DEFAULT_ZOOM)
    p.add_argument("--max-deposit", type=int, default=default_max_deposit())
    p.add_argument("--max-rent", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"dabang_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    p.add_argument("--delay-ms", type=int, default=DABANG_DEFAULT_DELAY_MS)
    p.set_defaults(func=crawl_dabang)

    p = sub.add_parser("crawl-zigbang")
    add_common_bbox(p)
    p.add_argument("--geohashes", nargs="+", default=DEFAULT_ZIGBANG_GEOHASHES)
    p.add_argument("--max-deposit-manwon", type=int, default=default_max_deposit())
    p.add_argument("--max-rent-manwon", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"zigbang_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.set_defaults(func=crawl_zigbang)

    p = sub.add_parser("crawl-daangn")
    p.add_argument("--region-ids", nargs="+", type=int, default=default_daangn_region_ids())
    p.add_argument("--max-deposit", type=int, default=default_max_deposit())
    p.add_argument("--max-rent", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"daangn_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--skip-detail", action="store_true")
    add_common_bbox(p)  # bbox filter applied post-fetch; defaults to env-based centre/radius
    p.set_defaults(func=crawl_daangn)

    p = sub.add_parser("crawl-naver")
    add_common_bbox(p)
    p.add_argument("--url", dest="urls", action="append", default=[])
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"naver_land_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    # See NAVER_DEFAULT_MAX_PAGES — covers ~2000 articles per cortarNo. 5
    # (the old default) left isMoreData=True on 91% of payloads at this radius.
    p.add_argument("--max-pages", type=int, default=NAVER_DEFAULT_MAX_PAGES)
    p.add_argument("--chrome-path", default="")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--skip-home", action="store_true")
    # If set, dump the set of cortarNos the grid pass actually resolved to as
    # a JSON array at this path. region_runner reads it after the crawl and
    # UNION-merges into regions.naver_cortar_nos so subsequent runs benefit
    # from the cortarNo backstop without an admin having to look them up
    # by hand. Empty file ("[]") is fine — caller treats that as "first
    # crawl found nothing new", not an error.
    p.add_argument("--cortarnos-out", default="",
                   help="Dump discovered cortarNos as JSON to this path.")
    # --skip-detail: skip the per-article detail-API enrichment pass. Useful for
    # fast smoke tests; production crawls should leave it off so address/phone/
    # parking/move-in/room/structure/description fields get populated.
    p.add_argument("--skip-detail", action="store_true")
    p.set_defaults(func=crawl_naver)

    p = sub.add_parser("gen-web")
    p.add_argument("--data-dir", default=str(ROOT / "data"))
    p.add_argument("--out-dir", default=str(ROOT / "web"))
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument(
        "--source",
        choices=("auto", "db", "csv"),
        default="auto",
        help=(
            "Data source for the bundle. 'db' reads active listings + latest "
            "snapshot from Postgres. 'csv' reads the dated CSV files (legacy). "
            "'auto' (default) prefers DB and falls back to CSV per-platform "
            "when DB is empty/unreachable."
        ),
    )
    p.set_defaults(func=gen_web)

    p = sub.add_parser("finalize-missing")
    p.add_argument("--platform", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dry-run-webhooks", action="store_true")
    p.set_defaults(func=finalize_missing)

    p = sub.add_parser("retry-missing")
    p.add_argument("--platform", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dry-run-webhooks", action="store_true")
    p.add_argument("--probe-attempts", type=int, default=env_int("RENTMAP_MISSING_PROBE_ATTEMPTS", MISSING_PROBE_ATTEMPTS))
    p.add_argument("--probe-delay-seconds", type=float, default=env_float("RENTMAP_MISSING_PROBE_DELAY_SECONDS", MISSING_PROBE_DELAY_SECONDS))
    p.add_argument("--naver-probe-delay-seconds", type=float, default=env_float("RENTMAP_NAVER_MISSING_PROBE_DELAY_SECONDS", NAVER_MISSING_PROBE_DELAY_SECONDS))
    p.add_argument("--naver-rate-limit-cooldown-seconds", type=float, default=env_float("RENTMAP_NAVER_RATE_LIMIT_COOLDOWN_SECONDS", NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS))
    p.set_defaults(func=retry_missing)

    p = sub.add_parser("crawl-all")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--center-lat", type=float, default=None)
    p.add_argument("--center-lng", type=float, default=None)
    p.add_argument("--radius-km", type=float, default=None)
    p.add_argument("--skip-naver", action="store_true")
    p.add_argument("--gen-web", action="store_true")
    p.add_argument("--gen-web-after-each", action="store_true")
    p.set_defaults(func=crawl_all)
    return parser


def _data_csv(prefix: str, date: str) -> str:
    return str(ROOT / "data" / f"{prefix}_{DEFAULT_AREA}_{date}.csv")


def _bbox_kwargs(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    min_lat, max_lat, min_lng, max_lng = bbox
    return {"min_lat": min_lat, "max_lat": max_lat, "min_lng": min_lng, "max_lng": max_lng}


def _dabang_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    return argparse.Namespace(
        zoom=DABANG_DEFAULT_ZOOM, max_deposit=max_deposit, max_rent=max_rent,
        output_csv=_data_csv("dabang", date), raw_json="", delay_ms=DABANG_DEFAULT_DELAY_MS,
        **_bbox_kwargs(bbox),
    )


def _zigbang_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    return argparse.Namespace(
        geohashes=DEFAULT_ZIGBANG_GEOHASHES,
        max_deposit_manwon=max_deposit, max_rent_manwon=max_rent,
        output_csv=_data_csv("zigbang", date),
        **_bbox_kwargs(bbox),
    )


def _daangn_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    # crawl_daangn checks center_lat/lng/radius_km via apply_center_radius
    # but we've already resolved the bbox, so pass None for the centre flags.
    return argparse.Namespace(
        region_ids=default_daangn_region_ids(),
        max_deposit=max_deposit, max_rent=max_rent,
        output_csv=_data_csv("daangn", date), skip_detail=False,
        center_lat=None, center_lng=None, radius_km=None,
        **_bbox_kwargs(bbox),
    )


def _naver_args(date: str, bbox: tuple[float, float, float, float]) -> argparse.Namespace:
    return argparse.Namespace(
        urls=[],
        output_csv=_data_csv("naver_land", date),
        raw_json=str(ROOT / "data" / f"naver_land_{DEFAULT_AREA}_{date}.raw.json"),
        max_pages=NAVER_DEFAULT_MAX_PAGES, chrome_path="",
        headed=False, skip_home=True, skip_detail=False,
        **_bbox_kwargs(bbox),
    )


def _run_parallel_crawlers(
    jobs: list[tuple[str, Any, argparse.Namespace]],
    *,
    gen_web_after_each: bool = False,
    date_for_gen_web: str = DEFAULT_DATE,
) -> dict[str, BaseException | None]:
    """Run each (label, fn, ns) job on its own thread and return per-job result.

    - Each crawler creates its own ``requests.Session()`` inside its body, so
      no shared mutable state crosses threads.
    - Exceptions are captured per-job; one crawler's failure must not stop the
      others (we'd rather have 2/3 fresh CSVs than 0/3).
    - stdout from each thread interleaves naturally — every line is line-buffered
      by Python and the embedded source name in messages keeps it readable.
    """
    errors: dict[str, BaseException | None] = {label: None for label, _, _ in jobs}
    start = time.time()
    print(f"[crawl-all] launching {len(jobs)} crawlers in parallel: {', '.join(l for l, _, _ in jobs)}", flush=True)
    for label, _, ns in jobs:
        print(f"[crawl-all] [{label}] target output={getattr(ns, 'output_csv', '-')} bbox=({_fmt_bbox(ns)})", flush=True)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        future_to_label = {ex.submit(fn, ns): label for label, fn, ns in jobs}
        for fut in as_completed(future_to_label):
            label = future_to_label[fut]
            elapsed = time.time() - start
            try:
                fut.result()
                print(f"[crawl-all] [{label}] done at +{elapsed:.1f}s", flush=True)
                if gen_web_after_each:
                    print(f"[crawl-all] [{label}] gen-web refresh after crawler completion", flush=True)
                    try:
                        gen_web(argparse.Namespace(data_dir=str(ROOT / "data"), out_dir=str(ROOT / "web"), date=date_for_gen_web, source="auto"))
                    except Exception as exc:
                        print(f"[crawl-all] [{label}] gen-web refresh failed: {exc}", file=sys.stderr, flush=True)
            except BaseException as exc:
                errors[label] = exc
                print(f"[crawl-all] [{label}] FAILED at +{elapsed:.1f}s: {exc}", file=sys.stderr, flush=True)
    print(f"[crawl-all] parallel crawlers finished in {time.time()-start:.1f}s", flush=True)
    return errors


def crawl_all(args: argparse.Namespace) -> None:
    started = time.monotonic()
    bbox = default_bbox_from_env()
    cr = _resolve_center_radius(args)
    if cr is not None:
        bbox = bbox_from_center_radius(*cr)
    max_deposit = default_max_deposit()
    max_rent = default_max_rent()
    min_lat, max_lat, min_lng, max_lng = bbox
    print(
        "[crawl-all] START "
        f"date={args.date} area={_target_area()} "
        f"bbox=(lat={min_lat:.6f}..{max_lat:.6f} lng={min_lng:.6f}..{max_lng:.6f}) "
        f"max_deposit={_fmt_limit(max_deposit)} max_rent={_fmt_limit(max_rent)} "
        f"skip_naver={args.skip_naver} gen_web={args.gen_web} "
        f"gen_web_after_each={args.gen_web_after_each}",
        flush=True,
    )

    # Dabang/Zigbang/Daangn are I/O-bound (external HTTP), no shared state, and
    # each writes to its own CSV — perfect candidates for thread-parallel.
    # Naver stays out: it owns a Playwright browser instance, runs in its own
    # container (see scheduler_naver.py), and the inline path (--no-skip-naver)
    # is rare so the extra concurrency wouldn't help most callers.
    jobs: list[tuple[str, Any, argparse.Namespace]] = [
        ("dabang",  crawl_dabang,  _dabang_args(args.date, bbox, max_deposit, max_rent)),
        ("zigbang", crawl_zigbang, _zigbang_args(args.date, bbox, max_deposit, max_rent)),
        # _daangn_args passes the actual bbox so out-of-radius listings fetched
        # by Daangn region-ID are excluded post-fetch.
        ("daangn",  crawl_daangn,  _daangn_args(args.date, bbox, max_deposit, max_rent)),
    ]
    errors = _run_parallel_crawlers(
        jobs,
        gen_web_after_each=args.gen_web_after_each,
        date_for_gen_web=args.date,
    )
    failed = [label for label, exc in errors.items() if exc is not None]
    print(f"[crawl-all] crawler_summary ok={len(jobs) - len(failed)} failed={failed or []}", flush=True)

    if not args.skip_naver:
        crawl_naver(_naver_args(args.date, bbox))
    if args.gen_web:
        gen_web(argparse.Namespace(data_dir=str(ROOT / "data"), out_dir=str(ROOT / "web"), date=args.date, source="auto"))
    print(f"[crawl-all] DONE elapsed={time.monotonic() - started:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "center_lat"):
            args = apply_center_radius(args)
        result = args.func(args)
        return int(result or 0)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
