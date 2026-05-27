"""Invite-code lifecycle for RentMap signup.

Replaces the single ``RENTMAP_SIGNUP_CODE`` env-var gate with a per-code DB
record: each code has an optional usage cap, optional expiry, and a revoke
flag. The signup endpoint calls :func:`validate_and_consume` which both
checks the code AND atomically bumps ``used_count`` — the cap check has to
happen inside the UPDATE so two concurrent signups can't both squeeze past
``used_count = max_uses - 1`` and end up at ``max_uses + 1``.

Admin endpoints in ``server.py`` use the other functions for the management
UI: :func:`list_invites` (with the per-code signup roster), :func:`create`,
:func:`update`, :func:`delete`.
"""

from __future__ import annotations

import secrets
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402


class InviteError(Exception):
    """Raised when a signup code is invalid for any reason.

    The ``reason`` attribute is one of: ``unknown``, ``revoked``, ``expired``,
    ``exhausted``. Callers translate it to an HTTP response — we don't raise
    HTTPException from this layer so non-FastAPI callers (CLIs, scripts) can
    use the same module.
    """

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


# Charset used for the auto-generated codes the admin UI offers. We avoid
# ``0/O`` and ``1/l/I`` so a code read aloud to a friend isn't ambiguous.
_AUTOGEN_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_code(length: int = 10) -> str:
    """Cryptographic-grade random invite code, hand-friendly charset."""
    if length < 4 or length > 64:
        raise ValueError("length must be between 4 and 64")
    return "".join(secrets.choice(_AUTOGEN_CHARSET) for _ in range(length))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Signup-time consume
# ──────────────────────────────────────────────────────────────────────────────

def validate_and_consume(code: str) -> int:
    """Atomically check + increment a code's used_count. Returns invite_code_id.

    Raises :class:`InviteError` (with a ``reason``) if the code is unknown,
    revoked, expired, or already at its usage cap. The single UPDATE
    statement is the choke point: a row only changes if all guards pass, so
    two parallel signups racing for the last seat will see one INSERT
    succeed (rows_affected=1) and the other observe rows_affected=0.
    """
    if not code:
        raise InviteError("unknown")
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE invite_codes
            SET used_count = used_count + 1
            WHERE code = %s
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
              AND (max_uses IS NULL OR used_count < max_uses)
            RETURNING id
            """,
            (code,),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        # No row updated → figure out *why* so the API can surface a useful
        # message. This second SELECT is non-racey from the user's POV: even
        # if state flipped between the UPDATE and SELECT, the code still
        # didn't accept *this* signup attempt.
        cur.execute(
            "SELECT revoked_at, expires_at, max_uses, used_count "
            "FROM invite_codes WHERE code = %s",
            (code,),
        )
        row = cur.fetchone()
    if not row:
        raise InviteError("unknown", "Unknown invite code")
    if row["revoked_at"] is not None:
        raise InviteError("revoked", "This invite code has been revoked")
    if row["expires_at"] is not None and row["expires_at"] <= _now():
        raise InviteError("expired", "This invite code has expired")
    if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
        raise InviteError("exhausted", "This invite code's signup limit has been reached")
    # Fallback: shouldn't normally hit this — treat as unknown.
    raise InviteError("unknown", "Invite code is not accepting signups")


# ──────────────────────────────────────────────────────────────────────────────
# Admin queries / mutations
# ──────────────────────────────────────────────────────────────────────────────

def _status(row: dict[str, Any], now: datetime) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
        return "exhausted"
    return "active"


def _serialize(row: dict[str, Any], now: datetime, signups: list[dict] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "note": row["note"],
        "maxUses": row["max_uses"],
        "usedCount": row["used_count"],
        "expiresAt": row["expires_at"].isoformat() if row["expires_at"] else None,
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "createdBy": row.get("created_by"),
        "createdByUsername": row.get("created_by_username"),
        "revokedAt": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "status": _status(row, now),
        "signups": signups or [],
    }


def list_invites() -> list[dict[str, Any]]:
    """All codes (active + revoked + exhausted) with the signup roster per code.

    Roster query is one extra round-trip; we keep it separate from the main
    SELECT because grouping JSON in SQL fights with psycopg's dict_row
    factory and the row count is small enough that two queries are simpler.
    """
    now = _now()
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ic.id, ic.code, ic.note, ic.max_uses, ic.used_count,
                   ic.expires_at, ic.created_by, ic.created_at, ic.revoked_at,
                   creator.username AS created_by_username
            FROM invite_codes ic
            LEFT JOIN users creator ON creator.id = ic.created_by
            ORDER BY ic.created_at DESC, ic.id DESC
            """
        )
        rows = cur.fetchall()
        if not rows:
            return []
        cur.execute(
            """
            SELECT invite_code_id, id, username, display_name, created_at
            FROM users
            WHERE invite_code_id IS NOT NULL
            ORDER BY created_at ASC
            """
        )
        signup_map: dict[int, list[dict[str, Any]]] = {}
        for u in cur.fetchall():
            signup_map.setdefault(u["invite_code_id"], []).append({
                "userId": u["id"],
                "username": u["username"],
                "displayName": u["display_name"],
                "joinedAt": u["created_at"].isoformat() if u["created_at"] else None,
            })
    return [_serialize(dict(r), now, signup_map.get(r["id"], [])) for r in rows]


def create_invite(
    *,
    code: str | None,
    note: str | None = None,
    max_uses: int | None = None,
    expires_at: datetime | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    """Insert a new invite code. ``code=None`` auto-generates a random one.

    Validates that ``max_uses`` is None or >=1 and that ``expires_at`` is
    in the future. Raises :class:`InviteError("duplicate")` if the code
    string is already taken (unique constraint).
    """
    code_value = (code or "").strip() or generate_code()
    if not (4 <= len(code_value) <= 64):
        raise InviteError("invalid", "code must be 4-64 characters")
    if max_uses is not None and max_uses < 1:
        raise InviteError("invalid", "max_uses must be >= 1 or null")
    if expires_at is not None and expires_at <= _now():
        raise InviteError("invalid", "expires_at must be in the future")

    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO invite_codes (code, note, max_uses, expires_at, created_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, code, note, max_uses, used_count,
                          expires_at, created_at, created_by, revoked_at
                """,
                (code_value, (note or None), max_uses, expires_at, created_by),
            )
            row = dict(cur.fetchone())
            if created_by is not None:
                cur.execute(
                    "SELECT username FROM users WHERE id = %s", (created_by,)
                )
                creator = cur.fetchone()
                row["created_by_username"] = creator["username"] if creator else None
            else:
                row["created_by_username"] = None
    except Exception as exc:
        # psycopg.errors.UniqueViolation has class_name="UniqueViolation";
        # we duck-type so this module stays importable even when
        # psycopg.errors symbol layout changes between versions.
        if exc.__class__.__name__ == "UniqueViolation":
            raise InviteError("duplicate", "Invite code already exists") from exc
        raise
    return _serialize(row, _now(), [])


def update_invite(
    invite_id: int,
    *,
    note: str | None = None,
    max_uses: int | None = None,
    expires_at: datetime | None = None,
    revoked: bool | None = None,
    # Sentinel so we can tell "leave field alone" from "set to NULL".
    update_note: bool = False,
    update_max_uses: bool = False,
    update_expires_at: bool = False,
) -> dict[str, Any]:
    """PATCH-style update. Only fields whose ``update_*`` flag is True change.

    ``revoked=True`` sets revoked_at=now() (if not already revoked).
    ``revoked=False`` clears revoked_at (re-activate, useful for a mistaken
    revoke). max_uses or expires_at validation matches :func:`create_invite`.
    """
    fields: list[str] = []
    values: list[Any] = []
    if update_note:
        fields.append("note = %s")
        values.append(note)
    if update_max_uses:
        if max_uses is not None and max_uses < 1:
            raise InviteError("invalid", "max_uses must be >= 1 or null")
        fields.append("max_uses = %s")
        values.append(max_uses)
    if update_expires_at:
        if expires_at is not None and expires_at <= _now():
            raise InviteError("invalid", "expires_at must be in the future")
        fields.append("expires_at = %s")
        values.append(expires_at)
    if revoked is True:
        fields.append("revoked_at = COALESCE(revoked_at, now())")
    elif revoked is False:
        fields.append("revoked_at = NULL")
    if not fields:
        raise InviteError("invalid", "No changes requested")
    values.append(invite_id)
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE invite_codes
            SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, code, note, max_uses, used_count,
                      expires_at, created_at, created_by, revoked_at
            """,
            values,
        )
        row = cur.fetchone()
        if not row:
            raise InviteError("unknown", "Invite code not found")
        row = dict(row)
        if row["created_by"] is not None:
            cur.execute("SELECT username FROM users WHERE id = %s", (row["created_by"],))
            creator = cur.fetchone()
            row["created_by_username"] = creator["username"] if creator else None
        else:
            row["created_by_username"] = None
        cur.execute(
            """
            SELECT id, username, display_name, created_at
            FROM users
            WHERE invite_code_id = %s
            ORDER BY created_at ASC
            """,
            (invite_id,),
        )
        signups = [
            {
                "userId": u["id"],
                "username": u["username"],
                "displayName": u["display_name"],
                "joinedAt": u["created_at"].isoformat() if u["created_at"] else None,
            }
            for u in cur.fetchall()
        ]
    return _serialize(row, _now(), signups)


def delete_invite(invite_id: int) -> dict[str, Any]:
    """Hard-delete a code. Refuses if any user has it set as invite_code_id.

    Use :func:`update_invite` with ``revoked=True`` for the normal "kill
    this code" flow — delete is reserved for cleaning up never-used codes.
    """
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE invite_code_id = %s", (invite_id,))
        n = cur.fetchone()["n"] or 0
        if n:
            raise InviteError(
                "in_use",
                f"Cannot delete: {n} user(s) signed up with this code. Revoke instead."
            )
        cur.execute(
            "DELETE FROM invite_codes WHERE id = %s RETURNING code",
            (invite_id,),
        )
        row = cur.fetchone()
        if not row:
            raise InviteError("unknown", "Invite code not found")
    return {"id": invite_id, "code": row["code"], "deleted": True}


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap helper — call from server startup to seed the legacy env-var code.
# ──────────────────────────────────────────────────────────────────────────────

def seed_env_code_if_missing(env_code: str | None) -> dict[str, Any] | None:
    """Idempotently register the legacy ``RENTMAP_SIGNUP_CODE`` value.

    Returns the seeded row (or None if env_code is falsy / already present).
    Called once at server boot so an upgrade from the old single-code system
    keeps working without manual SQL.
    """
    code = (env_code or "").strip()
    if not code:
        return None
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO invite_codes (code, note, max_uses, expires_at, created_by)
            VALUES (%s, %s, NULL, NULL, NULL)
            ON CONFLICT (code) DO NOTHING
            RETURNING id, code, note, max_uses, used_count,
                      expires_at, created_at, created_by, revoked_at
            """,
            (code, "RENTMAP_SIGNUP_CODE seed"),
        )
        row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    row["created_by_username"] = None
    return _serialize(row, _now(), [])
