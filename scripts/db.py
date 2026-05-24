"""Postgres connection helper for RentMap.

Single source of truth for ``RENTMAP_DB_URL`` parsing and connection lifecycle.
Everything else (migrations, reconcile, webhook worker, gen-web) imports from
here so connection semantics — autocommit policy, row factory, prepared-
statement reuse — change in one place.

Why psycopg 3 over psycopg2:
- Built-in connection pool we can grow into without an extra dependency
- Native dict_row factory (no DictCursor wrapper boilerplate)
- ``with conn.transaction():`` blocks read better than savepoint juggling
- Server-side prepared statements are cached automatically — relevant for the
  reconcile hot loop that runs the same UPSERT thousands of times per crawl

Hot-path callers (reconcile, webhook worker) should reuse a single connection
across their batch via ``connect()``; one-shot CLIs (migrate, manual scripts)
can just use the context-manager form.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


class DBConfigError(RuntimeError):
    """Raised when ``RENTMAP_DB_URL`` is missing or unparseable.

    Distinct from ``psycopg.Error`` so callers can decide whether to retry
    (transient DB issue) vs. fail loudly (misconfiguration).
    """


def database_url() -> str:
    """Return the connection string from ``RENTMAP_DB_URL`` or raise.

    We deliberately do **not** fall back to a localhost default — silent
    fallbacks are the #1 way "everything worked in dev" ships broken to prod.
    """
    url = os.environ.get("RENTMAP_DB_URL", "").strip()
    if not url:
        raise DBConfigError(
            "RENTMAP_DB_URL is not set. Expected a postgres URL like "
            "postgresql://user:pass@host:5432/dbname"
        )
    return url


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Open a single connection. Caller owns close/commit.

    Prefer the ``transaction()`` / ``session()`` context managers below for
    most call sites; this raw form is for the long-lived workers (webhook
    flush loop) that want to manage their own transaction boundaries.
    """
    return psycopg.connect(database_url(), autocommit=autocommit, row_factory=dict_row)


@contextmanager
def session(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Context-managed connection. Commits on success, rolls back on exception.

    Use for read-mostly call sites or scripts where one connection per
    invocation is fine. Autocommit defaults to off so a script can group
    multiple writes into one transaction without ceremony.
    """
    conn = connect(autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[psycopg.Cursor]:
    """One transaction → one cursor. Convenience for short ops.

    Equivalent to::

        with session() as conn, conn.cursor() as cur:
            ...

    but trims the boilerplate at the use site.
    """
    with session(autocommit=False) as conn:
        with conn.cursor() as cur:
            yield cur


def healthcheck() -> dict[str, object]:
    """Return basic connectivity info. Used by ``migrate.py status`` and any
    future ``/api/health`` endpoint. Never raises — failures return ``ok=False``
    so the caller can decide how to render them.
    """
    try:
        with session(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT version(), now() AS server_time")
            row = cur.fetchone() or {}
            return {"ok": True, **row}
    except Exception as exc:  # noqa: BLE001 — health probe swallows everything
        return {"ok": False, "error": str(exc)}
