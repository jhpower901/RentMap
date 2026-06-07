"""Auth routes: signup, login, logout, me."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api import auth
from app.api import invites as invite_store

router = APIRouter()

# sentinel hash for constant-time comparison on unknown usernames
_DUMMY_PW_HASH = auth.hash_password("__rentmap_dummy_password__")

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


class SignupBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    code: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


def _public_user(user: auth.User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "isAdmin": user.is_admin,
    }


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


@router.post("/api/auth/signup")
async def auth_signup(body: SignupBody, request: Request, response: Response):
    # Code → invite_codes lookup. Atomically bumps used_count if the code is
    # still active; raises InviteError otherwise (translated to 400/409).
    try:
        invite_id = invite_store.validate_and_consume(body.code)
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)

    username = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
        # User-facing error AFTER consuming an invite use is unfortunate but
        # rare (Pydantic validation already covers shape; this regex is the
        # extra char-class check). Cheaper than rolling back the consume on
        # every malformed username.
        raise HTTPException(
            status_code=400,
            detail="Username may contain letters, digits, '.', '_', '-' only",
        )

    pw_hash = auth.hash_password(body.password)
    display_name = (body.display_name or "").strip() or username

    # Race-safety: two concurrent signups for the same username both pass the
    # SELECT and rely on the UNIQUE constraint to catch the duplicate. The
    # admin-flag race (both observe count=0) is harder to eliminate cheaply,
    # so we approximate by serializing the count→insert under a pg advisory
    # lock keyed to a constant so concurrent signups queue rather than race
    # on the admin decision. The lock auto-releases on transaction end.
    try:
        with db_session() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(98231)")
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Username already taken")
            # First user in the system becomes admin so the operator can run
            # migrate-globals immediately after first signup if they prefer
            # that over `users.py create-admin`.
            cur.execute("SELECT COUNT(*) AS n FROM users")
            is_first = (cur.fetchone()["n"] == 0)
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name,
                                   is_admin, last_login_at, invite_code_id)
                VALUES (%s, %s, %s, %s, now(), %s)
                RETURNING id, username, display_name, is_admin, is_active
                """,
                (username, pw_hash, display_name, is_first, invite_id),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        # Belt-and-suspenders for the (advisory-lock-bypassed) race: if a
        # second connection sneaks in after the SELECT, the UNIQUE constraint
        # still fires. Translate to a sensible 409 instead of leaking 500.
        raise HTTPException(status_code=409, detail="Username already taken")

    user = auth.User(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        is_admin=row["is_admin"], is_active=row["is_active"],
    )
    token, expires_at = auth.create_session(
        user.id,
        user_agent=request.headers.get("user-agent"),
        ip=auth.get_client_ip(request),
    )
    auth.set_session_cookie(response, token, expires_at, request)
    return {"user": _public_user(user)}


@router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, display_name, is_admin, is_active "
            "FROM users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        # Timing-equalize: if the user doesn't exist we still spend bcrypt
        # time verifying against a sentinel hash so a remote attacker can't
        # tell "no such user" from "wrong password" by stopwatch.
        if not row or not row["is_active"]:
            auth.verify_password(body.password, _DUMMY_PW_HASH)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))

    user = auth.User(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        is_admin=row["is_admin"], is_active=row["is_active"],
    )
    token, expires_at = auth.create_session(
        user.id,
        user_agent=request.headers.get("user-agent"),
        ip=auth.get_client_ip(request),
    )
    auth.set_session_cookie(response, token, expires_at, request)
    return {"user": _public_user(user)}


@router.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    # Same-origin middleware guard already runs ahead of this handler, so a
    # cross-site form-submit POST to /api/auth/logout never reaches here.
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    auth.clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/api/auth/me")
async def auth_me(user: auth.User = Depends(auth.current_user)):
    return {"user": _public_user(user)}


