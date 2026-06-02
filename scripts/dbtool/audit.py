"""Audit-log writer for the DB-tool.

Every mutate handler in the DB-tool calls :func:`record` *inside the same
transaction* as its data change. That guarantee — one transaction covers
both the change and its audit row — is the entire reason we keep
admin_audit_log in the same Postgres rather than shipping events to a
separate sink.

The module also owns:

* :func:`scrub_payload` — strips sensitive keys (passwords, hashes,
  raw cookies) from API payloads before they land in cmd_payload. Audit
  trails are read by other admins; plaintext credentials must never enter
  them.
* :func:`diff_columns` — picks only the columns whose value actually
  changed for the ``before_json`` / ``after_json`` JSONB blobs. Storing
  full rows would inflate the table and make the UI noisier.
* :func:`build_reverse_update` / :func:`build_reverse_insert` —
  parameter-safe reverse-SQL builders. Returned strings are the literal
  text we persist in ``reverse_sql``; the rollback handler re-executes
  them verbatim, so the quoting needs to be psycopg-safe (we use
  ``psycopg.sql`` to enforce that).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

import psycopg
from psycopg import sql


# Keys that must never reach admin_audit_log.cmd_payload. Anything that
# *contains* one of these substrings is scrubbed. Case-insensitive.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "session",
)


@dataclass(frozen=True)
class Actor:
    """Thin wrapper on (user_id, username).

    The audit row stores both the FK and a snapshotted username so the row
    stays readable after the user is later deleted (FK then SET NULL).
    """
    id: int
    username: str


# ──────────────────────────────────────────────────────────────────────────────
# Sanitization + diff helpers
# ──────────────────────────────────────────────────────────────────────────────

def scrub_payload(payload: Any) -> Any:
    """Return a deep copy of *payload* with sensitive keys masked.

    Strings / numbers / None pass through. dicts / lists are walked
    recursively; matching keys become the literal ``"***"`` so an admin
    reading the audit log can still see "yes a password was submitted"
    without seeing what it was.
    """
    if isinstance(payload, Mapping):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            lk = str(k).lower()
            if any(part in lk for part in _SENSITIVE_KEY_PARTS):
                out[str(k)] = "***"
            else:
                out[str(k)] = scrub_payload(v)
        return out
    if isinstance(payload, (list, tuple)):
        return [scrub_payload(item) for item in payload]
    return payload


def diff_columns(before: Mapping[str, Any] | None,
                 after: Mapping[str, Any] | None,
                 *,
                 ignore: Iterable[str] = ("updated_at",)) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(before_changed, after_changed)`` containing only the columns
    whose value actually differs. Useful so the audit UI shows a tight diff
    instead of the full row.

    ``ignore`` skips columns that change on every UPDATE without carrying
    information (timestamps).
    """
    if before is None and after is None:
        return ({}, {})
    if before is None:
        return ({}, dict(after))
    if after is None:
        return (dict(before), {})
    ignore_set = set(ignore)
    b: dict[str, Any] = {}
    a: dict[str, Any] = {}
    keys = set(before.keys()) | set(after.keys())
    for k in keys:
        if k in ignore_set:
            continue
        bv = before.get(k)
        av = after.get(k)
        if bv != av:
            b[k] = bv
            a[k] = av
    return (b, a)


# ──────────────────────────────────────────────────────────────────────────────
# Reverse-SQL builders
# ──────────────────────────────────────────────────────────────────────────────

def _as_string(query: sql.Composable, conn: psycopg.Connection) -> str:
    """Render ``psycopg.sql.Composable`` against a real connection so the
    final literal escaping matches what would actually execute."""
    return query.as_string(conn)


def build_reverse_update(conn: psycopg.Connection, table: str,
                         pk: Mapping[str, Any],
                         before_changed: Mapping[str, Any]) -> Optional[str]:
    """SQL that resets the changed columns to their pre-change values.

    Returns ``None`` if nothing changed — the audit row is still written
    (so the action is attributable) but ``reverse_sql`` stays NULL and the
    UI hides the rollback button.
    """
    if not before_changed:
        return None
    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(col), sql.Literal(val))
        for col, val in before_changed.items()
    )
    where_parts = sql.SQL(" AND ").join(
        sql.SQL("{} = {}").format(sql.Identifier(col), sql.Literal(val))
        for col, val in pk.items()
    )
    query = sql.SQL("UPDATE {} SET {} WHERE {}").format(
        sql.Identifier(table), set_parts, where_parts
    )
    return _as_string(query, conn)


def build_reverse_insert(conn: psycopg.Connection, table: str,
                         row: Mapping[str, Any]) -> Optional[str]:
    """SQL that re-inserts a row that the action just DELETEd.

    Only safe for tables without ON DELETE CASCADE children that lost
    their own rows — the caller is responsible for not asking for this
    in those cases (see scripts/dbtool/server.py guard rails). Returns
    ``None`` if the row is empty (defense in depth).
    """
    if not row:
        return None
    cols = list(row.keys())
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    val_literals = sql.SQL(", ").join(sql.Literal(row[c]) for c in cols)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table), col_idents, val_literals
    )
    return _as_string(query, conn)


def build_reverse_delete(conn: psycopg.Connection, table: str,
                         pk: Mapping[str, Any]) -> Optional[str]:
    """SQL that removes a row the action just INSERTed.

    Used when an admin creates a record and we want a one-click undo.
    Composite PKs are supported through the ``pk`` mapping.
    """
    if not pk:
        return None
    where_parts = sql.SQL(" AND ").join(
        sql.SQL("{} = {}").format(sql.Identifier(col), sql.Literal(val))
        for col, val in pk.items()
    )
    query = sql.SQL("DELETE FROM {} WHERE {}").format(
        sql.Identifier(table), where_parts
    )
    return _as_string(query, conn)


# ──────────────────────────────────────────────────────────────────────────────
# Writer
# ──────────────────────────────────────────────────────────────────────────────

def record(cur: psycopg.Cursor, *,
           actor: Actor,
           action: str,
           target_table: str,
           target_id: Optional[str | int] = None,
           target_count: int = 1,
           before: Mapping[str, Any] | None = None,
           after: Mapping[str, Any] | None = None,
           reverse_sql: Optional[str] = None,
           cmd_payload: Any = None,
           request_ip: Optional[str] = None,
           request_path: Optional[str] = None) -> int:
    """Insert one audit-log row and return its id.

    *cur* is the cursor that just performed the data change; both writes
    land in the same transaction. The caller still needs to ``commit``
    its connection (or rely on the FastAPI dependency that does so).
    """
    cur.execute(
        """
        INSERT INTO admin_audit_log (
            actor_user_id, actor_username,
            action, target_table, target_id, target_count,
            before_json, after_json,
            reverse_sql, cmd_payload,
            request_ip, request_path
        ) VALUES (
            %s, %s,
            %s, %s, %s, %s,
            %s::jsonb, %s::jsonb,
            %s, %s::jsonb,
            %s, %s
        )
        RETURNING id
        """,
        (
            actor.id, actor.username,
            action, target_table,
            None if target_id is None else str(target_id),
            int(target_count),
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            reverse_sql,
            json.dumps(scrub_payload(cmd_payload), default=str) if cmd_payload is not None else None,
            request_ip, request_path,
        ),
    )
    row = cur.fetchone()
    return int(row["id"] if isinstance(row, dict) else row[0])


def mark_reverted(cur: psycopg.Cursor, *, audit_id: int, reverted_by: Actor) -> None:
    """Stamp the original entry so the UI can collapse it as 'rolled back'.

    The rollback itself also writes its own audit row (action='rollback'),
    so the chain is fully traceable.
    """
    cur.execute(
        """
        UPDATE admin_audit_log
           SET reverted_at = now(),
               reverted_by = %s
         WHERE id = %s
           AND reverted_at IS NULL
        """,
        (reverted_by.id, audit_id),
    )
