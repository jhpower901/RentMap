"""Daangn region_id auto-discovery from a coordinate + radius.

Daangn doesn't expose its region tree as a public REST endpoint, but the
realty.daangn.com SPA calls a persisted-query GraphQL mutation
(``getRegionByCoordinate``) that maps a single coordinate to one daangn
region (= name3 dong) with a stable numeric ``originalId``. The hash
identifies the query on the server side so we don't have to send the full
GraphQL text — just the hash + variables + the auth-light headers the
SPA itself uses (no login token, just origin/referer/platform).

To cover a region's full radius we sweep a small lat/lng grid and union
every distinct originalId we get back. For a 3km radius with 1km step the
sweep is 7×7=49 calls; pacing at ~250ms keeps us well under any apparent
rate limit and the whole pass finishes in ~12s.

If daangn rotates the persisted-query hash, this module will start failing
silently (200 response with ``errors`` instead of the data shape we expect)
— the caller is expected to log and fall back to the admin-fills-by-hand
flow. The hash + endpoint live as module constants so a future fix is one
edit.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Any

# Persisted-query identifier for getRegionByCoordinate. Captured from the
# realty.daangn.com SPA build current as of 2026-05-27 via Playwright
# page.route("**/graphql*"). If daangn ships a new bundle this may
# rotate; re-capture by visiting the realty page and inspecting the
# POST body of the first graphql call after page load.
_DAANGN_GRAPHQL_URL = "https://realty.kr.karrotmarket.com/graphql"
_GET_REGION_BY_COORDINATE_HASH = (
    "a76189036fe43bedc04c812f118f9d281a48fa508f8e38df6136a05cc33be35d"
)

# Headers the SPA sends. No auth token — these are the bare minimum the
# server checks before answering an anonymous coordinate→region lookup.
_DAANGN_HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "accept-language": "ko-KR",
    "origin": "https://realty.daangn.com",
    "referer": "https://realty.daangn.com/",
    "x-realty-platform": "realty-web",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

# Pacing: 250ms × ~50 calls = ~12s for a 3km radius. Conservative enough
# that we never tripped any block during testing.
_REQUEST_DELAY_S = 0.25
_REQUEST_TIMEOUT_S = 10
_GRID_STEP_KM_DEFAULT = 1.0


def get_region_by_coordinate(lat: float, lng: float) -> dict[str, Any] | None:
    """Single GraphQL call. Returns the data.getRegionByCoordinate dict or None."""
    body = json.dumps({
        "variables": {"coordinate": {"lat": f"{lat:.6f}", "lon": f"{lng:.6f}"}},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": _GET_REGION_BY_COORDINATE_HASH,
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        _DAANGN_GRAPHQL_URL, data=body, method="POST", headers=_DAANGN_HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[daangn-finder] coordinate=({lat:.5f},{lng:.5f}) request failed: {exc}",
              flush=True)
        return None
    # Persisted-query mismatch returns 200 with ``errors`` (not data) —
    # most likely cause is a SPA rebuild rotating the hash. Log loudly so
    # the operator notices.
    if "errors" in payload and not payload.get("data", {}).get("getRegionByCoordinate"):
        print(f"[daangn-finder] persisted-query error (hash may have rotated): "
              f"{payload['errors']}", flush=True)
        return None
    return (payload.get("data") or {}).get("getRegionByCoordinate")


def discover_region_ids(center_lat: float, center_lng: float, radius_km: float,
                        step_km: float = _GRID_STEP_KM_DEFAULT
                        ) -> tuple[list[int], list[tuple[int, str]]]:
    """Sweep a lat/lng grid and return every distinct daangn region_id.

    Returns ``(sorted_ids, discoveries)`` where ``discoveries`` is the
    list of ``(id, name3)`` tuples for logging — handy when the operator
    wants to confirm the auto-learned set matches their expectation
    ("did this region pick up the right dongs?").

    A radius_km <= 0 short-circuits to an empty result; the caller (region
    runner) treats that as "skip auto-learning for this region".
    """
    if radius_km <= 0:
        return [], []
    deg_per_km_lat = 1.0 / 111.0
    deg_per_km_lng = 1.0 / (111.0 * max(0.01, math.cos(math.radians(center_lat))))
    steps = max(1, math.ceil(radius_km / step_km))
    found_ids: set[int] = set()
    discoveries: list[tuple[int, str]] = []
    started = time.monotonic()
    n_calls = 0
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            lat = center_lat + i * step_km * deg_per_km_lat
            lng = center_lng + j * step_km * deg_per_km_lng
            result = get_region_by_coordinate(lat, lng)
            n_calls += 1
            if result and isinstance(result.get("originalId"), int):
                rid = result["originalId"]
                if rid not in found_ids:
                    found_ids.add(rid)
                    discoveries.append((rid, str(result.get("name3") or "?")))
            time.sleep(_REQUEST_DELAY_S)
    elapsed = time.monotonic() - started
    print(
        f"[daangn-finder] center=({center_lat:.5f},{center_lng:.5f}) "
        f"radius={radius_km}km step={step_km}km calls={n_calls} elapsed={elapsed:.1f}s "
        f"unique_regions={len(found_ids)}",
        flush=True,
    )
    return sorted(found_ids), discoveries
