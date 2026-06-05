"""Naver Land cortarNo auto-discovery via the region hierarchy API.

Replaces the unreliable viewport-grid → cortarNo path. The grid relies on
Naver's SPA mapping a viewport center to a single cortarNo, but that
mapping is non-deterministic and sticky — one large dong like 4127110500
(Ansan 성포동) "captures" most of the outer grid tiles, so all smaller
neighbouring dongs go undiscovered. We've seen the grid surface only
5 / 16 expected cortarNos for the ERICA area, every run.

This module walks Naver Land's region hierarchy instead:

    시도 (province)  →  시군구 (district)  →  읍면동 (neighborhood)

Naver exposes ``GET /api/regions/list?cortarNo={parent}`` which returns
the immediate children with centerLat/centerLon. We:

1. Fetch the 시도 list (empty cortarNo = root)
2. Keep 시도 whose centers are within ``radius + 80km`` of the target.
3. For each kept 시도, fetch 시군구 list and keep those within
   ``radius + 15km``.
4. For each kept 시군구, fetch 읍면동 list and keep those whose centroid
   is within ``radius + 1.5km``. Boundary dongs (centroid just outside)
   often still overlap the search disc — urban dongs are 1.5–2.5km
   across so the centroid sits ~1km from any edge. The per-listing bbox
   filter in ``crawl_naver_one`` discards genuinely out-of-area articles
   downstream, so over-inclusion at the dong level is harmless.

CRITICAL: Naver's ``/api/regions/list`` endpoint requires the same
Authorization Bearer token that ``/api/articles`` does. Plain anonymous
``urllib`` calls get a flat 429 ("TOO_MANY_REQUESTS") regardless of
request rate. We therefore expose two entry points:

* :func:`discover_cortarnos_async` — pass an ``async fetch(cortar_no)``
  callable that performs the HTTP call with proper headers. The
  intended caller is ``rentmap.py``'s ``crawl_naver_async``, where
  Playwright has already captured the article-list Authorization
  header from the live SPA session.
* :func:`discover_cortarnos` — synchronous urllib-based wrapper for
  CLI testing. Almost always fails with 429 in real use; kept only
  as a smoke-test entry point.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Awaitable, Callable

# Endpoint used by new.land.naver.com's region selector. Same hostname as
# the article-list API and behind the same auth gate (Authorization
# Bearer token set by the SPA's JS bundle).
_REGION_LIST_URL = "https://new.land.naver.com/api/regions/list"

# Headers for the urllib fallback. Real production calls go through
# Playwright and inherit the captured Authorization header from the
# live SPA session; these are only the bare-minimum that Naver's edge
# checks for unauthenticated probes (which still 429 in most cases).
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ko-KR,ko;q=0.9",
    "referer": "https://new.land.naver.com/",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

# Pacing: 500ms × ~25-30 calls = ~12-15s per discovery. Same rationale
# as the daangn finder — slow enough to never trip the WAF, paid once
# per crawl so absolute latency doesn't matter much.
_REQUEST_DELAY_S = 0.5
_REQUEST_TIMEOUT_S = 10

# When set, log every raw response body. Useful for diagnosing field
# renames if Naver rotates the API shape.
_DEBUG = bool(os.environ.get("RENTMAP_NAVER_FINDER_DEBUG"))

# Hierarchy filter margins. See module docstring for the rationale.
_SIDO_MARGIN_KM = 80.0
_SIGUNGU_MARGIN_KM = 15.0
_DONG_MARGIN_KM = 1.5

# Type alias for the fetch callable plugged into the BFS. Signature is
# ``async def fetch(cortar_no: str) -> list[dict] | None`` where None
# means "request failed" and the BFS should treat that node as leafy
# but not add it to the results.
FetchFunc = Callable[[str], Awaitable["list[dict[str, Any]] | None"]]


# ----- shared geometry helpers ----------------------------------------

def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Equirectangular approximation. Good for the sub-100km scale we
    operate at; avoids the cos/sin overhead of haversine."""
    dlat = (lat2 - lat1) * 111.0
    dlng = (lng2 - lng1) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlng * dlng)


def _region_distance_km(region: dict[str, Any], center_lat: float,
                        center_lng: float) -> float | None:
    """km from the search center to this region's centroid, or None
    if the payload lacks coordinates (so the caller can decide whether
    to skip or descend blindly)."""
    try:
        rlat = float(region["centerLat"])
        rlng = float(region["centerLon"])
    except (KeyError, TypeError, ValueError):
        return None
    return _distance_km(center_lat, center_lng, rlat, rlng)


def _margin_for_depth(depth: int) -> float:
    """``depth`` = level we're about to PROBE INTO. 1=시도, 2=시군구, etc."""
    return [_SIDO_MARGIN_KM, _SIDO_MARGIN_KM, _SIGUNGU_MARGIN_KM, 5.0, 0.0][min(depth, 4)]


# ----- async BFS — the shared core ------------------------------------

async def discover_cortarnos_async(
    fetch: FetchFunc,
    center_lat: float, center_lng: float, radius_km: float,
    *,
    delay_s: float = _REQUEST_DELAY_S,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Walk the region hierarchy and return every leaf cortarNo in bbox.

    ``fetch(cortar_no)`` is the only IO callback — pass a Playwright-
    backed fetch from the crawler, or :func:`_urllib_fetch` from the
    CLI. The BFS structure, bbox filtering, and pacing live here.

    Returns ``(sorted_cortarnos, discoveries)`` where ``discoveries``
    is a list of ``(cortarno, name)`` tuples for operator-visible
    logging.

    ``radius_km <= 0`` short-circuits to empty — caller's contract is
    that this means "skip auto-discovery for this region".
    """
    if radius_km <= 0:
        return [], []

    started = time.monotonic()
    n_calls = 0
    leaves: list[dict[str, Any]] = []
    visited: set[str] = set()

    # Seed: fetch the root (시도) list once.
    root_children = await fetch("") or []
    n_calls += 1
    await asyncio.sleep(delay_s)

    # BFS: each queue entry is (region_dict, depth_of_this_region).
    # depth 1 = 시도, 2 = 시군구 (or 시), 3 = 구 (or 동), 4 = 동, ...
    queue: deque[tuple[dict[str, Any], int]] = deque(
        (c, 1) for c in root_children
    )

    while queue:
        region, depth = queue.popleft()
        code = str(region.get("cortarNo") or "")
        if not code or code in visited:
            continue
        visited.add(code)

        # Bbox filter at the CURRENT level. If this region's centroid
        # is well outside the search area, skip — its children would
        # fan out around it and be even further away.
        dist = _region_distance_km(region, center_lat, center_lng)
        margin = _margin_for_depth(depth)
        if dist is not None and dist > radius_km + margin:
            continue

        # Probe children to determine leaf-ness. Empty = leaf (실제 동).
        children = await fetch(code) or []
        n_calls += 1
        await asyncio.sleep(delay_s)

        if not children:
            # Leaf. Add when the dong centroid lies inside
            # ``radius + _DONG_MARGIN_KM`` — strict bbox would drop
            # legitimate boundary dongs whose area still overlaps the
            # search disc (see module docstring).
            if dist is not None and dist <= radius_km + _DONG_MARGIN_KM:
                leaves.append(region)
            continue

        # Non-leaf: enqueue children. Hard cap on depth to defend
        # against an API change that returns a self-referential parent.
        if depth >= 5:
            continue
        for child in children:
            queue.append((child, depth + 1))

    elapsed = time.monotonic() - started
    cortarnos = sorted({str(d["cortarNo"]) for d in leaves if d.get("cortarNo")})
    discoveries = sorted(
        [(str(d["cortarNo"]), str(d.get("cortarName") or "?")) for d in leaves],
        key=lambda x: x[0],
    )
    print(
        f"[naver-finder] center=({center_lat:.5f},{center_lng:.5f}) "
        f"radius={radius_km}km calls={n_calls} elapsed={elapsed:.1f}s "
        f"dongs_in_bbox={len(cortarnos)}",
        flush=True,
    )
    return cortarnos, discoveries


# ----- urllib fetch (CLI fallback — usually 429s in real use) ---------

def _urllib_get_region_list(cortar_no: str = "") -> list[dict[str, Any]] | None:
    """Anonymous urllib call to /api/regions/list. Logs and returns
    None on transport error or 429."""
    url = f"{_REGION_LIST_URL}?cortarNo={cortar_no}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"[naver-finder] cortarNo={cortar_no!r} HTTP {exc.code}: "
              f"{exc.read()[:200].decode('utf-8', 'replace')}", flush=True)
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[naver-finder] cortarNo={cortar_no!r} request failed: {exc}",
              flush=True)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[naver-finder] cortarNo={cortar_no!r} non-JSON response: {exc}",
              flush=True)
        return None
    if _DEBUG:
        print(f"[naver-finder][debug] cortarNo={cortar_no!r} raw={raw[:500]}",
              flush=True)
    region_list = data.get("regionList")
    if not isinstance(region_list, list):
        return []
    return region_list


async def _urllib_fetch(cortar_no: str) -> list[dict[str, Any]] | None:
    """Async wrapper around the sync urllib call so the BFS can await it."""
    return await asyncio.to_thread(_urllib_get_region_list, cortar_no)


def discover_cortarnos(center_lat: float, center_lng: float,
                       radius_km: float) -> tuple[list[str], list[tuple[str, str]]]:
    """Synchronous CLI/standalone entry point using anonymous urllib.

    NOTE: Naver's region API rejects unauthenticated requests with HTTP
    429, so this function almost always returns 0 cortarNos in real
    usage. The crawler integrates :func:`discover_cortarnos_async`
    directly using Playwright's captured Authorization header — that's
    the only path that actually works against production Naver.

    Kept as-is for quick smoke testing of the BFS / bbox filter logic
    in isolation; an operator running the CLI test from a never-touched
    IP can occasionally get through.
    """
    return asyncio.run(discover_cortarnos_async(
        _urllib_fetch, center_lat, center_lng, radius_km,
    ))


# ----- Playwright fetch builder — what the crawler actually uses ------

def build_playwright_fetch(context: Any, headers: dict[str, str] | None) -> FetchFunc:
    """Build a fetch callable that uses Playwright's request API.

    ``context`` is an active ``playwright.async_api.BrowserContext``
    (the same one the crawler opens for the article-list pass).
    ``headers`` is the captured article-API request header set — must
    include the Naver Authorization Bearer token, otherwise the region
    endpoint 429s just like the urllib path. Pass the dict that
    ``crawl_naver_async`` already saves into ``article_headers``.
    """
    # Reuse clean_headers semantics from rentmap.py — drop pseudo
    # headers and connection-level fields that Playwright sets itself.
    blocked = {"accept-encoding", "connection", "content-length", "cookie", "host"}

    def _clean(h: dict[str, str] | None) -> dict[str, str]:
        if not h:
            return {}
        return {k: v for k, v in h.items()
                if not k.startswith(":") and k.lower() not in blocked}

    cleaned = _clean(headers)

    async def _fetch(cortar_no: str) -> list[dict[str, Any]] | None:
        url = f"{_REGION_LIST_URL}?cortarNo={cortar_no}"
        try:
            response = await context.request.get(url, headers=cleaned, timeout=15000)
        except Exception as exc:  # noqa: BLE001
            print(f"[naver-finder] cortarNo={cortar_no!r} fetch error: {exc!r}",
                  flush=True)
            return None
        if not response.ok:
            body = (await response.text())[:200]
            print(f"[naver-finder] cortarNo={cortar_no!r} HTTP {response.status}: {body}",
                  flush=True)
            return None
        try:
            data = await response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[naver-finder] cortarNo={cortar_no!r} JSON parse failed: {exc}",
                  flush=True)
            return None
        if _DEBUG:
            print(f"[naver-finder][debug] cortarNo={cortar_no!r} "
                  f"keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}",
                  flush=True)
        region_list = data.get("regionList") if isinstance(data, dict) else None
        if not isinstance(region_list, list):
            return []
        return region_list

    return _fetch


# ---- CLI test entry --------------------------------------------------
# ``python scripts/naver_region_finder.py <lat> <lng> <radius_km>``
# Sanity-checks the BFS / bbox-filter logic against the live API. Will
# usually 429 since it uses the anonymous urllib path; use the crawler-
# integrated path for production discovery.
if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) != 4:
        print("usage: naver_region_finder.py <center_lat> <center_lng> <radius_km>",
              file=sys.stderr)
        raise SystemExit(2)
    lat, lng, r = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    ids, discoveries = discover_cortarnos(lat, lng, r)
    print(f"\nDiscovered {len(ids)} cortarNos:")
    for cn, name in discoveries:
        print(f"  {cn}  {name}")
    print(f"\nComma-separated for merge-cortarnos:")
    print(",".join(ids))
