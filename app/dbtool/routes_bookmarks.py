"""Bookmarks (실사 기록) — parallel to favorites but a separate axis.

A user bookmarks listings they actually visited; the row carries
``sort_order`` (visit order, 1 = 첫 번째 본 방) and a richer entry_json
(notes, checklist, photos). Unlike favorites there's no like/dislike
``kind`` — a listing is bookmarked or it isn't — so the API surface is
trimmed compared with routes_favorites: no kind filter, but every row
exposes sort_order for the UI.

The schema mirrors favorites enough that the transfer / bulk-delete
flows reuse the same UX pattern: per-user list, optional key selection,
copy-vs-move, skip-vs-overwrite, mandatory dry-run preview before any
destructive operation commits.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auth as rm_auth  # noqa: E402

from . import audit, deps

router = APIRouter()


def _check_user(cur, user_id: int) -> dict:
    cur.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    return row


@router.get("/api/tool/bookmarks")
def list_bookmarks(user_id: int, q: str | None = None,
                   source: str | None = None, limit: int = 200,
                   _: rm_auth.User = Depends(deps.require_admin)):
    """Bookmarks ordered by sort_order ASC — the same order the user sees
    on their /bookmarks page. Limit defaults to 200 because the typical
    user list is < 50."""
    limit = max(1, min(int(limit), 1000))
    with deps.tx() as cur:
        _check_user(cur, user_id)
        cur.execute(
            "SELECT count(*) AS n FROM bookmarks WHERE user_id = %s",
            (user_id,),
        )
        total = int(cur.fetchone()["n"])
        clauses = ["WHERE user_id = %s"]
        params: list = [user_id]
        if source:
            clauses.append("AND source = %s")
            params.append(source)
        if q:
            clauses.append("AND (key ILIKE %s OR entry_json::text ILIKE %s)")
            like = f"%{q}%"
            params.extend([like, like])
        cur.execute(
            f"""
            SELECT key, source, listing_no, listing_id, sort_order,
                   entry_json, saved_at
            FROM bookmarks
            {' '.join(clauses)}
            ORDER BY sort_order ASC, saved_at DESC
            LIMIT %s
            """,
            params + [limit],
        )
        rows = cur.fetchall()
    return {
        "total": total,
        "bookmarks": [
            {
                "key": r["key"],
                "source": r["source"],
                "listingNo": r["listing_no"],
                "listingId": r["listing_id"],
                "sortOrder": r["sort_order"],
                "entry": r["entry_json"],
                "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
            }
            for r in rows
        ],
    }


class TransferBody(BaseModel):
    from_user_id: int
    to_user_id: int
    keys: list[str] | None = None
    """If None or empty → transfer ALL bookmarks from from_user_id."""
    mode: Literal["copy", "move"] = "copy"
    on_conflict: Literal["skip", "overwrite"] = "skip"


def _transfer_plan(cur, body: TransferBody) -> dict:
    if body.from_user_id == body.to_user_id:
        raise HTTPException(status_code=400,
                            detail="from_user_id and to_user_id must differ")
    _check_user(cur, body.from_user_id)
    _check_user(cur, body.to_user_id)
    base_params: list = [body.from_user_id]
    key_clause = ""
    if body.keys:
        key_clause = " AND key = ANY(%s)"
        base_params.append(list(body.keys))
    cur.execute(
        f"SELECT count(*) AS n FROM bookmarks WHERE user_id = %s{key_clause}",
        base_params,
    )
    source_count = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT count(*) AS n FROM bookmarks src
        WHERE src.user_id = %s{key_clause}
          AND EXISTS (SELECT 1 FROM bookmarks dst
                      WHERE dst.user_id = %s AND dst.key = src.key)
        """,
        base_params + [body.to_user_id],
    )
    conflicts = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT count(*) AS n FROM bookmarks src
        WHERE src.user_id = %s{key_clause}
          AND EXISTS (SELECT 1 FROM bookmark_deleted t
                      WHERE t.user_id = %s AND t.key = src.key)
        """,
        base_params + [body.to_user_id],
    )
    tombstones = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT key, source, listing_no, sort_order FROM bookmarks
        WHERE user_id = %s{key_clause}
        ORDER BY sort_order ASC LIMIT 10
        """,
        base_params,
    )
    sample = [
        {"key": r["key"], "source": r["source"],
         "listingNo": r["listing_no"], "sortOrder": r["sort_order"]}
        for r in cur.fetchall()
    ]
    return {
        "source": source_count,
        "conflictsOnTarget": conflicts,
        "tombstonesOnTarget": tombstones,
        "wouldCopy": source_count - (conflicts if body.on_conflict == "skip" else 0),
        "wouldOverwrite": conflicts if body.on_conflict == "overwrite" else 0,
        "wouldDeleteSource": source_count if body.mode == "move" else 0,
        "sample": sample,
    }


@router.post("/api/tool/bookmarks/transfer/preview")
def transfer_preview(body: TransferBody,
                     _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        return _transfer_plan(cur, body)


@router.post("/api/tool/bookmarks/transfer")
def transfer(body: TransferBody, request: Request,
             user: rm_auth.User = Depends(deps.require_admin)):
    """Copy/move bookmarks between accounts.

    sort_order is carried over verbatim — the target user's existing list
    keeps its numbering and the inserted rows slot in by whatever values
    they brought. That preserves "1번 본 방, 2번 본 방" intent when an
    operator restores an account; the cost is occasional ordering ties
    if source/target ranges overlap. We deliberately do NOT renumber on
    insert because that would invalidate any client-side localStorage
    references the operator was trying to keep stable.
    """
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        plan = _transfer_plan(cur, body)
        params: list = [body.to_user_id, body.from_user_id]
        key_clause = ""
        if body.keys:
            key_clause = " AND key = ANY(%s)"
            params.append(list(body.keys))
        conflict_clause = "ON CONFLICT (user_id, key) DO NOTHING"
        if body.on_conflict == "overwrite":
            conflict_clause = (
                "ON CONFLICT (user_id, key) DO UPDATE SET "
                "source = EXCLUDED.source, "
                "listing_no = EXCLUDED.listing_no, "
                "listing_id = EXCLUDED.listing_id, "
                "sort_order = EXCLUDED.sort_order, "
                "entry_json = EXCLUDED.entry_json, "
                "saved_at = EXCLUDED.saved_at, "
                "updated_at = now()"
            )
        cur.execute(
            f"""
            INSERT INTO bookmarks (user_id, key, source, listing_no,
                                   listing_id, sort_order, entry_json,
                                   saved_at, created_at, updated_at)
            SELECT %s, key, source, listing_no, listing_id, sort_order,
                   entry_json, saved_at, now(), now()
            FROM bookmarks
            WHERE user_id = %s{key_clause}
            {conflict_clause}
            """,
            params,
        )
        copied = cur.rowcount or 0

        # Wipe target tombstones so a transferred row is immediately
        # reachable by the client merge logic.
        cur.execute(
            f"""
            DELETE FROM bookmark_deleted
            WHERE user_id = %s
              AND key IN (SELECT key FROM bookmarks
                          WHERE user_id = %s{key_clause})
            """,
            [body.to_user_id, body.from_user_id] + ([list(body.keys)] if body.keys else []),
        )

        deleted_source = 0
        if body.mode == "move":
            del_params: list = [body.from_user_id]
            del_key_clause = ""
            if body.keys:
                del_key_clause = " AND key = ANY(%s)"
                del_params.append(list(body.keys))
            cur.execute(
                f"DELETE FROM bookmarks WHERE user_id = %s{del_key_clause}",
                del_params,
            )
            deleted_source = cur.rowcount or 0

        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="bookmarks_transfer",
            target_table="bookmarks",
            target_id=None,
            target_count=copied + deleted_source,
            before={"plan": plan},
            after={"copied": copied, "deletedSource": deleted_source},
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"copied": copied, "deletedSource": deleted_source, "plan": plan}


class BulkDeleteBody(BaseModel):
    user_id: int
    keys: list[str] | None = None
    all: bool = False
    write_tombstones: bool = True


def _bulk_delete_plan(cur, body: BulkDeleteBody) -> dict:
    _check_user(cur, body.user_id)
    if not body.keys and not body.all:
        raise HTTPException(status_code=400,
                            detail="provide keys[] or set all=true")
    params: list = [body.user_id]
    key_clause = ""
    if body.keys:
        key_clause = " AND key = ANY(%s)"
        params.append(list(body.keys))
    cur.execute(
        f"SELECT count(*) AS n FROM bookmarks WHERE user_id = %s{key_clause}",
        params,
    )
    n = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT key, source, listing_no, sort_order, saved_at FROM bookmarks
        WHERE user_id = %s{key_clause}
        ORDER BY sort_order ASC LIMIT 20
        """,
        params,
    )
    sample = [
        {"key": r["key"], "source": r["source"],
         "listingNo": r["listing_no"], "sortOrder": r["sort_order"],
         "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None}
        for r in cur.fetchall()
    ]
    return {"wouldDelete": n, "sample": sample}


@router.post("/api/tool/bookmarks/bulk-delete/preview")
def bulk_delete_preview(body: BulkDeleteBody,
                        _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        return _bulk_delete_plan(cur, body)


@router.post("/api/tool/bookmarks/bulk-delete")
def bulk_delete(body: BulkDeleteBody, request: Request,
                user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        plan = _bulk_delete_plan(cur, body)
        params: list = [body.user_id]
        key_clause = ""
        if body.keys:
            key_clause = " AND key = ANY(%s)"
            params.append(list(body.keys))
        if body.write_tombstones:
            cur.execute(
                f"""
                INSERT INTO bookmark_deleted (user_id, key, deleted_at)
                SELECT user_id, key, now()
                FROM bookmarks WHERE user_id = %s{key_clause}
                ON CONFLICT (user_id, key) DO UPDATE SET deleted_at = now()
                """,
                params,
            )
        cur.execute(
            f"DELETE FROM bookmarks WHERE user_id = %s{key_clause}",
            params,
        )
        deleted = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="bookmarks_bulk_delete",
            target_table="bookmarks",
            target_id=None,
            target_count=deleted,
            before={"plan": plan},
            after={"deleted": deleted},
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"deleted": deleted, "plan": plan}
