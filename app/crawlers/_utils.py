"""Shared utilities, constants, and helpers for all RentMap crawlers."""
from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATE = datetime.now().strftime("%Y-%m-%d")
DEFAULT_AREA = (os.environ.get("RENTMAP_AREA_NAME") or "ajou").strip() or "ajou"
DEFAULT_MIN_LAT = 37.260
DEFAULT_MAX_LAT = 37.290
DEFAULT_MIN_LNG = 127.025
DEFAULT_MAX_LNG = 127.095
DEFAULT_CENTER_LAT = 37.280062
DEFAULT_CENTER_LNG = 127.043688
DEFAULT_RADIUS_KM = 3.0
NO_PRICE_LIMIT_MANWON = 999999
COS_LAT_FLOOR = 0.01
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
CRAWL_DETAIL_PROGRESS_EVERY = 20
MISSING_PROBE_ATTEMPTS = 3
MISSING_PROBE_DELAY_SECONDS = 2.0
NAVER_MISSING_PROBE_DELAY_SECONDS = 15.0
NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RETRY_DEFERRED_EXIT = 75

def print(*args, **kwargs):  # noqa: A001 — shadow builtins.print within this module only
    """Prepend HH:MM:SS to every print() call in rentmap.py."""
    _ts = datetime.now().strftime("%H:%M:%S")
    if args and isinstance(args[0], str):
        args = (f"{_ts} {args[0]}", *args[1:])
    _builtins.print(*args, **kwargs)


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


def center_radius_from_bbox(min_lat: float, max_lat: float, min_lng: float, max_lng: float) -> tuple[float, float, float]:
    """Derive a grid center/radius from an already-resolved bbox."""
    center_lat = (min_lat + max_lat) / 2
    center_lng = (min_lng + max_lng) / 2
    lat_radius_km = abs(max_lat - min_lat) * 111.0 / 2
    lng_radius_km = (
        abs(max_lng - min_lng)
        * 111.0
        * max(math.cos(math.radians(center_lat)), COS_LAT_FLOOR)
        / 2
    )
    return center_lat, center_lng, max(lat_radius_km, lng_radius_km)


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
    # Dict/list inputs would otherwise serialize via repr() and have every
    # contained digit concatenated by to_number, producing astronomical floats
    # that overflow PG BIGINT downstream. The Naver detail API surfaces this
    # at articleDetail.maintenanceCost when it returns the historical
    # costsByDate breakdown instead of a flat amount — handled separately
    # by extract_naver_maintenance_amount before this point.
    if isinstance(value, (dict, list)):
        return None
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


def extract_naver_maintenance_amount(raw: Any) -> Any:
    """Normalize Naver detail-API management-cost field for parse_manwon_from_text.

    The detail API at /api/articles/{no} returns one of two shapes:
    1. articleDetail.monthlyManagementCost = int (e.g. 80000 — won/month).
    2. articleDetail.maintenanceCost = dict
       {costsByDate: [{basisYearMonth: '202602', totalPrice: '84697', ...}, ...]}
       — the historical month-by-month invoice. costsByDate is newest-first;
       pick [0].totalPrice as the current month's amount.

    Returns the raw value unchanged for shape (1), the latest totalPrice for
    shape (2), or None when the dict isn't in the expected shape — letting
    the caller skip the field rather than overwriting with garbage.
    """
    if isinstance(raw, dict):
        items = raw.get("costsByDate")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    total = item.get("totalPrice")
                    if total not in (None, "", 0, "0"):
                        return total
        return None
    if isinstance(raw, list):
        return None
    return raw


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
    # Region scoping happens inside reconcile_crawl now: it resolves the
    # target_area slug to a region row and per-region tracking
    # (listing_regions) drives both the missing-detection candidate set
    # and gen-web's read filter. No more bbox approximation needed.
    try:
        reconcile_csv_rows_safely(platform_code, rows, label=label,
                                  target_area=target_area)
    except Exception as exc:  # noqa: BLE001 — defensive
        print(f"[reconcile] {label}: outer guard caught {type(exc).__name__}: {exc}", file=sys.stderr)


