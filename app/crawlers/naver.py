"""Naver Land crawler."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse, parse_qs, urlunparse

import requests

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON, UA, CRAWL_DETAIL_PROGRESS_EVERY,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    nested, to_text, to_number, round1, join_text_list,
    first, first_deep, image_url,
    parse_manwon_from_text, extract_naver_maintenance_amount, to_iso_date, days_ago_text, split_area_pair,
    write_csv, request_json, _fmt_bbox, _fmt_limit, _log_crawl_start, _log_crawl_done,
    float_or_inf, normalize_phone,
    _reconcile_after_crawl,
)

NAVER_TILE_STEP_KM = 1.2
NAVER_ZOOM = 16
NAVER_DEFAULT_PARAMS = (
    "a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL"
    "&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM"
)
NAVER_PAGE_DELAY_MS = 250
NAVER_DETAIL_DELAY_MS = 250
NAVER_DETAIL_RETRIES = 2
NAVER_PROGRESS_EVERY = 50
NAVER_DEFAULT_MAX_PAGES = 20
NAVER_LIST_RETRIES = 2
NAVER_RATE_LIMIT_STATUS = 429
NAVER_TRANSIENT_STATUS_CODES = {429, 503}
NAVER_LIST_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
NAVER_DETAIL_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
NAVER_RATE_POLICY_STREAK_THRESHOLD = 3

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


async def _fetch_naver_cortarno(
    context: Any,
    headers: dict[str, str] | None,
    center_lat: float,
    center_lng: float,
    zoom: int = NAVER_ZOOM,
) -> str:
    """Look up the dong cortarNo for a given map centre via the /api/cortars endpoint.

    Discovered 2026-05-28: the browser fires this API on every map viewport change.
    Using it directly replaces a full page.goto() browser navigation and lets us
    discover all grid cortarNos with simple HTTP calls after loading the home page once.
    Returns an empty string on failure.
    """
    url = (
        f"https://new.land.naver.com/api/cortars"
        f"?zoom={zoom}&centerLat={center_lat}&centerLon={center_lng}"
    )
    try:
        resp = await context.request.get(url, headers=clean_headers(headers), timeout=15000)
        if resp.ok:
            data = await resp.json()
            return to_text(data.get("cortarNo", ""))
    except Exception:
        pass
    return ""


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
    urls = args.urls or default_naver_urls(args)
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
            # Capture auth headers from ANY naver land API call so we get the JWT
            # from the home-page load itself (not just from an articles search page).
            # Discovered 2026-05-28: the home page fires /api/cortars which also
            # carries the Authorization: Bearer token.
            if "new.land.naver.com/api/" in request.url:
                try:
                    h = await request.all_headers()
                    if h.get("authorization") and article_headers is None:
                        article_headers = h
                    # Only treat articles URL as template when it carries the
                    # small-space-rent filter (our search pages do; the home page
                    # default viewport may not).
                    if "/api/articles?" in request.url and first_list_url is None:
                        if "SMALLSPCRENT" in request.url or "ONEROOM" in request.url or "aa=" in request.url:
                            article_headers = h
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
            # Grid loop: the FIRST url still needs a real page.goto() so the
            # browser fires /api/articles? and we capture the full template URL
            # (with all filter params). Every subsequent tile uses the cortars
            # API to discover its cortarNo and _paginate_naver_cortarno directly —
            # replacing a ~5s browser navigation with a ~0.5s API call.
            for idx, url in enumerate(urls, 1):
                print(f"[crawl:naver] url_progress={idx}/{len(urls)} url={url}", flush=True)
                # Fast path: once we have auth headers + a template list URL, skip
                # the browser navigation for remaining tiles and use API only.
                if idx > 1 and first_list_url and article_headers:
                    center = get_map_center(url)
                    cn = await _fetch_naver_cortarno(
                        context, article_headers,
                        center["latitude"], center["longitude"],
                    )
                    if not cn:
                        print(f"[crawl:naver] cortars_api no cortarNo for tile={idx}", file=sys.stderr, flush=True)
                        continue
                    if cn in seen_cortarnos:
                        print(f"[crawl:naver] cortars_api cortarNo={cn} already seen, skipping tile={idx}", flush=True)
                        continue
                    seen_cortarnos.add(cn)
                    payloads = await _paginate_naver_cortarno(
                        context, first_list_url, cn, article_headers, args, naver_rate_stats,
                    )
                    raw_payloads.extend(payloads)
                    new_count = 0
                    tile_center = center
                    for payload in payloads:
                        for article in payload.get("articleList") or []:
                            record = normalize_naver_article(article, first_list_url, tile_center)
                            if not bbox_ok(record.get("latitude"), record.get("longitude"), args):
                                continue
                            key = to_text(record.get("listing_no"))
                            if key and key in seen:
                                continue
                            if key:
                                seen.add(key)
                            records.append(record)
                            new_count += 1
                    print(f"[crawl:naver] cortars_api cortarNo={cn} pages={len(payloads)} new_in_bbox={new_count}", flush=True)
                    continue

                # Slow path (first tile or fallback): full browser navigation.
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
            print(f"[crawl:naver] grid_pass unique_articles={len(records)} cortarNos={len(seen_cortarnos)} payload_pages={len(raw_payloads)} fast_tiles={max(0,len(urls)-1)}", flush=True)

            # Region-hierarchy discovery: walk Naver's /api/regions/list
            # endpoint to enumerate every leaf 동 in this region's bbox.
            # Replaces relying on the viewport grid for cortarNo coverage
            # (the grid finds 5-12 of ~16 expected for a typical 3km
            # urban area — Naver's SPA picks a single dominant cortarNo
            # per tile rather than mapping coordinates 1:1 to dongs).
            #
            # MUST run after the grid pass because that's when
            # ``article_headers`` (with the Naver Authorization Bearer
            # token) gets captured. The region endpoint rejects anonymous
            # requests with 429, so without those headers the walk
            # silently returns 0 — we log loudly when that happens so
            # an operator can see why the discovery is missing.
            if article_headers and len(seen_cortarnos) > 0:
                try:
                    import naver_region_finder as _nrf  # noqa: WPS433
                    center_lat = (float(args.min_lat) + float(args.max_lat)) / 2
                    center_lng = (float(args.min_lng) + float(args.max_lng)) / 2
                    # Use the longer half-axis as the radius — the bbox
                    # is the bounding square of a circle, so any corner
                    # is sqrt(2)*radius from the center; we want enough
                    # to cover the whole inscribed circle plus a bit.
                    half_lat_km = (float(args.max_lat) - center_lat) * 111.0
                    half_lng_km = (float(args.max_lng) - center_lng) * 111.0 * math.cos(math.radians(center_lat))
                    radius_km = max(half_lat_km, half_lng_km)
                    nrf_fetch = _nrf.build_playwright_fetch(context, article_headers)
                    discovered, names = await _nrf.discover_cortarnos_async(
                        nrf_fetch, center_lat, center_lng, radius_km,
                    )
                    if discovered:
                        sample = ", ".join(f"{cn}={nm}" for cn, nm in names[:8])
                        if len(names) > 8:
                            sample += f", … (+{len(names) - 8} more)"
                        print(f"[crawl:naver] naver-finder discovered {len(discovered)} "
                              f"cortarNos: {sample}", flush=True)
                        # Union into explicit_cortarnos so the backstop
                        # below picks them up. Dedup happens naturally
                        # in the missing_cortarnos comprehension.
                        explicit_cortarnos = sorted(set(explicit_cortarnos) | set(discovered))
                except Exception as exc:  # noqa: BLE001
                    print(f"[crawl:naver] naver-finder failed: {exc!r} — "
                          f"falling back to env/DB cortarNos only", flush=True)
            elif not article_headers:
                print(f"[crawl:naver] naver-finder skipped: no article_headers captured "
                      f"(grid pass didn't yield a usable list-API URL)", flush=True)

            # Coverage backstop: paginate every cortarNo from RENTMAP_NAVER_CORTARNOS
            # (plus whatever naver-finder discovered above) that the grid
            # didn't already cover. Defends against Naver's non-deterministic
            # ms= → cortarNo mapping (the same tile can flip between dongs
            # across requests, so grid-only coverage can silently drop entire
            # dongs of listings).
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


# Zoom used when linking to a single article. /rooms?articleNo=N alone
# redirects to a viewport-only URL that drops articleNo; including an ms=
# viewport at the building's coordinates blocks that redirect so the side
# panel opens with the article selected. 17 frames a single building well.
NAVER_ARTICLE_LINK_ZOOM = 17


def naver_article_url(article_no: Any, lat: Any, lon: Any) -> str:
    """Build a Naver Land URL that opens the given article without redirect.

    Falls back to the legacy articleNo-only URL when coordinates are missing
    (Naver will redirect that one, but it's the best we can do without coords).
    Returns "" when there's no article number to link to.
    """
    if not article_no:
        return ""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return f"https://new.land.naver.com/rooms?articleNo={article_no}"
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)) or lat_f == 0 or lon_f == 0:
        return f"https://new.land.naver.com/rooms?articleNo={article_no}"
    ms = f"{encode_coord(lat_f)},{encode_coord(lon_f)},{NAVER_ARTICLE_LINK_ZOOM}"
    return (
        f"https://new.land.naver.com/rooms?ms={ms}"
        f"&{NAVER_DEFAULT_PARAMS}&articleNo={article_no}"
    )


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


def _naver_grid_center_radius(args: argparse.Namespace | None = None) -> tuple[float, float, float]:
    if args is not None:
        cr = _resolve_center_radius(args)
        if cr is not None:
            return cr
        if all(hasattr(args, name) for name in ("min_lat", "max_lat", "min_lng", "max_lng")):
            return center_radius_from_bbox(args.min_lat, args.max_lat, args.min_lng, args.max_lng)
    return (
        env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT),
        env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG),
        env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM),
    )


def default_naver_urls(args: argparse.Namespace | None = None) -> list[str]:
    """Return Naver crawl URLs.

    Priority:
    1. RENTMAP_NAVER_URLS env var (pipe-separated list of full URLs).
    2. Auto-generated grid from the active CLI bbox/center, falling back to
       RENTMAP_CENTER_LAT/LNG + RENTMAP_RADIUS_KM.
    """
    raw = os.environ.get("RENTMAP_NAVER_URLS", "").strip()
    if raw:
        # Use "|" as separator — Naver ms= URLs contain commas (ms=lat,lng,zoom)
        # so comma-splitting would corrupt them.
        urls = [u.strip() for u in raw.split("|") if u.strip()]
        if urls:
            print(f"[naver] using {len(urls)} URLs from RENTMAP_NAVER_URLS", file=sys.stderr)
            return urls
    center_lat, center_lng, radius_km = _naver_grid_center_radius(args)
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
    """Parse a Korean rent/deposit string ("1억", "1억 5,000", "5,000") into a
    만원-unit float. Returns "" when the input is empty / has no parseable
    digits — but 0 is a *valid* return (e.g. naver 전세 매물 rentPrc=0). Callers
    must distinguish 0 from "" instead of truthiness-checking the result."""
    text = re.sub(r"\s+", "", to_text(value)).replace(",", "")
    if not text:
        return ""
    eok = re.search(r"([0-9.]+)억", text)
    rest = re.sub(r"[^0-9.]", "", re.sub(r"[0-9.]+억", "", text))
    if not eok and not rest:
        return ""
    return (float(eok.group(1)) * 10000 if eok else 0) + (float(rest) if rest else 0)


def normalize_naver_article(article: dict[str, Any], source_url: str, center: dict[str, Any]) -> dict[str, Any]:
    deposit_text = first(article, ["dealOrWarrantPrc", "priceText"])
    parsed = parse_manwon(f"{first(article, ['tradeTypeName'])}{deposit_text}/{first(article, ['rentPrc'])}") or {}

    # Resolve rent — naver gives rentPrc='' or 0 for 전세/반전세 listings, but
    # the older `parsed.get('rent') or fallback` collapsed both into ""
    # because 0 is falsy. Symptom: 보증금 2억의 반전세 매물에서 월세가 빈
    # 칸으로 빠져 사용자가 "값이 안 들어와" 보고. Distinguish None (parse
    # failed) from 0 (parse succeeded with monthly-rent = 0).
    parsed_rent = parsed.get("rent")
    if parsed_rent is None:
        rent_text = to_text(first(article, ["rentPrc"])).replace(",", "").strip()
        if rent_text == "":
            # rentPrc absent — coerce to 0 when the listing carries a deposit
            # (전세/반전세 with month=0); leave "" when both are blank so the
            # downstream filter still treats it as unknown.
            rent = 0 if to_text(deposit_text) else ""
        else:
            rent = float_or_empty(rent_text)
    else:
        rent = parsed_rent

    # Same problem for deposit: parse_manwon may yield {} for malformed inputs,
    # in which case parse_amount_manwon was the fallback — but the old
    # ``parsed.get('deposit') or fallback`` ignored 0 (월세 only listings).
    parsed_deposit = parsed.get("deposit")
    if parsed_deposit is None:
        deposit_manwon = parse_amount_manwon(deposit_text)
    else:
        deposit_manwon = parsed_deposit

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
        "url": naver_article_url(article_no, lat, lon) or source_url,
        "agency": first(article, ["realtorName", "cpName"]),
        "agent_name": "",
        "agent_phone": "",
        "region": first(article, ["cityName", "divisionName", "sectionName"]),
        "address": region_addr or first(article, ["articleName", "buildingName"]),
        "latitude": lat,
        "longitude": lon,
        "address_public_level": "naver_dong_level_until_detail_enrichment",
        "title": first(article, ["articleFeatureDesc", "articleName"]),
        "deposit_manwon": deposit_manwon,
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
    maintenance_value = parse_manwon_from_text(extract_naver_maintenance_amount(maintenance_raw))
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


