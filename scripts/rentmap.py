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
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse, parse_qs, urlunparse

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATE = "2026-05-22"
DEFAULT_MIN_LAT = 37.260
DEFAULT_MAX_LAT = 37.290
DEFAULT_MIN_LNG = 127.025
DEFAULT_MAX_LNG = 127.095
NO_PRICE_LIMIT_MANWON = 999999
DEFAULT_ZIGBANG_GEOHASHES = ["wyd7f", "wyd7g", "wyd7u", "wydk4", "wydk5", "wydkh"]
DEFAULT_DAANGN_REGION_IDS = [1289, 1290, 1298, 1294, 1295, 1296, 1297, 1291, 1302, 1303]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"

DABANG_COLUMNS = [
    "source", "listing_no", "room_id", "url", "agency", "agent_name", "agent_phone",
    "region", "address", "latitude", "longitude", "address_public_level", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "area_m2", "floor", "direction", "parking", "move_in", "approval_date",
    "building_use", "options", "security_options", "image_1", "image_2", "crawl_note",
]

ZIGBANG_COLUMNS = [
    "source", "listing_no", "item_id", "url", "agency", "agent_name", "agent_phone",
    "realtor_name", "realtor_phone", "agency_address", "agency_reg_no", "region", "address",
    "latitude", "longitude", "address_public_level", "title", "deposit_manwon", "rent_manwon",
    "maintenance_manwon", "total_monthly_manwon", "room_type", "service_type", "area_m2",
    "floor", "direction", "parking", "move_in", "approval_date", "residence_type",
    "non_compliant_building", "options", "image_1", "image_2", "crawl_note",
]

DAANGN_COLUMNS = [
    "source", "listing_no", "url", "writer_type", "agency", "region_depth1",
    "region_depth2", "region_depth3", "address", "latitude", "longitude", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "room_count", "area_m2", "floor", "approval_date", "image_1", "image_2",
    "crawl_note",
]


def first(obj: Any, names: list[str], default: Any = "") -> Any:
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None and value != "":
            return value
    return default


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


def crawl_dabang(args: argparse.Namespace) -> None:
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

    print("Fetching Dabang list...")
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
    print(f"Found {len(rooms)} list rows. Fetching details...")

    detail_headers = dict(headers)
    detail_headers["D-Api-Version"] = "3.0.1"
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    raw_details: list[Any] = []

    for room in rooms:
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
            "floor": f"{first(room_data, ['room_floor_str', 'roomFloorStr'])}/{first(room_data, ['building_floor_str', 'buildingFloorStr'])}",
            "direction": first(room_data, ["direction_str", "directionStr", "direction"]),
            "parking": first(room_data, ["parking_str", "parkingStr", "parking"]),
            "move_in": first(room_data, ["moving_date", "movingDate"]),
            "approval_date": first(room_data, ["building_approval_date_str", "buildingApprovalDateStr"]),
            "building_use": join_text_list(first(room_data, ["building_use_types_str", "buildingUseTypesStr"])),
            "options": join_text_list(options),
            "security_options": join_text_list(security),
            "image_1": image_url(images, 0),
            "image_2": image_url(images, 1),
            "crawl_note": "",
        })
        time.sleep(args.delay_ms / 1000)

    records.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["total_monthly_manwon"]), float_or_inf(r["rent_manwon"])))
    write_csv(Path(args.output_csv), records, DABANG_COLUMNS)
    if args.raw_json:
        Path(args.raw_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.raw_json).write_text(json.dumps(raw_details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} rows to {args.output_csv}")


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


def format_date_text(value: Any) -> str:
    text = to_text(value)
    return f"{text[:4]}.{text[4:6]}.{text[6:]}" if re.match(r"^\d{8}$", text) else text


def crawl_zigbang(args: argparse.Namespace) -> None:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*", "Origin": "https://www.zigbang.com", "Referer": "https://www.zigbang.com/"}
    items_by_id: dict[str, dict[str, Any]] = {}
    for geohash in args.geohashes:
        print(f"Fetching Zigbang list {geohash}")
        url = f"https://apis.zigbang.com/v2/items/oneroom?geohash={quote(geohash)}&depositMin=0&rentMin=0&salesTypes%5B0%5D=%EC%9B%94%EC%84%B8&domain=zigbang&checkAnyItemWithoutFilter=true"
        payload = request_json(session, url, headers=headers)
        for item in payload.get("items", []):
            lat, lng = float(item.get("lat", 0)), float(item.get("lng", 0))
            if args.min_lat <= lat <= args.max_lat and args.min_lng <= lng <= args.max_lng:
                items_by_id[to_text(item.get("itemId"))] = item
    print(f"Detail candidates in bbox: {len(items_by_id)}")

    rows: list[dict[str, Any]] = []
    for idx, item_id in enumerate(sorted(items_by_id), 1):
        if idx % 20 == 0:
            print(f"Fetched details: {idx}/{len(items_by_id)}")
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
            images = item.get("images") or []
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
                "service_type": item.get("serviceType", ""),
                "area_m2": get_area_m2(item.get("area")),
                "floor": get_floor_text(item.get("floor")),
                "direction": item.get("roomDirection", ""),
                "parking": item.get("parkingAvailableText", ""),
                "move_in": item.get("moveinDate", ""),
                "approval_date": format_date_text(item.get("approveDate", "")),
                "residence_type": item.get("residenceType", ""),
                "non_compliant_building": item.get("nonCompliantBuilding", ""),
                "options": join_text_list(item.get("options")),
                "image_1": images[0] if len(images) > 0 else "",
                "image_2": images[1] if len(images) > 1 else "",
                "crawl_note": "",
            })
        except Exception as exc:
            print(f"WARNING: Failed detail {item_id}: {exc}", file=sys.stderr)
    rows.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["rent_manwon"]), float_or_inf(r["deposit_manwon"])))
    write_csv(Path(args.output_csv), rows, ZIGBANG_COLUMNS)
    print(f"Wrote {len(rows)} rows to {args.output_csv}")


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
    valid_types = {"SPLIT_ONE_ROOM", "OPEN_ONE_ROOM", "TWO_ROOM", "OFFICETEL"}
    session = requests.Session()
    all_raw: list[dict[str, Any]] = []
    seen: set[str] = set()
    print(f"Fetching Daangn listings from {len(args.region_ids)} regions...")
    for region_id in args.region_ids:
        listings = get_daangn_listings(session, region_id, args.max_deposit, args.max_rent, valid_types)
        print(f"  Region {region_id}: {len(listings)} listings within budget")
        for listing in listings:
            article_id = article_id_from_url(listing.get("webUrl", ""))
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            all_raw.append(listing)
    print(f"Total unique listings: {len(all_raw)}")

    records: list[dict[str, Any]] = []
    for idx, listing in enumerate(all_raw, 1):
        article_id = article_id_from_url(listing.get("webUrl", ""))
        print(f"[{idx}/{len(all_raw)}] {article_id}")
        trades = listing.get("trades") or []
        trade = next((t for t in trades if t.get("type") == "MONTH"), {})
        detail = {} if args.skip_detail else get_daangn_article_detail(session, article_id)
        region = listing.get("_regionInfo") or {}
        lat, lon = detail.get("lat", ""), detail.get("lon", "")
        public_addr = detail.get("publicAddress") or listing.get("address", "")
        approval = detail.get("approvalDate") or listing.get("buildingApprovalDate", "")
        writer_type = detail.get("writerType") or listing.get("writerType", "")
        maintenance = float(listing.get("manageCost") or 0)
        rent = float(trade.get("monthlyPay") or 0)
        title = re.sub(r"\s*\|\s*[^\|]+$", "", to_text(listing.get("title", "")))
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
            "area_m2": listing.get("area", ""),
            "floor": listing.get("floor", ""),
            "approval_date": approval,
            "image_1": image_url(listing.get("images"), 0),
            "image_2": image_url(listing.get("images"), 1),
            "crawl_note": "",
        })
    if all(v != 0 for v in [args.min_lat, args.max_lat, args.min_lng, args.max_lng]):
        before = len(records)
        records = [r for r in records if bbox_ok(r.get("latitude"), r.get("longitude"), args)]
        print(f"Bbox filter: {before} -> {len(records)} records")
    records.sort(key=lambda r: (to_text(r["region_depth3"]), float_or_inf(r["total_monthly_manwon"]), float_or_inf(r["rent_manwon"])))
    write_csv(Path(args.output_csv), records, DAANGN_COLUMNS)
    print(f"Wrote {len(records)} rows to {args.output_csv}")


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


def get_daangn_article_detail(session: requests.Session, article_id: str) -> dict[str, str]:
    try:
        text = get_utf8(session, f"https://realty.daangn.com/articles/{article_id}", delay_ms=80)
    except Exception as exc:
        print(f"WARNING: Article {article_id} fetch failed: {exc}", file=sys.stderr)
        return {}
    detail = {"lat": "", "lon": "", "publicAddress": "", "roomCnt": "", "approvalDate": "", "writerType": "", "agencyName": ""}
    coord_ref = re.search(r'originalId\\":\\"' + re.escape(article_id) + r'\\".*?publicCoordinate\\":\{\\"__ref\\":\\"([^\\"]+)', text)
    if coord_ref:
        coord = re.search(re.escape(coord_ref.group(1)) + r'\\":\{\\"__id\\":\\"[^\\"]+\\",\\"__typename\\":\\"Coordinate\\",\\"lat\\":\\"([^\\"]+)\\",\\"lon\\":\\"([^\\"]+)', text)
        if coord:
            detail["lat"], detail["lon"] = coord.group(1), coord.group(2)
    patterns = {
        "publicAddress": r'publicAddress\\":\\"([^\\"]*)',
        "roomCnt": r'roomCnt\\":\\"?([^\\",}]*)',
        "approvalDate": r'buildingApprovalDate\\":\\"([^\\"]*)',
        "writerType": r'writerTypeV2\\":\\"([^\\"]*)',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            detail[key] = match.group(1)
    meta = ""
    m1 = re.search(r'name="description"\s+content="([^"]+)"', text)
    m2 = re.search(r'content="([^"]+)"\s+name="description"', text)
    if m1:
        meta = html.unescape(m1.group(1))
    elif m2:
        meta = html.unescape(m2.group(1))
    parts = meta.split("\u2014", 1)
    if len(parts) == 2:
        after = parts[1].strip()
        phone = re.search(r"\s[0-9]{2,3}-[0-9]", after)
        candidate = after[: phone.start()] if phone else after[:35]
        candidate = re.sub(r"^[\W]+|[\W]+$", "", candidate.strip()).strip()
        if 2 <= len(candidate) <= 30 and re.search(r"부동산|공인중개|중개사|사무소", candidate):
            detail["agencyName"] = candidate
    return detail


def bbox_ok(lat: Any, lon: Any, args: argparse.Namespace) -> bool:
    if not any([args.min_lat, args.max_lat, args.min_lng, args.max_lng]):
        return True
    if lat in (None, "") or lon in (None, ""):
        return True
    try:
        return args.min_lat <= float(lat) <= args.max_lat and args.min_lng <= float(lon) <= args.max_lng
    except Exception:
        return True


NAVER_DEFAULT_URLS = [
    "https://new.land.naver.com/rooms?cortarNo=4111710200&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
    "https://new.land.naver.com/rooms?cortarNo=4111514000&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
    "https://new.land.naver.com/rooms?cortarNo=4111710100&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
    "https://new.land.naver.com/rooms?cortarNo=4111710300&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
    "https://new.land.naver.com/rooms?cortarNo=4111710400&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
]


def crawl_naver(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Python Playwright is required for Naver crawling. Install with: python -m pip install playwright && python -m playwright install chromium") from exc
    asyncio.run(crawl_naver_async(args, async_playwright))


async def crawl_naver_async(args: argparse.Namespace, async_playwright: Any) -> None:
    urls = args.urls or NAVER_DEFAULT_URLS
    chrome = find_chrome(args.chrome_path)
    async with async_playwright() as p:
        launch_options: dict[str, Any] = {
            "headless": not args.headed,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if chrome:
            launch_options["executable_path"] = chrome
        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context(locale="ko-KR", user_agent=UA)
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        article_headers: dict[str, str] | None = None

        async def on_request(request: Any) -> None:
            nonlocal article_headers
            if "/api/articles?" in request.url:
                try:
                    article_headers = await request.all_headers()
                except Exception:
                    pass

        page.on("request", on_request)
        try:
            if not args.skip_home:
                await page.goto("https://new.land.naver.com/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1200)
            seen: set[str] = set()
            records: list[dict[str, Any]] = []
            raw_payloads: list[Any] = []
            for idx, url in enumerate(urls, 1):
                print(f"\nCrawling Naver URL {idx}/{len(urls)}: {url}")
                one_records, payloads = await crawl_naver_one(page, context, url, article_headers, args)
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
                print(f"  Found {len(one_records)} in bbox, {new_count} new after dedup")
            records.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["total_monthly_manwon"])))
            write_csv(Path(args.output_csv), records, DABANG_COLUMNS)
            if args.raw_json:
                Path(args.raw_json).parent.mkdir(parents=True, exist_ok=True)
                Path(args.raw_json).write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nWrote {len(records)} rows to {args.output_csv}")
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


async def crawl_naver_one(page: Any, context: Any, target_url: str, article_headers: dict[str, str] | None, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[Any]]:
    center = get_map_center(target_url)
    async with page.expect_response(lambda r: "/api/articles?" in r.url and r.status == 200, timeout=45000) as response_info:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
    first_response = await response_info.value
    first_url = first_response.url
    request_headers = await first_response.request.all_headers()
    print(f"  captured: {first_url}")
    try:
        first_json = await first_response.json()
    except Exception:
        response = await context.request.get(first_url, headers=clean_headers(request_headers or article_headers), timeout=30000)
        if not response.ok:
            raise RuntimeError(f"Naver article API request failed: {response.status}")
        first_json = await response.json()
    payloads = [first_json]
    page_no = 2
    while page_no <= args.max_pages and first_json.get("isMoreData"):
        next_url = set_query_param(first_url, "page", str(page_no))
        response = await context.request.get(next_url, headers=clean_headers(request_headers or article_headers), timeout=30000)
        if not response.ok:
            break
        payload = await response.json()
        payloads.append(payload)
        first_json = payload
        page_no += 1
        await page.wait_for_timeout(250)
    records = []
    for payload in payloads:
        for article in payload.get("articleList") or []:
            record = normalize_naver_article(article, target_url, center)
            if bbox_ok(record.get("latitude"), record.get("longitude"), args):
                records.append(record)
    return records, payloads


def clean_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    blocked = {"accept-encoding", "connection", "content-length", "cookie", "host"}
    return {k: v for k, v in headers.items() if not k.startswith(":") and k.lower() not in blocked}


def set_query_param(url: str, key: str, value: str) -> str:
    parts = urlparse(url)
    query = parse_qs(parts.query)
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
    return {
        "source": "naver_land",
        "listing_no": article_no,
        "room_id": article_no,
        "url": f"https://new.land.naver.com/rooms?articleNo={article_no}" if article_no else source_url,
        "agency": first(article, ["realtorName", "cpName"]),
        "agent_name": "",
        "agent_phone": "",
        "region": first(article, ["cityName", "divisionName", "sectionName"]),
        "address": first(article, ["articleName", "buildingName"]),
        "latitude": lat,
        "longitude": lon,
        "address_public_level": "naver_public_listing_level",
        "title": first(article, ["articleFeatureDesc", "articleName"]),
        "deposit_manwon": parsed.get("deposit") or parse_amount_manwon(deposit_text),
        "rent_manwon": rent,
        "maintenance_manwon": maintenance,
        "total_monthly_manwon": "" if rent == "" else round1(float(rent) + (float(maintenance) if maintenance != "" else 0)),
        "room_type": first(article, ["realEstateTypeName", "articleName"]),
        "area_m2": "/".join([to_text(x) for x in [first(article, ["supplySpace", "area1"]), first(article, ["exclusiveSpace", "area2"])] if x]),
        "floor": " ".join([to_text(x) for x in [first(article, ["floorInfo"]), first(article, ["floorLayerName"])] if x]),
        "direction": first(article, ["direction"]),
        "parking": "",
        "move_in": "",
        "approval_date": format_date_text(first(article, ["articleConfirmYmd", "confirmYmd"])),
        "building_use": first(article, ["articleRealEstateTypeName"]),
        "options": join_text_list([first(article, ["tagList"]), first(article, ["articleFeatureDesc"])]),
        "security_options": "",
        "image_1": img,
        "image_2": "",
        "crawl_note": "Captured from Naver Land article list API.",
    }


def float_or_empty(value: Any) -> Any:
    try:
        if value in (None, ""):
            return ""
        return float(value)
    except Exception:
        return ""


def gen_web(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl_dir = Path(__file__).resolve().parent
    tpl_platform = (tpl_dir / "_tpl_platform.html").read_text(encoding="utf-8")
    tpl_index = (tpl_dir / "_tpl_index.html").read_text(encoding="utf-8")

    dabang = read_csv(data_dir / f"dabang_ajou_{args.date}.csv")
    daangn = read_csv(data_dir / f"daangn_ajou_{args.date}.csv")
    zigbang = read_csv(data_dir / f"zigbang_ajou_{args.date}.csv")
    naver = read_csv(data_dir / f"naver_land_ajou_{args.date}.csv")
    print(f"Loaded: dabang={len(dabang)} daangn={len(daangn)} zigbang={len(zigbang)} naver={len(naver)}")

    js_dabang = js_array([normal_dabang(r) for r in dabang])
    js_daangn = js_array([normal_daangn(r) for r in daangn])
    js_zigbang = js_array([normal_zigbang(r) for r in zigbang])
    js_naver = js_array([normal_naver(r) for r in naver])

    write_platform(out_dir / "dabang.html", tpl_platform, "dabang", "#FF5C38", js_dabang)
    write_platform(out_dir / "daangn.html", tpl_platform, "daangn", "#FF6F00", js_daangn)
    write_platform(out_dir / "zigbang.html", tpl_platform, "zigbang", "#6366F1", js_zigbang)
    write_platform(out_dir / "naver.html", tpl_platform, "naver", "#03C75A", js_naver)

    (out_dir / "data_dabang.js").write_text(f"window.DATA_DABANG = {js_dabang};", encoding="utf-8")
    (out_dir / "data_daangn.js").write_text(f"window.DATA_DAANGN = {js_daangn};", encoding="utf-8")
    (out_dir / "data_zigbang.js").write_text(f"window.DATA_ZIGBANG = {js_zigbang};", encoding="utf-8")
    (out_dir / "data_naver.js").write_text(f"window.DATA_NAVER = {js_naver};", encoding="utf-8")
    (out_dir / "index.html").write_text(tpl_index, encoding="utf-8")
    print(f"Wrote web files to {out_dir}")


def normal_common(r: dict[str, str], source: str) -> dict[str, Any]:
    return {
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
        "floor": r.get("floor", ""),
        "img1": r.get("image_1", ""),
        "img2": r.get("image_2", ""),
    }


def normal_dabang(r: dict[str, str]) -> dict[str, Any]:
    return normal_common(r, "dabang")


def normal_zigbang(r: dict[str, str]) -> dict[str, Any]:
    return normal_common(r, "zigbang")


def normal_naver(r: dict[str, str]) -> dict[str, Any]:
    out = normal_common(r, "naver")
    return out


def normal_daangn(r: dict[str, str]) -> dict[str, Any]:
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


def add_common_bbox(parser: argparse.ArgumentParser, *, naver: bool = False) -> None:
    parser.add_argument("--min-lat", type=float, default=0 if naver else DEFAULT_MIN_LAT)
    parser.add_argument("--max-lat", type=float, default=0 if naver else DEFAULT_MAX_LAT)
    parser.add_argument("--min-lng", type=float, default=0 if naver else DEFAULT_MIN_LNG)
    parser.add_argument("--max-lng", type=float, default=0 if naver else DEFAULT_MAX_LNG)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RentMap Python crawler and web generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("crawl-dabang")
    add_common_bbox(p)
    p.add_argument("--zoom", type=int, default=18)
    p.add_argument("--max-deposit", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--max-rent", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"dabang_ajou_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    p.add_argument("--delay-ms", type=int, default=120)
    p.set_defaults(func=crawl_dabang)

    p = sub.add_parser("crawl-zigbang")
    add_common_bbox(p)
    p.add_argument("--geohashes", nargs="+", default=DEFAULT_ZIGBANG_GEOHASHES)
    p.add_argument("--max-deposit-manwon", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--max-rent-manwon", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"zigbang_ajou_{DEFAULT_DATE}.csv"))
    p.set_defaults(func=crawl_zigbang)

    p = sub.add_parser("crawl-daangn")
    p.add_argument("--region-ids", nargs="+", type=int, default=DEFAULT_DAANGN_REGION_IDS)
    p.add_argument("--max-deposit", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--max-rent", type=int, default=NO_PRICE_LIMIT_MANWON)
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"daangn_ajou_{DEFAULT_DATE}.csv"))
    p.add_argument("--skip-detail", action="store_true")
    p.add_argument("--min-lat", type=float, default=0)
    p.add_argument("--max-lat", type=float, default=0)
    p.add_argument("--min-lng", type=float, default=0)
    p.add_argument("--max-lng", type=float, default=0)
    p.set_defaults(func=crawl_daangn)

    p = sub.add_parser("crawl-naver")
    add_common_bbox(p, naver=True)
    p.add_argument("--url", dest="urls", action="append", default=[])
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"naver_land_ajou_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--chrome-path", default="")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--skip-home", action="store_true")
    p.set_defaults(func=crawl_naver)

    p = sub.add_parser("gen-web")
    p.add_argument("--data-dir", default=str(ROOT / "data"))
    p.add_argument("--out-dir", default=str(ROOT / "web"))
    p.add_argument("--date", default=DEFAULT_DATE)
    p.set_defaults(func=gen_web)

    p = sub.add_parser("crawl-all")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--skip-naver", action="store_true")
    p.add_argument("--gen-web", action="store_true")
    p.set_defaults(func=crawl_all)
    return parser


def crawl_all(args: argparse.Namespace) -> None:
    crawl_dabang(argparse.Namespace(min_lat=DEFAULT_MIN_LAT, min_lng=DEFAULT_MIN_LNG, max_lat=DEFAULT_MAX_LAT, max_lng=DEFAULT_MAX_LNG, zoom=18, max_deposit=NO_PRICE_LIMIT_MANWON, max_rent=NO_PRICE_LIMIT_MANWON, output_csv=str(ROOT / "data" / f"dabang_ajou_{args.date}.csv"), raw_json="", delay_ms=120))
    crawl_zigbang(argparse.Namespace(min_lat=DEFAULT_MIN_LAT, min_lng=DEFAULT_MIN_LNG, max_lat=DEFAULT_MAX_LAT, max_lng=DEFAULT_MAX_LNG, geohashes=DEFAULT_ZIGBANG_GEOHASHES, max_deposit_manwon=NO_PRICE_LIMIT_MANWON, max_rent_manwon=NO_PRICE_LIMIT_MANWON, output_csv=str(ROOT / "data" / f"zigbang_ajou_{args.date}.csv")))
    crawl_daangn(argparse.Namespace(region_ids=DEFAULT_DAANGN_REGION_IDS, max_deposit=NO_PRICE_LIMIT_MANWON, max_rent=NO_PRICE_LIMIT_MANWON, output_csv=str(ROOT / "data" / f"daangn_ajou_{args.date}.csv"), skip_detail=False, min_lat=0, max_lat=0, min_lng=0, max_lng=0))
    if not args.skip_naver:
        crawl_naver(argparse.Namespace(urls=[], output_csv=str(ROOT / "data" / f"naver_land_ajou_{args.date}.csv"), raw_json=str(ROOT / "data" / f"naver_land_ajou_{args.date}.raw.json"), max_pages=5, chrome_path="", headed=False, skip_home=True, min_lat=0, max_lat=0, min_lng=0, max_lng=0))
    if args.gen_web:
        gen_web(argparse.Namespace(data_dir=str(ROOT / "data"), out_dir=str(ROOT / "web"), date=args.date))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
