"""User-management routes for the DB-tool.

Distinct from /api/admin/users in scripts/server.py: this tool runs
out-of-band so an admin's hand-edit can't crash the public service.
Every mutate writes one admin_audit_log row in the same transaction.
Destructive operations (delete-user) have ``dry-run`` siblings the UI
hits first so the operator sees row-counts before committing.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
from app.api import auth as rm_auth

from . import audit, deps

router = APIRouter()

PHOTOS_DIR = ROOT / "data" / "photos"


def _user_row(cur, user_id: int) -> dict[str, Any]:
    cur.execute(
        "SELECT id, username, display_name, is_admin, is_active, "
        "       created_at, last_login_at "
        "FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return row


def _serialize_user(row: dict[str, Any], extras: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "username": row["username"],
        "displayName": row.get("display_name") or row["username"],
        "isAdmin": bool(row.get("is_admin")),
        "isActive": bool(row.get("is_active")),
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "lastLoginAt": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
    }
    if extras:
        out.update(extras)
    return out


@router.get("/api/tool/users")
def list_users(_: rm_auth.User = Depends(deps.require_admin)):
    """Single LEFT JOIN aggregate so the list table can render counts
    without N+1 round-trips."""
    with deps.tx() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.is_active,
                   u.created_at, u.last_login_at,
                   COALESCE(fav_c.n, 0) AS favorites,
                   COALESCE(sess_c.n, 0) AS sessions,
                   (SELECT 1 FROM user_area_filters
                      WHERE user_id = u.id LIMIT 1) IS NOT NULL AS has_area_filter
            FROM users u
            LEFT JOIN (
              SELECT user_id, count(*) AS n FROM favorites GROUP BY user_id
            ) fav_c ON fav_c.user_id = u.id
            LEFT JOIN (
              SELECT user_id, count(*) AS n FROM sessions
                WHERE expires_at > now() GROUP BY user_id
            ) sess_c ON sess_c.user_id = u.id
            ORDER BY u.id
            """
        )
        rows = cur.fetchall()
    users = [
        _serialize_user(
            r,
            extras={
                "favorites": int(r["favorites"] or 0),
                "sessions": int(r["sessions"] or 0),
                "hasAreaFilter": bool(r["has_area_filter"]),
            },
        )
        for r in rows
    ]
    return {"users": users}


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool = False


@router.post("/api/tool/users")
def create_user(body: CreateUserBody, request: Request,
                user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    pw_hash = rm_auth.hash_password(body.password)
    with deps.tx() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (body.username,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="username already exists")
        cur.execute(
            """
            INSERT INTO users (username, password_hash, display_name, is_admin)
            VALUES (%s, %s, %s, %s)
            RETURNING id, username, display_name, is_admin, is_active,
                      created_at, last_login_at
            """,
            (body.username, pw_hash, body.display_name or body.username, body.is_admin),
        )
        new_row = cur.fetchone()
        # Reverse-SQL: a clean DELETE by id is safe because the row is
        # fresh — no favorites/sessions/area-filter exist yet for it.
        reverse = audit.build_reverse_delete(
            cur.connection, "users", {"id": new_row["id"]}
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="create_user",
            target_table="users",
            target_id=new_row["id"],
            before=None,
            after={
                "id": new_row["id"],
                "username": new_row["username"],
                "display_name": new_row["display_name"],
                "is_admin": new_row["is_admin"],
                "is_active": new_row["is_active"],
            },
            reverse_sql=reverse,
            cmd_payload=body.dict(),
            request_ip=ip, request_path=path,
        )
    return {"user": _serialize_user(new_row)}


class UpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool | None = None
    is_active: bool | None = None


@router.patch("/api/tool/users/{user_id}")
def update_user(user_id: int, body: UpdateUserBody, request: Request,
                user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    fields = body.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if user.id == user_id and "is_admin" in fields and fields["is_admin"] is False:
        # Refuse to demote yourself — prevents an admin from accidentally
        # locking themselves out of the tool with no way back.
        raise HTTPException(status_code=400, detail="cannot remove your own admin flag")
    if user.id == user_id and "is_active" in fields and fields["is_active"] is False:
        raise HTTPException(status_code=400, detail="cannot deactivate yourself")
    with deps.tx() as cur:
        before = _user_row(cur, user_id)
        sets = []
        params = []
        for key, val in fields.items():
            sets.append(f"{key} = %s")
            params.append(val)
        params.append(user_id)
        cur.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, username, display_name, is_admin, is_active, "
            "created_at, last_login_at",
            params,
        )
        after = cur.fetchone()
        # If is_active flipped to FALSE, also kill sessions — same policy
        # as the existing CLI.
        sessions_killed = 0
        if fields.get("is_active") is False and before["is_active"]:
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            sessions_killed = cur.rowcount or 0
        # Compute audit diff.
        b_diff, a_diff = audit.diff_columns(before, after)
        reverse = audit.build_reverse_update(
            cur.connection, "users", {"id": user_id}, b_diff
        )
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="update_user",
            target_table="users",
            target_id=user_id,
            before=b_diff,
            after=a_diff,
            reverse_sql=reverse,
            cmd_payload=fields,
            request_ip=ip, request_path=path,
        )
    return {
        "user": _serialize_user(after),
        "sessionsKilled": sessions_killed,
    }


class ResetPasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=200)


@router.post("/api/tool/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPasswordBody, request: Request,
                   user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    pw_hash = rm_auth.hash_password(body.password)
    with deps.tx() as cur:
        cur.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="user not found")
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (pw_hash, user_id))
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        sessions_killed = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="reset_password",
            target_table="users",
            target_id=user_id,
            # No reverse — we don't store the old hash for rollback because
            # we never want the previous password reusable from the audit log.
            before={"password": "***"},
            after={"password": "***"},
            reverse_sql=None,
            cmd_payload=body.dict(),  # scrubbed by audit.record
            request_ip=ip, request_path=path,
        )
    return {"ok": True, "sessionsKilled": sessions_killed}


@router.post("/api/tool/users/{user_id}/kill-sessions")
def kill_sessions(user_id: int, request: Request,
                  user: rm_auth.User = Depends(deps.require_admin)):
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        _user_row(cur, user_id)
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        killed = cur.rowcount or 0
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="kill_sessions",
            target_table="sessions",
            target_id=None,
            target_count=killed,
            before=None,
            after=None,
            reverse_sql=None,
            cmd_payload={"user_id": user_id},
            request_ip=ip, request_path=path,
        )
    return {"sessionsKilled": killed}


@router.get("/api/tool/users/{user_id}/sessions")
def list_user_sessions(user_id: int,
                       _: rm_auth.User = Depends(deps.require_admin)):
    with deps.tx() as cur:
        _user_row(cur, user_id)
        cur.execute(
            """
            SELECT id, created_at, last_seen_at, expires_at, user_agent, ip
            FROM sessions
            WHERE user_id = %s
            ORDER BY last_seen_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    return {
        "sessions": [
            {
                # Token id is sensitive — never expose the full value. Hash
                # prefix is enough to identify a row in the UI.
                "id": (r["id"] or "")[:8] + "…",
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
                "lastSeenAt": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                "expiresAt": r["expires_at"].isoformat() if r["expires_at"] else None,
                "userAgent": r["user_agent"],
                "ip": str(r["ip"]) if r["ip"] else None,
                "isDbtool": (r["user_agent"] or "").startswith("[dbtool] "),
            }
            for r in rows
        ]
    }


@router.get("/api/tool/users/{user_id}/delete-preview")
def delete_preview(user_id: int,
                   _: rm_auth.User = Depends(deps.require_admin)):
    """Dry-run that shows what a DELETE on this user would cascade.

    The UI calls this before showing the modal confirm so the operator
    sees concrete row counts ("이 작업으로 N개 찜, M개 세션이 삭제됩니다").
    """
    with deps.tx() as cur:
        _user_row(cur, user_id)
        cur.execute("SELECT count(*) AS n FROM favorites WHERE user_id = %s", (user_id,))
        favs = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*) AS n FROM favorite_deleted WHERE user_id = %s",
                    (user_id,))
        tombs = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*) AS n FROM sessions WHERE user_id = %s", (user_id,))
        sessions = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT 1 FROM user_area_filters WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        has_area = cur.fetchone() is not None
        cur.execute("SELECT count(*) AS n FROM user_webhooks WHERE user_id = %s",
                    (user_id,))
        hooks = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM user_filter_preferences WHERE user_id = %s",
            (user_id,),
        )
        prefs = int(cur.fetchone()["n"])
    photo_dir = PHOTOS_DIR / str(user_id)
    photo_count = sum(1 for _ in photo_dir.rglob("*") if _.is_file()) if photo_dir.exists() else 0
    return {
        "favorites": favs,
        "favoriteDeleted": tombs,
        "sessions": sessions,
        "userAreaFilter": has_area,
        "userWebhooks": hooks,
        "filterPreferences": prefs,
        "photoFiles": photo_count,
        "photoDir": str(photo_dir),
    }


class DeleteUserBody(BaseModel):
    confirm_username: str = Field(min_length=1)
    """Operator must retype the username — same defense as gh/git destructive
    confirms. Prevents the API from being abused by a stale UI click."""


@router.delete("/api/tool/users/{user_id}")
def delete_user(user_id: int, body: DeleteUserBody, request: Request,
                user: rm_auth.User = Depends(deps.require_admin)):
    if user.id == user_id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    ip, path = deps.request_context(request)
    with deps.tx() as cur:
        before = _user_row(cur, user_id)
        if body.confirm_username.strip().lower() != str(before["username"]).lower():
            raise HTTPException(status_code=400,
                                detail="confirmation username does not match")
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        # No reverse — CASCADE took out favorites/sessions/etc. that we
        # don't snapshot for rollback. Operator restores from a backup if
        # needed; audit row preserves attribution + intent.
        audit.record(
            cur,
            actor=deps.actor_from(user),
            action="delete_user",
            target_table="users",
            target_id=user_id,
            before={
                "id": before["id"],
                "username": before["username"],
                "is_admin": before["is_admin"],
                "is_active": before["is_active"],
            },
            after=None,
            reverse_sql=None,
            cmd_payload={"user_id": user_id},
            request_ip=ip, request_path=path,
        )
    photo_dir = PHOTOS_DIR / str(user_id)
    removed_photos = False
    if photo_dir.exists():
        shutil.rmtree(photo_dir, ignore_errors=True)
        removed_photos = True
    return {"ok": True, "photosRemoved": removed_photos}
