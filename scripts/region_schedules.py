"""CRUD + telemetry for ``region_schedules`` rows.

Schedules are split from the ``regions`` table because cron tuning happens
far more often than the request/approval flow — collapsing them would force
every cron edit through a full region PATCH endpoint. Each row says "for
this region, fire the *source* crawler whenever this cron matches"; the
phase-3 runner is what actually shells out to ``rentmap.py crawl-X``.

Cron expression validation runs through ``apscheduler.triggers.cron.CronTrigger
.from_crontab`` so we accept exactly the dialect the runner will eventually
hand back — no risk of saving a string the scheduler will choke on at fire
time.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402


class ScheduleError(Exception):
    """Same shape as RegionError / InviteError so server.py can translate uniformly."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


_VALID_SOURCES = ("all_light", "naver", "dabang", "zigbang", "daangn")
# Free-form on the DB side, but we constrain at the API layer so the admin
# UI / scheduler runner can switch on these without worrying about
# unexpected variants leaking in.
_VALID_LAST_STATUSES = ("ok", "failed", "timeout", "running", "skipped")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_cron(expr: str) -> None:
    """Parse via apscheduler — raises ScheduleError on failure.

    apscheduler.from_crontab handles the standard 5-field syntax we want
    (no @hourly, no @daily, no 7th-field year). Anything else is a typo.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ScheduleError("invalid", "cron_expr is required")
    try:
        CronTrigger.from_crontab(expr.strip())
    except (ValueError, TypeError) as exc:
        raise ScheduleError("invalid", f"Invalid cron expression: {exc}") from exc


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "regionId": row["region_id"],
        "regionSlug": row.get("region_slug"),
        "regionName": row.get("region_name"),
        "source": row["source"],
        "cronExpr": row["cron_expr"],
        "enabled": bool(row["enabled"]),
        "lastRunAt": row["last_run_at"].isoformat() if row.get("last_run_at") else None,
        "lastStatus": row.get("last_status"),
        "lastLogExcerpt": row.get("last_log_excerpt"),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


_SELECT_WITH_REGION = """
SELECT s.id, s.region_id, s.source, s.cron_expr, s.enabled,
       s.last_run_at, s.last_status, s.last_log_excerpt,
       s.created_at, s.updated_at,
       r.slug AS region_slug, r.name AS region_name
FROM region_schedules s
JOIN regions r ON r.id = s.region_id
"""


def list_schedules(
    *,
    region_id: int | None = None,
    only_enabled: bool = False,
    only_approved_regions: bool = False,
) -> list[dict[str, Any]]:
    """List schedule rows with region slug/name joined in.

    ``only_enabled`` + ``only_approved_regions`` is what the phase-3 runner
    will call (DB-driven sync loop); the admin UI calls with both flags
    off so it can show disabled rows too.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if region_id is not None:
        conditions.append("s.region_id = %s")
        params.append(region_id)
    if only_enabled:
        conditions.append("s.enabled = TRUE")
    if only_approved_regions:
        conditions.append("r.status = 'approved'")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            _SELECT_WITH_REGION + where + " ORDER BY r.slug, s.source, s.id",
            params,
        )
        return [_serialize(dict(r)) for r in cur.fetchall()]


def get_schedule(schedule_id: int) -> dict[str, Any]:
    with session() as conn, conn.cursor() as cur:
        cur.execute(_SELECT_WITH_REGION + " WHERE s.id = %s", (schedule_id,))
        row = cur.fetchone()
        if not row:
            raise ScheduleError("unknown", "Schedule not found")
        return _serialize(dict(row))


def create_schedule(
    *,
    region_id: int,
    source: str,
    cron_expr: str,
    enabled: bool = True,
) -> dict[str, Any]:
    if source not in _VALID_SOURCES:
        raise ScheduleError(
            "invalid",
            f"source must be one of {_VALID_SOURCES}",
        )
    cron_expr = (cron_expr or "").strip()
    validate_cron(cron_expr)
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM regions WHERE id = %s", (region_id,))
        if not cur.fetchone():
            raise ScheduleError("unknown", "Region not found")
        cur.execute(
            """
            INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (region_id, source, cron_expr, bool(enabled)),
        )
        new_id = cur.fetchone()["id"]
        cur.execute(_SELECT_WITH_REGION + " WHERE s.id = %s", (new_id,))
        return _serialize(dict(cur.fetchone()))


# Sentinel that mirrors regions.py's _UNSET, so callers can distinguish
# "leave alone" from "set to NULL".
class _Unset:
    pass


_UNSET: Any = _Unset()


def update_schedule(
    schedule_id: int,
    *,
    cron_expr: Any = _UNSET,
    enabled: Any = _UNSET,
    source: Any = _UNSET,
) -> dict[str, Any]:
    """PATCH-style. region_id is intentionally immutable — to move a schedule
    to another region, delete + recreate."""
    fields: list[str] = []
    values: list[Any] = []
    if cron_expr is not _UNSET:
        expr = (cron_expr or "").strip()
        validate_cron(expr)
        fields.append("cron_expr = %s")
        values.append(expr)
    if enabled is not _UNSET:
        fields.append("enabled = %s")
        values.append(bool(enabled))
    if source is not _UNSET:
        if source not in _VALID_SOURCES:
            raise ScheduleError("invalid", f"source must be one of {_VALID_SOURCES}")
        fields.append("source = %s")
        values.append(source)
    if not fields:
        raise ScheduleError("invalid", "No changes requested")
    fields.append("updated_at = now()")
    values.append(schedule_id)
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE region_schedules SET {', '.join(fields)} WHERE id = %s",
            values,
        )
        if cur.rowcount == 0:
            raise ScheduleError("unknown", "Schedule not found")
        cur.execute(_SELECT_WITH_REGION + " WHERE s.id = %s", (schedule_id,))
        return _serialize(dict(cur.fetchone()))


def delete_schedule(schedule_id: int) -> dict[str, Any]:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM region_schedules WHERE id = %s "
            "RETURNING id, region_id, source",
            (schedule_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ScheduleError("unknown", "Schedule not found")
    return {"id": row["id"], "regionId": row["region_id"], "source": row["source"], "deleted": True}


def record_run(
    schedule_id: int,
    *,
    status: str,
    log_excerpt: str | None = None,
    ran_at: datetime | None = None,
) -> None:
    """Phase-3 scheduler runner calls this after each fire to leave a trail.

    ``log_excerpt`` is capped at ~1KB; the full log goes to stdout. status
    is constrained to the small enum at the top of the module so the admin
    UI can colorize it without a free-text-to-class mapping.
    """
    if status not in _VALID_LAST_STATUSES:
        raise ScheduleError("invalid", f"status must be one of {_VALID_LAST_STATUSES}")
    excerpt = None
    if log_excerpt:
        s = str(log_excerpt)
        # Truncate to keep the row small — full output goes to stdout / container logs.
        excerpt = s if len(s) <= 1024 else s[:1021] + "..."
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE region_schedules
            SET last_run_at = %s,
                last_status = %s,
                last_log_excerpt = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (ran_at or _now(), status, excerpt, schedule_id),
        )
