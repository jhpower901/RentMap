"""Postgres-backed favorites store, replacing the older sqlite path.

Wire format and merge semantics match the old sqlite implementation so
the client doesn't need to change:

    {
        "favorites": [{key, id, source, savedAt, data, rating, notes, ...}, ...],
        "deleted":   {"<key>": "<iso-timestamp>", ...}
    }

A POST merges incoming state with existing rows, preferring whichever side
has the later ``savedAt`` per key. A row whose key is in ``deleted`` with a
later timestamp than its ``savedAt`` is suppressed (tombstone wins) — that's
how a delete on device A propagates to device B.

source values stay client-side ('dabang', 'daangn', 'zigbang', 'naver',
'manual'). For crawled sources we resolve the matching ``listings.id`` so
favorite rows can join back to the live listing; 'naver' is mapped to the
platforms.code 'naver_land' on the way in.

All operations are scoped to ``user_id`` — favorites are per-user since the
self-login system landed. The 003 migration added ``user_id`` as nullable and
the operator backfills via ``scripts/users.py migrate-globals --to <user>``;
004 promotes it to NOT NULL and swaps the primary key to ``(user_id, key)``.
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

# UI source code → platforms.code in the DB.
_SOURCE_TO_PLATFORM_CODE = {
    "dabang": "dabang",
    "daangn": "daangn",
    "zigbang": "zigbang",
    "naver": "naver_land",
    "peterpan": "peterpan",
    # 'manual' has no matching platform; resolve_listing_id() returns None.
}


def _iso_time(value: Any) -> float:
    """Match server.py's existing semantics: best-effort parse, 0 on miss.

    Comparisons in merge_favorites / filter_deleted use this so an absent
    timestamp loses to any present one.
    """
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def normalize_payload(payload: Any) -> dict[str, Any]:
    """Coerce client payload (legacy list or modern dict) to the canonical shape."""
    if isinstance(payload, list):
        return {"favorites": payload, "deleted": {}}
    if isinstance(payload, dict):
        favorites = payload.get("favorites")
        deleted = payload.get("deleted")
        return {
            "favorites": favorites if isinstance(favorites, list) else [],
            "deleted": deleted if isinstance(deleted, dict) else {},
        }
    return {"favorites": [], "deleted": {}}


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


def _parse_ts(value: str | None) -> datetime:
    """Convert an ISO string to UTC datetime. Empty → epoch, which sorts last."""
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def load_state(user_id: int) -> dict[str, Any]:
    """Read this user's favorites state from Postgres, applying the tombstone filter.

    Returns the same shape the client expects: a dict with ``favorites`` (list
    sorted newest-first) and ``deleted`` (key → iso timestamp).
    """
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT key, deleted_at FROM favorite_deleted WHERE user_id = %s",
            (user_id,),
        )
        deleted: dict[str, str] = {
            row["key"]: row["deleted_at"].isoformat() for row in cur.fetchall()
        }
        cur.execute(
            "SELECT key, entry_json, saved_at FROM favorites "
            "WHERE user_id = %s ORDER BY saved_at DESC",
            (user_id,),
        )
        favorites: list[dict[str, Any]] = []
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
            # Tombstone check: if a key was deleted after it was saved, hide it.
            if _iso_time(deleted.get(key)) >= _iso_time(entry.get("savedAt")):
                continue
            favorites.append(entry)
    return {"favorites": favorites, "deleted": deleted}


def _pick_field(a: dict, b: dict, field: str, ts_field: str) -> Any:
    """Pick `field` between two entries using their per-field timestamp.

    Presence beats absence so a device that explicitly edited a field always
    wins over one that never touched it (even if the latter's savedAt is newer
    from an edit on a different field). When neither has the per-field stamp
    — legacy entries — fall back to whole-entry savedAt.
    """
    a_ts = a.get(ts_field)
    b_ts = b.get(ts_field)
    if a_ts and b_ts:
        return b.get(field) if _iso_time(b_ts) >= _iso_time(a_ts) else a.get(field)
    if b_ts:
        return b.get(field)
    if a_ts:
        return a.get(field)
    if _iso_time(b.get("savedAt")) >= _iso_time(a.get("savedAt")):
        return b.get(field)
    return a.get(field)


def _pick_max_ts(a_ts: Any, b_ts: Any) -> Any:
    if a_ts and b_ts:
        return b_ts if _iso_time(b_ts) >= _iso_time(a_ts) else a_ts
    return b_ts or a_ts or None


def _merge_entries(a: dict, b: dict) -> dict:
    """Field-by-field merge of two same-key entries.

    A concurrent rating edit on device A and notes edit on device B both
    survive — without this the whole-entry savedAt tiebreak would let one
    device's stale rating overwrite the other's update. data/kind come from
    whichever side's savedAt is newer (entry-level base); rating and notes
    are overlaid by their own timestamps.
    """
    base = b if _iso_time(b.get("savedAt")) >= _iso_time(a.get("savedAt")) else a
    out = dict(base)
    out["rating"] = _pick_field(a, b, "rating", "ratingUpdatedAt")
    out["notes"] = _pick_field(a, b, "notes", "notesUpdatedAt")
    r_ts = _pick_max_ts(a.get("ratingUpdatedAt"), b.get("ratingUpdatedAt"))
    if r_ts:
        out["ratingUpdatedAt"] = r_ts
    n_ts = _pick_max_ts(a.get("notesUpdatedAt"), b.get("notesUpdatedAt"))
    if n_ts:
        out["notesUpdatedAt"] = n_ts
    # firstVisitedAt is monotonic — earliest known visit wins.
    a_fv = a.get("firstVisitedAt")
    b_fv = b.get("firstVisitedAt")
    if a_fv and b_fv:
        out["firstVisitedAt"] = a_fv if _iso_time(a_fv) <= _iso_time(b_fv) else b_fv
    elif a_fv or b_fv:
        out["firstVisitedAt"] = a_fv or b_fv
    return out


def merge_payload(user_id: int, incoming: Any) -> dict[str, Any]:
    """Merge an incoming POST payload with current DB state and persist.

    Per-key resolution (scoped to this user):
      - Tombstones merge by max(deleted_at) per key.
      - Favorites: same-key duplicates collapse via _merge_entries (field-level
        merge by per-field timestamps). Tombstone with later timestamp kills
        both sides.
    """
    incoming_state = normalize_payload(incoming)
    with session() as conn, conn.cursor() as cur:
        # Pull existing state in the same transaction so the read+write is consistent.
        cur.execute(
            "SELECT key, deleted_at FROM favorite_deleted WHERE user_id = %s",
            (user_id,),
        )
        existing_deleted: dict[str, str] = {
            row["key"]: row["deleted_at"].isoformat() for row in cur.fetchall()
        }
        cur.execute(
            "SELECT key, entry_json, saved_at FROM favorites WHERE user_id = %s",
            (user_id,),
        )
        existing_favs: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            entry = row["entry_json"]
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except (TypeError, ValueError):
                    continue
            if isinstance(entry, dict) and entry.get("key"):
                existing_favs[entry["key"]] = entry

        # ── Merge deletions ────────────────────────────────────────────────
        merged_deleted: dict[str, str] = dict(existing_deleted)
        for key, value in incoming_state["deleted"].items():
            if isinstance(key, str) and isinstance(value, str):
                if _iso_time(value) >= _iso_time(merged_deleted.get(key)):
                    merged_deleted[key] = value

        # ── Merge favorites ────────────────────────────────────────────────
        merged_favs: dict[str, dict[str, Any]] = {}
        for entry in list(existing_favs.values()) + incoming_state["favorites"]:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            if _iso_time(merged_deleted.get(key)) >= _iso_time(entry.get("savedAt")):
                continue
            prev = merged_favs.get(key)
            merged_favs[key] = _merge_entries(prev, entry) if prev else entry

        # ── Persist (full replace for this user; the merged result IS the source of truth) ─
        cur.execute("DELETE FROM favorites WHERE user_id = %s", (user_id,))
        for entry in merged_favs.values():
            saved_at = _parse_ts(entry.get("savedAt"))
            source = str(entry.get("source") or "")
            listing_no = str(entry.get("id") or "")
            if not entry.get("key") or not source or not listing_no:
                continue
            listing_id = _resolve_listing_id(cur, source, listing_no)
            cur.execute(
                """
                INSERT INTO favorites (user_id, key, source, listing_no, listing_id,
                                       entry_json, saved_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, now(), now())
                """,
                (
                    user_id, entry["key"], source, listing_no, listing_id,
                    json.dumps(entry, ensure_ascii=False), saved_at,
                ),
            )

        cur.execute("DELETE FROM favorite_deleted WHERE user_id = %s", (user_id,))
        for key, deleted_at in merged_deleted.items():
            cur.execute(
                "INSERT INTO favorite_deleted (user_id, key, deleted_at) VALUES (%s, %s, %s)",
                (user_id, key, _parse_ts(deleted_at)),
            )

    return load_state(user_id)


# ──────────────────────────────────────────────────────────────────────────────
# One-shot migration from sqlite — call from a script when the postgres table
# is empty and a legacy data/rentmap.db sqlite is present.
# ──────────────────────────────────────────────────────────────────────────────

def import_from_sqlite(user_id: int, sqlite_path: str | Path) -> dict[str, int]:
    """Copy sqlite favorites + deleted into Postgres for ``user_id``, idempotent.

    No-ops if postgres already has rows for that user (assume the import was
    done). Returns counts of rows considered / inserted for logging.
    """
    import sqlite3

    summary = {"sqlite_favs": 0, "sqlite_deleted": 0, "skipped": 0}
    path = Path(sqlite_path)
    if not path.exists():
        log.warning("sqlite favorites file not found at %s; nothing to import", path)
        return summary

    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM favorites WHERE user_id = %s", (user_id,))
        existing = cur.fetchone()["n"]
        if existing:
            log.info("postgres favorites already has %d rows for user_id=%s; import skipped",
                     existing, user_id)
            summary["skipped"] = existing
            return summary

    sconn = sqlite3.connect(str(path))
    sconn.row_factory = sqlite3.Row
    try:
        favs = []
        for row in sconn.execute("SELECT entry_json FROM favorites"):
            try:
                entry = json.loads(row["entry_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(entry, dict) and entry.get("key"):
                favs.append(entry)
        deleted = {
            row["key"]: row["deleted_at"]
            for row in sconn.execute("SELECT key, deleted_at FROM favorite_deleted")
        }
    finally:
        sconn.close()

    summary["sqlite_favs"] = len(favs)
    summary["sqlite_deleted"] = len(deleted)
    merge_payload(user_id, {"favorites": favs, "deleted": deleted})
    log.info(
        "imported %d favorites + %d deletions from sqlite for user_id=%s",
        summary["sqlite_favs"], summary["sqlite_deleted"], user_id,
    )
    return summary


if __name__ == "__main__":
    # CLI entry: `python scripts/favorites.py import` — runs sqlite → pg one-shot.
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Favorites postgres utilities.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_import = sub.add_parser("import", help="One-shot sqlite → postgres import")
    p_import.add_argument("--user-id", type=int, required=True)
    p_import.add_argument("--sqlite", default="data/rentmap.db")
    p_dump = sub.add_parser("dump", help="Print a user's postgres state as JSON")
    p_dump.add_argument("--user-id", type=int, required=True)
    args = parser.parse_args()
    if args.cmd == "import":
        print(import_from_sqlite(args.user_id, args.sqlite))
    elif args.cmd == "dump":
        print(json.dumps(load_state(args.user_id), default=str, ensure_ascii=False, indent=2))
