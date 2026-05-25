"""Authentication primitives + FastAPI dependencies for RentMap.

Wire format:
  - Browser holds an opaque session token in the ``rentmap_session`` cookie,
    HttpOnly + SameSite=Lax (+ Secure in prod).
  - Server validates by looking the token up in ``sessions`` and joining to
    ``users``. No JWT — DB lookup is one indexed query per request, and lets
    us revoke any session immediately by deleting a row.

Password hashing uses passlib's bcrypt scheme — the canonical default for
FastAPI tutorials and good enough for a personal multi-tenant deployment.

This module is intentionally narrow: it owns hashing, session lifecycle,
and the FastAPI ``Depends`` shims. It does NOT own:
  - user creation (see scripts/users.py / signup endpoint in server.py)
  - HTML guard middleware (see server.py — middleware sits closer to the route
    layer because it needs to know about static file paths)
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402

# bcrypt 4.x refuses passwords over 72 bytes outright (no silent truncation).
# We SHA-256 → base64 the plaintext first so arbitrarily long passwords map to
# a stable 44-byte buffer that's always under the limit. This is the same
# trick passlib's bcrypt_sha256 uses; we just inline it to keep the auth path
# independent of passlib's known compatibility problems with bcrypt 4.x.
_BCRYPT_ROUNDS = 12


def _prehash(plain: str) -> bytes:
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.b64encode(digest)

COOKIE_NAME = "rentmap_session"
SESSION_TTL = timedelta(days=30)
# Refresh ``last_seen_at`` at most this often per request — avoids one UPDATE
# per page load when a user is clicking around.
SESSION_TOUCH_INTERVAL = timedelta(minutes=10)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    is_active: bool


# ──────────────────────────────────────────────────────────────────────────────
# Passwords
# ──────────────────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(_prehash(plain), salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash or non-ASCII input — treat as auth failure.
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(user_id: int, *, user_agent: str | None = None,
                   ip: str | None = None) -> tuple[str, datetime]:
    """Insert a new session row and return ``(token, expires_at)``."""
    token = secrets.token_urlsafe(32)
    expires_at = _now() + SESSION_TTL
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (id, user_id, expires_at, user_agent, ip)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (token, user_id, expires_at, user_agent, ip),
        )
    return token, expires_at


def revoke_session(token: str) -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE id = %s", (token,))


def revoke_all_sessions(user_id: int) -> int:
    with session() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        return cur.rowcount or 0


def lookup_session(token: str) -> Optional[User]:
    """Return the user behind a session token, or None.

    Side effect: bumps ``sessions.last_seen_at`` at most once per
    ``SESSION_TOUCH_INTERVAL`` so we're not writing on every API call.
    Expired or revoked tokens — and tokens belonging to deactivated users —
    return None.
    """
    if not token:
        return None
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS sid, s.expires_at, s.last_seen_at,
                   u.id, u.username, u.display_name, u.is_admin, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = %s
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["expires_at"] <= _now():
            cur.execute("DELETE FROM sessions WHERE id = %s", (row["sid"],))
            return None
        if not row["is_active"]:
            return None
        # Throttled touch.
        if _now() - row["last_seen_at"] > SESSION_TOUCH_INTERVAL:
            cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE id = %s",
                (row["sid"],),
            )
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            is_admin=row["is_admin"],
            is_active=row["is_active"],
        )


# ──────────────────────────────────────────────────────────────────────────────
# Cookie helpers
# ──────────────────────────────────────────────────────────────────────────────

def _secure_cookies(request: Request | None = None) -> bool:
    """Decide whether the session cookie should be HTTPS-only.

    ``RENTMAP_SESSION_SECURE`` may explicitly force this on/off. If unset or
    set to ``auto``, infer it from the request so HTTPS deployments keep
    secure cookies while plain-http LAN/mobile development still works.
    """
    setting = os.environ.get("RENTMAP_SESSION_SECURE", "auto").strip().lower()
    if setting in {"1", "true", "yes", "on"}:
        return True
    if setting in {"0", "false", "no", "off"}:
        return False
    if request is None:
        return True

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return forwarded_proto == "https" or request.url.scheme == "https"


def set_session_cookie(response: Response, token: str, expires_at: datetime, request: Request | None = None) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=int((expires_at - _now()).total_seconds()),
        expires=int(expires_at.timestamp()),
        path="/",
        httponly=True,
        secure=_secure_cookies(request),
        samesite="lax",
    )


def clear_session_cookie(response: Response, request: Request | None = None) -> None:
    # Set expiry in the past so the browser drops the cookie even if it was
    # marked Secure by an earlier deploy.
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_secure_cookies(request),
        samesite="lax",
    )


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ──────────────────────────────────────────────────────────────────────────────

def current_user(
    rentmap_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    """Required-auth dependency for API endpoints.

    The middleware in server.py already 401s un-authenticated API requests
    before they get here, but using this dependency is what wires user_id
    into the call sites without each handler re-reading the cookie.
    """
    user = lookup_session(rentmap_session) if rentmap_session else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def current_user_optional(
    rentmap_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> Optional[User]:
    return lookup_session(rentmap_session) if rentmap_session else None


def current_admin(
    rentmap_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    user = current_user(rentmap_session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# ──────────────────────────────────────────────────────────────────────────────
# Misc
# ──────────────────────────────────────────────────────────────────────────────

def signup_code_required() -> str:
    """Return the configured signup code, or raise to refuse signups.

    A missing/empty ``RENTMAP_SIGNUP_CODE`` is treated as "signup disabled"
    rather than "open registration" — fail closed.
    """
    code = (os.environ.get("RENTMAP_SIGNUP_CODE") or "").strip()
    if not code:
        raise HTTPException(
            status_code=503,
            detail="Signup is disabled (RENTMAP_SIGNUP_CODE not set).",
        )
    return code


def get_client_ip(request: Request) -> str | None:
    """Best-effort client IP, honoring an X-Forwarded-For from Caddy."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None
