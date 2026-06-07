"""Region and region-schedule management routes."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api import auth
from app.api import regions as region_store
from app.api import region_schedules as schedule_store

ROOT = Path(__file__).resolve().parents[3]

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Regions (request/approval) + per-region crawl schedules
# ─────────────────────────────────────────────────────────────────────────────

_REGION_ERROR_STATUS = {
    "unknown": 404,
    "invalid": 400,
    "duplicate": 409,
    "forbidden": 403,
    "in_use": 409,
}


def _region_http_error(exc: "region_store.RegionError") -> HTTPException:
    return HTTPException(
        status_code=_REGION_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


def _schedule_http_error(exc: "schedule_store.ScheduleError") -> HTTPException:
    return HTTPException(
        status_code=_REGION_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


class RegionRequestBody(BaseModel):
    # User-facing submission. Slug is intentionally NOT a user input — see
    # regions._generate_slug for the reasoning. The admin can rename via
    # PATCH /api/admin/regions/{id} once they've reviewed the request.
    name: str = Field(min_length=1, max_length=80)
    center_lat: float = Field(ge=-90, le=90)
    center_lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=50)
    note: str | None = Field(default=None, max_length=500)


class AdminUpdateRegionBody(BaseModel):
    # All optional. ``model_fields_set`` is what we forward to update_region
    # via the _UNSET sentinel pattern so an absent key = "don't touch".
    name: str | None = Field(default=None, max_length=80)
    slug: str | None = Field(default=None, max_length=63)
    center_lat: float | None = Field(default=None, ge=-90, le=90)
    center_lng: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0, le=50)
    naver_cortar_nos: list[str] | None = None
    daangn_region_ids: list[int] | None = None
    naver_urls: list[str] | None = None
    max_deposit_manwon: int | None = Field(default=None, ge=0)
    max_rent_manwon: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    status: str | None = None  # 'pending' | 'approved' | 'disabled'


class ScheduleCreateBody(BaseModel):
    region_id: int = Field(ge=1)
    source: str  # validated by schedule_store
    cron_expr: str = Field(min_length=1, max_length=100)
    enabled: bool = True


class ScheduleUpdateBody(BaseModel):
    cron_expr: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    source: str | None = None


_WEB_DIR = ROOT / "web"
_GENWEB_SOURCES: tuple[str, ...] = ("dabang", "daangn", "zigbang", "naver", "peterpan")


def _data_mtimes_for_slug(slug: str) -> dict[str, int]:
    """Per-source mtime (int seconds) of the gen-web data_<src>_<slug>.js bundles.

    The map / favorites pages append these as ``?v=<mtime>`` to the data
    bundle URLs. A stable URL for unchanged data means the browser uses its
    own cache (zero round-trip), and a fresh crawl bumps the mtime so the
    cached copy is bypassed automatically — without the old
    ``?v=Date.now()`` which re-downloaded ~20 MB on every page load.
    """
    out: dict[str, int] = {}
    for src in _GENWEB_SOURCES:
        try:
            out[src] = int((_WEB_DIR / f"data_{src}_{slug}.js").stat().st_mtime)
        except OSError:
            # File doesn't exist (region hasn't crawled yet, or this source
            # is intentionally absent). 0 keeps the URL stable so a follow-up
            # crawl appearing produces a real mtime that invalidates the URL.
            out[src] = 0
    return out


@router.get("/api/regions")
async def list_regions(user: auth.User = Depends(auth.current_user),
                       mine: bool = False):
    """Region listing for the region selector / request page.

    - ``mine=true``: the caller's own submissions (any status) — used by the
      "내 신청 내역" table on /region-request.html so a user can see their
      pending or rejected rows.
    - admin caller, no ``mine``: every row, every status (used by admin.html).
    - regular caller, no ``mine``: only approved rows (the region selector
      should never offer something a user can't act on).

    Each region carries ``dataMtimes`` so the web client can build cache-
    stable URLs for ``data_<src>_<slug>.js`` — see ``_data_mtimes_for_slug``.
    """
    if mine:
        regions = region_store.list_regions(requested_by=user.id)
    elif user.is_admin:
        regions = region_store.list_regions()
    else:
        regions = region_store.list_regions(statuses=("approved",))
    for r in regions:
        r["dataMtimes"] = _data_mtimes_for_slug(r["slug"])
    return {"regions": regions}


@router.post("/api/regions")
async def request_region(body: RegionRequestBody,
                         user: auth.User = Depends(auth.current_user)):
    """Any logged-in user can submit a region proposal (status='pending').

    The admin reviews in admin.html and either fills in cortarNos /
    region_ids and approves, or flips it straight to 'disabled' as a soft
    reject.
    """
    try:
        region = region_store.request_region(
            name=body.name,
            center_lat=body.center_lat,
            center_lng=body.center_lng,
            radius_km=body.radius_km,
            note=body.note,
            requested_by=user.id,
        )
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@router.get("/api/admin/regions")
async def admin_list_regions(_admin: auth.User = Depends(auth.current_admin)):
    return {"regions": region_store.list_regions()}


@router.get("/api/admin/regions/{region_id}")
async def admin_get_region(region_id: int,
                           _admin: auth.User = Depends(auth.current_admin)):
    try:
        region = region_store.get_region(region_id)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@router.patch("/api/admin/regions/{region_id}")
async def admin_update_region(region_id: int, body: AdminUpdateRegionBody,
                              admin: auth.User = Depends(auth.current_admin)):
    # model_fields_set tells us which keys the client sent. We translate
    # "absent" into the regions._UNSET sentinel by skipping the kwarg
    # entirely; "explicit null" becomes a None we forward.
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    for field in (
        "name", "slug", "center_lat", "center_lng", "radius_km",
        "naver_cortar_nos", "daangn_region_ids", "naver_urls",
        "max_deposit_manwon", "max_rent_manwon", "note", "status",
    ):
        if field in sent:
            kwargs[field] = getattr(body, field)
    if "status" in sent and body.status == "approved":
        kwargs["approved_by"] = admin.id
    try:
        region = region_store.update_region(region_id, **kwargs)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@router.delete("/api/admin/regions/{region_id}")
async def admin_delete_region(region_id: int,
                              _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = region_store.delete_region(region_id)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return result


@router.get("/api/admin/region-schedules")
async def admin_list_region_schedules(_admin: auth.User = Depends(auth.current_admin),
                                      region_id: int | None = None):
    schedules = schedule_store.list_schedules(region_id=region_id)
    return {"schedules": schedules}


@router.post("/api/admin/region-schedules")
async def admin_create_region_schedule(body: ScheduleCreateBody,
                                       _admin: auth.User = Depends(auth.current_admin)):
    try:
        schedule = schedule_store.create_schedule(
            region_id=body.region_id,
            source=body.source,
            cron_expr=body.cron_expr,
            enabled=body.enabled,
        )
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {"schedule": schedule}


@router.patch("/api/admin/region-schedules/{schedule_id}")
async def admin_update_region_schedule(schedule_id: int, body: ScheduleUpdateBody,
                                       _admin: auth.User = Depends(auth.current_admin)):
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    for field in ("cron_expr", "enabled", "source"):
        if field in sent:
            kwargs[field] = getattr(body, field)
    try:
        schedule = schedule_store.update_schedule(schedule_id, **kwargs)
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {"schedule": schedule}


@router.delete("/api/admin/region-schedules/{schedule_id}")
async def admin_delete_region_schedule(schedule_id: int,
                                       _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = schedule_store.delete_schedule(schedule_id)
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return result


