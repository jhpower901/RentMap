"""Dabang crawler."""
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

import requests

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON, UA, CRAWL_DETAIL_PROGRESS_EVERY,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    nested, to_text, to_number, round1, join_text_list, join_nested_text,
    first, first_deep, has_address_detail, image_url,
    parse_manwon_from_text, to_iso_date, days_ago_text, split_area_pair,
    write_csv, request_json, _fmt_bbox, _fmt_limit, _log_crawl_start, _log_crawl_done,
    best_address, float_or_inf, normalize_phone,
    _reconcile_after_crawl,
)

DABANG_DEFAULT_DELAY_MS = 120
DABANG_DEFAULT_ZOOM = 18
DABANG_DETAIL_WORKERS = 8

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

    # Deduplicate rooms before issuing any detail requests.
    seen: set[str] = set()
    unique_rooms: list[tuple[str, dict[str, Any]]] = []
    for room in rooms:
        room_id = to_text(first(room, ["id", "room_id", "roomId", "seq", "hash"]))
        if room_id and room_id not in seen:
            seen.add(room_id)
            unique_rooms.append((room_id, room))

    # Parallel detail fetches — each worker gets its own Session.
    def _fetch_dabang_detail(args_: tuple[str, dict]) -> tuple[str, dict[str, Any] | None]:
        rid, _ = args_
        detail_url = (
            f"https://www.dabangapp.com/api/3/new-room/detail"
            f"?room_id={quote(rid)}&api_version=3.0.1&call_type=web&version=1"
        )
        s = requests.Session()
        try:
            payload = request_json(s, detail_url, headers=detail_headers)
            return rid, payload.get("result", payload)
        except Exception as exc:
            print(f"WARNING: Detail fetch failed for room_id={rid}: {exc}", file=sys.stderr)
            return rid, None

    print(f"[crawl:dabang] detail_fetch rooms={len(unique_rooms)} workers={DABANG_DETAIL_WORKERS}", flush=True)
    detail_map: dict[str, dict[str, Any]] = {}
    done_cnt = 0
    with ThreadPoolExecutor(max_workers=DABANG_DETAIL_WORKERS) as pool:
        for rid, det in pool.map(_fetch_dabang_detail, unique_rooms):
            if det is not None:
                detail_map[rid] = det
            done_cnt += 1
            if done_cnt % CRAWL_DETAIL_PROGRESS_EVERY == 0:
                print(f"[crawl:dabang] detail_progress={done_cnt}/{len(unique_rooms)}", flush=True)
    print(f"[crawl:dabang] detail_done={done_cnt}", flush=True)

    records: list[dict[str, Any]] = []
    raw_details: list[Any] = []

    for room_id, room in unique_rooms:
        detail = detail_map.get(room_id)
        if detail is None:
            continue
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

    records.sort(key=lambda r: (to_text(r["agency"]), float_or_inf(r["total_monthly_manwon"]), float_or_inf(r["rent_manwon"])))
    write_csv(Path(args.output_csv), records, DABANG_COLUMNS)
    _reconcile_after_crawl("dabang", records, "dabang")
    if args.raw_json:
        Path(args.raw_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.raw_json).write_text(json.dumps(raw_details, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_crawl_done("dabang", len(records), args.output_csv, time.monotonic() - started)



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


