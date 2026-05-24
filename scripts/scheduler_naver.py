"""Standalone scheduler for the naver crawler.

Runs inside the playwright-based image (Dockerfile.naver). Every hour at :00
KST, in lock-step with the main `rentmap-server` container which crawls the
other three sources at the same time. Both containers share `./data`, so
the `rentmap-server`'s gen-web cron (at :50) picks up whichever
naver CSV is currently on disk — and gen-web falls back to the most recent
naver CSV if the current run hasn't finished yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))


def run_webhook_flush(trigger: str = "manual") -> None:
    """Drain pending listing_status_events to Discord after naver reconcile."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from webhook_worker import flush_once  # noqa: WPS433
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"[naver-scheduler] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:
        print(f"[naver-scheduler] webhook-flush failed: {exc}", flush=True)


def run_naver_crawl() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    out_csv = ROOT / "data" / f"naver_land_ajou_{today}.csv"
    raw_json = ROOT / "data" / f"naver_land_ajou_{today}.raw.json"
    started = time.monotonic()
    area = os.environ.get("RENTMAP_AREA_NAME", "")
    center_lat = os.environ.get("RENTMAP_CENTER_LAT", "")
    center_lng = os.environ.get("RENTMAP_CENTER_LNG", "")
    radius_km = os.environ.get("RENTMAP_RADIUS_KM", "")
    max_deposit = os.environ.get("RENTMAP_MAX_DEPOSIT", "")
    max_rent = os.environ.get("RENTMAP_MAX_RENT", "")
    print(
        "[naver-scheduler] crawl-naver START "
        f"date={today} area={area or '-'} center={center_lat},{center_lng} "
        f"radius_km={radius_km or '-'} max_deposit={max_deposit or '-'} max_rent={max_rent or '-'} "
        f"output={out_csv}",
        flush=True,
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(RENTMAP_CLI),
                "crawl-naver",
                "--output-csv", str(out_csv),
                "--raw-json", str(raw_json),
                # 20 pages × 100 articles = up to 2000 per cortarNo. cortarNo dedup
                # inside rentmap.py keeps total pagination calls bounded.
                "--max-pages", "20",
                "--skip-home",
            ],
            cwd=str(ROOT),
            check=False,
            # 45 min: list crawl (~5min) + detail-API enrichment for ~1000 bbox
            # articles at ~250ms each (~5min) leaves comfortable headroom.
            timeout=45 * 60,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        print(f"[naver-scheduler] crawl-naver {status} exit={result.returncode} elapsed={elapsed:.1f}s output={out_csv}", flush=True)
        if result.returncode == 0:
            run_webhook_flush(trigger="crawl-naver-complete")
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"[naver-scheduler] crawl-naver TIMEOUT after {elapsed:.1f}s limit=2700s output={out_csv}: {exc}", flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[naver-scheduler] crawl-naver ERROR after {elapsed:.1f}s output={out_csv}: {exc}", flush=True)


def main() -> None:
    scheduler = BlockingScheduler(timezone=TZ)
    # Every hour at :00, in lock-step with rentmap-server's crawl cron.
    scheduler.add_job(
        run_naver_crawl,
        trigger=CronTrigger(minute=0, timezone=TZ),
        id="naver_crawl",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    # Startup kick a bit after boot
    scheduler.add_job(
        run_naver_crawl,
        trigger="date",
        run_date=datetime.now(TZ) + timedelta(seconds=30),
        id="naver_startup",
        max_instances=1,
        coalesce=True,
    )
    print("[naver-scheduler] started — hourly at :00 KST, plus startup kick", flush=True)
    scheduler.start()


if __name__ == "__main__":
    main()
