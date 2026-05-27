"""Standalone scheduler for the naver crawler.

Runs inside the playwright-based image (Dockerfile.naver). Naver is more
rate-sensitive than the other sources, so it gets its own container with a
heavier image — but the scheduling model is identical to the server
container's: a 30s DB sync loop reconciles APScheduler's job set against
``region_schedules`` rows whose source is in ``ALLOWED_SOURCES_NAVER``
(just ``naver``).

What this container additionally owns:

- Missing-retry + finalize for the ``naver_land`` platform on an hourly
  cron, separate from any region-scoped crawl.
- Webhook flush after the retry cycle (region_runner already flushes after
  a normal crawl).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
NAVER_PLATFORM_CODES = ("naver_land",)
MISSING_RETRY_LIMIT = 1
RETRY_DEFERRED_EXIT = 75
CRAWL_LOCK = threading.Lock()

# Sources this container is the sole owner of. server.py handles the
# lightweight 3-source bundle; naver here is the only thing we touch so a
# single ``region_schedules`` row is safe to read from both containers.
ALLOWED_SOURCES_NAVER: tuple[str, ...] = ("naver",)

sys.path.insert(0, str(ROOT / "scripts"))
import region_runner  # noqa: E402
import region_scheduler_sync  # noqa: E402


def _run_rentmap(args: list[str], *, timeout_s: int, label: str) -> int | None:
    """Invoke rentmap.py with the inherited env. Returns exit code or None."""
    started = time.monotonic()
    command = " ".join(args)
    print(f"[naver-scheduler] {label}: START rentmap {command}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            check=False,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        print(f"[naver-scheduler] {label}: {status} exit={result.returncode} elapsed={elapsed:.1f}s rentmap {command}", flush=True)
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"[naver-scheduler] {label}: TIMEOUT after {elapsed:.1f}s limit={timeout_s}s rentmap {command}: {exc}", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        print(f"[naver-scheduler] {label}: ERROR after {elapsed:.1f}s rentmap {command}: {exc}", flush=True)
        return None


def run_webhook_flush(trigger: str = "manual") -> None:
    """Drain pending listing_status_events to Discord after naver reconcile."""
    try:
        from webhook_worker import flush_once  # noqa: WPS433
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"[naver-scheduler] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[naver-scheduler] webhook-flush failed: {exc}", flush=True)


def _missing_queue_count(platform_codes: tuple[str, ...]) -> int:
    try:
        from db import session  # noqa: WPS433
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM listings l
                JOIN platforms p ON p.id = l.platform_id
                WHERE p.code = ANY(%s)
                  AND l.current_status = 'missing'
                """,
                (list(platform_codes),),
            )
            return int(cur.fetchone()["n"] or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[naver-scheduler] missing-retry: queue check failed — {exc}", flush=True)
        return 0


def run_naver_missing_retry_cycle() -> None:
    """Probe + finalize missing naver listings.

    Runs on its own cron decoupled from region crawl fires — region_runner
    handles the per-crawl webhook flush; this one cleans up listings that
    aged out without showing up in a fresh crawl.
    """
    if not CRAWL_LOCK.acquire(blocking=False):
        print("[naver-scheduler] missing-retry: SKIP already running", flush=True)
        return
    try:
        _run_naver_missing_retry_cycle_locked()
    finally:
        CRAWL_LOCK.release()


def _run_naver_missing_retry_cycle_locked() -> None:
    missing_count = _missing_queue_count(NAVER_PLATFORM_CODES)
    if missing_count == 0:
        return
    print(f"[naver-scheduler] missing-retry: pending={missing_count}", flush=True)
    for attempt in range(1, MISSING_RETRY_LIMIT + 1):
        retry_code = _run_rentmap(
            ["retry-missing", "--platform", "naver_land"],
            label=f"missing-retry-{attempt}",
            timeout_s=10 * 60,
        )
        if retry_code == RETRY_DEFERRED_EXIT:
            # Naver-specific exit: rate limit asked us to back off. Skip
            # finalize so the next crawl can try again before we drop these
            # listings entirely.
            print(
                "[naver-scheduler] missing-retry: deferred by Naver rate limit; "
                "will retry on the next cycle without finalizing",
                flush=True,
            )
            return
        if retry_code != 0:
            return
        missing_count = _missing_queue_count(NAVER_PLATFORM_CODES)
        if missing_count == 0:
            _run_rentmap(["gen-web"], label="missing-retry-resolved/gen-web",
                         timeout_s=5 * 60)
            run_webhook_flush(trigger="missing-retry-resolved")
            return

    # Still pending after retries — finalize.
    print(
        f"[naver-scheduler] missing-retry: pending={missing_count} after retries; "
        "finalizing unresolved listings",
        flush=True,
    )
    finalize_code = _run_rentmap(
        ["finalize-missing", "--platform", "naver_land"],
        label="missing-finalize",
        timeout_s=5 * 60,
    )
    if finalize_code == 0:
        _run_rentmap(["gen-web"], label="missing-finalize/gen-web", timeout_s=5 * 60)
        run_webhook_flush(trigger="missing-finalize")


def main() -> None:
    scheduler = BlockingScheduler(timezone=TZ)

    def run_region_sync() -> None:
        region_scheduler_sync.sync_schedules(
            scheduler,
            allowed_sources=ALLOWED_SOURCES_NAVER,
            run_callback=region_runner.run_schedule,
            tz=TZ,
        )

    # Region-driven scheduling: 30s DB sync reconciles jobs whose source
    # is 'naver'. region_runner.run_schedule is what each registered job
    # invokes when its cron matches.
    scheduler.add_job(
        run_region_sync,
        trigger=IntervalTrigger(seconds=30, timezone=TZ),
        id="region_sync_interval",
        max_instances=1,
        coalesce=True,
    )
    # Hourly missing-retry decoupled from the region-driven crawl fires.
    # :30 to interleave with the server container's missing-retry slot.
    scheduler.add_job(
        run_naver_missing_retry_cycle,
        trigger=CronTrigger(minute=30, timezone=TZ),
        id="naver_missing_retry_hourly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    # Startup kick: sync immediately so cron firings don't have to wait
    # for the first 30s interval tick.
    now = datetime.now(TZ)
    scheduler.add_job(
        run_region_sync,
        trigger="date",
        run_date=now + timedelta(seconds=5),
        id="startup_region_sync",
        max_instances=1,
        coalesce=True,
    )
    print(
        f"[naver-scheduler] started - region-driven crawl via 30s DB sync, "
        f"missing-retry at :30 hourly, allowed sources: {ALLOWED_SOURCES_NAVER}",
        flush=True,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
