"""Webhook delivery queue triage.

The crawler fans listing_status_events out to user_webhooks via
webhook_deliveries. When Discord rate-limits or a URL goes stale, rows
pile up in ``status='failed'`` with retry counts maxed out. This tool
gives an operator two well-defined moves:

  retry      → reset status to 'pending', clear next_try_at + last_error
                so the worker picks the row back up on its next flush.
  mark-sent  → stamp sent_at=now() so the worker stops considering it
                (effectively a "drop on the floor"). Equivalent to the
                webhook_worker's --dry-run for a single row.

Bulk versions exist for the common "drain everything that failed today"
case; each has a /preview sibling so the operator sees row counts before
committing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
from app.api import auth as rm_auth

from . import audit, deps

router = APIRouter()


@router.get("/api/tool/events/deliveries")
def list_deliveries(status: str | None = Query(default=None,
                                                pattern="^(pending|sent|failed|suppressed)$"),
                    webhook_id: int | None = None,
                    limit: int = 200, offset: int = 0,
                    _: rm_auth.User = Depends(deps.require_admin)):
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list = []
    if status:
        where.append("d.status = %s")
        params.append(status)
    if webhook_id is not None:
        where.append("d.webhook_id = %s")
        params.append(webhook_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    with deps.tx() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM webhook_deliveries d {where_clause}",
            params,
        )
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT d.id, d.event_id, d.webhook_id, d.status, d.attempts,
                   d.sent_at, d.next_try_at, d.last_error, d.created_at,
                   e.event_type, e.event_at, e.listing_id,
                   uw.label AS webhook_label,
                   u.username AS webhook_owner
            FROM webhook_deliveries d
            JOIN listing_status_events e ON e.id = d.event_id
            LEFT JOIN user_webhooks uw ON uw.id = d.webhook_id
            LEFT JOIN users u ON u.id = uw.user_id
            {where_clause}
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
    return {
        "total": total, "limit": limit, "offset": offset,
        "deliveries": [
            {
                "id": r["id"], "eventId": r["event_id"],
                "webhookId": r["webhook_id"], "webhookLabel": r["webhook_label"],
                "webhookOwner": r["webhook_owner"],
                "status": r["status"], "attempts": r["attempts"],
                "sentAt": r["sent_at"].isoformat() if r["sent_at"] else None,
                "nextTryAt": r["next_try_at"].isoformat() if r["next_try_at"] else None,
                "lastError": r["last_error"],
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
                "eventType": r["event_type"],
                "eventAt": r["event_at"].isoformat() if r["event_at"] else None,
                "listingId": r["listing_id"],
            }
            for r in rows
        ],
    }


@router.post("/api/tool/events/deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: int, request: Request,
                   user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, status, attempts, sent_at, next_try_at, last_error "
            "FROM webhook_deliveries WHERE id = %s",
            (delivery_id,),
        )
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="delivery not found")
        cur.execute(
            """
            UPDATE webhook_deliveries
            SET status = 'pending',
                next_try_at = NULL,
                last_error = NULL
            WHERE id = %s
            RETURNING id, status, attempts, sent_at, next_try_at, last_error
            """,
            (delivery_id,),
        )
        after = cur.fetchone()
        b_diff, a_diff = audit.diff_columns(before, after)
        reverse = audit.build_reverse_update(
            cur.connection, "webhook_deliveries", {"id": delivery_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delivery_retry",
            target_table="webhook_deliveries",
            target_id=delivery_id,
            before=b_diff, after=a_diff,
            reverse_sql=reverse,
            cmd_payload={"delivery_id": delivery_id},
            request_ip=ip, request_path=path,
        )
    return {"id": after["id"], "status": after["status"]}


@router.post("/api/tool/events/deliveries/{delivery_id}/mark-sent")
def mark_sent(delivery_id: int, request: Request,
              user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, status, attempts, sent_at, next_try_at, last_error "
            "FROM webhook_deliveries WHERE id = %s",
            (delivery_id,),
        )
        before = cur.fetchone()
        if not before:
            raise HTTPException(status_code=404, detail="delivery not found")
        cur.execute(
            """
            UPDATE webhook_deliveries
            SET status = 'sent', sent_at = now(),
                next_try_at = NULL, last_error = NULL
            WHERE id = %s
            RETURNING id, status, sent_at
            """,
            (delivery_id,),
        )
        after = cur.fetchone()
        # Reverse is the status that was actually there pre-change.
        b_diff, a_diff = audit.diff_columns(before, dict(after))
        reverse = audit.build_reverse_update(
            cur.connection, "webhook_deliveries", {"id": delivery_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delivery_mark_sent",
            target_table="webhook_deliveries",
            target_id=delivery_id,
            before=b_diff, after=a_diff,
            reverse_sql=reverse,
            cmd_payload={"delivery_id": delivery_id},
            request_ip=ip, request_path=path,
        )
    return {"id": after["id"], "status": after["status"]}


class BulkDeliveryBody(BaseModel):
    status_filter: str = Field(pattern="^(pending|failed|suppressed)$")
    webhook_id: int | None = None
    older_than_hours: int | None = Field(default=None, ge=0, le=24 * 365)


def _bulk_where(body: BulkDeliveryBody) -> tuple[str, list]:
    where = ["status = %s"]
    params: list = [body.status_filter]
    if body.webhook_id is not None:
        where.append("webhook_id = %s")
        params.append(body.webhook_id)
    if body.older_than_hours is not None:
        where.append("created_at < now() - (%s || ' hours')::interval")
        params.append(body.older_than_hours)
    return " AND ".join(where), params


@router.post("/api/tool/events/bulk-retry/preview")
def bulk_retry_preview(body: BulkDeliveryBody,
                       _: rm_auth.User = Depends(deps.require_admin)):
    w, p = _bulk_where(body)
    with deps.tx() as cur:
        cur.execute(f"SELECT count(*) AS n FROM webhook_deliveries WHERE {w}", p)
        return {"wouldRetry": int(cur.fetchone()["n"]), "filter": body.dict()}


@router.post("/api/tool/events/bulk-retry")
def bulk_retry(body: BulkDeliveryBody, request: Request,
               user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    w, p = _bulk_where(body)
    with deps.tx() as cur:
        cur.execute(
            f"""
            UPDATE webhook_deliveries
            SET status = 'pending', next_try_at = NULL, last_error = NULL
            WHERE {w}
            """,
            p,
        )
        n = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delivery_bulk_retry",
            target_table="webhook_deliveries",
            target_id=None,
            target_count=n,
            before=None, after={"newStatus": "pending"},
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"retried": n}


@router.post("/api/tool/events/bulk-mark-sent/preview")
def bulk_mark_sent_preview(body: BulkDeliveryBody,
                           _: rm_auth.User = Depends(deps.require_admin)):
    w, p = _bulk_where(body)
    with deps.tx() as cur:
        cur.execute(f"SELECT count(*) AS n FROM webhook_deliveries WHERE {w}", p)
        return {"wouldMarkSent": int(cur.fetchone()["n"]), "filter": body.dict()}


@router.post("/api/tool/events/bulk-mark-sent")
def bulk_mark_sent(body: BulkDeliveryBody, request: Request,
                   user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    w, p = _bulk_where(body)
    with deps.tx() as cur:
        cur.execute(
            f"""
            UPDATE webhook_deliveries
            SET status = 'sent', sent_at = now(),
                next_try_at = NULL, last_error = NULL
            WHERE {w}
            """,
            p,
        )
        n = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delivery_bulk_mark_sent",
            target_table="webhook_deliveries",
            target_id=None,
            target_count=n,
            before=None, after={"newStatus": "sent"},
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"markedSent": n}


@router.get("/api/tool/events/unfanned")
def list_unfanned(limit: int = 100,
                  _: rm_auth.User = Depends(deps.require_admin)):
    """Events that the per-user fan-out worker hasn't processed yet.

    Read-only — actually re-running fan-out is the production worker's
    job. This view exists so an operator can spot pile-ups.
    """
    limit = max(1, min(int(limit), 500))
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT e.id, e.listing_id, e.event_type, e.event_at,
                   e.created_at, l.platform_listing_id,
                   p.code AS platform_code
            FROM listing_status_events e
            JOIN listings l ON l.id = e.listing_id
            JOIN platforms p ON p.id = l.platform_id
            WHERE e.user_webhook_fanned_out_at IS NULL
            ORDER BY e.created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return {
        "events": [
            {
                "id": r["id"], "listingId": r["listing_id"],
                "eventType": r["event_type"],
                "eventAt": r["event_at"].isoformat() if r["event_at"] else None,
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
                "platformListingId": r["platform_listing_id"],
                "platformCode": r["platform_code"],
            }
            for r in rows
        ]
    }
