"""Listings inspection + targeted status patches.

This is the most-touched tool surface in practice: when a crawler bug
mis-classifies a listing as ``removed`` an operator wants to (a) see the
snapshot history, (b) flip the row back to ``active``, and (c) have the
change attributed in admin_audit_log so a follow-up question
("who reactivated 7821?") has a non-archaeological answer.

We deliberately do NOT expose snapshot/event INSERT here — those are
crawler outputs and editing them by hand would muddy the history. The
admin can flip ``current_status`` on listings (and the per-region
``listing_regions`` row), nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
from app.api import auth as rm_auth

from . import audit, deps

router = APIRouter()


_VALID_STATUS = {"active", "missing", "removed", "expired", "blocked", "unknown"}


def _platform_options(cur) -> list[dict]:
    cur.execute("SELECT id, code, name FROM platforms ORDER BY id")
    return [{"id": r["id"], "code": r["code"], "name": r["name"]}
            for r in cur.fetchall()]


def _region_options(cur) -> list[dict]:
    cur.execute("SELECT id, slug, name, status FROM regions ORDER BY id")
    return [{"id": r["id"], "slug": r["slug"], "name": r["name"],
             "status": r["status"]}
            for r in cur.fetchall()]


@router.get("/api/tool/listings/meta")
def listings_meta(_: rm_auth.User = Depends(deps.require_admin)):
    """Static lookups for filter dropdowns. One query each, small tables."""
    with deps.tx() as cur:
        return {
            "platforms": _platform_options(cur),
            "regions": _region_options(cur),
            "statuses": sorted(_VALID_STATUS),
        }


@router.get("/api/tool/listings")
def list_listings(platform_id: int | None = None,
                  region_id: int | None = None,
                  status: str | None = None,
                  q: str | None = None,
                  limit: int = 100,
                  offset: int = 0,
                  _: rm_auth.User = Depends(deps.require_admin)):
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if status and status not in _VALID_STATUS:
        raise HTTPException(status_code=400, detail=f"invalid status {status!r}")
    where: list[str] = []
    params: list[Any] = []
    if platform_id is not None:
        where.append("l.platform_id = %s")
        params.append(platform_id)
    if status:
        where.append("l.current_status = %s")
        params.append(status)
    if region_id is not None:
        where.append("EXISTS (SELECT 1 FROM listing_regions lr "
                     "WHERE lr.listing_id = l.id AND lr.region_id = %s)")
        params.append(region_id)
    if q:
        where.append("(l.platform_listing_id ILIKE %s OR l.source_url ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like])
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    with deps.tx() as cur:
        cur.execute(
            f"""
            SELECT count(*) AS n FROM listings l
            {where_clause}
            """,
            params,
        )
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT l.id, l.platform_id, p.code AS platform_code,
                   l.platform_listing_id, l.source_url,
                   l.current_status, l.miss_count,
                   l.first_seen_at, l.last_seen_at,
                   l.removed_at, l.reappeared_at,
                   (SELECT count(*) FROM listing_snapshots
                    WHERE listing_id = l.id) AS snapshot_count
            FROM listings l
            JOIN platforms p ON p.id = l.platform_id
            {where_clause}
            ORDER BY l.last_seen_at DESC NULLS LAST
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "listings": [
            {
                "id": r["id"],
                "platformId": r["platform_id"],
                "platformCode": r["platform_code"],
                "platformListingId": r["platform_listing_id"],
                "sourceUrl": r["source_url"],
                "currentStatus": r["current_status"],
                "missCount": r["miss_count"],
                "firstSeenAt": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                "lastSeenAt": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                "removedAt": r["removed_at"].isoformat() if r["removed_at"] else None,
                "reappearedAt": r["reappeared_at"].isoformat() if r["reappeared_at"] else None,
                "snapshotCount": int(r["snapshot_count"] or 0),
            }
            for r in rows
        ],
    }


@router.get("/api/tool/listings/{listing_id}")
def listing_detail(listing_id: int,
                   _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT l.*, p.code AS platform_code, p.name AS platform_name
            FROM listings l JOIN platforms p ON p.id = l.platform_id
            WHERE l.id = %s
            """,
            (listing_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="listing not found")
        cur.execute(
            """
            SELECT lr.id, lr.region_id, r.slug, r.name AS region_name,
                   lr.current_status, lr.miss_count,
                   lr.first_seen_at, lr.last_seen_at,
                   lr.removed_at, lr.reappeared_at
            FROM listing_regions lr
            JOIN regions r ON r.id = lr.region_id
            WHERE lr.listing_id = %s
            ORDER BY r.id
            """,
            (listing_id,),
        )
        region_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, captured_at, content_hash, price_hash, detail_hash,
                   title, address_raw, deposit_won, monthly_rent_won,
                   maintenance_fee_won, sale_price_won, jeonse_price_won
            FROM listing_snapshots
            WHERE listing_id = %s
            ORDER BY captured_at DESC
            LIMIT 30
            """,
            (listing_id,),
        )
        snapshots = cur.fetchall()
        cur.execute(
            """
            SELECT id, event_type, event_at, changed_fields,
                   webhook_sent_at, webhook_attempts
            FROM listing_status_events
            WHERE listing_id = %s
            ORDER BY event_at DESC
            LIMIT 30
            """,
            (listing_id,),
        )
        events = cur.fetchall()
    return {
        "listing": {
            "id": row["id"],
            "platformId": row["platform_id"],
            "platformCode": row["platform_code"],
            "platformListingId": row["platform_listing_id"],
            "sourceUrl": row["source_url"],
            "currentStatus": row["current_status"],
            "missCount": row["miss_count"],
            "firstSeenAt": row["first_seen_at"].isoformat() if row["first_seen_at"] else None,
            "lastSeenAt": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            "removedAt": row["removed_at"].isoformat() if row["removed_at"] else None,
            "reappearedAt": row["reappeared_at"].isoformat() if row["reappeared_at"] else None,
        },
        "regions": [
            {
                "id": r["id"], "regionId": r["region_id"],
                "slug": r["slug"], "regionName": r["region_name"],
                "currentStatus": r["current_status"],
                "missCount": r["miss_count"],
                "firstSeenAt": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                "lastSeenAt": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                "removedAt": r["removed_at"].isoformat() if r["removed_at"] else None,
                "reappearedAt": r["reappeared_at"].isoformat() if r["reappeared_at"] else None,
            }
            for r in region_rows
        ],
        "snapshots": [
            {
                "id": s["id"],
                "capturedAt": s["captured_at"].isoformat(),
                "contentHash": (s["content_hash"] or "")[:12],
                "title": s["title"],
                "addressRaw": s["address_raw"],
                "depositWon": s["deposit_won"],
                "monthlyRentWon": s["monthly_rent_won"],
                "maintenanceFeeWon": s["maintenance_fee_won"],
                "salePriceWon": s["sale_price_won"],
                "jeonsePriceWon": s["jeonse_price_won"],
            }
            for s in snapshots
        ],
        "events": [
            {
                "id": e["id"],
                "eventType": e["event_type"],
                "eventAt": e["event_at"].isoformat(),
                "changedFields": e["changed_fields"],
                "webhookSentAt": e["webhook_sent_at"].isoformat() if e["webhook_sent_at"] else None,
                "webhookAttempts": e["webhook_attempts"],
            }
            for e in events
        ],
    }


class StatusPatchBody(BaseModel):
    current_status: str = Field(pattern="^(active|missing|removed|expired|blocked|unknown)$")
    note: str | None = Field(default=None, max_length=400)


@router.patch("/api/tool/listings/{listing_id}/status")
def patch_status(listing_id: int, body: StatusPatchBody, request: Request,
                 user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, current_status, miss_count, removed_at, reappeared_at "
            "FROM listings WHERE id = %s",
            (listing_id,),
        )
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="listing not found")
        new_status = body.current_status
        # Set timestamps so the listing matches the natural state for the
        # status — otherwise an operator flipping to 'active' would leave
        # a stale removed_at that confuses gen-web's "actually-gone" view.
        removed_at_sql = "removed_at"
        reappeared_at_sql = "reappeared_at"
        if new_status == "removed" and before["removed_at"] is None:
            removed_at_sql = "now()"
        if new_status == "active" and before["current_status"] != "active":
            reappeared_at_sql = "now()"
            # Reset miss_count when we manually reactivate — otherwise the
            # next missing pass will instantly knock it back to 'missing'.
            cur.execute(
                f"""
                UPDATE listings
                SET current_status = %s,
                    miss_count = 0,
                    reappeared_at = {reappeared_at_sql}
                WHERE id = %s
                """,
                (new_status, listing_id),
            )
        else:
            cur.execute(
                f"""
                UPDATE listings
                SET current_status = %s,
                    removed_at = {removed_at_sql},
                    reappeared_at = {reappeared_at_sql}
                WHERE id = %s
                """,
                (new_status, listing_id),
            )
        cur.execute(
            "SELECT id, current_status, miss_count, removed_at, reappeared_at "
            "FROM listings WHERE id = %s",
            (listing_id,),
        )
        after = cur.fetchone()
        b_diff, a_diff = audit.diff_columns(before, after)
        reverse = audit.build_reverse_update(
            cur.connection, "listings", {"id": listing_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="listing_status_patch",
            target_table="listings",
            target_id=listing_id,
            before=b_diff,
            after=a_diff,
            reverse_sql=reverse,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"id": after["id"], "currentStatus": after["current_status"],
            "missCount": after["miss_count"]}


class BulkStatusBody(BaseModel):
    listing_ids: list[int] = Field(min_length=1, max_length=2000)
    current_status: str = Field(pattern="^(active|missing|removed|expired|blocked|unknown)$")


@router.post("/api/tool/listings/bulk-status/preview")
def bulk_status_preview(body: BulkStatusBody,
                        _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT current_status, count(*) AS n
            FROM listings WHERE id = ANY(%s)
            GROUP BY current_status
            """,
            (body.listing_ids,),
        )
        by_status = {r["current_status"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT count(*) AS n FROM listings WHERE id = ANY(%s)",
            (body.listing_ids,),
        )
        found = int(cur.fetchone()["n"])
    return {
        "requested": len(body.listing_ids),
        "found": found,
        "missing": len(body.listing_ids) - found,
        "currentBreakdown": by_status,
        "willTarget": body.current_status,
    }


@router.post("/api/tool/listings/bulk-status")
def bulk_status(body: BulkStatusBody, request: Request,
                user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, current_status FROM listings WHERE id = ANY(%s)",
            (body.listing_ids,),
        )
        before_rows = {r["id"]: r["current_status"] for r in cur.fetchall()}
        cur.execute(
            "UPDATE listings SET current_status = %s WHERE id = ANY(%s)",
            (body.current_status, body.listing_ids),
        )
        updated = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="listing_bulk_status",
            target_table="listings",
            target_id=None,
            target_count=updated,
            before={"sample": dict(list(before_rows.items())[:20])},
            after={"newStatus": body.current_status},
            # Bulk reverts are deliberately unsupported — too easy for an
            # operator to mass-flip thousands of rows by accident. If a
            # rollback is needed they re-run the same endpoint with the
            # restored values from the audit-log before-snapshot.
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"updated": updated}
