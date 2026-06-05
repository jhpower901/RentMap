"""Region + schedule CRUD with audit hooks.

Mostly a thin wrapper over scripts/regions.py + scripts/region_schedules.py
that adds (1) per-mutate audit rows and (2) a delete-preview that surfaces
cascade impact (listing_regions rows, schedule rows, data/<slug>/* files
that would be orphaned).

The crawl scheduler in the main service polls region_schedules every 30s,
so a PATCH here propagates without a restart — same machinery as the
existing admin.html.
"""

from __future__ import annotations

import sys
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auth as rm_auth  # noqa: E402

from . import audit, deps

router = APIRouter()

_VALID_SOURCE = {"all_light", "naver", "dabang", "zigbang", "daangn", "peterpan"}
_VALID_STATUS = {"pending", "approved", "disabled"}


def _region_row(cur, region_id: int) -> dict:
    cur.execute(
        """
        SELECT id, slug, name, center_lat, center_lng, radius_km,
               naver_cortar_nos, daangn_region_ids, naver_urls,
               max_deposit_manwon, max_rent_manwon,
               status, note, requested_by, approved_by, approved_at,
               created_at, updated_at
        FROM regions WHERE id = %s
        """,
        (region_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="region not found")
    return row


def _serialize_region(row: dict) -> dict:
    return {
        "id": row["id"], "slug": row["slug"], "name": row["name"],
        "centerLat": float(row["center_lat"]),
        "centerLng": float(row["center_lng"]),
        "radiusKm": float(row["radius_km"]),
        "naverCortarNos": list(row["naver_cortar_nos"] or []),
        "daangnRegionIds": list(row["daangn_region_ids"] or []),
        "naverUrls": list(row["naver_urls"] or []),
        "maxDepositManwon": row["max_deposit_manwon"],
        "maxRentManwon": row["max_rent_manwon"],
        "status": row["status"], "note": row["note"],
        "requestedBy": row["requested_by"],
        "approvedBy": row["approved_by"],
        "approvedAt": row["approved_at"].isoformat() if row["approved_at"] else None,
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/api/tool/regions")
def list_regions(_: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT r.*, u1.username AS requested_by_username,
                   u2.username AS approved_by_username,
                   (SELECT count(*) FROM region_schedules WHERE region_id = r.id) AS schedule_count,
                   (SELECT count(*) FROM listing_regions WHERE region_id = r.id) AS listing_region_count
            FROM regions r
            LEFT JOIN users u1 ON u1.id = r.requested_by
            LEFT JOIN users u2 ON u2.id = r.approved_by
            ORDER BY r.id
            """
        )
        rows = cur.fetchall()
    return {
        "regions": [
            _serialize_region(r) | {
                "requestedByUsername": r["requested_by_username"],
                "approvedByUsername": r["approved_by_username"],
                "scheduleCount": int(r["schedule_count"] or 0),
                "listingRegionCount": int(r["listing_region_count"] or 0),
            }
            for r in rows
        ]
    }


class CreateRegionBody(BaseModel):
    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=80)
    center_lat: float = Field(ge=-90, le=90)
    center_lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=50)
    naver_cortar_nos: list[str] = []
    daangn_region_ids: list[int] = []
    naver_urls: list[str] = []
    max_deposit_manwon: int | None = Field(default=None, ge=0)
    max_rent_manwon: int | None = Field(default=None, ge=0)
    status: str = Field(default="pending", pattern="^(pending|approved|disabled)$")
    note: str | None = Field(default=None, max_length=400)


@router.post("/api/tool/regions")
def create_region(body: CreateRegionBody, request: Request,
                  user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute("SELECT id FROM regions WHERE slug = %s", (body.slug,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="slug already exists")
        approved_at_clause = ", approved_at = now(), approved_by = %s" if body.status == "approved" else ""
        approved_at_params: list = [user.id] if body.status == "approved" else []
        cur.execute(
            f"""
            INSERT INTO regions (slug, name, center_lat, center_lng, radius_km,
                                 naver_cortar_nos, daangn_region_ids, naver_urls,
                                 max_deposit_manwon, max_rent_manwon,
                                 status, note, requested_by{', approved_by, approved_at' if body.status == 'approved' else ''})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s{', %s, now()' if body.status == 'approved' else ''})
            RETURNING *
            """,
            [
                body.slug, body.name, body.center_lat, body.center_lng, body.radius_km,
                body.naver_cortar_nos, body.daangn_region_ids, body.naver_urls,
                body.max_deposit_manwon, body.max_rent_manwon,
                body.status, body.note, user.id,
            ] + ([user.id] if body.status == "approved" else []),
        )
        new_row = cur.fetchone()
        reverse = audit.build_reverse_delete(
            cur.connection, "regions", {"id": new_row["id"]}
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="create_region",
            target_table="regions",
            target_id=new_row["id"],
            before=None,
            after={k: new_row[k] for k in ("id", "slug", "name", "status")},
            reverse_sql=reverse,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"region": _serialize_region(new_row)}


class UpdateRegionBody(BaseModel):
    slug: str | None = Field(default=None, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    center_lat: float | None = Field(default=None, ge=-90, le=90)
    center_lng: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0, le=50)
    naver_cortar_nos: list[str] | None = None
    daangn_region_ids: list[int] | None = None
    naver_urls: list[str] | None = None
    max_deposit_manwon: int | None = Field(default=None, ge=0)
    max_rent_manwon: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern="^(pending|approved|disabled)$")
    note: str | None = Field(default=None, max_length=400)


@router.patch("/api/tool/regions/{region_id}")
def update_region(region_id: int, body: UpdateRegionBody, request: Request,
                  user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    fields = body.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    with deps.tx() as cur:
        before = _region_row(cur, region_id)
        sets: list[str] = []
        params: list = []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            params.append(v)
        sets.append("updated_at = now()")
        # Auto-stamp approval metadata on transition into 'approved'.
        if fields.get("status") == "approved" and before["status"] != "approved":
            sets.append("approved_by = %s")
            sets.append("approved_at = now()")
            params.append(user.id)
        params.append(region_id)
        cur.execute(
            f"UPDATE regions SET {', '.join(sets)} WHERE id = %s RETURNING *",
            params,
        )
        after = cur.fetchone()
        b_diff, a_diff = audit.diff_columns(before, after)
        reverse = audit.build_reverse_update(
            cur.connection, "regions", {"id": region_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="update_region",
            target_table="regions",
            target_id=region_id,
            before=b_diff, after=a_diff,
            reverse_sql=reverse,
            cmd_payload=fields,
            request_ip=ip, request_path=path,
        )
    return {"region": _serialize_region(after)}


@router.get("/api/tool/regions/{region_id}/delete-preview")
def region_delete_preview(region_id: int,
                          _: rm_auth.User = Depends(deps.require_admin)):
    """Show the cascade footprint of a region delete.

    listing_regions ON DELETE CASCADE drops the per-region lifecycle rows
    (other regions for the same listing still survive). region_schedules
    ON DELETE CASCADE drops the cron entries. data/<slug>/*.csv files are
    *not* deleted by Postgres — operator removes them out of band.
    """
    with deps.tx() as cur:
        row = _region_row(cur, region_id)
        cur.execute(
            "SELECT count(*) AS n FROM listing_regions WHERE region_id = %s",
            (region_id,),
        )
        lr = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM region_schedules WHERE region_id = %s",
            (region_id,),
        )
        sc = int(cur.fetchone()["n"])
    return {
        "region": {"id": row["id"], "slug": row["slug"], "name": row["name"]},
        "listingRegionRows": lr,
        "scheduleRows": sc,
        "dataDirectoryHint": f"data/{row['slug']}/",
    }


class DeleteRegionBody(BaseModel):
    confirm_slug: str


@router.delete("/api/tool/regions/{region_id}")
def delete_region(region_id: int, body: DeleteRegionBody, request: Request,
                  user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        before = _region_row(cur, region_id)
        if body.confirm_slug.strip().lower() != before["slug"].lower():
            raise HTTPException(status_code=400,
                                detail="confirmation slug does not match")
        cur.execute("DELETE FROM regions WHERE id = %s", (region_id,))
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delete_region",
            target_table="regions",
            target_id=region_id,
            before={k: before[k] for k in ("id", "slug", "name", "status")},
            after=None,
            reverse_sql=None,
            cmd_payload={"region_id": region_id},
            request_ip=ip, request_path=path,
        )
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Schedules
# ──────────────────────────────────────────────────────────────────────────────

def _validate_cron(expr: str) -> None:
    try:
        CronTrigger.from_crontab(expr)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid cron: {exc}")


@router.get("/api/tool/region-schedules")
def list_schedules(_: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT s.id, s.region_id, r.slug AS region_slug,
                   s.source, s.cron_expr, s.enabled,
                   s.last_run_at, s.last_status, s.last_log_excerpt,
                   s.created_at, s.updated_at
            FROM region_schedules s
            JOIN regions r ON r.id = s.region_id
            ORDER BY s.region_id, s.id
            """
        )
        rows = cur.fetchall()
    return {
        "schedules": [
            {
                "id": r["id"], "regionId": r["region_id"],
                "regionSlug": r["region_slug"],
                "source": r["source"], "cronExpr": r["cron_expr"],
                "enabled": r["enabled"],
                "lastRunAt": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                "lastStatus": r["last_status"],
                "lastLogExcerpt": r["last_log_excerpt"],
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
                "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


class CreateScheduleBody(BaseModel):
    region_id: int
    source: str = Field(pattern="^(all_light|naver|dabang|zigbang|daangn|peterpan)$")
    cron_expr: str = Field(min_length=3, max_length=80)
    enabled: bool = True


@router.post("/api/tool/region-schedules")
def create_schedule(body: CreateScheduleBody, request: Request,
                    user: rm_auth.User = Depends(deps.require_admin)):
    _validate_cron(body.cron_expr)
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute("SELECT id FROM regions WHERE id = %s", (body.region_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="region not found")
        cur.execute(
            """
            INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
            VALUES (%s, %s, %s, %s)
            RETURNING id, region_id, source, cron_expr, enabled
            """,
            (body.region_id, body.source, body.cron_expr, body.enabled),
        )
        new_row = cur.fetchone()
        reverse = audit.build_reverse_delete(
            cur.connection, "region_schedules", {"id": new_row["id"]}
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="create_schedule",
            target_table="region_schedules",
            target_id=new_row["id"],
            before=None,
            after=dict(new_row),
            reverse_sql=reverse,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"schedule": dict(new_row)}


class UpdateScheduleBody(BaseModel):
    cron_expr: str | None = Field(default=None, min_length=3, max_length=80)
    enabled: bool | None = None
    source: str | None = Field(default=None, pattern="^(all_light|naver|dabang|zigbang|daangn|peterpan)$")


@router.patch("/api/tool/region-schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: UpdateScheduleBody, request: Request,
                    user: rm_auth.User = Depends(deps.require_admin)):
    fields = body.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "cron_expr" in fields:
        _validate_cron(fields["cron_expr"])
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, region_id, source, cron_expr, enabled "
            "FROM region_schedules WHERE id = %s",
            (schedule_id,),
        )
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="schedule not found")
        sets, params = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            params.append(v)
        sets.append("updated_at = now()")
        params.append(schedule_id)
        cur.execute(
            f"UPDATE region_schedules SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, region_id, source, cron_expr, enabled",
            params,
        )
        after = cur.fetchone()
        b_diff, a_diff = audit.diff_columns(before, after)
        reverse = audit.build_reverse_update(
            cur.connection, "region_schedules", {"id": schedule_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="update_schedule",
            target_table="region_schedules",
            target_id=schedule_id,
            before=b_diff, after=a_diff,
            reverse_sql=reverse,
            cmd_payload=fields,
            request_ip=ip, request_path=path,
        )
    return {"schedule": dict(after)}


@router.delete("/api/tool/region-schedules/{schedule_id}")
def delete_schedule(schedule_id: int, request: Request,
                    user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, region_id, source, cron_expr, enabled "
            "FROM region_schedules WHERE id = %s",
            (schedule_id,),
        )
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="schedule not found")
        cur.execute("DELETE FROM region_schedules WHERE id = %s", (schedule_id,))
        reverse = audit.build_reverse_insert(
            cur.connection, "region_schedules", dict(before)
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delete_schedule",
            target_table="region_schedules",
            target_id=schedule_id,
            before=dict(before), after=None,
            reverse_sql=reverse,
            cmd_payload={"schedule_id": schedule_id},
            request_ip=ip, request_path=path,
        )
    return {"ok": True}
