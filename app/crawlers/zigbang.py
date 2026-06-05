"""Zigbang crawler."""
from __future__ import annotations

import sys

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import math
import requests

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON, UA, CRAWL_DETAIL_PROGRESS_EVERY,
    DEFAULT_CENTER_LAT, DEFAULT_CENTER_LNG, DEFAULT_RADIUS_KM,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    nested, to_text, to_number, round1, join_text_list,
    first, first_deep, image_url,
    parse_manwon_from_text, to_iso_date, days_ago_text,
    write_csv, request_json, _fmt_bbox, _fmt_limit, _log_crawl_start, _log_crawl_done,
    float_or_inf, normalize_phone, get_floor_text, get_area_m2, get_utf8,
    _reconcile_after_crawl,
)

DEFAULT_ZIGBANG_GEOHASHES = ["wyd7f", "wyd7g", "wyd7u", "wydk4", "wydk5", "wydkh"]

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



# ── Zigbang geohash auto-generation ──────────────────────────────────────────
# Zigbang's list API is geohash-scoped — one query returns the items inside a
# single geohash cell. For ajou we used to hard-code 6 cells covering a ~3km
# radius; for multi-region we compute the cell set from the region's
# center+radius so adding a new region doesn't need an admin to look up
# geohashes manually.
_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
ZIGBANG_GEOHASH_PRECISION = 5           # ~4.9km × 4.9km at our latitudes
ZIGBANG_GEOHASH_STEP_KM = 1.2           # Aligned with NAVER_TILE_STEP_KM —
                                         # 1.2km gives enough overlap that a
                                         # radius bbox reliably picks up the
                                         # 2 outer cells our legacy 6-cell
                                         # ajou list had (verified).


def encode_geohash(lat: float, lng: float, precision: int = ZIGBANG_GEOHASH_PRECISION) -> str:
    """Encode (lat, lng) to a base32 geohash of the given precision.

    Pure-Python so we don't need to add a geohash dependency to
    requirements.txt — the algorithm is small and the API is stable.
    Bit interleaving is lng-first, matching the public spec / the strings
    Zigbang's URL uses (verified against the hard-coded ajou cells:
    37.280062, 127.043688 → "wydk5" with precision 5).
    """
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    bits: list[int] = []
    is_lng = True
    while len(bits) < precision * 5:
        if is_lng:
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng >= mid:
                bits.append(1)
                lng_range[0] = mid
            else:
                bits.append(0)
                lng_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                bits.append(1)
                lat_range[0] = mid
            else:
                bits.append(0)
                lat_range[1] = mid
        is_lng = not is_lng
    out = []
    for i in range(0, len(bits), 5):
        chunk = bits[i:i + 5]
        idx = (chunk[0] << 4) | (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
        out.append(_GEOHASH_BASE32[idx])
    return "".join(out)


def gen_zigbang_geohashes(center_lat: float, center_lng: float, radius_km: float,
                          precision: int = ZIGBANG_GEOHASH_PRECISION,
                          step_km: float = ZIGBANG_GEOHASH_STEP_KM) -> list[str]:
    """All distinct precision-N geohashes that cover a center+radius bbox.

    Sweeps a square grid that comfortably overshoots the radius — a
    precision-5 geohash cell is ~4.9km × 4.9km and a listing can sit
    near a cell edge that falls just outside the strict radius. We
    apply a 1.5× radius padding (verified to recover the original ajou
    "wydkh" / "wyd7u" cells the hand-tuned 6-cell list relied on)
    before sweeping; the post-fetch bbox filter (``args.min_lat`` etc.)
    still trims listings that genuinely fall outside the user's radius.
    """
    effective_radius = radius_km * 1.5
    deg_per_km_lat = 1.0 / 111.0
    deg_per_km_lng = 1.0 / (111.0 * max(0.01, math.cos(math.radians(center_lat))))
    steps = max(1, math.ceil(effective_radius / step_km))
    seen: set[str] = set()
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            lat = center_lat + i * step_km * deg_per_km_lat
            lng = center_lng + j * step_km * deg_per_km_lng
            seen.add(encode_geohash(lat, lng, precision))
    return sorted(seen)


def default_zigbang_geohashes() -> list[str]:
    """Return Zigbang geohash cells for the active region.

    Priority:
    1. ``RENTMAP_ZIGBANG_GEOHASHES`` env (comma-separated). Override for
       cases where the auto-computed set misses an oddly-shaped area.
    2. Auto-computed from RENTMAP_CENTER_LAT/LNG + RENTMAP_RADIUS_KM.
    3. Fall back to the hard-coded Ajou cells so a stray manual CLI
       invocation (no env, no region) still works.
    """
    raw = os.environ.get("RENTMAP_ZIGBANG_GEOHASHES", "").strip()
    if raw:
        cells = [x.strip() for x in raw.split(",") if x.strip()]
        if cells:
            print(f"[zigbang] using {len(cells)} geohashes from RENTMAP_ZIGBANG_GEOHASHES",
                  file=sys.stderr)
            return cells
    center_lat = env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT)
    center_lng = env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG)
    radius_km = env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM)
    auto = gen_zigbang_geohashes(center_lat, center_lng, radius_km)
    if auto:
        print(f"[zigbang] auto-generated {len(auto)} geohash(es) for "
              f"center=({center_lat:.5f},{center_lng:.5f}) radius={radius_km}km: {','.join(auto)}",
              file=sys.stderr)
        return auto
    return list(DEFAULT_ZIGBANG_GEOHASHES)



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


