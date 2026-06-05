"""PeterPan (피터팬) crawler."""
from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON, UA, CRAWL_DETAIL_PROGRESS_EVERY,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    nested, to_text, to_number, round1, join_text_list, join_nested_text,
    first, first_deep, image_url, text_has_any,
    parse_manwon_from_text, to_iso_date, days_ago_text,
    write_csv, _fmt_bbox, _fmt_limit, _log_crawl_start, _log_crawl_done,
    float_or_inf, normalize_phone,
    _reconcile_after_crawl,
)

PETERPAN_DETAIL_WORKERS = 8

PETERPAN_COLUMNS = [
    "source", "listing_no", "url",
    "agency", "agent_name", "agent_phone", "writer_type",
    "region", "address", "road_address", "jibun_address",
    "latitude", "longitude", "title",
    "deposit_manwon", "rent_manwon", "maintenance_manwon", "total_monthly_manwon",
    "room_type", "building_form",
    "room_count", "bathroom_count",
    "area_m2", "supply_area_m2", "exclusive_area_m2",
    "floor", "direction", "parking", "elevator", "pet_allowed",
    "move_in", "approval_date",
    "published_at", "confirmed_at", "listing_age_text",
    "maintenance_detail", "maintenance_basis", "maintenance_items",
    "options", "description",
    "image_1", "image_2", "crawl_note",
]

# ─────────────────────────────────────────────────────────────────────────────
# Peterpan (https://www.peterpanz.com) — anonymous JSON API, similar pattern
# to Dabang/Zigbang. No Playwright needed.
#
# Endpoint:
#   GET https://api.peterpanz.com/houses/area/pc
#     ?filter=<filter-string>
#     &pageSize=<n>&pageIndex=<n>&filter_version=5.1&response_version=5.3
#
# The filter-string serialization is custom, decoded from app.js's
# acceptFilter()/filterResult() in peterpanz.com:
#   - "range" filters: ``key:min~max`` joined by ``||``  (lat/lng/price/size)
#   - "in" filters:    ``key;JSON-array``                (buildingType etc.)
#   - tail ``||`` is stripped
#   - the whole string is encodeURI-style: reserved chars (:, ~, ||, ;, [, ],
#     /) stay literal; only Korean values get percent-encoded
#
# IMPORTANT: the API rejects (400 / 500) when reserved chars are
# percent-encoded — we MUST keep them literal. requests' `params=` dict
# urlencodes everything, so we build the query string by hand and pass it
# as part of the URL.
#
# bbox key is ``latitude`` / ``longitude`` (NOT ``checkLatitude`` — that's
# what an earlier survey claimed, but ``check`` is only a prefix for non-
# bbox range keys like checkRealSize, checkDeposit, checkPrice).
#
# Response shape:
#   { houses: { recommend: { image: [room...] }, withoutFee: { image: [...] }, ... },
#     totalCount: N, ... }
# Each room has hidx, info.{subject,thumbnail,created_at,supplied_size,real_size},
# type.{contract_type,building_type,building_form}, price.{deposit,monthly_fee,maintenance_cost},
# floor.{target,total,floor_text}, location.{coordinate,address},
# attribute.{userType,peterVerified,safeDirectTrade}, images.S[].path,
# additional_options.{have_parking_lot,have_elevator,allow_pet,is_full_option,...}
#
# We crawl 빌라/주택, 오피스텔, 방/거실 (월세 매물 위주). 아파트는 별도 endpoint
# (/houses/area/apt/agency/pc) — 월세 비율이 매우 낮아 현재 스코프에서 제외.
# ─────────────────────────────────────────────────────────────────────────────

PETERPAN_API_LIST = "https://api.peterpanz.com/houses/area/pc"
# Korean text values for the BUILDING_TYPE enum (see peterpanz.com app.js:
# VILLA_AND_HOUSING_TEXT, OFFICETEL_TEXT, ONE_TWO_ROOM_TEXT).
# STORE_AND_OFFICE ('상가/사무실') and APARTMENT are intentionally omitted —
# we focus on residential monthly-rent listings; aparts have their own
# endpoint (/houses/area/apt/agency/pc).
PETERPAN_BUILDING_TYPES = ("빌라/주택", "오피스텔", "원/투룸")
PETERPAN_PAGE_SIZE = 50
PETERPAN_MAX_PAGES = 30  # 30 × 50 × 3 buildingTypes = ~4500 rooms cap


def _peterpan_filter_param(min_lat: float, max_lat: float,
                           min_lng: float, max_lng: float,
                           building_type: str) -> str:
    """Build the custom filter string the peterpan API expects.

    Format (see module docstring):
      latitude:<min>~<max>||longitude:<min>~<max>||buildingType;["<value>"]

    The Korean buildingType value is percent-encoded but the surrounding
    structural chars (`:`, `~`, `||`, `;`, `[`, `]`, `/`) MUST stay literal —
    if any of them gets encoded the server returns 400 (slash) or 500
    (everything else). quote(..., safe='/') matches that exactly: encode
    spaces / Korean only, keep `/` raw inside the value.
    """
    bt_encoded = quote(building_type, safe='/')
    return (
        f"latitude:{min_lat:.6f}~{max_lat:.6f}"
        f"||longitude:{min_lng:.6f}~{max_lng:.6f}"
        f'||buildingType;["{bt_encoded}"]'
    )


def _peterpan_request_json(session: requests.Session, url: str,
                           headers: dict[str, str],
                           timeout: int = 30) -> Any:
    """GET a peterpan URL without letting requests percent-encode the query.

    The peterpan filter string contains `||`, `;`, `[`, `]` as structural
    separators — encoding any of them returns 500. requests.prepare_url()
    re-quotes those by default (urllib3's requote_uri), so we override
    PreparedRequest.url with the raw string after prepare_request() builds
    everything else (headers / auth / cookies). The bypass is peterpan-only;
    other crawlers' URLs are well-formed and need the normalization.
    """
    req = requests.Request("GET", url, headers=headers)
    prepped = session.prepare_request(req)
    prepped.url = url
    resp = session.send(prepped, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.json()


def _peterpan_flatten_rooms(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The `houses` object groups rooms by recommend/direct/agency/...; flatten
    those into a single list. Group structure is `{ groupName: { image: [...] } }`."""
    houses = payload.get("houses") or {}
    if not isinstance(houses, dict):
        return []
    out: list[dict[str, Any]] = []
    for group in houses.values():
        if not isinstance(group, dict):
            continue
        rooms = group.get("image")
        if isinstance(rooms, list):
            out.extend(r for r in rooms if isinstance(r, dict))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Peterpan detail fetch
#
# The /house/{hidx} PC page is server-rendered HTML with a 249-field
# ``const house = {...};`` object inline in one of the <script> tags. That
# object carries every detail field we care about (description, direction,
# room/bathroom counts, move-in, building_date, options, maintenance items),
# so no JS engine / Playwright is needed — a single GET + regex + brace-
# matched JSON slice gets it all. The list API stays our primary source for
# enumeration; this detail pass only enriches each row.
#
# Contact info is rendered server-side too but only as a button — the
# /house-sale-query/{hidx} API exposes the agency name and a callable phone
# number (real number for agency listings, safe forward for direct
# listings). We call that endpoint per listing so we end up with the same
# agent_name / agent_phone columns the other crawlers have.
# ─────────────────────────────────────────────────────────────────────────────

PETERPAN_DETAIL_HTML_URL = "https://www.peterpanz.com/house/{hidx}"
PETERPAN_DETAIL_QUERY_URL = "https://api.peterpanz.com/house-sale-query/{hidx}"

# Match `const house = {`, `var house = {`, or `let house = {`. The actual
# pattern in the page is `const house = {...}` inside a DOMContentLoaded
# handler, but we keep the alternation in case peterpan rewrites the script
# binding name. Capture the opening `{` position so we can brace-match the
# rest of the JSON literal.
_PETERPAN_HOUSE_RE = re.compile(r"(?:const|let|var)\s+house\s*=\s*\{")


def _peterpan_extract_house_json(html: str) -> dict[str, Any] | None:
    """Find ``const house = {...}`` inline JSON and return the parsed object.

    The HTML carries the full house record as a JSON object literal
    embedded in a `<script>` tag. We locate the opening `{` of that
    literal, scan forward with a brace counter that respects string
    quoting + escapes, then ``json.loads`` the slice.

    Returns None if the marker is absent or the JSON fails to parse —
    the caller falls back to list-API-only data.
    """
    m = _PETERPAN_HOUSE_RE.search(html)
    if not m:
        return None
    start = m.end() - 1  # at the '{'
    depth = 0
    in_str = False
    esc = False
    end: int | None = None
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(html[start:end])
    except json.JSONDecodeError:
        return None


def _peterpan_fetch_detail(session: requests.Session, hidx: str,
                           headers: dict[str, str],
                           fetch_query: bool = True,
                           timeout: int = 30) -> tuple[dict[str, Any] | None,
                                                       dict[str, Any] | None,
                                                       str | None]:
    """GET the detail HTML (+ optional sale-query) for one hidx.

    Returns ``(house_detail, sale_query, error_note)``:
      - ``house_detail`` — parsed `const house = {...}` JSON or None
      - ``sale_query``   — parsed /house-sale-query/{hidx} JSON or None
      - ``error_note``   — a short string ("html_HTTP_503", "parse_failed",
                            "query_HTTP_404", ...) when something failed,
                            so callers can stamp the row's crawl_note.
    """
    html_url = PETERPAN_DETAIL_HTML_URL.format(hidx=hidx)
    detail: dict[str, Any] | None = None
    err: str | None = None
    try:
        resp = session.get(html_url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            err = f"html_HTTP_{resp.status_code}"
        else:
            resp.encoding = "utf-8"
            detail = _peterpan_extract_house_json(resp.text)
            if detail is None:
                err = "parse_failed"
    except Exception as exc:  # noqa: BLE001 — best-effort detail
        err = f"html_err_{type(exc).__name__}"

    query: dict[str, Any] | None = None
    if fetch_query:
        try:
            q_url = PETERPAN_DETAIL_QUERY_URL.format(hidx=hidx)
            q_resp = session.get(q_url, headers=headers, timeout=timeout)
            if q_resp.status_code == 200:
                q_resp.encoding = "utf-8"
                q_json = q_resp.json()
                if isinstance(q_json, dict):
                    query = q_json
            # Don't promote a sale-query miss to a row-level error: detail
                # HTML is the main payload, contact info is "nice to have."
        except Exception:  # noqa: BLE001 — best-effort contact
            pass

    return detail, query, err


_PETERPAN_DESC_OPTION_KEYWORDS = [
    "에어컨", "세탁기", "건조기", "냉장고", "인덕션", "가스레인지", "가스렌지",
    "전자레인지", "오븐", "식기세척기", "TV", "와이파이", "비데", "샤워부스", "욕조",
    "침대", "책상", "옷장", "신발장", "붙박이장", "싱크대", "화장대",
    "베란다", "발코니", "테라스",
]


def _peterpan_options_from_detail(detail: dict[str, Any], base: list[str]) -> list[str]:
    """Build the options string. Starts with the list-API-derived options
    (parking/elevator/...) and folds in keyword hits from the description,
    matching how the daangn crawler scans free-form body text."""
    opts: list[str] = list(base)
    seen = set(opts)

    def push(label: str) -> None:
        if label and label not in seen:
            opts.append(label)
            seen.add(label)

    if detail.get("is_full_option"):
        push("풀옵션")
    if detail.get("is_new_building"):
        push("신축")
    if detail.get("is_half_underground"):
        push("반지하")
    if detail.get("is_octop"):
        push("옥탑")
    if detail.get("shorterm_contract"):
        push("단기임대")
    desc = to_text(detail.get("description"))
    if desc:
        for kw in _PETERPAN_DESC_OPTION_KEYWORDS:
            if kw in desc:
                push(kw)
    return opts


def _peterpan_maintenance_text(detail: dict[str, Any]) -> tuple[str, str, str]:
    """Compose (maintenance_detail, maintenance_basis, maintenance_items) from
    the detail JSON. Peterpan exposes two parallel lists:

      - ``maintenance_included``         — items rolled into the flat fee
      - ``individual_usage_included``    — items billed separately

    We assemble a human-readable summary in ``maintenance_detail`` so the UI
    can show "포함: 인터넷; 별도: 전기,가스,수도" without further parsing.
    """
    included = to_text(detail.get("maintenance_included"))
    individual = to_text(detail.get("individual_usage_included"))
    parts: list[str] = []
    if included:
        parts.append(f"포함: {included}")
    if individual:
        parts.append(f"별도: {individual}")
    detail_text = "; ".join(parts)
    items = included or individual or ""
    return detail_text, "", items


def _peterpan_approval_date(detail: dict[str, Any]) -> str:
    """Return the building's approval date when peterpan tagged it as such.

    `building_date` carries either a 사용승인일 (approval) or 준공인가일
    (completion). We only surface it in our approval_date column when the
    type matches, to mirror what dabang/naver call 사용승인일.
    """
    type_name = to_text(detail.get("building_date_type_name"))
    if "사용승인" not in type_name:
        return ""
    raw = to_text(detail.get("building_date"))
    if not raw or raw.startswith("0000"):
        return ""
    return raw


def _peterpan_apply_detail(row: dict[str, Any],
                           detail: dict[str, Any] | None,
                           query: dict[str, Any] | None,
                           note: str | None) -> None:
    """In-place enrich ``row`` with detail-page + sale-query info.

    All inserts are merge-only — if the list API row already has a value
    we keep it, since the per-list response is the canonical "what the user
    sees in the search results" snapshot. Detail fills the empty cells.
    """
    if note:
        existing = (row.get("crawl_note") or "").strip()
        row["crawl_note"] = f"{existing}; detail:{note}" if existing else f"detail:{note}"

    if detail:
        desc = to_text(detail.get("description"))
        if desc:
            row["description"] = desc

        # parking/elevator/pet are reliably populated in the detail JSON but
        # come back as 0 on the list API for every row in some areas — so we
        # overwrite the list-derived value when the detail says otherwise.
        if detail.get("have_parking_lot"):
            row["parking"] = "가능"
        if detail.get("have_elevator"):
            row["elevator"] = "있음"
        if detail.get("allow_pet"):
            row["pet_allowed"] = "가능"

        direction = to_text(detail.get("direction"))
        if direction:
            base = to_text(detail.get("directionBaseName"))
            row["direction"] = f"{direction} ({base})" if base else direction

        rc = detail.get("bedroom_count")
        if isinstance(rc, (int, float)) and rc > 0:
            row["room_count"] = int(rc)
        bc = detail.get("bathroom_count")
        if isinstance(bc, (int, float)) and bc > 0:
            row["bathroom_count"] = int(bc)

        move = to_text(detail.get("move_text")) or to_text(detail.get("move_type_string"))
        if move:
            row["move_in"] = move

        approval = _peterpan_approval_date(detail)
        if approval:
            row["approval_date"] = approval

        verified = to_text(detail.get("verified_date"))
        if verified and not verified.startswith("0000"):
            row["confirmed_at"] = verified

        road = to_text(detail.get("road_address"))
        if road:
            row["road_address"] = road
        jibun = to_text(detail.get("jibun_address"))
        if jibun:
            row["jibun_address"] = jibun
        # Prefer road_address as the public-facing address when the list-API
        # one was dong-only (no number). We don't overwrite a longer string.
        existing_addr = to_text(row.get("address"))
        if road and len(road) > len(existing_addr):
            row["address"] = road

        # Maintenance breakdown
        m_detail, m_basis, m_items = _peterpan_maintenance_text(detail)
        if m_detail:
            row["maintenance_detail"] = m_detail
        if m_basis:
            row["maintenance_basis"] = m_basis
        if m_items:
            row["maintenance_items"] = m_items

        # Re-derive options now that we have the description text + extra flags
        base_opts = (row.get("options") or "").split("; ") if row.get("options") else []
        base_opts = [o for o in base_opts if o]
        enriched = _peterpan_options_from_detail(detail, base_opts)
        if enriched:
            row["options"] = "; ".join(enriched)

    if query:
        name = to_text(query.get("name"))
        phone = to_text(query.get("mobile_phone"))
        agency_name = to_text(query.get("agency_name"))
        if agency_name:
            row["agency"] = agency_name
        if name:
            row["agent_name"] = name
        if phone:
            row["agent_phone"] = phone


def _peterpan_room_to_row(room: dict[str, Any]) -> dict[str, Any] | None:
    """Map one peterpan room JSON → CSV row dict. Returns None if the row is
    missing essential fields (hidx, lat/lng) or isn't a 월세 listing."""
    hidx = to_text(room.get("hidx"))
    if not hidx:
        return None

    info = room.get("info") or {}
    type_data = room.get("type") or {}
    price = room.get("price") or {}
    floor_data = room.get("floor") or {}
    loc = room.get("location") or {}
    coord = loc.get("coordinate") or {}
    addr = loc.get("address") or {}
    attr = room.get("attribute") or {}
    additional = room.get("additional_options") or {}

    # Filter to monthly rent. Peterpan contract_type values include 월세, 전세, 매매.
    contract = to_text(type_data.get("contract_type"))
    if "월세" not in contract:
        return None

    lat = to_number(coord.get("latitude"))
    lng = to_number(coord.get("longitude"))
    if lat is None or lng is None:
        return None

    user_type = to_text(attr.get("userType"))
    agency_label = "DIRECT" if user_type == "user" else (to_text(attr.get("agencyName")) or "BROKER")

    # Peterpan returns prices in WON (e.g. deposit=100000000 for 1억). We
    # store 만원 throughout the pipeline (matches CSV column names + DB +
    # web filters), so divide by 10000. round1 collapses trailing .0s.
    # 0 is a valid value (전세 매물 → monthly_fee=0) — keep it; only None
    # collapses to None so downstream "missing data" filters still work.
    def _to_manwon(value):
        n = to_number(value)
        if n is None:
            return None
        return round1(n / 10000)

    deposit_man = _to_manwon(price.get("deposit"))
    rent_man = _to_manwon(price.get("monthly_fee"))
    maint_man = _to_manwon(price.get("maintenance_cost"))
    total = None
    if rent_man is not None:
        total = round1(rent_man + (maint_man or 0))

    # Address concat: sido + sigungu + dong + (optional jibun number)
    address_parts = [to_text(addr.get(k)) for k in ("sido", "sigungu", "dong") if addr.get(k)]
    jibun = to_text(addr.get("jibun") or addr.get("addressNumber"))
    if jibun:
        address_parts.append(jibun)
    address_str = " ".join(p for p in address_parts if p).strip()

    # Floor: prefer floor_text ("3/15"), else build from target/total
    floor_text = to_text(floor_data.get("floor_text"))
    if not floor_text:
        tgt = floor_data.get("target")
        tot = floor_data.get("total")
        if tgt is not None and tot is not None:
            floor_text = f"{tgt}/{tot}"

    images = []
    raw_images = (room.get("images") or {}).get("S")
    if isinstance(raw_images, list):
        for img in raw_images:
            if isinstance(img, dict) and img.get("path"):
                images.append(to_text(img["path"]))
    img1 = images[0] if len(images) > 0 else ""
    img2 = images[1] if len(images) > 1 else ""

    options_parts: list[str] = []
    if additional.get("have_parking_lot"): options_parts.append("주차")
    if additional.get("have_elevator"): options_parts.append("엘리베이터")
    if additional.get("is_full_option"): options_parts.append("풀옵션")
    if additional.get("allow_pet"): options_parts.append("반려동물")
    if attr.get("peterVerified"): options_parts.append("피터팬확인")
    if attr.get("safeDirectTrade"): options_parts.append("안심직거래")
    if attr.get("withoutFee"): options_parts.append("중개수수료없음")

    published_at = to_text(info.get("created_at"))
    # days_ago_text only accepts YYYY-MM-DD; peterpan publishes timestamps as
    # `YYYY-MM-DD HH:MM:SS`, so slice off the time portion before formatting.
    age_date = published_at[:10] if published_at else ""

    # Pre-populate all detail-only columns with "" so the CSV writer never
    # KeyErrors when a row skipped the detail pass (e.g. fetch failed).
    return {
        "source": "peterpan",
        "listing_no": hidx,
        "url": f"https://www.peterpanz.com/house/{hidx}",
        "agency": agency_label,
        "agent_name": "",
        "agent_phone": "",
        "writer_type": user_type,
        "region": to_text(addr.get("dong")),
        "address": address_str,
        "road_address": "",
        "jibun_address": "",
        "latitude": lat,
        "longitude": lng,
        "title": to_text(info.get("subject")),
        "deposit_manwon": deposit_man if deposit_man is not None else "",
        "rent_manwon": rent_man if rent_man is not None else "",
        "maintenance_manwon": maint_man if maint_man is not None else "",
        "total_monthly_manwon": total if total is not None else "",
        "room_type": to_text(type_data.get("building_form")) or to_text(type_data.get("building_type")),
        "building_form": to_text(type_data.get("building_form")),
        "room_count": "",
        "bathroom_count": "",
        "area_m2": to_text(info.get("supplied_size") or info.get("real_size") or ""),
        "supply_area_m2": to_text(info.get("supplied_size") or ""),
        "exclusive_area_m2": to_text(info.get("real_size") or ""),
        "floor": floor_text,
        "direction": "",
        "parking": "가능" if additional.get("have_parking_lot") else "",
        "elevator": "있음" if additional.get("have_elevator") else "",
        "pet_allowed": "가능" if additional.get("allow_pet") else "",
        "move_in": "",
        "approval_date": "",
        "published_at": published_at,
        "confirmed_at": "",
        "listing_age_text": days_ago_text(age_date) if age_date else "",
        "maintenance_detail": "",
        "maintenance_basis": "",
        "maintenance_items": "",
        "options": "; ".join(options_parts),
        "description": "",
        "image_1": img1,
        "image_2": img2,
        "crawl_note": "",
    }


def crawl_peterpan(args: argparse.Namespace) -> None:
    started = time.monotonic()
    _log_crawl_start(
        "peterpan",
        args,
        extra=(
            f"source=peterpan-api "
            f"max_deposit={_fmt_limit(args.max_deposit)} max_rent={_fmt_limit(args.max_rent)}"
        ),
    )
    session = requests.Session()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UA,
        "Referer": "https://www.peterpanz.com/villa",
        "Origin": "https://www.peterpanz.com",
        "Cache-Control": "no-cache",
    }

    raw_rooms: list[dict[str, Any]] = []
    seen_hidx: set[str] = set()

    for bt in PETERPAN_BUILDING_TYPES:
        page = 1
        while page <= PETERPAN_MAX_PAGES:
            filter_str = _peterpan_filter_param(args.min_lat, args.max_lat,
                                                args.min_lng, args.max_lng, bt)
            # Build the query by hand: urlencode() would percent-encode the
            # ':', '||', ';', '[', ']', '/' inside `filter_str` and the API
            # returns 400/500 for any of those. `filter_str` already has the
            # Korean value escaped (done in _peterpan_filter_param).
            # No `order_by`: 'newest' returns 500 (not a valid enum), 'random'
            # would break pagination dedup. Server's default order is fine —
            # we dedup by hidx anyway.
            query = (
                f"filter={filter_str}"
                f"&pageSize={PETERPAN_PAGE_SIZE}"
                f"&pageIndex={page}"
                "&filter_version=5.1"
                "&response_version=5.3"
            )
            url = f"{PETERPAN_API_LIST}?{query}"
            try:
                payload = _peterpan_request_json(session, url, headers)
            except Exception as exc:
                print(f"[crawl:peterpan] fetch failed bt={bt} page={page}: {exc}", flush=True)
                break

            rooms = _peterpan_flatten_rooms(payload)
            if not rooms:
                break

            new_rooms = []
            for r in rooms:
                hidx = to_text(r.get("hidx"))
                if hidx and hidx not in seen_hidx:
                    seen_hidx.add(hidx)
                    new_rooms.append(r)
            raw_rooms.extend(new_rooms)
            print(f"[crawl:peterpan] bt={bt} page={page} fetched={len(rooms)} new={len(new_rooms)} total={len(raw_rooms)}",
                  flush=True)

            # Stop paginating when the page wasn't full (no more results)
            if len(rooms) < PETERPAN_PAGE_SIZE:
                break
            page += 1
            time.sleep(0.15)

    if not raw_rooms:
        raise RuntimeError("No Peterpan rooms found in the requested bbox.")

    records: list[dict[str, Any]] = []
    for room in raw_rooms:
        row = _peterpan_room_to_row(room)
        if not row:
            continue
        # Price-cap filter (client-side; the API doesn't honor a filter param)
        dep = row.get("deposit_manwon")
        rent = row.get("rent_manwon")
        if isinstance(dep, (int, float)) and args.max_deposit and dep > args.max_deposit:
            continue
        if isinstance(rent, (int, float)) and args.max_rent and rent > args.max_rent:
            continue
        records.append(row)

    # Detail enrichment pass: parallel HTML + house-sale-query fetch per
    # row. Skipped entirely when --no-detail is set, so a peterpan-side
    # block on the detail page can be worked around without losing the
    # base list-API crawl.
    fetch_detail = not getattr(args, "no_detail", False)
    if fetch_detail and records:
        detail_headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.peterpanz.com/villa",
        }
        # New session — the JSON list API stuffs a few cookies in via
        # request_json that the HTML endpoint doesn't need, and isolating
        # the session avoids accidental coupling.
        det_session = requests.Session()

        def _job(rec: dict[str, Any]) -> tuple[str, dict[str, Any] | None,
                                               dict[str, Any] | None, str | None]:
            hidx = to_text(rec.get("listing_no"))
            # Sale-query is always useful: it provides agent_name even for
            # user listings, and the agency name + real phone for agency
            # listings. ~500-byte response, cheap.
            detail, query, err = _peterpan_fetch_detail(
                det_session, hidx, detail_headers, fetch_query=True,
            )
            return hidx, detail, query, err

        print(f"[crawl:peterpan] detail_fetch rooms={len(records)} workers={PETERPAN_DETAIL_WORKERS}",
              flush=True)
        results: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]] = {}
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=PETERPAN_DETAIL_WORKERS) as pool:
            futures = [pool.submit(_job, r) for r in records]
            for fut in as_completed(futures):
                hidx, detail, query, err = fut.result()
                results[hidx] = (detail, query, err)
                done_cnt += 1
                if done_cnt % CRAWL_DETAIL_PROGRESS_EVERY == 0:
                    print(f"[crawl:peterpan] detail_progress={done_cnt}/{len(records)}", flush=True)
        ok = sum(1 for (d, _, e) in results.values() if d is not None)
        print(f"[crawl:peterpan] detail_done={done_cnt} ok={ok} miss={done_cnt - ok}", flush=True)

        for row in records:
            detail, query, err = results.get(to_text(row.get("listing_no")), (None, None, None))
            _peterpan_apply_detail(row, detail, query, err)

    records.sort(key=lambda r: (
        to_text(r.get("region")),
        float_or_inf(r.get("total_monthly_manwon")),
        float_or_inf(r.get("rent_manwon")),
    ))
    write_csv(Path(args.output_csv), records, PETERPAN_COLUMNS)
    _reconcile_after_crawl("peterpan", records, "peterpan")
    _log_crawl_done("peterpan", len(records), args.output_csv, time.monotonic() - started)


