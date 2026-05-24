"""Minimal SQL migration runner for RentMap.

Design goals:
- Zero dependencies beyond psycopg (already required by db.py).
- Filesystem-ordered migrations: ``db/migrations/NNN_name.sql`` runs in
  lexicographic order. The numeric prefix is enforced so a developer can't
  ship ``init.sql`` ahead of ``001_init.sql`` by accident.
- Each migration file is its own transaction. Files already wrap ``BEGIN; ...
  COMMIT;`` so we just execute the script as-is rather than wrapping again
  (psycopg's transaction wrapper would conflict with the explicit BEGIN).
- A ``schema_migrations`` table records applied filename + sha256 + applied_at.
  We do **not** allow re-applying a migration with a changed hash — if you
  need to fix a shipped migration, write a new one.

CLI:

    python scripts/migrate.py status   # list applied vs pending
    python scripts/migrate.py up       # apply all pending migrations
    python scripts/migrate.py up --to 002    # apply through 002 only

Re-running ``up`` after a partial failure is safe: applied rows are committed
per-file, so the next run picks up at the first un-recorded file.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import psycopg

# Local import — adjust if you move db.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session, healthcheck  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "db" / "migrations"
# Enforce ``NNN_short_name.sql`` so ordering is stable and reviewers can spot
# accidental duplicates at a glance.
FILENAME_RE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")

SCHEMA_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    sha256      CHAR(64) NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def discover() -> list[Path]:
    """Return migration files in lexicographic order, validating filenames."""
    if not MIGRATIONS_DIR.exists():
        raise SystemExit(f"migrations dir not found: {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    bad = [f.name for f in files if not FILENAME_RE.match(f.name)]
    if bad:
        raise SystemExit(
            f"migration filenames must match NNN_name.sql — offenders: {bad}"
        )
    return files


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_meta_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_TABLE_DDL)
    # Commit so the connection leaves transaction-idle state; later code may
    # need to flip autocommit, which psycopg 3 refuses while INTRANS.
    conn.commit()


def applied_map(conn: psycopg.Connection) -> dict[str, str]:
    """Return ``{filename: sha256}`` for migrations already in the DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT filename, sha256 FROM schema_migrations")
        return {row["filename"]: row["sha256"] for row in cur.fetchall()}


def apply_one(conn: psycopg.Connection, path: Path) -> None:
    """Execute one migration file in its own transaction.

    The .sql files already contain ``BEGIN; ... COMMIT;`` — psycopg's
    autocommit-off mode would add an outer transaction on top, so we flip
    autocommit on for the duration of ``execute()`` and re-enter
    transactional mode afterwards to insert the bookkeeping row atomically.
    """
    sql = path.read_text(encoding="utf-8")
    digest = file_hash(path)
    print(f"[migrate] applying {path.name} (sha256={digest[:12]}...)")

    # 1. Execute the migration with autocommit so its own BEGIN/COMMIT runs
    #    as intended. psycopg 3 refuses an autocommit flip while INTRANS, so
    #    close any open implicit transaction before toggling.
    if not conn.autocommit:
        conn.commit()  # leave INTRANS → IDLE
    prev_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.autocommit = prev_autocommit

    # 2. Record success in its own short transaction.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)",
            (path.name, digest),
        )
    conn.commit()
    print(f"[migrate]   ok")


def cmd_status(_args: argparse.Namespace) -> None:
    health = healthcheck()
    if not health.get("ok"):
        print(f"[migrate] DB unreachable: {health.get('error')}", file=sys.stderr)
        raise SystemExit(2)
    print(f"[migrate] connected to {health.get('version', '?')[:60]}...")
    with session() as conn:
        ensure_meta_table(conn)
        applied = applied_map(conn)
    files = discover()
    print(f"[migrate] {len(files)} migration file(s) on disk:")
    for path in files:
        on_disk = file_hash(path)
        rec = applied.get(path.name)
        if rec is None:
            mark = "PENDING"
        elif rec == on_disk:
            mark = "applied "
        else:
            mark = "DRIFT!! "  # file changed after being applied — fix by adding a new migration
        print(f"  {mark}  {path.name}  ({on_disk[:12]}...)")


def cmd_up(args: argparse.Namespace) -> None:
    files = discover()
    if args.to:
        files = [f for f in files if f.name <= args.to or f.name.startswith(args.to)]
        if not files:
            raise SystemExit(f"no migrations matched --to={args.to}")
    with session() as conn:
        ensure_meta_table(conn)
        applied = applied_map(conn)
        for path in files:
            on_disk = file_hash(path)
            rec = applied.get(path.name)
            if rec is None:
                apply_one(conn, path)
            elif rec == on_disk:
                continue
            else:
                raise SystemExit(
                    f"refusing to re-apply {path.name}: file changed since it was "
                    f"applied. Fix this by writing a new migration on top."
                )
    print("[migrate] done")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run SQL migrations against RENTMAP_DB_URL.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Show applied vs pending")
    p_status.set_defaults(func=cmd_status)

    p_up = sub.add_parser("up", help="Apply pending migrations")
    p_up.add_argument("--to", help="Apply through this filename prefix (e.g. 002)")
    p_up.set_defaults(func=cmd_up)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
