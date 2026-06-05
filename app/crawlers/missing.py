"""Missing-listing detection, retry, and finalization logic."""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from app.crawlers._utils import (
    print, env_int, env_float,
    nested, to_text,
    MISSING_PROBE_ATTEMPTS, MISSING_PROBE_DELAY_SECONDS,
    NAVER_MISSING_PROBE_DELAY_SECONDS, NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS,
    RETRY_DEFERRED_EXIT,
    _reconcile_after_crawl,
)
from app.crawlers.naver import (
    _retry_after_seconds, ProbeRateLimited, _probe_naver_missing,
    naver_article_url, clean_headers,
)
from app.crawlers.dabang import _dabang_room_id, _probe_dabang_missing
from app.crawlers.zigbang import _probe_zigbang_missing
from app.crawlers.daangn import _probe_daangn_missing

def finalize_missing(args: argparse.Namespace) -> None:
    """Finalize the in-schedule missing retry queue for selected platforms."""
    try:
        from app.db import session
        from reconcile import finalize_missing_queue
    except ImportError as exc:
        raise RuntimeError(f"finalize-missing unavailable: {exc}") from exc
    finalized_at = datetime.now(timezone.utc)
    with session() as conn:
        count = finalize_missing_queue(
            conn,
            args.platform,
            finalized_at,
            dry_run_webhooks=args.dry_run_webhooks,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(
        f"[reconcile] finalize-missing platforms={','.join(args.platform)} "
        f"removed={count} dry_run={args.dry_run}",
        flush=True,
    )


def _read_db_missing_candidates(platform_codes: list[str]) -> list[dict[str, Any]]:
    """Find one row per listing that has at least one region marking it 'missing'.

    Post-migration 012 the missing queue lives in listing_regions, not
    listings — a listing missed by ERICA's crawl gets lr.current_status
    flipped to 'missing' while listings.current_status stays put (because
    AJOU's view might still be active). The retry-missing cron has to
    look at the per-region rows or it would skip every freshly-flagged
    item.

    We DISTINCT on listing_id because the probe is a platform-level HTTP
    check (does this URL still exist?) — running it twice for the same
    listing because two regions both flagged it 'missing' is wasted work.
    Plus a legacy DISTINCT branch for pre-migration items still flagged
    in listings.current_status.
    """
    from app.db import session  # type: ignore

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.code AS platform_code,
                l.platform_listing_id, l.source_url, l.current_status,
                COALESCE((
                    SELECT MAX(miss_count) FROM listing_regions
                    WHERE listing_id = l.id AND current_status = 'missing'
                ), l.miss_count) AS miss_count,
                s.title, s.description, s.room_type_raw, s.address_raw,
                s.lat, s.lng,
                s.deposit_won, s.monthly_rent_won, s.maintenance_fee_won,
                s.expected_monthly_cost_won,
                s.supply_area_m2, s.exclusive_area_m2, s.area_raw,
                s.floor_raw, s.room_count, s.bathroom_count,
                s.direction, s.parking_raw, s.move_in_raw,
                s.approval_date, s.building_usage, s.structure_type,
                s.raw_normalized_json
            FROM listings l
            JOIN platforms p ON p.id = l.platform_id
            JOIN LATERAL (
                SELECT *
                FROM listing_snapshots
                WHERE listing_id = l.id
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
            ) s ON TRUE
            WHERE p.code = ANY(%s)
              AND (
                  l.current_status = 'missing'
                  OR EXISTS (
                      SELECT 1 FROM listing_regions
                      WHERE listing_id = l.id AND current_status = 'missing'
                  )
              )
            ORDER BY p.code, l.id
            """,
            (platform_codes,),
        )
        candidates: list[dict[str, Any]] = []
        for row in cur.fetchall():
            csv_row = _db_row_to_csv_shape(row)
            csv_row["_platform_code"] = row["platform_code"]
            csv_row["_miss_count"] = row["miss_count"]
            candidates.append(csv_row)
        return candidates



def _probe_missing_row_once(session: requests.Session, row: dict[str, Any]) -> bool | None:
    platform = row.get("_platform_code")
    if platform == "dabang":
        return _probe_dabang_missing(session, row)
    if platform == "zigbang":
        return _probe_zigbang_missing(session, row)
    if platform == "daangn":
        return _probe_daangn_missing(session, row)
    if platform == "naver_land":
        return _probe_naver_missing(session, row)
    return None


def _missing_probe_delay(platform: str, attempt: int, default_delay_s: float, naver_delay_s: float) -> float:
    base = naver_delay_s if platform == "naver_land" else default_delay_s
    return max(0.0, base * attempt)


def _probe_missing_row(
    session: requests.Session,
    row: dict[str, Any],
    attempts: int,
    default_delay_s: float,
    naver_delay_s: float,
    rate_limit_cooldown_s: float,
) -> bool:
    platform = str(row.get("_platform_code") or "")
    listing_no = str(row.get("listing_no") or row.get("room_id") or "").strip()
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            result = _probe_missing_row_once(session, row)
        except ProbeRateLimited as exc:
            wait_s = exc.retry_after_s if exc.retry_after_s is not None else rate_limit_cooldown_s
            if attempt < max_attempts:
                print(
                    f"[reconcile] retry-missing {platform}:{listing_no} "
                    f"probe=rate-limited attempt={attempt}/{max_attempts} wait={wait_s:.1f}s",
                    flush=True,
                )
                if wait_s:
                    time.sleep(wait_s)
                continue
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                f"probe=rate-limited attempts={max_attempts}; deferring batch",
                flush=True,
            )
            raise
        if result is not None:
            return result
        if attempt < max_attempts:
            wait_s = _missing_probe_delay(platform, attempt, default_delay_s, naver_delay_s)
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                f"probe=unknown attempt={attempt}/{max_attempts} wait={wait_s:.1f}s",
                flush=True,
            )
            if wait_s:
                time.sleep(wait_s)
    print(
        f"[reconcile] retry-missing {platform}:{listing_no} "
        f"probe=no-data attempts={max_attempts}; treating as absent",
        flush=True,
    )
    return False


def retry_missing(args: argparse.Namespace) -> int:
    """Probe only listings already marked missing and update that retry queue."""
    try:
        from app.db import session
        from reconcile import reconcile_missing_probe
    except ImportError as exc:
        raise RuntimeError(f"retry-missing unavailable: {exc}") from exc

    platform_codes = list(dict.fromkeys(args.platform))
    candidates = _read_db_missing_candidates(platform_codes)
    print(
        f"[reconcile] retry-missing platforms={','.join(platform_codes)} "
        f"candidates={len(candidates)} dry_run={args.dry_run}",
        flush=True,
    )
    by_platform: dict[str, dict[str, Any]] = {
        code: {"found": [], "probed": [], "unknown": 0, "rate_limited": 0}
        for code in platform_codes
    }
    http = requests.Session()
    probe_attempts = max(1, int(args.probe_attempts))
    probe_delay_s = max(0.0, float(args.probe_delay_seconds))
    naver_probe_delay_s = max(0.0, float(args.naver_probe_delay_seconds))
    naver_rate_limit_cooldown_s = max(0.0, float(args.naver_rate_limit_cooldown_seconds))
    deferred_by_rate_limit = False
    for idx, row in enumerate(candidates, 1):
        platform = str(row.get("_platform_code") or "")
        listing_no = str(row.get("listing_no") or "")
        bucket = by_platform.setdefault(platform, {"found": [], "probed": [], "unknown": 0, "rate_limited": 0})
        try:
            result = _probe_missing_row(
                http,
                row,
                attempts=probe_attempts,
                default_delay_s=probe_delay_s,
                naver_delay_s=naver_probe_delay_s,
                rate_limit_cooldown_s=naver_rate_limit_cooldown_s,
            )
        except ProbeRateLimited:
            bucket["unknown"] += 1
            bucket["rate_limited"] += 1
            deferred_by_rate_limit = True
            print(
                f"[reconcile] retry-missing {platform}:{listing_no} "
                "batch_deferred=rate_limited; leaving unresolved missing rows queued",
                flush=True,
            )
            break
        bucket["probed"].append(listing_no)
        if result:
            bucket["found"].append(row)
        if idx % 25 == 0:
            print(f"[reconcile] retry-missing progress={idx}/{len(candidates)}", flush=True)

    retry_at = datetime.now(timezone.utc)
    with session() as conn:
        for platform_code in platform_codes:
            bucket = by_platform.get(platform_code) or {"found": [], "probed": [], "unknown": 0, "rate_limited": 0}
            if not bucket["probed"]:
                print(
                    f"[reconcile] retry-missing {platform_code}: probed=0 "
                    f"found=0 unknown={bucket['unknown']} rate_limited={bucket['rate_limited']}",
                    flush=True,
                )
                continue
            summary = reconcile_missing_probe(
                conn,
                platform_code,
                bucket["found"],
                bucket["probed"],
                retry_at,
                target_area=os.environ.get("RENTMAP_AREA_NAME") or None,
                dry_run_webhooks=args.dry_run_webhooks,
            )
            print(
                f"[reconcile] retry-missing {platform_code}: "
                f"probed={len(bucket['probed'])} found={len(bucket['found'])} "
                f"missing={summary.missing} removed={summary.removed} "
                f"unchanged={summary.unchanged} price={summary.price_changed} "
                f"detail={summary.detail_changed} unknown={bucket['unknown']} "
                f"rate_limited={bucket['rate_limited']} "
                f"errors={len(summary.errors)}",
                flush=True,
            )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    return RETRY_DEFERRED_EXIT if deferred_by_rate_limit else 0


