"""User management CLI for RentMap.

Commands:

    python scripts/users.py create-admin <username>     # prompts for password
    python scripts/users.py create <username>           # non-admin
    python scripts/users.py reset-password <username>
    python scripts/users.py list
    python scripts/users.py deactivate <username>
    python scripts/users.py activate <username>
    python scripts/users.py delete-user <username> [--yes]
    python scripts/users.py migrate-globals --to <username>

``migrate-globals`` is a one-shot tool for the Caddy-basic-auth → self-login
transition: it assigns every existing favorites / favorite_deleted row to the
named user and moves data/photos/{source}_{id}/ folders into
data/photos/{user_id}/{source}_{id}/. It's idempotent — if a row already has
user_id or a destination folder already contains the source folder it skips.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import shutil
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402
import auth  # noqa: E402

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PHOTOS_DIR = ROOT / "data" / "photos"

# Match the folder shape server.py writes: "<source>_<listing_no>".
# Sources are platform codes which are all lower-case alphanumerics.
_LEGACY_FOLDER_RE = re.compile(r"^(dabang|daangn|zigbang|naver|manual)_.+$")


def _find_user(cur, username: str) -> dict | None:
    cur.execute("SELECT id, username, is_admin, is_active FROM users WHERE username = %s",
                (username,))
    return cur.fetchone()


def _prompt_new_password() -> str:
    p1 = getpass.getpass("New password: ")
    if len(p1) < 6:
        raise SystemExit("password must be at least 6 characters")
    p2 = getpass.getpass("Confirm password: ")
    if p1 != p2:
        raise SystemExit("passwords do not match")
    return p1


def cmd_create(args: argparse.Namespace) -> None:
    """Shared by ``create`` and ``create-admin``."""
    password = _prompt_new_password()
    pw_hash = auth.hash_password(password)
    with session() as conn, conn.cursor() as cur:
        if _find_user(cur, args.username):
            raise SystemExit(f"user already exists: {args.username}")
        cur.execute(
            """
            INSERT INTO users (username, password_hash, display_name, is_admin)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (args.username, pw_hash, args.display_name or args.username, args.admin),
        )
        uid = cur.fetchone()["id"]
    role = "admin" if args.admin else "user"
    print(f"[users] created {role} '{args.username}' (id={uid})")


def cmd_reset(args: argparse.Namespace) -> None:
    password = _prompt_new_password()
    pw_hash = auth.hash_password(password)
    with session() as conn, conn.cursor() as cur:
        user = _find_user(cur, args.username)
        if not user:
            raise SystemExit(f"user not found: {args.username}")
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (pw_hash, user["id"]))
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
    print(f"[users] reset password for '{args.username}', all sessions revoked")


def cmd_list(_args: argparse.Namespace) -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, display_name, is_admin, is_active, "
            "created_at, last_login_at FROM users ORDER BY id"
        )
        rows = cur.fetchall()
    if not rows:
        print("(no users)")
        return
    for r in rows:
        flags = []
        if r["is_admin"]:
            flags.append("admin")
        if not r["is_active"]:
            flags.append("inactive")
        flagstr = f" [{','.join(flags)}]" if flags else ""
        last = r["last_login_at"].isoformat() if r["last_login_at"] else "never"
        print(f"  #{r['id']:<4} {r['username']:<24} login={last}{flagstr}")


def _set_active(username: str, active: bool) -> None:
    with session() as conn, conn.cursor() as cur:
        user = _find_user(cur, username)
        if not user:
            raise SystemExit(f"user not found: {username}")
        cur.execute("UPDATE users SET is_active = %s WHERE id = %s",
                    (active, user["id"]))
        if not active:
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
    print(f"[users] {'activated' if active else 'deactivated'} '{username}'")


def cmd_deactivate(args: argparse.Namespace) -> None:
    _set_active(args.username, False)


def cmd_activate(args: argparse.Namespace) -> None:
    _set_active(args.username, True)


def cmd_delete(args: argparse.Namespace) -> None:
    """Hard-delete a user. CASCADE removes favorites/sessions/user_area_filters
    in Postgres; we also rmtree the photos directory because the filesystem
    isn't subject to the DB's CASCADE rule.

    Requires ``--yes`` to skip the interactive confirm (so it's safe to run
    from a shell script when the operator knows what they're doing).
    """
    with session() as conn, conn.cursor() as cur:
        user = _find_user(cur, args.username)
        if not user:
            raise SystemExit(f"user not found: {args.username}")
        uid = user["id"]

    if not args.yes:
        prompt = (
            f"Delete user '{args.username}' (id={uid}), all their favorites, "
            f"sessions, area filter, AND data/photos/{uid}/ on disk? [y/N] "
        )
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("[users] cancelled")
            return

    with session() as conn, conn.cursor() as cur:
        # CASCADE handles favorites, favorite_deleted, sessions,
        # user_area_filters (all FK-constrained to users.id).
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    photo_dir = PHOTOS_DIR / str(uid)
    if photo_dir.exists():
        shutil.rmtree(photo_dir, ignore_errors=True)
        print(f"[users] removed photos directory {photo_dir}")
    print(f"[users] deleted user '{args.username}' (id={uid})")


# ──────────────────────────────────────────────────────────────────────────────
# Global → per-user backfill
# ──────────────────────────────────────────────────────────────────────────────

def _move_legacy_photos(user_id: int) -> tuple[int, int]:
    """Move data/photos/<source>_<id>/ → data/photos/<user_id>/<source>_<id>/.

    Returns ``(moved, skipped)``. Idempotent: if the destination already exists
    we skip rather than overwrite.
    """
    if not PHOTOS_DIR.exists():
        return (0, 0)
    user_root = PHOTOS_DIR / str(user_id)
    user_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    skipped = 0
    for child in list(PHOTOS_DIR.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        # Don't touch user-id folders we already wrote on a prior run.
        if name.isdigit():
            continue
        if not _LEGACY_FOLDER_RE.match(name):
            continue
        dest = user_root / name
        if dest.exists():
            skipped += 1
            continue
        shutil.move(str(child), str(dest))
        moved += 1
    return (moved, skipped)


def cmd_migrate_globals(args: argparse.Namespace) -> None:
    with session() as conn, conn.cursor() as cur:
        user = _find_user(cur, args.to)
        if not user:
            raise SystemExit(f"user not found: {args.to}")
        uid = user["id"]
        cur.execute(
            "UPDATE favorites SET user_id = %s WHERE user_id IS NULL",
            (uid,),
        )
        favs = cur.rowcount or 0
        cur.execute(
            "UPDATE favorite_deleted SET user_id = %s WHERE user_id IS NULL",
            (uid,),
        )
        tombs = cur.rowcount or 0
    moved, skipped = _move_legacy_photos(uid)
    summary = {
        "user": args.to,
        "user_id": uid,
        "favorites_updated": favs,
        "favorite_deleted_updated": tombs,
        "photo_folders_moved": moved,
        "photo_folders_skipped": skipped,
    }
    print(json.dumps(summary, indent=2))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Manage RentMap users.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _name_arg(p):
        p.add_argument("username")
        p.add_argument("--display-name", help="Optional display name (defaults to username)")

    p_create = sub.add_parser("create", help="Create a regular user")
    _name_arg(p_create)
    p_create.set_defaults(func=cmd_create, admin=False)

    p_admin = sub.add_parser("create-admin", help="Create an admin user")
    _name_arg(p_admin)
    p_admin.set_defaults(func=cmd_create, admin=True)

    p_reset = sub.add_parser("reset-password", help="Reset a user's password (revokes all sessions)")
    p_reset.add_argument("username")
    p_reset.set_defaults(func=cmd_reset)

    p_list = sub.add_parser("list", help="List all users")
    p_list.set_defaults(func=cmd_list)

    p_deact = sub.add_parser("deactivate", help="Mark a user inactive (revokes sessions)")
    p_deact.add_argument("username")
    p_deact.set_defaults(func=cmd_deactivate)

    p_act = sub.add_parser("activate", help="Re-activate a user")
    p_act.add_argument("username")
    p_act.set_defaults(func=cmd_activate)

    p_del = sub.add_parser(
        "delete-user",
        help="Hard-delete a user (DB cascade + photos rmtree). Prompts for confirmation.",
    )
    p_del.add_argument("username")
    p_del.add_argument("--yes", action="store_true",
                       help="Skip the interactive confirm prompt.")
    p_del.set_defaults(func=cmd_delete)

    p_mig = sub.add_parser(
        "migrate-globals",
        help="One-shot: assign legacy global favorites/photos to a user",
    )
    p_mig.add_argument("--to", required=True, help="Username to receive the data")
    p_mig.set_defaults(func=cmd_migrate_globals)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
