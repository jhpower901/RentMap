"""Replay saved CSV files through the reconcile engine.

Usage:

    python scripts/backfill.py            # all *_2026-*.csv in data/
    python scripts/backfill.py --date 2026-05-24
    python scripts/backfill.py --csv data/dabang_ajou_2026-05-24.csv

Behavior:
- Defaults to ``--dry-run-webhooks`` so reseeding never spams Discord. Pass
  ``--live`` to allow webhook delivery (use only for tiny smoke tests).
- Each CSV is fed to ``reconcile_crawl()`` as a single "crawl" anchored at
  the date implied by the filename (midnight KST that day) — that way a
  multi-day backfill produces a sensible event timeline.
- Files are processed in (date, platform) order so the cross-platform
  ``missing`` heuristic compares like-for-like.
- One Postgres transaction per file; a failure aborts that file but does
  not roll back files already committed.

Filename convention parsed: ``<source>_ajou_<YYYY-MM-DD>.csv`` (or
``naver_land_ajou_<DATE>.csv``).
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Local imports — make scripts/ importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402
from reconcile import reconcile_crawl  # noqa: E402

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"

# Filename → (platform_code, date). naver's prefix is two tokens so handle it
# specifically rather than splitting on underscores.
FILENAME_RE = re.compile(r"^(?P<src>[a-z_]+?)_ajou_(?P<date>\d{4}-\d{2}-\d{2})\.csv$")
PLATFORM_ALIASES = {
    # CSV prefix → platforms.code
    "dabang": "dabang",
    "zigbang": "zigbang",
    "daangn": "daangn",
    "naver_land": "naver_land",
}


def discover_csvs(data_dir: Path, only_date: str | None) -> list[tuple[Path, str, datetime]]:
    """Find ``<src>_ajou_<DATE>.csv`` files in data_dir.

    Returns a list of (path, platform_code, crawled_at) tuples, sorted by
    (date, platform_code) so the diff against a prior day is meaningful.
    """
    found: list[tuple[Path, str, datetime]] = []
    for path in sorted(data_dir.glob("*_ajou_*.csv")):
        m = FILENAME_RE.match(path.name)
        if not m:
            log.warning("skipping unparseable filename: %s", path.name)
            continue
        src = m.group("src")
        if src not in PLATFORM_ALIASES:
            log.warning("skipping unknown platform prefix: %s", src)
            continue
        if only_date and m.group("date") != only_date:
            continue
        # Anchor every replayed crawl to noon KST of that day so events have
        # a sensible event_at. (Midnight would risk wrapping to the prior
        # day when displayed in another timezone.)
        crawled_at = datetime.combine(
            datetime.strptime(m.group("date"), "%Y-%m-%d").date(),
            time(12, 0),
            tzinfo=KST,
        )
        found.append((path, PLATFORM_ALIASES[src], crawled_at))
    # Sort by (date, platform) — same date all together so missing detection
    # has the right peer comparison.
    found.sort(key=lambda t: (t[2], t[1]))
    return found


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def backfill_one(path: Path, platform_code: str, crawled_at: datetime, dry_run: bool) -> None:
    rows = load_csv(path)
    log.info("[backfill] %s  %s  %d rows  dry_run=%s", path.name, platform_code, len(rows), dry_run)
    with session() as conn:
        summary = reconcile_crawl(
            conn,
            platform_code=platform_code,
            rows=rows,
            crawled_at=crawled_at,
            target_area=None,
            dry_run_webhooks=dry_run,
        )
    log.info(
        "[backfill]   crawl_run=%d  seen=%d  +disc=%d  Δprice=%d  Δdetail=%d  "
        "unchanged=%d  missing=%d  removed=%d  reappeared=%d  errors=%d",
        summary.crawl_run_id, summary.rows_seen,
        summary.discovered, summary.price_changed, summary.detail_changed,
        summary.unchanged, summary.missing, summary.removed, summary.reappeared,
        len(summary.errors),
    )
    for e in summary.errors[:5]:
        log.warning("[backfill]   err: %s", e)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Replay CSVs through the DB reconcile engine.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="Directory holding *_ajou_*.csv files")
    parser.add_argument("--date", help="Filter by date (YYYY-MM-DD)")
    parser.add_argument("--csv", help="Single CSV path; overrides --data-dir / --date")
    parser.add_argument(
        "--live", action="store_true",
        help="Allow webhook delivery (default is dry-run for safety).",
    )
    args = parser.parse_args(argv)

    if args.csv:
        path = Path(args.csv)
        m = FILENAME_RE.match(path.name)
        if not m or m.group("src") not in PLATFORM_ALIASES:
            raise SystemExit(f"can't infer platform/date from filename: {path.name}")
        crawled_at = datetime.combine(
            datetime.strptime(m.group("date"), "%Y-%m-%d").date(),
            time(12, 0), tzinfo=KST,
        )
        backfill_one(path, PLATFORM_ALIASES[m.group("src")], crawled_at, dry_run=not args.live)
        return

    targets = discover_csvs(Path(args.data_dir), args.date)
    if not targets:
        raise SystemExit("no CSVs matched. nothing to backfill.")
    log.info("[backfill] %d file(s) to replay", len(targets))
    for path, platform, crawled_at in targets:
        try:
            backfill_one(path, platform, crawled_at, dry_run=not args.live)
        except Exception:
            log.exception("[backfill] %s failed; continuing with next file", path.name)


if __name__ == "__main__":
    main()
