"""Admin routes: users and invites management."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api import auth
from app.api import invites as invite_store
from app.db import session as db_session

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
DATA_DIR = "data"
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
_ALLOWED_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_INVITE_ERROR_STATUS = {
    "unknown": 400, "revoked": 400, "expired": 400,
    "exhausted": 400, "invalid": 400,
    "duplicate": 409, "in_use": 409,
}


def _invite_http_error(exc: "invite_store.InviteError") -> HTTPException:
    return HTTPException(
        status_code=_INVITE_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


router = APIRouter()

def _admin_user_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"] or row["username"],
        "isAdmin": row["is_admin"],
        "isActive": row["is_active"],
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "lastLoginAt": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
        "sessions": int(row.get("sessions") or 0),
        "favorites": int(row.get("favorites") or 0),
        "deletedFavorites": int(row.get("deleted_favorites") or 0),
        "hasAreaFilter": bool(row.get("has_area_filter")),
        "photoCount": int(row.get("photo_count") or 0),
    }


def _count_user_photos(user_id: int) -> int:
    root = Path(PHOTOS_DIR) / str(int(user_id))
    if not root.exists():
        return 0
    return sum(
        1
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _ALLOWED_PHOTO_EXTS
    )


def _list_user_photos(user_id: int) -> list[dict[str, Any]]:
    root = Path(PHOTOS_DIR) / str(int(user_id))
    if not root.exists():
        return []
    photos: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _ALLOWED_PHOTO_EXTS:
            continue
        rel = p.relative_to(Path(DATA_DIR)).as_posix()
        photos.append({
            "name": p.name,
            "folder": p.parent.name,
            "url": f"/data/{rel}",
            "size": p.stat().st_size,
            "modifiedAt": datetime.fromtimestamp(p.stat().st_mtime, TZ).isoformat(),
        })
    return photos


class AdminCreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool = False


class AdminUpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool | None = None
    is_active: bool | None = None


class AdminResetPasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=200)


# Cached hash of a sentinel password used to equalize login response time for
# missing usernames (timing-based username enumeration defense). Computed once
# at import — bcrypt(12) is ~250ms; doing it on every miss would be wasteful.
_DUMMY_PW_HASH = auth.hash_password("__rentmap_dummy_password__")


_INVITE_ERROR_STATUS = {
    "unknown": 400,
    "revoked": 400,
    "expired": 400,
    "exhausted": 400,
    "invalid": 400,
    "duplicate": 409,
    "in_use": 409,
}


def _invite_http_error(exc: "invite_store.InviteError") -> HTTPException:
    return HTTPException(
        status_code=_INVITE_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


@router.post("/api/auth/signup")
@router.get("/api/admin/users")
async def admin_list_users(_admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.is_active,
                   u.created_at, u.last_login_at,
                   COUNT(DISTINCT s.id) AS sessions,
                   COUNT(DISTINCT f.key) AS favorites,
                   COUNT(DISTINCT d.key) AS deleted_favorites,
                   (af.user_id IS NOT NULL) AS has_area_filter
            FROM users u
            LEFT JOIN sessions s ON s.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            LEFT JOIN favorite_deleted d ON d.user_id = u.id
            LEFT JOIN user_area_filters af ON af.user_id = u.id
            GROUP BY u.id, af.user_id
            ORDER BY u.id
            """
        )
        rows = cur.fetchall()
    users = []
    for row in rows:
        row = dict(row)
        row["photo_count"] = _count_user_photos(row["id"])
        users.append(_admin_user_dict(row))
    return {"users": users}


@router.post("/api/admin/users")
async def admin_create_user(body: AdminCreateUserBody,
                            _admin: auth.User = Depends(auth.current_admin)):
    username = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
        raise HTTPException(
            status_code=400,
            detail="Username may contain letters, digits, '.', '_', '-' only",
        )
    display_name = (body.display_name or "").strip() or username
    try:
        with db_session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name, is_admin)
                VALUES (%s, %s, %s, %s)
                RETURNING id, username, display_name, is_admin, is_active,
                          created_at, last_login_at
                """,
                (username, auth.hash_password(body.password), display_name, body.is_admin),
            )
            row = dict(cur.fetchone())
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Username already taken")
    row.update({
        "sessions": 0,
        "favorites": 0,
        "deleted_favorites": 0,
        "has_area_filter": False,
        "photo_count": 0,
    })
    return {"user": _admin_user_dict(row)}


@router.get("/api/admin/users/{user_id}")
async def admin_get_user(user_id: int, _admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.is_active,
                   u.created_at, u.last_login_at,
                   COUNT(DISTINCT s.id) AS sessions,
                   COUNT(DISTINCT f.key) AS favorites,
                   COUNT(DISTINCT d.key) AS deleted_favorites,
                   (af.user_id IS NOT NULL) AS has_area_filter
            FROM users u
            LEFT JOIN sessions s ON s.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            LEFT JOIN favorite_deleted d ON d.user_id = u.id
            LEFT JOIN user_area_filters af ON af.user_id = u.id
            WHERE u.id = %s
            GROUP BY u.id, af.user_id
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        cur.execute(
            """
            SELECT id, created_at, expires_at, last_seen_at, user_agent, ip
            FROM sessions
            WHERE user_id = %s
            ORDER BY last_seen_at DESC
            """,
            (user_id,),
        )
        sessions = [
            {
                "id": s["id"],
                "createdAt": s["created_at"].isoformat(),
                "expiresAt": s["expires_at"].isoformat(),
                "lastSeenAt": s["last_seen_at"].isoformat(),
                "userAgent": s["user_agent"],
                "ip": str(s["ip"]) if s["ip"] is not None else None,
            }
            for s in cur.fetchall()
        ]
    row = dict(row)
    row["photo_count"] = _count_user_photos(user_id)
    try:
        favorites_state = fav_store.load_state(user_id)
    except Exception as exc:
        favorites_state = {"favorites": [], "deleted": {}, "error": str(exc)}
    try:
        area_filter = area_store.load(user_id)
    except Exception as exc:
        area_filter = {"error": str(exc)}
    return {
        "user": _admin_user_dict(row),
        "sessions": sessions,
        "favoritesState": favorites_state,
        "areaFilter": area_filter,
        "photos": _list_user_photos(user_id),
    }


@router.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: AdminUpdateUserBody,
                            admin: auth.User = Depends(auth.current_admin)):
    fields: list[str] = []
    values: list[Any] = []
    if body.display_name is not None:
        fields.append("display_name = %s")
        values.append(body.display_name.strip() or None)
    if body.is_admin is not None:
        if user_id == admin.id and not body.is_admin:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin role")
        fields.append("is_admin = %s")
        values.append(body.is_admin)
    if body.is_active is not None:
        if user_id == admin.id and not body.is_active:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
        fields.append("is_active = %s")
        values.append(body.is_active)
    if not fields:
        raise HTTPException(status_code=400, detail="No changes requested")
    values.append(user_id)
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE users SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, username, display_name, is_admin, is_active,
                      created_at, last_login_at
            """,
            values,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if body.is_active is False:
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    row = dict(row)
    row.update({
        "sessions": 0,
        "favorites": 0,
        "deleted_favorites": 0,
        "has_area_filter": False,
        "photo_count": _count_user_photos(user_id),
    })
    return {"user": _admin_user_dict(row)}


@router.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, body: AdminResetPasswordBody,
                               _admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s RETURNING username",
            (auth.hash_password(body.password), user_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    return {"ok": True, "username": row["username"]}


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, admin: auth.User = Depends(auth.current_admin)):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    with db_session() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s RETURNING username", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
    photo_dir = Path(PHOTOS_DIR) / str(int(user_id))
    if photo_dir.exists():
        shutil.rmtree(photo_dir, ignore_errors=True)
    return {"ok": True, "username": row["username"]}


# ─────────────────────────────────────────────────────────────────────────────
# Invite codes (admin)
# ─────────────────────────────────────────────────────────────────────────────

class AdminCreateInviteBody(BaseModel):
    # All optional — server fills sensible defaults. ``code=None`` triggers
    # auto-generation; ``max_uses=None`` means unlimited; ``expires_at=None``
    # means no expiry.
    code: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=200)
    max_uses: int | None = Field(default=None, ge=1, le=10000)
    expires_at: datetime | None = None


class AdminUpdateInviteBody(BaseModel):
    # Same sentinel-vs-null trick as the user PATCH: a missing key leaves the
    # field alone; an explicit ``null`` clears it. Pydantic v2 distinguishes
    # via ``model_fields_set`` which we read below.
    note: str | None = None
    max_uses: int | None = Field(default=None, ge=1, le=10000)
    expires_at: datetime | None = None
    revoked: bool | None = None


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Pydantic gives us a tz-aware datetime if ISO had a TZ, naive otherwise.
    DB column is TIMESTAMPTZ — coerce naive to UTC so comparisons line up."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("/api/admin/invites")
async def admin_list_invites(_admin: auth.User = Depends(auth.current_admin)):
    return {"invites": invite_store.list_invites()}


@router.post("/api/admin/invites")
async def admin_create_invite(body: AdminCreateInviteBody,
                              admin: auth.User = Depends(auth.current_admin)):
    try:
        invite = invite_store.create_invite(
            code=body.code,
            note=body.note,
            max_uses=body.max_uses,
            expires_at=_ensure_utc(body.expires_at),
            created_by=admin.id,
        )
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return {"invite": invite}


@router.patch("/api/admin/invites/{invite_id}")
async def admin_update_invite(invite_id: int, body: AdminUpdateInviteBody,
                              _admin: auth.User = Depends(auth.current_admin)):
    # model_fields_set tells us which keys the client actually sent — that's
    # how we distinguish "don't touch" from "explicitly clear to null".
    sent = body.model_fields_set
    try:
        invite = invite_store.update_invite(
            invite_id,
            note=body.note,
            max_uses=body.max_uses,
            expires_at=_ensure_utc(body.expires_at),
            revoked=body.revoked,
            update_note="note" in sent,
            update_max_uses="max_uses" in sent,
            update_expires_at="expires_at" in sent,
        )
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return {"invite": invite}


@router.delete("/api/admin/invites/{invite_id}")
async def admin_delete_invite(invite_id: int,
                              _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = invite_store.delete_invite(invite_id)
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return result


