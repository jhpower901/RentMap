"""Postgres-backed bookmarks store.

Mirrors scripts/favorites.py — same wire format, same merge semantics — so the
client uses an identical "POST current state, server merges and returns
canonical" loop. The difference is intent: favorites = 관심표시 (like/dislike),
bookmarks = 실사 기록용 완충지대 ("첫 번째 본 방, 두 번째 본 방" ordered list
with notes / photos / checklist per visit). The two tables are independent;
a listing can be both liked and bookmarked.

Wire format (per user):

    {
        "bookmarks": [
            {key, id, source, sortOrder, savedAt, data, note, checklist, ...},
            ...
        ],
        "deleted": {"<key>": "<iso-timestamp>", ...}
    }

A POST merges incoming state with existing rows. Per-key resolution: latest
``savedAt`` wins. ``sortOrder`` rides on the surviving entry's payload —
clients are responsible for choosing the order when they add/reorder, and the
server just persists their decision. The ``sort_order`` column is a
denormalised copy of ``entry_json->>'sortOrder'`` so ORDER BY stays cheap.

Photos are shared with favorites via /api/photos (file path is keyed by
``user_id + source + listing_no``), so a user uploading a photo for a
bookmarked room sees the same shots on the favorites side and vice versa.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import session  # noqa: E402

log = logging.getLogger(__name__)

# UI source code → platforms.code in the DB. Same mapping favorites.py uses;
# kept duplicated rather than imported so bookmarks.py is standalone.
_SOURCE_TO_PLATFORM_CODE = {
    "dabang": "dabang",
    "daangn": "daangn",
    "zigbang": "zigbang",
    "naver": "naver_land",
    "peterpan": "peterpan",
}


def _iso_time(value: Any) -> float:
    """Best-effort ISO parse; absent / malformed → 0 so any present wins."""
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _coerce_sort_order(value: Any) -> int:
    """Pull sortOrder out of an entry payload, defaulting to 0.

    A bad / missing value lands at the top of the list (sort_order=0) so the
    user notices something is off rather than silently burying the row.
    """
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_payload(payload: Any) -> dict[str, Any]:
    """Coerce a client payload into {bookmarks: [...], deleted: {...}}."""
    if isinstance(payload, list):
        return {"bookmarks": payload, "deleted": {}}
    if isinstance(payload, dict):
        bookmarks = payload.get("bookmarks")
        deleted = payload.get("deleted")
        return {
            "bookmarks": bookmarks if isinstance(bookmarks, list) else [],
            "deleted": deleted if isinstance(deleted, dict) else {},
        }
    return {"bookmarks": [], "deleted": {}}


def _resolve_listing_id(cur, source: str, listing_no: str) -> int | None:
    code = _SOURCE_TO_PLATFORM_CODE.get(source)
    if not code or not listing_no:
        return None
    cur.execute(
        "SELECT id FROM listings WHERE platform_id = (SELECT id FROM platforms WHERE code = %s) "
        "AND platform_listing_id = %s",
        (code, listing_no),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def load_state(user_id: int) -> dict[str, Any]:
    """Read this user's bookmarks, tombstone-filtered, ordered by sort_order."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, deleted_at FROM bookmark_deleted WHERE user_id = %s",
            (user_id,),
        )
        deleted: dict[str, str] = {
            row["key"]: row["deleted_at"].isoformat() for row in cur.fetchall()
        }
        # Primary order is sort_order (the "first room I saw, second room I
        # saw" sequence). saved_at is the tiebreaker so a ZERO default at
        # the top still falls into add-order.
        cur.execute(
            "SELECT key, entry_json, saved_at, sort_order FROM bookmarks "
            "WHERE user_id = %s ORDER BY sort_order ASC, saved_at ASC",
            (user_id,),
        )
        bookmarks: list[dict[str, Any]] = []
        for row in cur.fetchall():
            entry = row["entry_json"]
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except (TypeError, ValueError):
                    continue
            if not isinstance(entry, dict):
                continue
            key = entry.get("key") or row["key"]
            if _iso_time(deleted.get(key)) >= _iso_time(entry.get("savedAt")):
                continue
            # Keep sortOrder mirrored from the column so a stale entry_json
            # value can't argue with what we actually return ORDER BY on.
            entry["sortOrder"] = int(row["sort_order"] or 0)
            bookmarks.append(entry)
    return {"bookmarks": bookmarks, "deleted": deleted}


def merge_payload(user_id: int, incoming: Any) -> dict[str, Any]:
    """Merge POST payload with current DB state. Latest savedAt per key wins."""
    incoming_state = normalize_payload(incoming)
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, deleted_at FROM bookmark_deleted WHERE user_id = %s",
            (user_id,),
        )
        existing_deleted: dict[str, str] = {
            row["key"]: row["deleted_at"].isoformat() for row in cur.fetchall()
        }
        cur.execute(
            "SELECT key, entry_json FROM bookmarks WHERE user_id = %s",
            (user_id,),
        )
        existing_bms: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            entry = row["entry_json"]
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except (TypeError, ValueError):
                    continue
            if isinstance(entry, dict) and entry.get("key"):
                existing_bms[entry["key"]] = entry

        merged_deleted: dict[str, str] = dict(existing_deleted)
        for key, value in incoming_state["deleted"].items():
            if isinstance(key, str) and isinstance(value, str):
                if _iso_time(value) >= _iso_time(merged_deleted.get(key)):
                    merged_deleted[key] = value

        merged_bms: dict[str, dict[str, Any]] = {}
        for entry in list(existing_bms.values()) + incoming_state["bookmarks"]:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            if _iso_time(merged_deleted.get(key)) >= _iso_time(entry.get("savedAt")):
                continue
            prev = merged_bms.get(key)
            if prev is None or _iso_time(entry.get("savedAt")) >= _iso_time(prev.get("savedAt")):
                merged_bms[key] = entry

        cur.execute("DELETE FROM bookmarks WHERE user_id = %s", (user_id,))
        for entry in merged_bms.values():
            saved_at = _parse_ts(entry.get("savedAt"))
            source = str(entry.get("source") or "")
            listing_no = str(entry.get("id") or "")
            if not entry.get("key") or not source or not listing_no:
                continue
            listing_id = _resolve_listing_id(cur, source, listing_no)
            sort_order = _coerce_sort_order(entry.get("sortOrder"))
            cur.execute(
                """
                INSERT INTO bookmarks (user_id, key, source, listing_no, listing_id,
                                       sort_order, entry_json, saved_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, now(), now())
                """,
                (
                    user_id, entry["key"], source, listing_no, listing_id,
                    sort_order, json.dumps(entry, ensure_ascii=False), saved_at,
                ),
            )

        cur.execute("DELETE FROM bookmark_deleted WHERE user_id = %s", (user_id,))
        for key, deleted_at in merged_deleted.items():
            cur.execute(
                "INSERT INTO bookmark_deleted (user_id, key, deleted_at) VALUES (%s, %s, %s)",
                (user_id, key, _parse_ts(deleted_at)),
            )

    return load_state(user_id)
