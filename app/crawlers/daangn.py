"""Daangn (당근) crawler."""
from __future__ import annotations

import html
import string
import sys

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON, UA, CRAWL_DETAIL_PROGRESS_EVERY,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    nested, to_text, to_number, round1, join_text_list,
    first, first_deep, has_address_detail, image_url, text_has_any,
    parse_manwon_from_text, to_iso_date, days_ago_text,
    write_csv, request_json, _fmt_bbox, _fmt_limit, _log_crawl_start, _log_crawl_done,
    float_or_inf, get_utf8,
    _reconcile_after_crawl,
)

DAANGN_BASE_URL = "https://realty.daangn.com"
DAANGN_GRAPHQL_URL = "https://realty.kr.karrotmarket.com/graphql"
DAANGN_ARTICLE_DETAIL_QUERY_HASH = "a6ca947b00f51b71850abb5757a9bf66e73dd50524352b78aed4138bc82b9ae0"
_daangn_article_detail_query_hash_cache = ""

DAANGN_GQL_WORKERS = 8

DEFAULT_DAANGN_REGION_IDS = [1289, 1290, 1298, 1294, 1295, 1296, 1297, 1291, 1302, 1303]

DAANGN_ORIENTATION_MAP: dict[str, str] = {
    "EAST_FACING": "동향", "WEST_FACING": "서향",
    "SOUTH_FACING": "남향", "NORTH_FACING": "북향",
    "SOUTH_EAST_FACING": "남동향", "SOUTH_WEST_FACING": "남서향",
    "NORTH_EAST_FACING": "북동향", "NORTH_WEST_FACING": "북서향",
}
DAANGN_BUILDING_USAGE_MAP: dict[str, str] = {
    "SINGLE_FAMILY_HOUSING": "단독주택",
    "MULTI_FAMILY_HOUSING": "공동주택",
    "NEIGHBORHOOD_FACILITY_2": "제2종근린생활시설",
    "NEIGHBORHOOD_FACILITY_1": "제1종근린생활시설",
    "OFFICETEL": "오피스텔",
}
DAANGN_OPTION_LABEL_MAP: dict[str, str] = {
    "PARKING": "주차", "ELEVATOR": "엘리베이터", "WASHER": "세탁기",
    "FRIDGE": "냉장고", "AIRCON": "에어컨", "GAS_RANGE": "가스레인지",
    "ELEC_RANGE": "전기레인지", "INDUCTION": "인덕션", "BED": "침대",
    "DESK": "책상", "CLOSET": "옷장", "SINK": "싱크대",
    "MICROWAVE": "전자레인지", "TV": "TV", "SHOE_CABINET": "신발장",
    "BIDET": "비데", "PET": "반려동물", "MORTGAGE": "대출",
    "LOFT": "다락방", "ILLEGAL_BUILDING": "위반건물",
}
DAANGN_MANAGE_COST_OPTION_MAP: dict[str, str] = {
    "WATERWORKS": "수도", "ELECTRICITY": "전기", "GAS": "가스",
    "INTERNET": "인터넷", "COMMON": "공용관리비", "TV": "TV",
    "CLEANING": "청소", "ELEVATOR": "엘리베이터", "PARKING": "주차",
}


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

DAANGN_FACILITY_KEYWORDS = [
    "세탁기", "건조기", "드럼세탁기", "냉장고", "에어컨", "천장형에어컨", "벽걸이에어컨",
    "인덕션", "가스레인지", "가스렌지", "전자레인지", "오븐", "식기세척기",
    "TV", "와이파이", "비데", "샤워부스", "욕조",
    "침대", "책상", "옷장", "신발장", "붙박이장", "싱크대", "화장대",
    "엘리베이터", "주차", "오토바이주차", "베란다", "발코니", "테라스",
]


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

    # Batch-fetch details via GraphQL with a thread pool (8 parallel sessions).
    # Each worker creates its own requests.Session to stay thread-safe.
    details_map: dict[str, dict[str, str]] = {}
    if not args.skip_detail and all_raw:
        article_ids_ordered = [article_id_from_url(l.get("webUrl", "")) for l in all_raw]

        def _fetch_detail(aid: str) -> tuple[str, dict[str, str]]:
            s = requests.Session()
            return aid, get_daangn_article_detail_gql(s, aid)

        total_d = len(article_ids_ordered)
        print(f"[crawl:daangn] gql_detail_fetch articles={total_d} workers={DAANGN_GQL_WORKERS}", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=DAANGN_GQL_WORKERS) as pool:
            for aid, det in pool.map(_fetch_detail, article_ids_ordered):
                details_map[aid] = det
                done += 1
                if done % CRAWL_DETAIL_PROGRESS_EVERY == 0:
                    print(f"[crawl:daangn] detail_progress={done}/{total_d}", flush=True)
        print(f"[crawl:daangn] detail_done={total_d}", flush=True)

    records: list[dict[str, Any]] = []
    for listing in all_raw:
        article_id = article_id_from_url(listing.get("webUrl", ""))
        trades = listing.get("trades") or []
        trade = next((t for t in trades if t.get("type") == "MONTH"), {})
        detail = details_map.get(article_id, {}) if not args.skip_detail else {}
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


def _daangn_detail_blank() -> dict[str, str]:
    return {
        "lat": "", "lon": "", "publicAddress": "", "roomCnt": "",
        "bathroomCnt": "", "approvalDate": "", "writerType": "", "agencyName": "",
        "publishedAt": "", "updatedAt": "", "direction": "", "parking": "",
        "elevator": "", "petAllowed": "", "loanAvailable": "", "moveIn": "",
        "maintenanceDetail": "", "maintenanceBasis": "", "maintenanceItems": "",
        "buildingUse": "", "description": "", "options": "",
    }


def get_daangn_article_detail_gql(
    session: requests.Session, article_id: str
) -> dict[str, str]:
    """Fetch Daangn article details via GraphQL only (no SSR HTML parse).

    Discovered 2026-05-28: ArticleDetailQuery returns all structured fields
    (options, moveInDate, buildingOrientation, totalManageCost, etc.) directly
    as JSON, which is faster and richer than SSR HTML scraping.
    """
    payload = {
        "operationName": "ArticleDetailQuery",
        "variables": {"articleId": article_id},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": DAANGN_ARTICLE_DETAIL_QUERY_HASH,
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
        article = (resp.json().get("data") or {}).get("articleByOriginalArticleId") or {}
    except Exception as exc:
        print(f"WARNING: GQL detail {article_id}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(article, dict) or not article:
        return {}

    detail = _daangn_detail_blank()

    coord = article.get("publicCoordinate") or {}
    if isinstance(coord, dict):
        detail["lat"] = to_text(coord.get("lat", ""))
        detail["lon"] = to_text(coord.get("lon", ""))

    detail["publicAddress"] = to_text(article.get("publicAddress", "")).strip()
    detail["roomCnt"] = to_text(article.get("roomCnt", ""))
    detail["bathroomCnt"] = to_text(article.get("bathroomCnt", ""))
    detail["approvalDate"] = to_text(article.get("buildingApprovalDate", ""))
    detail["writerType"] = to_text(article.get("writerTypeV2", ""))
    detail["publishedAt"] = to_text(article.get("publishedAt", ""))
    detail["updatedAt"] = to_text(article.get("updatedAt", ""))
    detail["description"] = to_text(article.get("content", "")).strip()

    biz = article.get("bizProfile") if isinstance(article.get("bizProfile"), dict) else {}
    detail["agencyName"] = to_text(first(biz, ["name", "businessCompanyName"])).strip()

    orientation = to_text(article.get("buildingOrientation", ""))
    detail["direction"] = DAANGN_ORIENTATION_MAP.get(orientation, "")

    usage = to_text(article.get("buildingUsage", ""))
    detail["buildingUse"] = DAANGN_BUILDING_USAGE_MAP.get(usage, "")

    move_in = to_text(article.get("moveInDate", ""))
    if move_in:
        detail["moveIn"] = move_in
    elif article.get("moveInDateNegotiable"):
        detail["moveIn"] = "협의"

    manage_desc = to_text(article.get("manageCostDescription", ""))
    if manage_desc:
        detail["maintenanceDetail"] = manage_desc

    detail["maintenanceBasis"] = to_text(article.get("manageCostChargeType", ""))

    include_opts = article.get("includeManageCostOptionV3") or []
    if include_opts:
        items = [
            DAANGN_MANAGE_COST_OPTION_MAP.get(o.get("option", ""), o.get("option", ""))
            for o in include_opts if isinstance(o, dict)
        ]
        detail["maintenanceItems"] = "; ".join(filter(None, items))

    options_list = article.get("options") or []
    yes_labels: list[str] = []
    for opt in options_list:
        if not isinstance(opt, dict):
            continue
        name, value = opt.get("name", ""), opt.get("value", "")
        if name == "PARKING":
            detail["parking"] = "가능" if value == "YES" else ("불가능" if value == "NO" else "")
        elif name == "ELEVATOR":
            detail["elevator"] = "있음" if value == "YES" else ("없음" if value == "NO" else "")
        elif name == "PET":
            detail["petAllowed"] = "가능" if value == "YES" else ("불가능" if value == "NO" else "")
        elif name == "MORTGAGE":
            detail["loanAvailable"] = "가능" if value == "YES" else ("불가능" if value == "NO" else "")
        if value == "YES":
            yes_labels.append(DAANGN_OPTION_LABEL_MAP.get(name, name))
    detail["options"] = "; ".join(yes_labels)

    return detail


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


