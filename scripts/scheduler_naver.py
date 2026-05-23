"""Standalone scheduler for the naver crawler.

Runs inside the playwright-based image (Dockerfile.naver). Every hour at :00
KST, in lock-step with the main `rentmap-server` container which crawls the
other three sources at the same time. Both containers share `./data`, so
the `rentmap-server`'s gen-web cron (at :00 and :30) picks up whichever
naver CSV is currently on disk — and gen-web falls back to the most recent
naver CSV if the current run hasn't finished yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))


def run_naver_crawl() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    out_csv = ROOT / "data" / f"naver_land_ajou_{today}.csv"
    raw_json = ROOT / "data" / f"naver_land_ajou_{today}.raw.json"
    print(f"[naver-scheduler] crawl-naver --date {today}", flush=True)
    try:
        subprocess.run(
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
        print(f"[naver-scheduler] crawl-naver done", flush=True)
    except Exception as exc:
        print(f"[naver-scheduler] crawl-naver failed: {exc}", flush=True)


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
