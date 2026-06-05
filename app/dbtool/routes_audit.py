"""Audit log viewer + per-entry rollback.

Read access is broad — any admin can see what any other admin did. Write
access (rollback) is also any admin, but the rollback itself writes a
new audit row attributing the revert. Two rules:

  1. An entry's ``reverse_sql`` is set only when the original write was
     safely reversible (single-row update/insert, not a cascading delete).
     The UI hides the rollback button for entries where it's NULL.
  2. Rolling back an already-reverted entry is a no-op — the writer
     guards on ``reverted_at IS NULL``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

ROOT = Path(__file__).resolve().parent.parent.parent
from app.api import auth as rm_auth

from . import audit, deps

router = APIRouter()


@router.get("/api/tool/audit")
def list_audit(actor_user_id: int | None = None,
               action: str | None = None,
               target_table: str | None = None,
               target_id: str | None = None,
               unreverted_only: bool = False,
               limit: int = 100, offset: int = 0,
               _: rm_auth.User = Depends(deps.require_admin)):
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list = []
    if actor_user_id is not None:
        where.append("a.actor_user_id = %s")
        params.append(actor_user_id)
    if action:
        where.append("a.action = %s")
        params.append(action)
    if target_table:
        where.append("a.target_table = %s")
        params.append(target_table)
    if target_id is not None:
        where.append("a.target_id = %s")
        params.append(target_id)
    if unreverted_only:
        where.append("a.reverted_at IS NULL")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    with deps.tx() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM admin_audit_log a {where_clause}",
            params,
        )
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT a.id, a.actor_user_id, a.actor_username,
                   a.action, a.target_table, a.target_id, a.target_count,
                   a.before_json, a.after_json,
                   (a.reverse_sql IS NOT NULL) AS revertible,
                   a.cmd_payload,
                   a.reverted_at, ru.username AS reverted_by_username,
                   a.request_ip, a.request_path, a.created_at
            FROM admin_audit_log a
            LEFT JOIN users ru ON ru.id = a.reverted_by
            {where_clause}
            ORDER BY a.id DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
    return {
        "total": total, "limit": limit, "offset": offset,
        "entries": [
            {
                "id": r["id"],
                "actorUserId": r["actor_user_id"],
                "actorUsername": r["actor_username"],
                "action": r["action"],
                "targetTable": r["target_table"],
                "targetId": r["target_id"],
                "targetCount": r["target_count"],
                "before": r["before_json"],
                "after": r["after_json"],
                "revertible": bool(r["revertible"]),
                "cmdPayload": r["cmd_payload"],
                "revertedAt": r["reverted_at"].isoformat() if r["reverted_at"] else None,
                "revertedByUsername": r["reverted_by_username"],
                "requestIp": str(r["request_ip"]) if r["request_ip"] else None,
                "requestPath": r["request_path"],
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/api/tool/audit/{audit_id}/rollback")
def rollback(audit_id: int, request: Request,
             user: rm_auth.User = Depends(deps.require_admin)):
    """Execute the stored reverse_sql in a fresh transaction.

    Safety:
      - We re-read the row INSIDE the transaction to make sure it hasn't
        already been reverted while the UI was idle.
      - We do NOT trust the URL — the SQL is the one we wrote and stored;
        no caller-supplied SQL enters the path here.
      - The rollback itself writes its own audit row so the chain is
        bi-directional ("X did A, Y reverted A").
    """
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        cur.execute(
            "SELECT id, action, target_table, target_id, reverse_sql, "
            "       reverted_at FROM admin_audit_log WHERE id = %s",
            (audit_id,),
        )
        orig = cur.fetchone()
        if not orig:
            raise HTTPException(status_code=404, detail="audit entry not found")
        if orig["reverted_at"] is not None:
            raise HTTPException(status_code=409, detail="already reverted")
        if not orig["reverse_sql"]:
            raise HTTPException(status_code=400,
                                detail="this entry has no reverse SQL")
        # Reverse SQL is self-contained (Composable already rendered with
        # quoted literals at write time). Execute verbatim.
        cur.execute(orig["reverse_sql"])
        affected = cur.rowcount or 0
        audit.mark_reverted(cur, audit_id=audit_id,
                            reverted_by=deps.actor_from(user))
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="rollback",
            target_table=orig["target_table"],
            target_id=orig["target_id"],
            target_count=affected,
            before=None,
            after={"reverted_audit_id": audit_id},
            reverse_sql=None,  # rolling back a rollback is not supported
            cmd_payload={"audit_id": audit_id},
            request_ip=ip, request_path=path,
        )
    return {"ok": True, "rowsAffected": affected}
