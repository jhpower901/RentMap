"""Per-user webhook registrations (Discord and Slack).

Each user may register up to MAX_WEBHOOKS_PER_USER webhook URLs and configure
which events / platforms / price ranges trigger a notification. The webhook
worker (webhook_worker.py) fans out new listing_status_events to all matching
active webhooks via the webhook_deliveries table.

Same error-convention as regions.py: WebhookError with a string ``reason``
translated to HTTP at the server.py boundary.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402

_DISCORD_URL_RE = re.compile(
    r'^https://discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$'
)
_SLACK_URL_RE = re.compile(
    r'^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+$'
)

VALID_EVENT_TYPES = frozenset([
    "discovered", "price_changed", "detail_changed",
    "removed", "reappeared", "agent_changed", "image_changed",
])
VALID_PLATFORMS = frozenset(["dabang", "daangn", "zigbang", "naver_land"])
MAX_WEBHOOKS_PER_USER = 5

DEFAULT_EVENT_TYPES = ["discovered", "price_changed", "removed", "reappeared"]
DEFAULT_PLATFORMS = ["dabang", "daangn", "zigbang", "naver_land"]


class WebhookError(Exception):
    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "label": row["label"] or "",
        "webhookUrl": row["webhook_url"],
        "isActive": row["is_active"],
        "eventTypes": list(row["event_types"] or []),
        "platforms": list(row["platforms"] or []),
        "maxDepositManwon": row["max_deposit_manwon"],
        "maxRentManwon": row["max_rent_manwon"],
        "useAreaFilter": row["use_area_filter"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def detect_webhook_type(url: str) -> str:
    """Return 'discord', 'slack', or 'unknown'."""
    if _DISCORD_URL_RE.match(url):
        return "discord"
    if _SLACK_URL_RE.match(url):
        return "slack"
    return "unknown"


def _validate_url(url: Any) -> str:
    s = str(url or "").strip()
    if detect_webhook_type(s) == "unknown":
        raise WebhookError("invalid", "webhook_url must be a valid Discord or Slack webhook URL")
    return s


def _validate_event_types(types: Any) -> list[str]:
    if not isinstance(types, (list, tuple)):
        raise WebhookError("invalid", "event_types must be a list")
    cleaned: list[str] = []
    for t in types:
        s = str(t)
        if s not in VALID_EVENT_TYPES:
            raise WebhookError("invalid", f"unknown event type {s!r}")
        if s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        raise WebhookError("invalid", "event_types must not be empty")
    return cleaned


def _validate_platforms(platforms: Any) -> list[str]:
    if not isinstance(platforms, (list, tuple)):
        raise WebhookError("invalid", "platforms must be a list")
    cleaned: list[str] = []
    for p in platforms:
        s = str(p)
        if s not in VALID_PLATFORMS:
            raise WebhookError("invalid", f"unknown platform {s!r}")
        if s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        raise WebhookError("invalid", "platforms must not be empty")
    return cleaned


def list_webhooks(user_id: int) -> list[dict[str, Any]]:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM user_webhooks WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        )
        return [_serialize(dict(r)) for r in cur.fetchall()]


def get_webhook(webhook_id: int, user_id: int) -> dict[str, Any]:
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM user_webhooks WHERE id = %s", (webhook_id,))
        row = cur.fetchone()
    if not row:
        raise WebhookError("unknown", "Webhook not found")
    row = dict(row)
    if row["user_id"] != user_id:
        raise WebhookError("forbidden", "Webhook not found")
    return _serialize(row)


def create_webhook(
    user_id: int,
    *,
    label: str = "",
    webhook_url: str,
    event_types: list[str] | None = None,
    platforms: list[str] | None = None,
    max_deposit_manwon: int | None = None,
    max_rent_manwon: int | None = None,
    use_area_filter: bool = True,
) -> dict[str, Any]:
    url = _validate_url(webhook_url)
    etypes = _validate_event_types(event_types if event_types is not None else DEFAULT_EVENT_TYPES)
    plats = _validate_platforms(platforms if platforms is not None else DEFAULT_PLATFORMS)
    label_clean = str(label or "").strip()[:80]

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM user_webhooks WHERE user_id = %s",
            (user_id,),
        )
        if cur.fetchone()["cnt"] >= MAX_WEBHOOKS_PER_USER:
            raise WebhookError(
                "limit", f"Maximum {MAX_WEBHOOKS_PER_USER} webhooks per user"
            )
        cur.execute(
            """
            INSERT INTO user_webhooks (
                user_id, label, webhook_url, is_active,
                event_types, platforms,
                max_deposit_manwon, max_rent_manwon, use_area_filter
            )
            VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, label_clean, url, etypes, plats,
             max_deposit_manwon, max_rent_manwon, use_area_filter),
        )
        new_id = cur.fetchone()["id"]
        cur.execute("SELECT * FROM user_webhooks WHERE id = %s", (new_id,))
        return _serialize(dict(cur.fetchone()))


class _Unset:
    pass


_UNSET: Any = _Unset()


def update_webhook(
    webhook_id: int,
    user_id: int,
    *,
    label: Any = _UNSET,
    webhook_url: Any = _UNSET,
    is_active: Any = _UNSET,
    event_types: Any = _UNSET,
    platforms: Any = _UNSET,
    max_deposit_manwon: Any = _UNSET,
    max_rent_manwon: Any = _UNSET,
    use_area_filter: Any = _UNSET,
) -> dict[str, Any]:
    fields: list[str] = []
    values: list[Any] = []

    def _set(col: str, val: Any) -> None:
        fields.append(f"{col} = %s")
        values.append(val)

    if label is not _UNSET:
        _set("label", str(label or "").strip()[:80])
    if webhook_url is not _UNSET:
        _set("webhook_url", _validate_url(webhook_url))
    if is_active is not _UNSET:
        _set("is_active", bool(is_active))
    if event_types is not _UNSET:
        _set("event_types", _validate_event_types(event_types))
    if platforms is not _UNSET:
        _set("platforms", _validate_platforms(platforms))
    if max_deposit_manwon is not _UNSET:
        _set("max_deposit_manwon", None if max_deposit_manwon is None else int(max_deposit_manwon))
    if max_rent_manwon is not _UNSET:
        _set("max_rent_manwon", None if max_rent_manwon is None else int(max_rent_manwon))
    if use_area_filter is not _UNSET:
        _set("use_area_filter", bool(use_area_filter))

    if not fields:
        raise WebhookError("invalid", "No changes requested")

    fields.append("updated_at = now()")
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_webhooks WHERE id = %s FOR UPDATE",
            (webhook_id,),
        )
        row = cur.fetchone()
        if not row:
            raise WebhookError("unknown", "Webhook not found")
        if row["user_id"] != user_id:
            raise WebhookError("forbidden", "Webhook not found")
        values.append(webhook_id)
        cur.execute(
            f"UPDATE user_webhooks SET {', '.join(fields)} WHERE id = %s",
            values,
        )
        cur.execute("SELECT * FROM user_webhooks WHERE id = %s", (webhook_id,))
        return _serialize(dict(cur.fetchone()))


def delete_webhook(webhook_id: int, user_id: int) -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_webhooks WHERE id = %s FOR UPDATE",
            (webhook_id,),
        )
        row = cur.fetchone()
        if not row:
            raise WebhookError("unknown", "Webhook not found")
        if row["user_id"] != user_id:
            raise WebhookError("forbidden", "Webhook not found")
        cur.execute("DELETE FROM user_webhooks WHERE id = %s", (webhook_id,))


def list_active_for_fanout() -> list[dict[str, Any]]:
    """Return all active webhooks joined with the owner's area-filter polygon.

    Called by webhook_worker during fan-out. The area polygon may be NULL if
    the user never saved one — in that case use_area_filter has no effect.
    """
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                w.id, w.user_id, w.webhook_url,
                w.event_types, w.platforms,
                w.max_deposit_manwon, w.max_rent_manwon,
                w.use_area_filter,
                uaf.points_json,
                uaf.enabled AS area_filter_enabled
            FROM user_webhooks w
            LEFT JOIN user_area_filters uaf ON uaf.user_id = w.user_id
            WHERE w.is_active = TRUE
            """
        )
        return [dict(r) for r in cur.fetchall()]
