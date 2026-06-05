"""Login / logout / me — the only routes a logged-out caller can reach.

Login does NOT call ``audit.record`` because there's no committed user
state to attribute to; failed logins are silently rate-limited via the
existing bcrypt-cost wall (same as the public site). Successful logouts
do log themselves so an audit reader can see when a session ended.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auth as rm_auth  # noqa: E402
from db import session as db_session  # noqa: E402

from . import deps
from . import audit


router = APIRouter()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


# Dummy hash to equalize timing for unknown usernames — same defense as the
# main service. bcrypt(12) is ~250ms; computing it once here avoids the
# attacker learning "user exists" from response time.
_DUMMY_PW_HASH = rm_auth.hash_password("__rentmap_dbtool_dummy__")


@router.post("/api/tool/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, is_admin, is_active "
            "FROM users WHERE username = %s",
            (body.username.strip(),),
        )
        row = cur.fetchone()

    pw_hash = row["password_hash"] if row else _DUMMY_PW_HASH
    ok = rm_auth.verify_password(body.password, pw_hash)
    if not row or not ok:
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="비활성 계정입니다.")
    if not row["is_admin"]:
        # Same wording as 403 elsewhere — the user is who they claim to be,
        # they just don't have the role.
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    user_agent = request.headers.get("user-agent") or None
    ip = request.client.host if request.client else None
    token, expires_at = deps.create_session(row["id"], user_agent=user_agent, ip=ip)
    deps.set_cookie(response, token, expires_at)

    # Touch last_login_at so the operator can spot stale admin accounts on
    # the users tab.
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET last_login_at = now() WHERE id = %s",
            (row["id"],),
        )

    return {
        "user": {
            "id": row["id"],
            "username": row["username"],
            "isAdmin": row["is_admin"],
        }
    }


@router.post("/api/tool/auth/logout")
async def logout(request: Request, response: Response,
                 rentmap_dbtool_session: str | None = Cookie(default=None,
                                                              alias=deps.COOKIE_NAME)):
    user = deps.lookup_session(rentmap_dbtool_session) if rentmap_dbtool_session else None
    if rentmap_dbtool_session:
        deps.revoke_session(rentmap_dbtool_session)
    deps.clear_cookie(response)
    if user:
        ip, path = deps.request_context(request)
        with deps.tx() as cur:
            audit.record(
                cur,
                actor=deps.actor_from(user),
                action="logout",
                target_table="sessions",
                target_id=None,
                target_count=1,
                request_ip=ip, request_path=path,
            )
    return {"ok": True}


@router.get("/api/tool/auth/me")
async def me(request: Request,
             rentmap_dbtool_session: str | None = Cookie(default=None,
                                                          alias=deps.COOKIE_NAME)):
    """Cheap probe used by every page to decide redirect-to-login.

    Does NOT raise on missing session — returns ``{user: null}`` so a
    fetch() can branch without try/except. The protected routes do raise.
    """
    user = deps.lookup_session(rentmap_dbtool_session) if rentmap_dbtool_session else None
    if not user:
        return {"user": None}
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "displayName": user.display_name or user.username,
            "isAdmin": user.is_admin,
        }
    }
