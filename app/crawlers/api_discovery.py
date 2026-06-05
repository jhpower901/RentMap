#!/usr/bin/env python3
"""
API discovery script for Dabang, Daangn, and Naver Land.

Launches headless Chromium via Playwright, browses each platform,
and captures every XHR/fetch request so we can identify faster API
endpoints than the current HTML-scraping approach.

Usage:
    python scripts/api_discovery.py [dabang|daangn|naver] [--headed]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"

# ── filter patterns: skip noise ──────────────────────────────────────────────
_SKIP_URLS = re.compile(
    r"(analytics|gtm|sentry|datadog|amplitude|mixpanel|hotjar|clarity|"
    r"fonts\.|cdn\.jsdelivr|unpkg\.com|cloudflare|s3\.amazonaws|"
    r"\.woff|\.woff2|\.ttf|\.png|\.jpg|\.jpeg|\.webp|\.gif|\.svg|\.ico|"
    r"\.css|\.js\.map)",
    re.I,
)
_INTERESTING = re.compile(
    r"(api|graphql|realty|room|article|listing|search|land|naver|daangn|"
    r"dabang|zigbang|map|region|query|fetch|xhr)",
    re.I,
)


@dataclass
class ApiCall:
    method: str
    url: str
    resource_type: str
    post_data: str = ""
    status: int = 0
    content_type: str = ""
    body_preview: str = ""
    request_headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)


async def capture_site(
    site: str,
    start_url: str,
    extra_clicks: list[str] | None = None,
    wait_ms: int = 5000,
    headed: bool = False,
) -> list[ApiCall]:
    """
    Navigate to start_url, optionally click some elements,
    wait wait_ms ms, and return all captured API calls.
    """
    from playwright.async_api import async_playwright

    calls: list[ApiCall] = []
    pending: dict[str, ApiCall] = {}

    async with async_playwright() as p:
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        chrome_path = next((p for p in chrome_candidates if Path(p).exists()), None)
        launch_kwargs: dict = {
            "headless": not headed,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            locale="ko-KR",
            user_agent=UA,
            ignore_https_errors=True,
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        async def on_request(request):
            if request.resource_type not in ("xhr", "fetch", "websocket"):
                return
            url = request.url
            if _SKIP_URLS.search(url):
                return
            try:
                headers = await request.all_headers()
            except Exception:
                headers = {}
            call = ApiCall(
                method=request.method,
                url=url,
                resource_type=request.resource_type,
                post_data=request.post_data or "",
                request_headers=headers,
            )
            pending[url + request.method] = call
            calls.append(call)

        async def on_response(response):
            url = response.url
            if _SKIP_URLS.search(url):
                return
            key = url + response.request.method
            call = pending.get(key)
            if call is None:
                return
            call.status = response.status
            try:
                call.response_headers = await response.all_headers()
                ct = call.response_headers.get("content-type", "")
                call.content_type = ct
                if "json" in ct or "graphql" in ct or "text" in ct:
                    text = await response.text()
                    call.body_preview = text[:800]
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"\n[{site}] Navigating to {start_url}", flush=True)
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"[{site}] goto failed: {exc}", file=sys.stderr)

        await page.wait_for_timeout(wait_ms)

        if extra_clicks:
            for selector in extra_clicks:
                try:
                    el = page.locator(selector).first
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                except Exception as exc:
                    print(f"[{site}] click({selector}) skipped: {exc}", file=sys.stderr)

        await page.wait_for_timeout(2000)
        await browser.close()

    return calls


def report(site: str, calls: list[ApiCall]) -> None:
    print(f"\n{'='*70}")
    print(f"  {site.upper()} - {len(calls)} API calls captured")
    print(f"{'='*70}")

    interesting = [c for c in calls if _INTERESTING.search(c.url)]
    boring = [c for c in calls if not _INTERESTING.search(c.url)]

    print(f"\n--- INTERESTING ({len(interesting)}) ---")
    for c in interesting:
        print(f"\n  [{c.method}] {c.url[:120]}")
        print(f"       status={c.status}  type={c.resource_type}  ct={c.content_type[:60]}")
        if c.post_data:
            try:
                parsed = json.loads(c.post_data)
                print(f"       POST body: {json.dumps(parsed, ensure_ascii=False)[:200]}")
            except Exception:
                print(f"       POST body: {c.post_data[:200]}")
        if c.body_preview:
            preview = c.body_preview.replace("\n", " ")[:300]
            print(f"       response:  {preview}")
        # Print key auth-related request headers
        interesting_headers = {
            k: v for k, v in c.request_headers.items()
            if k.lower() in (
                "authorization", "cookie", "x-auth-token", "x-csrf-token",
                "d-api-version", "d-call-type", "csrf", "x-nhn-application-key",
                "access-token",
            )
        }
        if interesting_headers:
            print(f"       auth headers: {interesting_headers}")

    if boring:
        print(f"\n--- OTHER ({len(boring)}) skipped (use --verbose to see) ---")


async def discover_dabang(headed: bool) -> None:
    """
    Capture Dabang map API calls.
    Goal: find if detail data can be fetched in batch or without the current
    per-room /api/3/new-room/detail calls.
    """
    # Start on the map page near Ajou University
    url = "https://www.dabangapp.com/map/onetwo?m_lat=37.2772634&m_lng=127.0451149&m_zoom=18"
    calls = await capture_site("dabang", url, wait_ms=8000, headed=headed)
    report("dabang", calls)


async def discover_daangn(headed: bool) -> None:
    """
    Capture Daangn realty API calls.
    Goal: find the actual API/GraphQL queries behind the SSR listing page,
    especially any list endpoint that doesn't require SSR scraping.
    """
    # Listing page for region 1289 (우만1동 near Ajou)
    url = "https://www.daangn.com/kr/realty/?in=x-1289"
    calls = await capture_site("daangn", url, wait_ms=8000, headed=headed)
    report("daangn", calls)

    # Also check the realty map/search page
    print("\n[daangn] Also checking realty.daangn.com map page...", flush=True)
    calls2 = await capture_site("daangn-map", "https://realty.daangn.com/", wait_ms=8000, headed=headed)
    report("daangn-map", calls2)


async def discover_naver(headed: bool) -> None:
    """
    Capture Naver Land API calls.
    Goal: identify which cookies/headers are required so we can skip
    the 37-tile browser navigation grid and make direct API calls instead.
    """
    # First: home page to warm session
    calls_home = await capture_site(
        "naver-home",
        "https://new.land.naver.com/",
        wait_ms=3000,
        headed=headed,
    )
    report("naver-home", calls_home)

    # Second: actual search page to capture the articles API
    url = (
        "https://new.land.naver.com/rooms"
        "?ms=2AzVQ9,3zkrDJ,16"
        "&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL"
        "&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM"
    )
    calls_search = await capture_site("naver-search", url, wait_ms=8000, headed=headed)
    report("naver-search", calls_search)

    # Print cookies we'd need to replicate
    all_calls = calls_home + calls_search
    naver_api_calls = [c for c in all_calls if "/api/articles" in c.url]
    if naver_api_calls:
        print("\n[naver] API calls found:")
        for c in naver_api_calls:
            print(f"  {c.method} {c.url[:150]}")
            cookies = c.request_headers.get("cookie", "")
            if cookies:
                # Parse and show cookie names (not values, which may be sensitive)
                cookie_names = [p.split("=")[0].strip() for p in cookies.split(";")]
                print(f"    cookies: {cookie_names}")
            auth = c.request_headers.get("authorization", "")
            if auth:
                print(f"    authorization: {auth[:60]}")


async def main() -> None:
    p = argparse.ArgumentParser(description="API discovery for Dabang / Daangn / Naver")
    p.add_argument(
        "sites",
        nargs="*",
        choices=["dabang", "daangn", "naver", "all"],
        default=["all"],
        help="Which sites to probe (default: all)",
    )
    p.add_argument("--headed", action="store_true", help="Show browser window")
    args = p.parse_args()

    sites = set(args.sites)
    if "all" in sites:
        sites = {"dabang", "daangn", "naver"}

    tasks = []
    if "dabang" in sites:
        tasks.append(discover_dabang(args.headed))
    if "daangn" in sites:
        tasks.append(discover_daangn(args.headed))
    if "naver" in sites:
        tasks.append(discover_naver(args.headed))

    # Run sequentially to avoid resource contention
    for t in tasks:
        await t


if __name__ == "__main__":
    asyncio.run(main())
