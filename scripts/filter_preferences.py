"""Per-user UI filter preference store."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402


CONTEXT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_STATE_BYTES = 20_000


def _validate_context(context: str) -> str:
    value = str(context or "").strip()
    if not CONTEXT_RE.fullmatch(value):
        raise ValueError("context must match ^[A-Za-z0-9_.:-]{1,80}$")
    return value


def _normalize_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("state must be a JSON object")
    try:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("state must be JSON serializable") from exc
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError(f"state must be <= {MAX_STATE_BYTES} bytes")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("state must be a JSON object")
    return parsed


def load(user_id: int, context: str) -> dict[str, Any]:
    """Return a user's saved filter state for one page/context."""
    context = _validate_context(context)
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT state_json, updated_at
            FROM user_filter_preferences
            WHERE user_id = %s AND context = %s
            """,
            (user_id, context),
        )
        row = cur.fetchone()
    if not row:
        return {
            "context": context,
            "state": {},
            "updated_at": None,
            "is_default": True,
        }
    state = row["state_json"]
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except (TypeError, ValueError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    return {
        "context": context,
        "state": state,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "is_default": False,
    }


def save(user_id: int, context: str, state: Any) -> dict[str, Any]:
    context = _validate_context(context)
    normalized = _normalize_state(state)
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_filter_preferences (user_id, context, state_json, updated_at)
            VALUES (%s, %s, %s::jsonb, now())
            ON CONFLICT (user_id, context) DO UPDATE
            SET state_json = EXCLUDED.state_json,
                updated_at = now()
            """,
            (user_id, context, json.dumps(normalized, ensure_ascii=False)),
        )
    return load(user_id, context)
