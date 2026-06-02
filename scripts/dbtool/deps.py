"""FastAPI dependencies + request-state helpers for the DB-tool.

The tool re-uses RentMap's bcrypt + sessions tables but issues its own
cookie name (``rentmap_dbtool_session``) so a logged-in admin browsing
the public site doesn't end up auto-authenticated against the DB-tool
just because the cookies happen to live on the same TLD when SSH-tunneled.

The tool only accepts users with ``is_admin = TRUE``. Non-admin
accounts get a 403 on every protected route.
"""

from __future__ import annotations

import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from fastapi import Cookie, HTTPException, Request, Response

# Reuse the production password hashing + User dataclass.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import auth as rm_auth  # noqa: E402
from db import connect, session as db_session  # noqa: E402

from .audit import Actor  # noqa: E402


# Distinct cookie name + table so the DB-tool's sessions can't be
# replayed against the main site (and vice versa). Same TTL though.
COOKIE_NAME = "rentmap_dbtool_session"
SESSION_TTL = timedelta(hours=12)
SESSION_TOUCH_INTERVAL = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Session helpers — share the ``sessions`` table but partition by user_agent
# prefix so a row inserted by the main service can be distinguished from a
# DB-tool row at lookup time. The prefix is data-only; the table schema
# doesn't change.
# ──────────────────────────────────────────────────────────────────────────────

_DBTOOL_UA_TAG = "[dbtool] "


def create_session(user_id: int, *, user_agent: str | None = None,
                   ip: str | None = None) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + SESSION_TTL
    tagged_ua = _DBTOOL_UA_TAG + (user_agent or "")
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (id, user_id, expires_at, user_agent, ip)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (token, user_id, expires_at, tagged_ua, ip),
        )
    return token, expires_at


def revoke_session(token: str) -> None:
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sessions WHERE id = %s AND user_agent LIKE %s",
            (token, _DBTOOL_UA_TAG + "%"),
        )


def lookup_session(token: str) -> Optional[rm_auth.User]:
    """Return the admin behind a DB-tool session, or None.

    Filters by the ``[dbtool] `` UA prefix so a main-site cookie that
    somehow ended up with a matching value can't authenticate here.
    Inactive or non-admin users also return None — the 403 is raised by
    the route dependency, not here, so the *no session* and *not-admin*
    cases stay distinct in the logs.
    """
    if not token:
        return None
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS sid, s.expires_at, s.last_seen_at,
                   u.id, u.username, u.display_name, u.is_admin, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = %s
              AND s.user_agent LIKE %s
            """,
            (token, _DBTOOL_UA_TAG + "%"),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["expires_at"] <= _now():
            cur.execute("DELETE FROM sessions WHERE id = %s", (row["sid"],))
            return None
        if not row["is_active"]:
            return None
        if _now() - row["last_seen_at"] > SESSION_TOUCH_INTERVAL:
            cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE id = %s",
                (row["sid"],),
            )
        return rm_auth.User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            is_admin=row["is_admin"],
            is_active=row["is_active"],
        )


def set_cookie(response: Response, token: str, expires_at: datetime) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=int((expires_at - _now()).total_seconds()),
        expires=int(expires_at.timestamp()),
        path="/",
        httponly=True,
        # Tool is 127.0.0.1-only behind SSH tunnel — plain HTTP locally.
        secure=False,
        samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(
        key=COOKIE_NAME, path="/", httponly=True, secure=False, samesite="lax"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Dependencies
# ──────────────────────────────────────────────────────────────────────────────

def require_admin(
    request: Request,
    rentmap_dbtool_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> rm_auth.User:
    """Raise 401 if no session, 403 if the session belongs to a non-admin.

    The two-status split exists so the UI can redirect-to-login on 401
    but show "this account is not allowed" on 403 — they're visibly
    different conditions to the operator.
    """
    cached = getattr(request.state, "user", None)
    if isinstance(cached, rm_auth.User):
        if not cached.is_admin:
            raise HTTPException(status_code=403, detail="Admin only")
        return cached
    user = lookup_session(rentmap_dbtool_session) if rentmap_dbtool_session else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    request.state.user = user
    return user


def actor_from(user: rm_auth.User) -> Actor:
    """Lift a logged-in User into an Actor for the audit writer."""
    return Actor(id=user.id, username=user.username)


def request_context(request: Request) -> tuple[str | None, str | None]:
    """Extract (ip, path) for audit row metadata. Both can be None when
    a route is invoked from a non-HTTP context (e.g. a future CLI hook).
    """
    ip = None
    if request.client:
        ip = request.client.host
    return ip, request.url.path


# ──────────────────────────────────────────────────────────────────────────────
# Transaction context manager exposed to route handlers
# ──────────────────────────────────────────────────────────────────────────────

@contextmanager
def tx() -> Iterator[psycopg.Cursor]:
    """One transaction → one cursor. Same shape as ``db.transaction()`` in
    the main app, redeclared here so route handlers don't have to know
    about the import path."""
    with db_session(autocommit=False) as conn:
        with conn.cursor() as cur:
            yield cur
