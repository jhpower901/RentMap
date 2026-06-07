"""APScheduler job functions for the main server container.

Handles: missing-retry cycle, gen-web, webhook flush, region-schedule sync,
expired session cleanup. Imported by server.py's lifespan to register crons.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.crawlers import region_runner
from app.crawlers import region_scheduler_sync

ROOT = Path(__file__).resolve().parent.parent.parent
RENTMAP_CLI = ROOT / "app" / "crawlers" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
MAIN_CRAWL_PLATFORM_CODES = ("dabang", "zigbang", "daangn", "peterpan")
MISSING_RETRY_LIMIT = 2
ALLOWED_SOURCES_SERVER: tuple[str, ...] = ("all_light", "dabang", "zigbang", "daangn", "peterpan")
CRAWL_LOCK: threading.Lock = region_runner.CRAWL_LOCK

def _ts() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S")


def _run_rentmap(args: list[str], label: str, timeout_s: int) -> int | None:
    started = time.monotonic()
    command = " ".join(args)
    print(f"{_ts()} [scheduler] {label}: START rentmap {command}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            check=False,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        print(f"{_ts()} [scheduler] {label}: {status} exit={result.returncode} elapsed={elapsed:.1f}s rentmap {command}", flush=True)
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"{_ts()} [scheduler] {label}: TIMEOUT after {elapsed:.1f}s limit={timeout_s}s rentmap {command}: {exc}", flush=True)
        return None
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"{_ts()} [scheduler] {label}: ERROR after {elapsed:.1f}s rentmap {command}: {exc}", flush=True)
        return None


def _missing_queue_count(platform_codes: tuple[str, ...]) -> int:
    """Count distinct listings flagged 'missing' anywhere.

    Post-migration 012 the missing queue is per-region (listing_regions).
    We DISTINCT on l.id so the retry cycle exit condition still fires
    once we've recovered/removed every listing whose status mentions
    'missing' in either the global listings row or any region row.
    """
    try:
        from app.db import session  # noqa: WPS433
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT l.id) AS n
                FROM listings l
                JOIN platforms p ON p.id = l.platform_id
                WHERE p.code = ANY(%s)
                  AND (
                      l.current_status = 'missing'
                      OR EXISTS (
                          SELECT 1 FROM listing_regions
                          WHERE listing_id = l.id
                            AND current_status = 'missing'
                      )
                  )
                """,
                (list(platform_codes),),
            )
            return int(cur.fetchone()["n"] or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [scheduler] missing-retry: queue check failed — {exc}", flush=True)
        return 0


def run_missing_retry_cycle() -> None:
    """Probe + finalize missing listings across the lightweight sources.

    Replaces the missing-retry logic that lived inside the old hourly_crawl.
    Decoupling it from a specific crawl fire lets the region-driven
    schedules run on whatever cadence the admin chooses while we still
    drain the missing queue on a predictable hourly cadence.
    """
    if not CRAWL_LOCK.acquire(blocking=False):
        print(f"{_ts()} [scheduler] missing-retry: SKIP already running", flush=True)
        return
    try:
        _run_missing_retry_cycle_locked()
    finally:
        CRAWL_LOCK.release()


def _run_missing_retry_cycle_locked() -> None:
    missing_count = _missing_queue_count(MAIN_CRAWL_PLATFORM_CODES)
    if missing_count == 0:
        return
    print(f"{_ts()} [scheduler] missing-retry: pending={missing_count}", flush=True)
    for attempt in range(1, MISSING_RETRY_LIMIT + 1):
        command = ["retry-missing"]
        for platform_code in MAIN_CRAWL_PLATFORM_CODES:
            command.extend(["--platform", platform_code])
        exit_code = _run_rentmap(command, label=f"missing-retry-{attempt}", timeout_s=10 * 60)
        if exit_code != 0:
            return
        missing_count = _missing_queue_count(MAIN_CRAWL_PLATFORM_CODES)
        if missing_count == 0:
            run_gen_web(trigger="missing-retry-resolved")
            run_webhook_flush(trigger="missing-retry-resolved")
            return
        if attempt < MISSING_RETRY_LIMIT:
            print(
                f"{_ts()} [scheduler] missing-retry: pending={missing_count}; "
                f"probing missing listings {attempt + 1}/{MISSING_RETRY_LIMIT}",
                flush=True,
            )
    # Still pending after retries — finalize the unresolved set.
    print(
        f"{_ts()} [scheduler] missing-retry: pending={missing_count} after retries; "
        "finalizing unresolved listings",
        flush=True,
    )
    finalize_args = ["finalize-missing"]
    for platform_code in MAIN_CRAWL_PLATFORM_CODES:
        finalize_args.extend(["--platform", platform_code])
    finalize_code = _run_rentmap(finalize_args, label="missing-finalize", timeout_s=5 * 60)
    if finalize_code == 0:
        run_gen_web(trigger="missing-finalize")
        run_webhook_flush(trigger="missing-finalize")


def run_region_sync() -> None:
    """Reconcile the in-memory APScheduler jobs with DB region_schedules.

    Cheap (one indexed read + diff against the current job set). Called on a
    fixed 30s interval so an admin's PATCH on region_schedules takes effect
    quickly without the operator restarting the container.
    """
    region_scheduler_sync.sync_schedules(
        scheduler,
        allowed_sources=ALLOWED_SOURCES_SERVER,
        run_callback=region_runner.run_schedule,
        tz=TZ,
    )


def run_gen_web(trigger: str = "scheduled") -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # gen_web is fault-tolerant: missing today's CSV falls back to most recent.
    print(f"{_ts()} [scheduler] gen-web[{trigger}]: target date={today} sources=db-auto-fallback", flush=True)
    _run_rentmap(["gen-web", "--date", today], label=f"gen-web[{trigger}]", timeout_s=5 * 60)


def run_webhook_flush(trigger: str = "manual") -> None:
    """Drain pending listing_status_events to Discord after crawl completion."""
    try:
        # Local import keeps DB / requests out of server startup if the worker
        # module ever gains heavier imports.
        from app.scheduler.webhook_worker import flush_once  # noqa: WPS433 — intentional late import
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"{_ts()} [scheduler] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:
        # Worker failures must never kill the scheduler thread. Log and move on.
        print(f"{_ts()} [scheduler] webhook-flush: failed — {exc}", flush=True)


def run_expired_session_cleanup() -> None:
    """Reap rows from ``sessions`` whose ``expires_at`` has already passed.

    ``auth.lookup_session`` only deletes the one row it just touched when a
    client presents an expired token, which leaves abandoned-but-expired rows
    accumulating forever. A trivial hourly DELETE keeps the table bounded.
    """
    try:
        from app.db import session  # noqa: WPS433
        with session() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < now()")
            n = cur.rowcount or 0
        if n:
            print(f"{_ts()} [scheduler] sessions-cleanup: deleted {n} expired rows", flush=True)
    except Exception as exc:
        # Cleanup failures must never kill the scheduler thread.
        print(f"{_ts()} [scheduler] sessions-cleanup: failed — {exc}", flush=True)

