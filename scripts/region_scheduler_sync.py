"""DB ↔ APScheduler sync for region crawl schedules.

Both the server (``server.py``) and naver (``scheduler_naver.py``) containers
keep a long-lived APScheduler instance and call :func:`sync_schedules`
periodically — typically every 30s — to diff the DB rows against the
currently registered jobs:

- DB row enabled, no job in scheduler  →  add_job
- Job in scheduler, DB row gone or disabled  →  remove_job
- Both present but the cron expression changed  →  add_job(replace_existing)
  which APScheduler treats as an in-place reschedule

Each container restricts itself to a subset of sources via
``allowed_sources`` so a single ``region_schedules`` row is safe to read
from both containers without firing twice. ``server.py`` handles the
lightweight 3-source bundle plus the individual lightweight sources;
``scheduler_naver.py`` is the sole owner of the ``naver`` source.

Why not push notifications via LISTEN/NOTIFY: the poll-based design keeps
both containers oblivious to each other and tolerant of brief DB outages —
a sync that fails just leaves the existing in-memory triggers running and
retries on the next tick.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import region_schedules as schedule_store  # noqa: E402

log = logging.getLogger(__name__)

# Prefix lets us distinguish region-driven jobs from other jobs the
# scheduler may host (startup gen-web kick, sessions cleanup, the sync
# loop itself). Removal logic only touches jobs starting with this.
JOB_PREFIX = "region_schedule_"


def _job_id(schedule_id: int) -> str:
    return f"{JOB_PREFIX}{schedule_id}"


def sync_schedules(
    scheduler: BaseScheduler,
    *,
    allowed_sources: tuple[str, ...],
    run_callback: Callable[[int], Any],
    tz: Any = None,
) -> None:
    """Reconcile the scheduler's job set with DB region_schedules.

    Adds/updates/removes APScheduler jobs to match the DB enabled-rows
    whose source falls in ``allowed_sources`` and whose region is
    approved. ``run_callback`` is invoked when each job fires — pass
    :func:`region_runner.run_schedule`.

    Safe to call frequently — uses ``replace_existing=True`` so an
    unchanged cron is a near-no-op on re-add; APScheduler de-dupes job
    ids internally.
    """
    try:
        rows = schedule_store.list_schedules(
            only_enabled=True, only_approved_regions=True,
        )
    except Exception as exc:  # noqa: BLE001
        # DB blip — existing jobs keep firing on cached triggers. Log and
        # move on; the next sync tick will retry.
        log.warning("region-sync: DB read failed (%s); keeping existing jobs", exc)
        return

    rows_in_scope = [r for r in rows if r["source"] in allowed_sources]
    wanted: dict[str, dict[str, Any]] = {_job_id(r["id"]): r for r in rows_in_scope}

    existing_ids = {
        job.id for job in scheduler.get_jobs()
        if job.id.startswith(JOB_PREFIX)
    }

    # Add or update wanted jobs.
    for job_id, row in wanted.items():
        try:
            trigger = CronTrigger.from_crontab(row["cronExpr"], timezone=tz)
        except (ValueError, TypeError) as exc:
            # An admin can save a valid cron then PATCH it to nonsense via
            # an out-of-band SQL update; warn and skip rather than crash
            # the sync loop.
            log.warning("region-sync: schedule id=%s has invalid cron %r (%s); skipping",
                        row["id"], row["cronExpr"], exc)
            continue
        scheduler.add_job(
            run_callback,
            trigger=trigger,
            id=job_id,
            args=[row["id"]],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60 * 60,
            replace_existing=True,
        )

    # Remove jobs whose DB row went away or got disabled / unapproved.
    for stale_id in existing_ids - set(wanted):
        try:
            scheduler.remove_job(stale_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("region-sync: failed to remove stale job %s: %s",
                        stale_id, exc)
