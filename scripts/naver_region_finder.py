"""Naver Land cortarNo auto-discovery via the region hierarchy API.

Replaces the unreliable viewport-grid → cortarNo path. The grid relies on
Naver's SPA mapping a viewport center to a single cortarNo, but that
mapping is non-deterministic and sticky — one large dong like 4127110500
(Ansan 성포동) "captures" most of the outer grid tiles, so all smaller
neighbouring dongs go undiscovered. We've seen the grid surface only
5 / 16 expected cortarNos for the ERICA area, every run.

This module walks Naver Land's public region hierarchy instead:

    시도 (province)  →  시군구 (district)  →  읍면동 (neighborhood)

Naver exposes ``GET /api/regions/list?cortarNo={parent}`` which returns
the immediate children with centerLat/centerLon. We:

1. Fetch the 시도 list (empty cortarNo = root)
2. Keep 시도 whose centers are within ``radius + 80km`` of the target.
   (Province centers can be far from province edges — 경기도's center is
    in 의정부 but the province reaches down to 안성, 200km south.)
3. For each kept 시도, fetch 시군구 list and keep those within
   ``radius + 15km`` (city centers tend to be near the city center).
4. For each kept 시군구, fetch 읍면동 list and keep those whose centroid
   is within ``radius + 1.5km``. The leaf margin matters: a dong's
   centroid being just outside the search circle doesn't mean the dong
   itself doesn't overlap (urban dongs are 1.5–2.5km across, so the
   centroid is typically ~1km from the dong's edge). The per-listing
   bbox filter in ``crawl_naver_one`` discards out-of-area articles
   downstream anyway.

Cost: ~25-30 HTTP calls per discovery (one per node we descend into:
1 root + 1-2 시도 + 3-5 시군구 + 5-10 구/동 + 10-15 leaf 동s). With
500ms pacing, the whole sweep runs in ~12-15 seconds. That's slower
than the viewport-grid pass but deterministic — every leaf 동 whose
centroid lies within the search radius is enumerated, regardless of
which one the SPA's stateful viewport→cortarNo logic would have
picked for any particular tile.

Cached at container lifetime via ``region_runner._NAVER_LEARN_DONE`` so
the cost is paid once per container restart per region, not per crawl.

Failure modes (all non-fatal — caller falls back to whatever's already
in ``regions.naver_cortar_nos``):

- HTTP 429 / rate limit — usually means the same IP did a lot of recent
  scraping. Production hosts on their regular crawl cadence never hit
  this, but local dev machines testing in a tight loop can.
- HTTP 4xx with success=false — endpoint or auth requirement changed.
- Empty regionList — Naver rotated codes for that parent; the next
  container restart will retry.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Any

# Endpoint used by new.land.naver.com's region selector. Same hostname as
# the article-list API, so behind the same WAF / rate-limit.
_REGION_LIST_URL = "https://new.land.naver.com/api/regions/list"

# Bare-minimum headers Naver checks before answering the public region
# call. We deliberately do NOT send cookies — the public region list
# endpoint doesn't require a session, and including cookies just opens
# us up to per-session rate limits on top of the per-IP one.
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ko-KR,ko;q=0.9",
    "referer": "https://new.land.naver.com/",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

# Pacing: at 500ms × ~25-30 calls = ~12-15s for a 3km radius. Slow
# enough that we comfortably stay under any "burst" rate limit on
# production hosts (which only run this once per container restart
# per region anyway, so the absolute latency doesn't matter much).
_REQUEST_DELAY_S = 0.5
_REQUEST_TIMEOUT_S = 10

# When the operator runs the CLI test with no results, set
# ``RENTMAP_NAVER_FINDER_DEBUG=1`` to log the raw response body of
# every call. Useful when the response shape doesn't match the
# assumed ``regionList`` / ``cortarNo`` / ``centerLat`` / ``centerLon``
# fields (e.g. if Naver renames a field or wraps it in a "result"
# envelope).
_DEBUG = bool(__import__("os").environ.get("RENTMAP_NAVER_FINDER_DEBUG"))

# Margins for the hierarchical filter. 시도 centers can be very far from
# the actual edges (경기도's centroid is ~50km from its southern edge),
# so we use a generous margin at that level. 시군구 are smaller, so a
# tighter margin works.
_SIDO_MARGIN_KM = 80.0
_SIGUNGU_MARGIN_KM = 15.0

# Leaf-level margin: a dong's centroid being just outside the search
# radius doesn't mean the dong has no overlap — a typical urban dong
# is 1.5–2.5 km across, so its centroid can be ~1–1.5 km from its
# edges. Without this margin we drop boundary dongs (we saw
# 본오1동/사1동/고잔2동/선부1동 lost for ERICA every run despite all
# being within reach of the 3km circle).
#
# Including a few extra dongs is essentially free in the crawl: the
# per-listing bbox filter inside ``crawl_naver_one`` discards any
# article that lands outside the exact bbox, so over-inclusion at the
# dong level just adds a handful of HTTP calls but never pollutes the
# CSV with out-of-area listings.
_DONG_MARGIN_KM = 1.5


def _get_region_list(cortar_no: str = "") -> list[dict[str, Any]] | None:
    """One ``GET /api/regions/list?cortarNo=X`` call.

    Returns the ``regionList`` array on success, ``None`` on transport
    or parse error, and ``[]`` if the response is well-formed but the
    array is missing/empty (Naver does occasionally return a 200 with
    no children for leaf-ish codes).
    """
    url = f"{_REGION_LIST_URL}?cortarNo={cortar_no}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 429 (rate limit) is the most common failure on a dev machine
        # that just did a lot of testing. Log and bail — the caller
        # treats this as "skip auto-learn this run".
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


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Equirectangular approximation — good enough for the few-km scale
    we operate at, and avoids the cos/sin overhead of haversine for the
    handful of calls we make."""
    dlat = (lat2 - lat1) * 111.0
    dlng = (lng2 - lng1) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlng * dlng)


def _region_distance_km(region: dict[str, Any], center_lat: float,
                        center_lng: float) -> float | None:
    """Returns straight-line km from target center to region's centroid,
    or None if the region payload lacks coordinates."""
    try:
        rlat = float(region["centerLat"])
        rlng = float(region["centerLon"])
    except (KeyError, TypeError, ValueError):
        return None
    return _distance_km(center_lat, center_lng, rlat, rlng)


def discover_cortarnos(
    center_lat: float, center_lng: float, radius_km: float,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Walk the region hierarchy and return all leaf cortarNos in bbox.

    Naver's hierarchy depth isn't uniform: most cities are 3 levels
    (시도 → 시군구 → 동), but cities split into 구 like 안산시, 수원시,
    고양시 are 4 levels (시도 → 시 → 구 → 동). We handle both by
    recursing until ``_get_region_list`` returns no children — those
    are leaf entries (실제 동 단위) regardless of depth.

    The bbox-margin is loose at higher levels (시도 centroids can be
    100km from their edges) and tightens as we descend, so we don't
    burn HTTP calls walking into 충청도 when the target is in 경기도.

    Returns ``(sorted_cortarnos, discoveries)`` where ``discoveries``
    is a list of ``(cortarno, name)`` tuples for operator-visible
    logging ("did we pick up 일동, 이동, 본오동, ...?").

    ``radius_km <= 0`` short-circuits to empty — caller's contract is
    that this means "skip auto-discovery for this region".
    """
    if radius_km <= 0:
        return [], []

    from collections import deque

    started = time.monotonic()
    n_calls = 0
    leaves: list[dict[str, Any]] = []
    visited: set[str] = set()

    def _margin_for_depth(depth: int) -> float:
        # depth = level we're about to PROBE INTO (1=시도, 2=시군구, etc.).
        # Tighten as we descend so the walk doesn't fan out wastefully.
        return [_SIDO_MARGIN_KM, _SIDO_MARGIN_KM, _SIGUNGU_MARGIN_KM, 5.0, 0.0][min(depth, 4)]

    # Seed: fetch the root (시도) list once.
    root_children = _get_region_list("") or []
    n_calls += 1
    time.sleep(_REQUEST_DELAY_S)

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
        # be even further away (region.centerLat/Lon is the geographic
        # midpoint, so children fan out around it).
        dist = _region_distance_km(region, center_lat, center_lng)
        margin = _margin_for_depth(depth)
        if dist is not None and dist > radius_km + margin:
            continue

        # Probe children to determine leaf-ness. Empty = leaf (실제 동).
        children = _get_region_list(code) or []
        n_calls += 1
        time.sleep(_REQUEST_DELAY_S)

        if not children:
            # Leaf. Add when the dong centroid lies inside
            # ``radius + _DONG_MARGIN_KM`` — see _DONG_MARGIN_KM docs
            # for why a strict bbox would drop legitimate boundary
            # dongs whose area still overlaps the search disc.
            if dist is not None and dist <= radius_km + _DONG_MARGIN_KM:
                leaves.append(region)
            continue

        # Non-leaf: enqueue children for further descent. Hard cap on
        # depth to defend against an API change that returns a self-
        # referential parent.
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


# ---- CLI test entry --------------------------------------------------
# ``python scripts/naver_region_finder.py 37.2999 126.8376 3.0``
# Lets an operator verify the discovery output for a new region before
# wiring up a region row, without having to deploy.
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
