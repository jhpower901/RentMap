"""Favorites + dislikes management — per-user view, cross-user transfer,
bulk delete.

Likes and dislikes share the favorites table: ``entry_json->>'kind'`` is
``'like'`` (or NULL for legacy rows that pre-date the dislike feature)
vs. ``'dislike'``. They share a key namespace (``{source}::{id}``) so a
single listing can be EITHER liked OR disliked, never both. That makes
the kind filter a simple JSONB predicate rather than a separate table.

The transfer flow exists because the historical scripts/users.py
``migrate-globals`` was a one-off; an admin still occasionally needs to
move favorites between accounts (e.g. an operator re-creating their
account and wanting their old saves back). The transfer is implemented
as INSERT … ON CONFLICT so re-running it is safe — the audit row
records how many were copied vs. skipped.

Every mutate has a ``/preview`` sibling so the UI can populate a
"will affect N rows" confirm modal before the operator clicks through.
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


# JSONB predicates that select likes vs. dislikes. We treat NULL/missing
# ``kind`` as 'like' to match the favorites.js client behaviour for legacy
# rows.
_KIND_PREDICATES = {
    "like": "(entry_json->>'kind' IS NULL OR entry_json->>'kind' = 'like')",
    "dislike": "(entry_json->>'kind' = 'dislike')",
}


def _kind_clause(kind: str | None) -> str:
    """Return a SQL fragment starting with AND, or empty string for 'all'/None."""
    if not kind or kind == "all":
        return ""
    pred = _KIND_PREDICATES.get(kind)
    if pred is None:
        raise HTTPException(status_code=400,
                            detail=f"invalid kind={kind!r}; expected like|dislike|all")
    return f" AND {pred}"


def _kind_of(entry) -> str:
    """Pull the canonical kind out of a row's entry_json. None / missing /
    'like' all map to 'like' so the UI never sees a NULL kind chip."""
    if isinstance(entry, dict):
        k = entry.get("kind")
        if k == "dislike":
            return "dislike"
    return "like"


def _check_user(cur, user_id: int) -> dict:
    cur.execute(
        "SELECT id, username FROM users WHERE id = %s", (user_id,)
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404,
                            detail=f"user {user_id} not found")
    return row


@router.get("/api/tool/favorites")
def list_favorites(user_id: int, q: str | None = None,
                   source: str | None = None,
                   kind: str | None = None,
                   limit: int = 200,
                   _: rm_auth.User = Depends(deps.require_admin)):
    """List a user's saved rows. ``kind`` defaults to 'all' (likes +
    dislikes); pass 'like' or 'dislike' to filter — handy for an admin
    who only wants to move one side over to another account."""
    limit = max(1, min(int(limit), 1000))
    kind_sql = _kind_clause(kind)
    with deps.tx() as cur:
        _check_user(cur, user_id)
        # Per-user counts broken down by kind so the UI can show the
        # split in the user picker label.
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE {_KIND_PREDICATES['like']}) AS likes,
              count(*) FILTER (WHERE {_KIND_PREDICATES['dislike']}) AS dislikes
            FROM favorites WHERE user_id = %s
            """,
            (user_id,),
        )
        counts = cur.fetchone() or {"likes": 0, "dislikes": 0}
        clauses = ["WHERE user_id = %s"]
        params: list = [user_id]
        if source:
            clauses.append("AND source = %s")
            params.append(source)
        if q:
            # Search the JSONB ``entry_json`` text representation. Cheap
            # for the small per-user volumes (a single favorites list is
            # rarely >500 entries); no GIN required.
            clauses.append("AND (key ILIKE %s OR entry_json::text ILIKE %s)")
            like = f"%{q}%"
            params.extend([like, like])
        if kind_sql:
            clauses.append(kind_sql.strip())  # already starts with AND
        cur.execute(
            f"""
            SELECT key, source, listing_no, listing_id, entry_json,
                   saved_at, created_at, updated_at
            FROM favorites
            {' '.join(clauses)}
            ORDER BY saved_at DESC LIMIT %s
            """,
            params + [limit],
        )
        rows = cur.fetchall()
    return {
        "counts": {
            "likes": int(counts["likes"] or 0),
            "dislikes": int(counts["dislikes"] or 0),
        },
        "favorites": [
            {
                "key": r["key"],
                "source": r["source"],
                "listingNo": r["listing_no"],
                "listingId": r["listing_id"],
                "kind": _kind_of(r["entry_json"]),
                "entry": r["entry_json"],
                "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
            }
            for r in rows
        ]
    }


class TransferBody(BaseModel):
    from_user_id: int
    to_user_id: int
    keys: list[str] | None = None
    """If None or empty → transfer ALL rows from from_user_id (subject to
    the kind filter)."""
    mode: Literal["copy", "move"] = "copy"
    """copy = INSERT on target, keep source. move = also DELETE on source."""
    on_conflict: Literal["skip", "overwrite"] = "skip"
    """If the target already holds a row at (to_user_id, key), skip leaves
    the target intact; overwrite replaces it. Tombstone cleanup runs
    either way so a transferred row is immediately re-saveable."""
    kind: Literal["all", "like", "dislike"] = "all"
    """Restrict the transfer to one side of the like/dislike axis. The
    common case is 'all' (admin restoring an account); 'like' / 'dislike'
    are there for the operator who only wants to move one side over."""


def _transfer_plan(cur, body: TransferBody) -> dict:
    """Common preview/execute analysis. Returns counts and a sample."""
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
    kind_clause = _kind_clause(body.kind)
    cur.execute(
        f"SELECT count(*) AS n FROM favorites WHERE user_id = %s{key_clause}{kind_clause}",
        base_params,
    )
    source_count = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE {_KIND_PREDICATES['like']}) AS likes,
          count(*) FILTER (WHERE {_KIND_PREDICATES['dislike']}) AS dislikes
        FROM favorites
        WHERE user_id = %s{key_clause}{kind_clause}
        """,
        base_params,
    )
    breakdown = cur.fetchone() or {"likes": 0, "dislikes": 0}
    cur.execute(
        f"""
        SELECT count(*) AS n FROM favorites src
        WHERE src.user_id = %s{key_clause}{kind_clause}
          AND EXISTS (SELECT 1 FROM favorites dst
                      WHERE dst.user_id = %s AND dst.key = src.key)
        """,
        base_params + [body.to_user_id],
    )
    conflicts = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT count(*) AS n FROM favorites src
        WHERE src.user_id = %s{key_clause}{kind_clause}
          AND EXISTS (SELECT 1 FROM favorite_deleted t
                      WHERE t.user_id = %s AND t.key = src.key)
        """,
        base_params + [body.to_user_id],
    )
    tombstones = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT key, source, listing_no, entry_json FROM favorites
        WHERE user_id = %s{key_clause}{kind_clause}
        ORDER BY saved_at DESC LIMIT 10
        """,
        base_params,
    )
    sample = [
        {
            "key": r["key"], "source": r["source"],
            "listingNo": r["listing_no"],
            "kind": _kind_of(r["entry_json"]),
        }
        for r in cur.fetchall()
    ]
    return {
        "source": source_count,
        "kindBreakdown": {
            "likes": int(breakdown["likes"] or 0),
            "dislikes": int(breakdown["dislikes"] or 0),
        },
        "conflictsOnTarget": conflicts,
        "tombstonesOnTarget": tombstones,
        "wouldCopy": source_count - (conflicts if body.on_conflict == "skip" else 0),
        "wouldOverwrite": conflicts if body.on_conflict == "overwrite" else 0,
        "wouldDeleteSource": source_count if body.mode == "move" else 0,
        "sample": sample,
    }


@router.post("/api/tool/favorites/transfer/preview")
def transfer_preview(body: TransferBody,
                     _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        return _transfer_plan(cur, body)


@router.post("/api/tool/favorites/transfer")
def transfer(body: TransferBody, request: Request,
             user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        plan = _transfer_plan(cur, body)

        # Compose the INSERT … SELECT so the whole operation is one
        # round-trip on the wire (fewer round-trips, but more importantly
        # one atomic commit point for audit consistency). The kind filter
        # rides along on both the source SELECT and the matching DELETE
        # so move/copy honour the user's like-vs-dislike scope.
        params: list = [body.to_user_id, body.from_user_id]
        key_clause = ""
        if body.keys:
            key_clause = " AND key = ANY(%s)"
            params.append(list(body.keys))
        kind_clause = _kind_clause(body.kind)
        conflict_clause = "ON CONFLICT (user_id, key) DO NOTHING"
        if body.on_conflict == "overwrite":
            conflict_clause = (
                "ON CONFLICT (user_id, key) DO UPDATE SET "
                "source = EXCLUDED.source, "
                "listing_no = EXCLUDED.listing_no, "
                "listing_id = EXCLUDED.listing_id, "
                "entry_json = EXCLUDED.entry_json, "
                "saved_at = EXCLUDED.saved_at, "
                "updated_at = now()"
            )
        cur.execute(
            f"""
            INSERT INTO favorites (user_id, key, source, listing_no,
                                   listing_id, entry_json, saved_at,
                                   created_at, updated_at)
            SELECT %s, key, source, listing_no, listing_id, entry_json,
                   saved_at, now(), now()
            FROM favorites
            WHERE user_id = %s{key_clause}{kind_clause}
            {conflict_clause}
            """,
            params,
        )
        copied = cur.rowcount or 0

        # Wipe tombstones on the target for the keys we just transferred
        # so the next user's "saved" check matches reality. Restrict the
        # subquery by the same kind so a like-only transfer doesn't blow
        # away a dislike tombstone on the target.
        cur.execute(
            f"""
            DELETE FROM favorite_deleted
            WHERE user_id = %s
              AND key IN (SELECT key FROM favorites
                          WHERE user_id = %s{key_clause}{kind_clause})
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
                f"DELETE FROM favorites WHERE user_id = %s{del_key_clause}{kind_clause}",
                del_params,
            )
            deleted_source = cur.rowcount or 0

        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="favorites_transfer",
            target_table="favorites",
            target_id=None,
            target_count=copied + deleted_source,
            before={"plan": plan},
            after={"copied": copied, "deletedSource": deleted_source},
            # No reverse — the move/copy mixes inserts and deletes across
            # two PKs and reverse_sql would balloon. Use the audit
            # before-snapshot + a manual restore if needed.
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"copied": copied, "deletedSource": deleted_source, "plan": plan}


class BulkDeleteBody(BaseModel):
    user_id: int
    keys: list[str] | None = None
    """If None or empty AND ``all=True``, delete every favorite for that
    user. The explicit ``all`` flag is what stops a UI bug from wiping a
    user's saves when ``keys`` was meant to be populated."""
    all: bool = False
    write_tombstones: bool = True
    """Insert into favorite_deleted so the browser's localStorage merge
    won't quietly resurrect the row on next page load."""
    kind: Literal["all", "like", "dislike"] = "all"
    """Scope the delete to one side. Combine with all=true to do e.g.
    "wipe this user's dislikes but keep their likes"."""


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
    kind_clause = _kind_clause(body.kind)
    cur.execute(
        f"SELECT count(*) AS n FROM favorites WHERE user_id = %s{key_clause}{kind_clause}",
        params,
    )
    n = int(cur.fetchone()["n"])
    cur.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE {_KIND_PREDICATES['like']}) AS likes,
          count(*) FILTER (WHERE {_KIND_PREDICATES['dislike']}) AS dislikes
        FROM favorites WHERE user_id = %s{key_clause}{kind_clause}
        """,
        params,
    )
    breakdown = cur.fetchone() or {"likes": 0, "dislikes": 0}
    cur.execute(
        f"""
        SELECT key, source, listing_no, entry_json, saved_at FROM favorites
        WHERE user_id = %s{key_clause}{kind_clause}
        ORDER BY saved_at DESC LIMIT 20
        """,
        params,
    )
    sample = [
        {
            "key": r["key"], "source": r["source"],
            "listingNo": r["listing_no"],
            "kind": _kind_of(r["entry_json"]),
            "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
        }
        for r in cur.fetchall()
    ]
    return {
        "wouldDelete": n,
        "kindBreakdown": {
            "likes": int(breakdown["likes"] or 0),
            "dislikes": int(breakdown["dislikes"] or 0),
        },
        "sample": sample,
    }


@router.post("/api/tool/favorites/bulk-delete/preview")
def bulk_delete_preview(body: BulkDeleteBody,
                        _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        return _bulk_delete_plan(cur, body)


@router.post("/api/tool/favorites/bulk-delete")
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
        kind_clause = _kind_clause(body.kind)
        if body.write_tombstones:
            cur.execute(
                f"""
                INSERT INTO favorite_deleted (user_id, key, deleted_at)
                SELECT user_id, key, now()
                FROM favorites WHERE user_id = %s{key_clause}{kind_clause}
                ON CONFLICT (user_id, key) DO UPDATE SET deleted_at = now()
                """,
                params,
            )
        cur.execute(
            f"DELETE FROM favorites WHERE user_id = %s{key_clause}{kind_clause}",
            params,
        )
        deleted = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="favorites_bulk_delete",
            target_table="favorites",
            target_id=None,
            target_count=deleted,
            before={"plan": plan},
            after={"deleted": deleted},
            reverse_sql=None,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"deleted": deleted, "plan": plan}
