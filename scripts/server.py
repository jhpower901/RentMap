import os
import re
import json
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import psycopg
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
MAIN_CRAWL_PLATFORM_CODES = ("dabang", "zigbang", "daangn")
MISSING_RETRY_LIMIT = 2
# CRAWL_LOCK assigned after region_runner is imported below. We share
# region_runner's container-global lock so the hourly missing-retry
# can't race a live region crawl (previously a local lock here only
# guarded missing-retry against itself, leaving a window where
# missing-retry and a crawl both hit Naver's rate budget).
CRAWL_LOCK: threading.Lock

# Sources this container is the sole owner of. Naver runs in the playwright
# image (rentmap-naver / scheduler_naver.py), so we never schedule it here
# — region_scheduler_sync filters DB rows by this tuple.
ALLOWED_SOURCES_SERVER: tuple[str, ...] = ("all_light", "dabang", "zigbang", "daangn")


def _ts() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S")


def _run_rentmap(args: list[str], label: str, timeout_s: int) -> int | None:
    started = time.monotonic()
    command = " ".join(args)
    print(f"{_ts()} [scheduler] {label}: START rentmap {command}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            check=False,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        print(f"{_ts()} [scheduler] {label}: {status} exit={result.returncode} elapsed={elapsed:.1f}s rentmap {command}", flush=True)
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"{_ts()} [scheduler] {label}: TIMEOUT after {elapsed:.1f}s limit={timeout_s}s rentmap {command}: {exc}", flush=True)
        return None
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"{_ts()} [scheduler] {label}: ERROR after {elapsed:.1f}s rentmap {command}: {exc}", flush=True)
        return None


def _missing_queue_count(platform_codes: tuple[str, ...]) -> int:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from db import session  # noqa: WPS433
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM listings l
                JOIN platforms p ON p.id = l.platform_id
                WHERE p.code = ANY(%s)
                  AND l.current_status = 'missing'
                """,
                (list(platform_codes),),
            )
            return int(cur.fetchone()["n"] or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [scheduler] missing-retry: queue check failed — {exc}", flush=True)
        return 0


def run_missing_retry_cycle() -> None:
    """Probe + finalize missing listings across the lightweight sources.

    Replaces the missing-retry logic that lived inside the old hourly_crawl.
    Decoupling it from a specific crawl fire lets the region-driven
    schedules run on whatever cadence the admin chooses while we still
    drain the missing queue on a predictable hourly cadence.
    """
    if not CRAWL_LOCK.acquire(blocking=False):
        print(f"{_ts()} [scheduler] missing-retry: SKIP already running", flush=True)
        return
    try:
        _run_missing_retry_cycle_locked()
    finally:
        CRAWL_LOCK.release()


def _run_missing_retry_cycle_locked() -> None:
    missing_count = _missing_queue_count(MAIN_CRAWL_PLATFORM_CODES)
    if missing_count == 0:
        return
    print(f"{_ts()} [scheduler] missing-retry: pending={missing_count}", flush=True)
    for attempt in range(1, MISSING_RETRY_LIMIT + 1):
        command = ["retry-missing"]
        for platform_code in MAIN_CRAWL_PLATFORM_CODES:
            command.extend(["--platform", platform_code])
        exit_code = _run_rentmap(command, label=f"missing-retry-{attempt}", timeout_s=10 * 60)
        if exit_code != 0:
            return
        missing_count = _missing_queue_count(MAIN_CRAWL_PLATFORM_CODES)
        if missing_count == 0:
            run_gen_web(trigger="missing-retry-resolved")
            run_webhook_flush(trigger="missing-retry-resolved")
            return
        if attempt < MISSING_RETRY_LIMIT:
            print(
                f"{_ts()} [scheduler] missing-retry: pending={missing_count}; "
                f"probing missing listings {attempt + 1}/{MISSING_RETRY_LIMIT}",
                flush=True,
            )
    # Still pending after retries — finalize the unresolved set.
    print(
        f"{_ts()} [scheduler] missing-retry: pending={missing_count} after retries; "
        "finalizing unresolved listings",
        flush=True,
    )
    finalize_args = ["finalize-missing"]
    for platform_code in MAIN_CRAWL_PLATFORM_CODES:
        finalize_args.extend(["--platform", platform_code])
    finalize_code = _run_rentmap(finalize_args, label="missing-finalize", timeout_s=5 * 60)
    if finalize_code == 0:
        run_gen_web(trigger="missing-finalize")
        run_webhook_flush(trigger="missing-finalize")


def run_region_sync() -> None:
    """Reconcile the in-memory APScheduler jobs with DB region_schedules.

    Cheap (one indexed read + diff against the current job set). Called on a
    fixed 30s interval so an admin's PATCH on region_schedules takes effect
    quickly without the operator restarting the container.
    """
    region_scheduler_sync.sync_schedules(
        scheduler,
        allowed_sources=ALLOWED_SOURCES_SERVER,
        run_callback=region_runner.run_schedule,
        tz=TZ,
    )


def run_gen_web(trigger: str = "scheduled") -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # gen_web is fault-tolerant: missing today's CSV falls back to most recent.
    print(f"{_ts()} [scheduler] gen-web[{trigger}]: target date={today} sources=db-auto-fallback", flush=True)
    _run_rentmap(["gen-web", "--date", today], label=f"gen-web[{trigger}]", timeout_s=5 * 60)


def run_webhook_flush(trigger: str = "manual") -> None:
    """Drain pending listing_status_events to Discord after crawl completion."""
    try:
        # Local import keeps DB / requests out of server startup if the worker
        # module ever gains heavier imports.
        sys.path.insert(0, str(ROOT / "scripts"))
        from webhook_worker import flush_once  # noqa: WPS433 — intentional late import
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"{_ts()} [scheduler] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:
        # Worker failures must never kill the scheduler thread. Log and move on.
        print(f"{_ts()} [scheduler] webhook-flush: failed — {exc}", flush=True)


def run_expired_session_cleanup() -> None:
    """Reap rows from ``sessions`` whose ``expires_at`` has already passed.

    ``auth.lookup_session`` only deletes the one row it just touched when a
    client presents an expired token, which leaves abandoned-but-expired rows
    accumulating forever. A trivial hourly DELETE keeps the table bounded.
    """
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from db import session  # noqa: WPS433
        with session() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < now()")
            n = cur.rowcount or 0
        if n:
            print(f"{_ts()} [scheduler] sessions-cleanup: deleted {n} expired rows", flush=True)
    except Exception as exc:
        # Cleanup failures must never kill the scheduler thread.
        print(f"{_ts()} [scheduler] sessions-cleanup: failed — {exc}", flush=True)


scheduler = BackgroundScheduler(timezone=TZ)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Region-driven scheduling: a 30s interval loop reconciles
    # APScheduler's job set with DB region_schedules so an admin can
    # add/edit/toggle a schedule from admin.html and see it take effect
    # within ~30s. region_runner is what each registered job actually
    # invokes when its cron matches. See region_scheduler_sync for the
    # diff/add/remove logic.
    scheduler.add_job(
        run_region_sync,
        trigger=IntervalTrigger(seconds=30, timezone=TZ),
        id="region_sync_interval",
        max_instances=1,
        coalesce=True,
    )
    # Missing-retry decoupled from any specific crawl fire — runs hourly
    # at :30 across the lightweight 3-platform set regardless of which
    # region scheduled a crawl this hour. Finalizes anything still
    # unresolved after MISSING_RETRY_LIMIT attempts.
    scheduler.add_job(
        run_missing_retry_cycle,
        trigger=CronTrigger(minute=30, timezone=TZ),
        id="missing_retry_hourly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    # Startup kicks: sync the region jobs into the scheduler immediately
    # (so cron firings don't have to wait up to 30s for the first
    # interval tick), refresh the web bundle from the latest CSVs, and
    # reap any abandoned-but-expired sessions.
    now = datetime.now(TZ)
    scheduler.add_job(
        run_region_sync, trigger="date",
        run_date=now + timedelta(seconds=5),
        id="startup_region_sync", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        run_gen_web, trigger="date",
        run_date=now + timedelta(seconds=30),
        id="startup_gen_web", max_instances=1, coalesce=True,
    )
    # Hourly at :15 — reap expired session rows. Light query (indexed on
    # expires_at, deletes only past rows) so it co-exists fine with the
    # :30 missing-retry slot and whatever region crawls cluster on :00.
    scheduler.add_job(
        run_expired_session_cleanup,
        trigger=CronTrigger(minute=15, timezone=TZ),
        id="sessions_cleanup_hourly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    scheduler.add_job(
        run_expired_session_cleanup, trigger="date",
        run_date=now + timedelta(seconds=45),
        id="startup_sessions_cleanup", max_instances=1, coalesce=True,
    )
    # One-shot seed: persist the legacy RENTMAP_SIGNUP_CODE value as a
    # regular invite_codes row so the old gate keeps working after the
    # invites table goes live. Idempotent (ON CONFLICT DO NOTHING).
    try:
        seeded = invite_store.seed_env_code_if_missing(os.environ.get("RENTMAP_SIGNUP_CODE"))
        if seeded:
            print(
                f"{_ts()} [startup] invites: seeded env code '{seeded['code']}' as id={seeded['id']}",
                flush=True,
            )
    except Exception as exc:
        print(f"{_ts()} [startup] invites: env-code seed failed — {exc}", flush=True)
    scheduler.start()
    print(
        f"{_ts()} [scheduler] started - region-driven crawl via 30s DB sync, "
        "missing-retry at :30 hourly, sessions-cleanup at :15 hourly, "
        f"allowed sources for this container: {ALLOWED_SOURCES_SERVER}",
        flush=True,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Auth (sessions, signup/login/logout, middleware)
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(ROOT / "scripts"))
import auth  # noqa: E402
import favorites as fav_store  # noqa: E402
import area_filters as area_store  # noqa: E402
import filter_preferences as filter_pref_store  # noqa: E402
import invites as invite_store  # noqa: E402
import user_webhooks as webhook_store  # noqa: E402
import regions as region_store  # noqa: E402
import region_schedules as schedule_store  # noqa: E402
import region_runner  # noqa: E402
import region_scheduler_sync  # noqa: E402
from db import session as db_session  # noqa: E402

# Bind the forward-declared CRAWL_LOCK to the shared region_runner one.
CRAWL_LOCK = region_runner.CRAWL_LOCK

# Paths the auth middleware will let through without a session cookie.
# Anything else under "/" or "/api" requires a logged-in user.
_PUBLIC_EXACT = {
    "/login.html",
    "/favicon.ico",
}
_PUBLIC_PREFIXES = (
    "/api/auth/",
)
# Static assets a logged-out user is allowed to request. login.html itself
# pulls some of these (CSS reset, fonts loaded over HTTPS). We accept .html
# being absent here on purpose — the only HTML reachable without a session
# is /login.html, which is in _PUBLIC_EXACT.
_PUBLIC_ASSET_EXTS = (".js", ".css", ".ico", ".png", ".jpg", ".jpeg",
                      ".svg", ".webp", ".gif", ".woff", ".woff2", ".ttf",
                      ".map")


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for p in _PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    # CSV crawl data + photos live under /data/*; both need auth so we do NOT
    # treat them as public assets even though the extension lookup might match.
    if path.startswith("/data/"):
        return False
    if path.endswith(_PUBLIC_ASSET_EXTS):
        return True
    return False


# CSRF guard: same-origin write methods. A browser sends Origin on
# cross-origin XHR/fetch and on form POST in modern Chromium/Firefox.
# If Origin is missing we fall back to Referer; if BOTH are missing on a
# write method we refuse — better than allowing a stripped-header request.
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# /api/auth/login + /api/auth/signup must be reachable from a top-level
# navigation to /login.html where Origin matches anyway; logout/me/photos/
# favorites are scoped to authenticated sessions and need the check.
# We don't except anything here — all write paths in this app are first-
# party JSON XHRs from our own pages.


def _same_origin(request: Request) -> bool:
    """True if the request's Origin (or Referer) host == request host.

    The request host is what the *server* sees, which behind Caddy is the
    public hostname (e.g. rentmap.example.com). Either header alone is
    sufficient — most browsers always set one of them on write requests.
    Missing both → reject; that's CSRF-shaped behavior (e.g. forged via
    `<form>` from a browser that strips both headers, which is rare today).
    """
    host = request.headers.get("host")
    if not host:
        return False

    def _host_of(value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        return parsed.netloc or None

    origin_host = _host_of(request.headers.get("origin"))
    if origin_host is not None:
        return origin_host == host
    # Origin absent (some same-origin GET-style fetch, or non-Chrome legacy):
    # accept matching Referer; otherwise refuse.
    referer_host = _host_of(request.headers.get("referer"))
    if referer_host is not None:
        return referer_host == host
    return False


@app.middleware("http")
async def session_guard(request: Request, call_next):
    """HTML pages + API + /data require a valid session cookie.

    /login.html, /api/auth/*, and static JS/CSS assets are open so the login
    page can render. Photos in /data/photos/<uid>/... are double-checked
    against the caller's user.id to keep one user from probing another's
    folder by URL.

    Also enforces a same-origin Origin/Referer check on write methods so
    cross-site form-style CSRF can't piggy-back on the cookie. Read methods
    (GET/HEAD/OPTIONS) are exempt — the threat model is "cause a state
    change", not "leak public HTML".
    """
    path = request.url.path

    # CSRF guard runs even before public-path short-circuit so a stray
    # cross-site write to /api/auth/login can't side-step. (Login itself
    # still works fine in a top-level form submit because the browser
    # treats it as same-origin Origin header on the login.html page.)
    if request.method in _WRITE_METHODS and not _same_origin(request):
        return JSONResponse(
            {"detail": "Cross-origin write rejected"}, status_code=403
        )

    if _is_public(path):
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE_NAME)
    try:
        user = auth.lookup_session(token) if token else None
    except psycopg.Error as exc:
        # DB blip — don't 500 the whole site. Send pages somewhere readable
        # (login page, which is static), API/data callers get 503 so the
        # client can degrade to local cache. Logged loudly so the operator
        # notices.
        print(f"{_ts()} [auth] session lookup failed (DB error): {exc}", flush=True)
        if path.startswith("/api/") or path.startswith("/data/"):
            return JSONResponse(
                {"detail": "Auth service unavailable"}, status_code=503
            )
        return RedirectResponse("/login.html", status_code=302)

    if user is None:
        if path.startswith("/api/") or path.startswith("/data/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # Pages → bounce to login. Preserve where the user was headed via
        # ?next=. quote() with safe='/' lets the path through but escapes
        # query chars (?, #, %, &) that would otherwise break the redirect.
        target = "/login.html"
        if path and path != "/":
            target += f"?next={quote(path, safe='/')}"
        return RedirectResponse(target, status_code=302)

    # Enforce per-user isolation on photo URLs. The folder layout is
    # /data/photos/<user_id>/<source>_<listing_no>/<filename>; any digit-only
    # segment in position 3 must equal the caller's user.id.
    if path.startswith("/data/photos/"):
        parts = path.split("/", 4)  # ['', 'data', 'photos', '<seg>', 'rest...']
        if len(parts) >= 4 and parts[3].isdigit() and int(parts[3]) != user.id and not user.is_admin:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

    # Stash so current_user dependency can reuse it without a second DB
    # round-trip. (See auth.current_user.)
    request.state.user = user
    return await call_next(request)


class SignupBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    code: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


def _public_user(user: auth.User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "isAdmin": user.is_admin,
    }


def _admin_user_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"] or row["username"],
        "isAdmin": row["is_admin"],
        "isActive": row["is_active"],
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "lastLoginAt": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
        "sessions": int(row.get("sessions") or 0),
        "favorites": int(row.get("favorites") or 0),
        "deletedFavorites": int(row.get("deleted_favorites") or 0),
        "hasAreaFilter": bool(row.get("has_area_filter")),
        "photoCount": int(row.get("photo_count") or 0),
    }


def _count_user_photos(user_id: int) -> int:
    root = Path(PHOTOS_DIR) / str(int(user_id))
    if not root.exists():
        return 0
    return sum(
        1
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _ALLOWED_PHOTO_EXTS
    )


def _list_user_photos(user_id: int) -> list[dict[str, Any]]:
    root = Path(PHOTOS_DIR) / str(int(user_id))
    if not root.exists():
        return []
    photos: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _ALLOWED_PHOTO_EXTS:
            continue
        rel = p.relative_to(Path(DATA_DIR)).as_posix()
        photos.append({
            "name": p.name,
            "folder": p.parent.name,
            "url": f"/data/{rel}",
            "size": p.stat().st_size,
            "modifiedAt": datetime.fromtimestamp(p.stat().st_mtime, TZ).isoformat(),
        })
    return photos


class AdminCreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool = False


class AdminUpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    is_admin: bool | None = None
    is_active: bool | None = None


class AdminResetPasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=200)


# Cached hash of a sentinel password used to equalize login response time for
# missing usernames (timing-based username enumeration defense). Computed once
# at import — bcrypt(12) is ~250ms; doing it on every miss would be wasteful.
_DUMMY_PW_HASH = auth.hash_password("__rentmap_dummy_password__")


_INVITE_ERROR_STATUS = {
    "unknown": 400,
    "revoked": 400,
    "expired": 400,
    "exhausted": 400,
    "invalid": 400,
    "duplicate": 409,
    "in_use": 409,
}


def _invite_http_error(exc: "invite_store.InviteError") -> HTTPException:
    return HTTPException(
        status_code=_INVITE_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


@app.post("/api/auth/signup")
async def auth_signup(body: SignupBody, request: Request, response: Response):
    # Code → invite_codes lookup. Atomically bumps used_count if the code is
    # still active; raises InviteError otherwise (translated to 400/409).
    try:
        invite_id = invite_store.validate_and_consume(body.code)
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)

    username = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
        # User-facing error AFTER consuming an invite use is unfortunate but
        # rare (Pydantic validation already covers shape; this regex is the
        # extra char-class check). Cheaper than rolling back the consume on
        # every malformed username.
        raise HTTPException(
            status_code=400,
            detail="Username may contain letters, digits, '.', '_', '-' only",
        )

    pw_hash = auth.hash_password(body.password)
    display_name = (body.display_name or "").strip() or username

    # Race-safety: two concurrent signups for the same username both pass the
    # SELECT and rely on the UNIQUE constraint to catch the duplicate. The
    # admin-flag race (both observe count=0) is harder to eliminate cheaply,
    # so we approximate by serializing the count→insert under a pg advisory
    # lock keyed to a constant so concurrent signups queue rather than race
    # on the admin decision. The lock auto-releases on transaction end.
    try:
        with db_session() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(98231)")
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Username already taken")
            # First user in the system becomes admin so the operator can run
            # migrate-globals immediately after first signup if they prefer
            # that over `users.py create-admin`.
            cur.execute("SELECT COUNT(*) AS n FROM users")
            is_first = (cur.fetchone()["n"] == 0)
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name,
                                   is_admin, last_login_at, invite_code_id)
                VALUES (%s, %s, %s, %s, now(), %s)
                RETURNING id, username, display_name, is_admin, is_active
                """,
                (username, pw_hash, display_name, is_first, invite_id),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        # Belt-and-suspenders for the (advisory-lock-bypassed) race: if a
        # second connection sneaks in after the SELECT, the UNIQUE constraint
        # still fires. Translate to a sensible 409 instead of leaking 500.
        raise HTTPException(status_code=409, detail="Username already taken")

    user = auth.User(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        is_admin=row["is_admin"], is_active=row["is_active"],
    )
    token, expires_at = auth.create_session(
        user.id,
        user_agent=request.headers.get("user-agent"),
        ip=auth.get_client_ip(request),
    )
    auth.set_session_cookie(response, token, expires_at, request)
    return {"user": _public_user(user)}


@app.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, display_name, is_admin, is_active "
            "FROM users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        # Timing-equalize: if the user doesn't exist we still spend bcrypt
        # time verifying against a sentinel hash so a remote attacker can't
        # tell "no such user" from "wrong password" by stopwatch.
        if not row or not row["is_active"]:
            auth.verify_password(body.password, _DUMMY_PW_HASH)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))

    user = auth.User(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        is_admin=row["is_admin"], is_active=row["is_active"],
    )
    token, expires_at = auth.create_session(
        user.id,
        user_agent=request.headers.get("user-agent"),
        ip=auth.get_client_ip(request),
    )
    auth.set_session_cookie(response, token, expires_at, request)
    return {"user": _public_user(user)}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    # Same-origin middleware guard already runs ahead of this handler, so a
    # cross-site form-submit POST to /api/auth/logout never reaches here.
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    auth.clear_session_cookie(response, request)
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(user: auth.User = Depends(auth.current_user)):
    return {"user": _public_user(user)}


@app.get("/api/admin/users")
async def admin_list_users(_admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.is_active,
                   u.created_at, u.last_login_at,
                   COUNT(DISTINCT s.id) AS sessions,
                   COUNT(DISTINCT f.key) AS favorites,
                   COUNT(DISTINCT d.key) AS deleted_favorites,
                   (af.user_id IS NOT NULL) AS has_area_filter
            FROM users u
            LEFT JOIN sessions s ON s.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            LEFT JOIN favorite_deleted d ON d.user_id = u.id
            LEFT JOIN user_area_filters af ON af.user_id = u.id
            GROUP BY u.id, af.user_id
            ORDER BY u.id
            """
        )
        rows = cur.fetchall()
    users = []
    for row in rows:
        row = dict(row)
        row["photo_count"] = _count_user_photos(row["id"])
        users.append(_admin_user_dict(row))
    return {"users": users}


@app.post("/api/admin/users")
async def admin_create_user(body: AdminCreateUserBody,
                            _admin: auth.User = Depends(auth.current_admin)):
    username = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
        raise HTTPException(
            status_code=400,
            detail="Username may contain letters, digits, '.', '_', '-' only",
        )
    display_name = (body.display_name or "").strip() or username
    try:
        with db_session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, display_name, is_admin)
                VALUES (%s, %s, %s, %s)
                RETURNING id, username, display_name, is_admin, is_active,
                          created_at, last_login_at
                """,
                (username, auth.hash_password(body.password), display_name, body.is_admin),
            )
            row = dict(cur.fetchone())
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Username already taken")
    row.update({
        "sessions": 0,
        "favorites": 0,
        "deleted_favorites": 0,
        "has_area_filter": False,
        "photo_count": 0,
    })
    return {"user": _admin_user_dict(row)}


@app.get("/api/admin/users/{user_id}")
async def admin_get_user(user_id: int, _admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.is_active,
                   u.created_at, u.last_login_at,
                   COUNT(DISTINCT s.id) AS sessions,
                   COUNT(DISTINCT f.key) AS favorites,
                   COUNT(DISTINCT d.key) AS deleted_favorites,
                   (af.user_id IS NOT NULL) AS has_area_filter
            FROM users u
            LEFT JOIN sessions s ON s.user_id = u.id
            LEFT JOIN favorites f ON f.user_id = u.id
            LEFT JOIN favorite_deleted d ON d.user_id = u.id
            LEFT JOIN user_area_filters af ON af.user_id = u.id
            WHERE u.id = %s
            GROUP BY u.id, af.user_id
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        cur.execute(
            """
            SELECT id, created_at, expires_at, last_seen_at, user_agent, ip
            FROM sessions
            WHERE user_id = %s
            ORDER BY last_seen_at DESC
            """,
            (user_id,),
        )
        sessions = [
            {
                "id": s["id"],
                "createdAt": s["created_at"].isoformat(),
                "expiresAt": s["expires_at"].isoformat(),
                "lastSeenAt": s["last_seen_at"].isoformat(),
                "userAgent": s["user_agent"],
                "ip": str(s["ip"]) if s["ip"] is not None else None,
            }
            for s in cur.fetchall()
        ]
    row = dict(row)
    row["photo_count"] = _count_user_photos(user_id)
    try:
        favorites_state = fav_store.load_state(user_id)
    except Exception as exc:
        favorites_state = {"favorites": [], "deleted": {}, "error": str(exc)}
    try:
        area_filter = area_store.load(user_id)
    except Exception as exc:
        area_filter = {"error": str(exc)}
    return {
        "user": _admin_user_dict(row),
        "sessions": sessions,
        "favoritesState": favorites_state,
        "areaFilter": area_filter,
        "photos": _list_user_photos(user_id),
    }


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: AdminUpdateUserBody,
                            admin: auth.User = Depends(auth.current_admin)):
    fields: list[str] = []
    values: list[Any] = []
    if body.display_name is not None:
        fields.append("display_name = %s")
        values.append(body.display_name.strip() or None)
    if body.is_admin is not None:
        if user_id == admin.id and not body.is_admin:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin role")
        fields.append("is_admin = %s")
        values.append(body.is_admin)
    if body.is_active is not None:
        if user_id == admin.id and not body.is_active:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
        fields.append("is_active = %s")
        values.append(body.is_active)
    if not fields:
        raise HTTPException(status_code=400, detail="No changes requested")
    values.append(user_id)
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE users SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, username, display_name, is_admin, is_active,
                      created_at, last_login_at
            """,
            values,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if body.is_active is False:
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    row = dict(row)
    row.update({
        "sessions": 0,
        "favorites": 0,
        "deleted_favorites": 0,
        "has_area_filter": False,
        "photo_count": _count_user_photos(user_id),
    })
    return {"user": _admin_user_dict(row)}


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, body: AdminResetPasswordBody,
                               _admin: auth.User = Depends(auth.current_admin)):
    with db_session() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s RETURNING username",
            (auth.hash_password(body.password), user_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    return {"ok": True, "username": row["username"]}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, admin: auth.User = Depends(auth.current_admin)):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    with db_session() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s RETURNING username", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
    photo_dir = Path(PHOTOS_DIR) / str(int(user_id))
    if photo_dir.exists():
        shutil.rmtree(photo_dir, ignore_errors=True)
    return {"ok": True, "username": row["username"]}


# ─────────────────────────────────────────────────────────────────────────────
# Invite codes (admin)
# ─────────────────────────────────────────────────────────────────────────────

class AdminCreateInviteBody(BaseModel):
    # All optional — server fills sensible defaults. ``code=None`` triggers
    # auto-generation; ``max_uses=None`` means unlimited; ``expires_at=None``
    # means no expiry.
    code: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=200)
    max_uses: int | None = Field(default=None, ge=1, le=10000)
    expires_at: datetime | None = None


class AdminUpdateInviteBody(BaseModel):
    # Same sentinel-vs-null trick as the user PATCH: a missing key leaves the
    # field alone; an explicit ``null`` clears it. Pydantic v2 distinguishes
    # via ``model_fields_set`` which we read below.
    note: str | None = None
    max_uses: int | None = Field(default=None, ge=1, le=10000)
    expires_at: datetime | None = None
    revoked: bool | None = None


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Pydantic gives us a tz-aware datetime if ISO had a TZ, naive otherwise.
    DB column is TIMESTAMPTZ — coerce naive to UTC so comparisons line up."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@app.get("/api/admin/invites")
async def admin_list_invites(_admin: auth.User = Depends(auth.current_admin)):
    return {"invites": invite_store.list_invites()}


@app.post("/api/admin/invites")
async def admin_create_invite(body: AdminCreateInviteBody,
                              admin: auth.User = Depends(auth.current_admin)):
    try:
        invite = invite_store.create_invite(
            code=body.code,
            note=body.note,
            max_uses=body.max_uses,
            expires_at=_ensure_utc(body.expires_at),
            created_by=admin.id,
        )
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return {"invite": invite}


@app.patch("/api/admin/invites/{invite_id}")
async def admin_update_invite(invite_id: int, body: AdminUpdateInviteBody,
                              _admin: auth.User = Depends(auth.current_admin)):
    # model_fields_set tells us which keys the client actually sent — that's
    # how we distinguish "don't touch" from "explicitly clear to null".
    sent = body.model_fields_set
    try:
        invite = invite_store.update_invite(
            invite_id,
            note=body.note,
            max_uses=body.max_uses,
            expires_at=_ensure_utc(body.expires_at),
            revoked=body.revoked,
            update_note="note" in sent,
            update_max_uses="max_uses" in sent,
            update_expires_at="expires_at" in sent,
        )
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return {"invite": invite}


@app.delete("/api/admin/invites/{invite_id}")
async def admin_delete_invite(invite_id: int,
                              _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = invite_store.delete_invite(invite_id)
    except invite_store.InviteError as exc:
        raise _invite_http_error(exc)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Regions (request/approval) + per-region crawl schedules
# ─────────────────────────────────────────────────────────────────────────────

_REGION_ERROR_STATUS = {
    "unknown": 404,
    "invalid": 400,
    "duplicate": 409,
    "forbidden": 403,
    "in_use": 409,
}


def _region_http_error(exc: "region_store.RegionError") -> HTTPException:
    return HTTPException(
        status_code=_REGION_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


def _schedule_http_error(exc: "schedule_store.ScheduleError") -> HTTPException:
    return HTTPException(
        status_code=_REGION_ERROR_STATUS.get(exc.reason, 400),
        detail=str(exc),
    )


class RegionRequestBody(BaseModel):
    # User-facing submission. Slug is intentionally NOT a user input — see
    # regions._generate_slug for the reasoning. The admin can rename via
    # PATCH /api/admin/regions/{id} once they've reviewed the request.
    name: str = Field(min_length=1, max_length=80)
    center_lat: float = Field(ge=-90, le=90)
    center_lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=50)
    note: str | None = Field(default=None, max_length=500)


class AdminUpdateRegionBody(BaseModel):
    # All optional. ``model_fields_set`` is what we forward to update_region
    # via the _UNSET sentinel pattern so an absent key = "don't touch".
    name: str | None = Field(default=None, max_length=80)
    slug: str | None = Field(default=None, max_length=63)
    center_lat: float | None = Field(default=None, ge=-90, le=90)
    center_lng: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0, le=50)
    naver_cortar_nos: list[str] | None = None
    daangn_region_ids: list[int] | None = None
    naver_urls: list[str] | None = None
    max_deposit_manwon: int | None = Field(default=None, ge=0)
    max_rent_manwon: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    status: str | None = None  # 'pending' | 'approved' | 'disabled'


class ScheduleCreateBody(BaseModel):
    region_id: int = Field(ge=1)
    source: str  # validated by schedule_store
    cron_expr: str = Field(min_length=1, max_length=100)
    enabled: bool = True


class ScheduleUpdateBody(BaseModel):
    cron_expr: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    source: str | None = None


@app.get("/api/regions")
async def list_regions(user: auth.User = Depends(auth.current_user),
                       mine: bool = False):
    """Region listing for the region selector / request page.

    - ``mine=true``: the caller's own submissions (any status) — used by the
      "내 신청 내역" table on /region-request.html so a user can see their
      pending or rejected rows.
    - admin caller, no ``mine``: every row, every status (used by admin.html).
    - regular caller, no ``mine``: only approved rows (the region selector
      should never offer something a user can't act on).
    """
    if mine:
        regions = region_store.list_regions(requested_by=user.id)
    elif user.is_admin:
        regions = region_store.list_regions()
    else:
        regions = region_store.list_regions(statuses=("approved",))
    return {"regions": regions}


@app.post("/api/regions")
async def request_region(body: RegionRequestBody,
                         user: auth.User = Depends(auth.current_user)):
    """Any logged-in user can submit a region proposal (status='pending').

    The admin reviews in admin.html and either fills in cortarNos /
    region_ids and approves, or flips it straight to 'disabled' as a soft
    reject.
    """
    try:
        region = region_store.request_region(
            name=body.name,
            center_lat=body.center_lat,
            center_lng=body.center_lng,
            radius_km=body.radius_km,
            note=body.note,
            requested_by=user.id,
        )
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@app.get("/api/admin/regions")
async def admin_list_regions(_admin: auth.User = Depends(auth.current_admin)):
    return {"regions": region_store.list_regions()}


@app.get("/api/admin/regions/{region_id}")
async def admin_get_region(region_id: int,
                           _admin: auth.User = Depends(auth.current_admin)):
    try:
        region = region_store.get_region(region_id)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@app.patch("/api/admin/regions/{region_id}")
async def admin_update_region(region_id: int, body: AdminUpdateRegionBody,
                              admin: auth.User = Depends(auth.current_admin)):
    # model_fields_set tells us which keys the client sent. We translate
    # "absent" into the regions._UNSET sentinel by skipping the kwarg
    # entirely; "explicit null" becomes a None we forward.
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    for field in (
        "name", "slug", "center_lat", "center_lng", "radius_km",
        "naver_cortar_nos", "daangn_region_ids", "naver_urls",
        "max_deposit_manwon", "max_rent_manwon", "note", "status",
    ):
        if field in sent:
            kwargs[field] = getattr(body, field)
    if "status" in sent and body.status == "approved":
        kwargs["approved_by"] = admin.id
    try:
        region = region_store.update_region(region_id, **kwargs)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return {"region": region}


@app.delete("/api/admin/regions/{region_id}")
async def admin_delete_region(region_id: int,
                              _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = region_store.delete_region(region_id)
    except region_store.RegionError as exc:
        raise _region_http_error(exc)
    return result


@app.get("/api/admin/region-schedules")
async def admin_list_region_schedules(_admin: auth.User = Depends(auth.current_admin),
                                      region_id: int | None = None):
    schedules = schedule_store.list_schedules(region_id=region_id)
    return {"schedules": schedules}


@app.post("/api/admin/region-schedules")
async def admin_create_region_schedule(body: ScheduleCreateBody,
                                       _admin: auth.User = Depends(auth.current_admin)):
    try:
        schedule = schedule_store.create_schedule(
            region_id=body.region_id,
            source=body.source,
            cron_expr=body.cron_expr,
            enabled=body.enabled,
        )
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {"schedule": schedule}


@app.patch("/api/admin/region-schedules/{schedule_id}")
async def admin_update_region_schedule(schedule_id: int, body: ScheduleUpdateBody,
                                       _admin: auth.User = Depends(auth.current_admin)):
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    for field in ("cron_expr", "enabled", "source"):
        if field in sent:
            kwargs[field] = getattr(body, field)
    try:
        schedule = schedule_store.update_schedule(schedule_id, **kwargs)
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {"schedule": schedule}


@app.delete("/api/admin/region-schedules/{schedule_id}")
async def admin_delete_region_schedule(schedule_id: int,
                                       _admin: auth.User = Depends(auth.current_admin)):
    try:
        result = schedule_store.delete_schedule(schedule_id)
    except schedule_store.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Listings (global data, login required)
# ─────────────────────────────────────────────────────────────────────────────
_VALID_SOURCES = {"dabang", "daangn", "zigbang", "naver"}
_SOURCE_TO_PLATFORM_CODE = {
    # UI uses short codes; DB platforms table stores "naver_land" for naver.
    "dabang": "dabang",
    "daangn": "daangn",
    "zigbang": "zigbang",
    "naver": "naver_land",
}


@app.get("/api/listings/{source}/{listing_no}/price-history")
def price_history(source: str, listing_no: str, limit: int = 60,
                  user: auth.User = Depends(auth.current_user)) -> dict[str, Any]:
    """Return up to ``limit`` price snapshots for one listing, oldest first.

    Listings data is global — login gates the endpoint but every user sees
    the same series.
    """
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source: {source}")
    if not listing_no or len(listing_no) > 100:
        raise HTTPException(status_code=400, detail="invalid listing_no")
    limit = max(1, min(int(limit), 500))

    platform_code = _SOURCE_TO_PLATFORM_CODE[source]
    try:
        from db import session, DBConfigError  # noqa: WPS433
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"db module unavailable: {exc}")

    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ps.captured_at, ps.deposit_won, ps.monthly_rent_won,
                       ps.maintenance_fee_won, ps.expected_monthly_cost_won
                FROM listing_price_snapshots ps
                JOIN listings l ON l.id = ps.listing_id
                JOIN platforms p ON p.id = l.platform_id
                WHERE p.code = %s AND l.platform_listing_id = %s
                ORDER BY ps.captured_at ASC
                LIMIT %s
                """,
                (platform_code, listing_no, limit),
            )
            rows = cur.fetchall()
    except DBConfigError:
        return {"points": []}
    except Exception as exc:  # noqa: BLE001
        # Don't 500 on a chart that's secondary UI; degrade to empty.
        return {"points": [], "error": str(exc)[:200]}

    def to_manwon(v: int | None) -> int | None:
        return v // 10000 if v is not None else None

    points = [
        {
            "t": r["captured_at"].isoformat(),
            "deposit": to_manwon(r["deposit_won"]),
            "rent": to_manwon(r["monthly_rent_won"]),
            "maint": to_manwon(r["maintenance_fee_won"]),
            "total": to_manwon(r["expected_monthly_cost_won"]),
        }
        for r in rows
    ]
    return {"points": points}


# ─────────────────────────────────────────────────────────────────────────────
# Favorites + photos (per-user)
# ─────────────────────────────────────────────────────────────────────────────
DATA_DIR = "data"
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")

os.makedirs(PHOTOS_DIR, exist_ok=True)

_SAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]")
_ALLOWED_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ALLOWED_PHOTO_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_PHOTO_BYTES = int(os.environ.get("RENTMAP_MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _sanitize_folder_segment(value: str) -> str:
    return _SAFE_FOLDER_RE.sub("_", value or "")


def _sanitize_filename(value: str) -> str:
    base = os.path.basename(value or "")
    cleaned = _SAFE_FILE_RE.sub("_", base)
    # Block "." / ".." / leading-dot names — whitelist allows dots for extensions.
    if not cleaned or cleaned.startswith("."):
        cleaned = "_" + cleaned
    return cleaned


def _validate_photo_upload(file: UploadFile) -> str:
    filename = _sanitize_filename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if ext not in _ALLOWED_PHOTO_EXTS:
        raise HTTPException(status_code=415, detail="Unsupported photo extension")
    if content_type and content_type not in _ALLOWED_PHOTO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported photo content type")
    return filename


def get_fav_dir(user_id: int, source: str, id: str) -> str:
    """Per-user folder: data/photos/<user_id>/<source>_<listing_no>/."""
    user_segment = str(int(user_id))
    folder_name = f"{_sanitize_folder_segment(source)}_{_sanitize_folder_segment(id)}"
    path = os.path.join(PHOTOS_DIR, user_segment, folder_name)
    resolved = os.path.realpath(path)
    photos_root = os.path.realpath(PHOTOS_DIR)
    if not resolved.startswith(photos_root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")
    os.makedirs(resolved, exist_ok=True)
    return resolved


@app.get("/api/favorites/state")
async def get_favorites_state(user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.load_state(user.id)
    except Exception as e:
        # Don't 500 the client over a DB blip — empty state lets local cache win.
        print(f"{_ts()} Error reading favorites: {e}")
        return {"favorites": [], "deleted": {}}


@app.get("/api/favorites")
async def get_favorites(user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.load_state(user.id)["favorites"]
    except Exception as e:
        print(f"{_ts()} Error reading favorites: {e}")
        return []


@app.post("/api/favorites")
async def save_favorites(request: Request, favorites: Any = Body(...),
                         user: auth.User = Depends(auth.current_user)):
    posted_user_id = request.headers.get("x-rentmap-user-id")
    if posted_user_id != str(user.id):
        raise HTTPException(status_code=409, detail="Favorites sync user changed; reload required")
    try:
        return fav_store.merge_payload(user.id, favorites)
    except Exception as e:
        print(f"{_ts()} Error saving favorites: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/photos")
async def list_photos(id: str, source: str,
                      user: auth.User = Depends(auth.current_user)):
    fav_dir = get_fav_dir(user.id, source, id)
    photos = []
    for filename in sorted(os.listdir(fav_dir)):
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            rel_path = os.path.relpath(os.path.join(fav_dir, filename), DATA_DIR).replace("\\", "/")
            photos.append({
                "photoKey": filename,
                "url": f"/data/{rel_path}",
                "addedAt": os.path.getctime(os.path.join(fav_dir, filename))
            })
    return photos


@app.post("/api/photos")
async def upload_photo(id: str, source: str, file: UploadFile = File(...),
                       user: auth.User = Depends(auth.current_user)):
    fav_dir = get_fav_dir(user.id, source, id)
    timestamp = int(time.time() * 1000)
    filename = f"{timestamp}_{_validate_photo_upload(file)}"
    file_path = os.path.join(fav_dir, filename)

    bytes_written = 0
    try:
        with open(file_path, "wb") as buffer:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                bytes_written += len(chunk)
                if bytes_written > _MAX_PHOTO_BYTES:
                    raise HTTPException(status_code=413, detail="Photo too large")
                buffer.write(chunk)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    finally:
        await file.close()

    rel_path = os.path.relpath(file_path, DATA_DIR).replace("\\", "/")
    return {"photoKey": filename, "url": f"/data/{rel_path}"}


@app.delete("/api/photos")
async def delete_photo(id: str, source: str, photoKey: str,
                       user: auth.User = Depends(auth.current_user)):
    fav_dir = get_fav_dir(user.id, source, id)
    file_path = os.path.join(fav_dir, _sanitize_filename(photoKey))
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Photo not found")


# ─────────────────────────────────────────────────────────────────────────────
# Per-user UI filter preferences
# ─────────────────────────────────────────────────────────────────────────────

class UserFilterPreferenceBody(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/user-filters/{context}")
async def get_user_filter_preference(context: str,
                                     user: auth.User = Depends(auth.current_user)):
    try:
        return filter_pref_store.load(user.id, context)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"{_ts()} Error reading user filter preference: {e}")
        return {
            "context": context,
            "state": {},
            "updated_at": None,
            "is_default": True,
            "error": str(e)[:200],
        }


@app.put("/api/user-filters/{context}")
async def put_user_filter_preference(context: str,
                                     body: UserFilterPreferenceBody,
                                     user: auth.User = Depends(auth.current_user)):
    try:
        return filter_pref_store.save(user.id, context, body.state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"{_ts()} Error saving user filter preference: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Per-user area filter polygon
# ─────────────────────────────────────────────────────────────────────────────

class AreaFilterBody(BaseModel):
    points: list[list[float]]
    enabled: bool = True


@app.get("/api/area-filter")
async def get_area_filter(user: auth.User = Depends(auth.current_user)):
    try:
        return area_store.load(user.id)
    except Exception as e:
        print(f"{_ts()} Error reading area filter: {e}")
        # Degrade to default rather than 500ing the UI.
        return {
            "points": [p[:] for p in area_store.DEFAULT_POINTS],
            "enabled": True,
            "updated_at": None,
            "is_default": True,
            "error": str(e)[:200],
        }


@app.put("/api/area-filter")
async def put_area_filter(body: AreaFilterBody,
                          user: auth.User = Depends(auth.current_user)):
    try:
        return area_store.save(user.id, body.points, body.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"{_ts()} Error saving area filter: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Per-user Discord webhook registrations
# ─────────────────────────────────────────────────────────────────────────────

class WebhookCreateBody(BaseModel):
    label: str = ""
    webhookUrl: str
    eventTypes: list[str] = webhook_store.DEFAULT_EVENT_TYPES
    platforms: list[str] = webhook_store.DEFAULT_PLATFORMS
    maxDepositManwon: int | None = None
    maxRentManwon: int | None = None
    useAreaFilter: bool = True


class WebhookUpdateBody(BaseModel):
    label: str | None = None
    webhookUrl: str | None = None
    isActive: bool | None = None
    eventTypes: list[str] | None = None
    platforms: list[str] | None = None
    maxDepositManwon: int | None = None
    maxRentManwon: int | None = None
    useAreaFilter: bool | None = None


def _webhook_error_to_http(exc: webhook_store.WebhookError) -> HTTPException:
    code = {"unknown": 404, "forbidden": 404, "invalid": 400, "limit": 409}.get(
        exc.reason, 400
    )
    return HTTPException(status_code=code, detail=str(exc))


@app.get("/api/user/webhooks")
async def list_user_webhooks(user: auth.User = Depends(auth.current_user)):
    return webhook_store.list_webhooks(user.id)


@app.post("/api/user/webhooks", status_code=201)
async def create_user_webhook(body: WebhookCreateBody,
                              user: auth.User = Depends(auth.current_user)):
    try:
        return webhook_store.create_webhook(
            user.id,
            label=body.label,
            webhook_url=body.webhookUrl,
            event_types=body.eventTypes,
            platforms=body.platforms,
            max_deposit_manwon=body.maxDepositManwon,
            max_rent_manwon=body.maxRentManwon,
            use_area_filter=body.useAreaFilter,
        )
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@app.patch("/api/user/webhooks/{webhook_id}")
async def update_user_webhook(webhook_id: int, body: WebhookUpdateBody,
                              user: auth.User = Depends(auth.current_user)):
    kwargs: dict[str, Any] = {}
    if body.label is not None:
        kwargs["label"] = body.label
    if body.webhookUrl is not None:
        kwargs["webhook_url"] = body.webhookUrl
    if body.isActive is not None:
        kwargs["is_active"] = body.isActive
    if body.eventTypes is not None:
        kwargs["event_types"] = body.eventTypes
    if body.platforms is not None:
        kwargs["platforms"] = body.platforms
    if body.maxDepositManwon is not None or "maxDepositManwon" in body.model_fields_set:
        kwargs["max_deposit_manwon"] = body.maxDepositManwon
    if body.maxRentManwon is not None or "maxRentManwon" in body.model_fields_set:
        kwargs["max_rent_manwon"] = body.maxRentManwon
    if body.useAreaFilter is not None:
        kwargs["use_area_filter"] = body.useAreaFilter
    try:
        return webhook_store.update_webhook(webhook_id, user.id, **kwargs)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@app.delete("/api/user/webhooks/{webhook_id}", status_code=204)
async def delete_user_webhook(webhook_id: int,
                              user: auth.User = Depends(auth.current_user)):
    try:
        webhook_store.delete_webhook(webhook_id, user.id)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@app.post("/api/user/webhooks/{webhook_id}/test")
async def test_user_webhook(webhook_id: int,
                            user: auth.User = Depends(auth.current_user)):
    try:
        wh = webhook_store.get_webhook(webhook_id, user.id)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)
    import requests as _req
    label = wh["label"] or "내 알림"
    wh_type = webhook_store.detect_webhook_type(wh["webhookUrl"])
    if wh_type == "slack":
        payload = {
            "attachments": [{
                "color": "#57F287",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🔔 RentMap 알림 테스트", "emoji": True}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}* webhook이 정상 연결됐습니다."}},
                    {"type": "section", "fields": [
                        {"type": "mrkdwn", "text": f"*이벤트*\n{', '.join(wh['eventTypes'])}"},
                        {"type": "mrkdwn", "text": f"*플랫폼*\n{', '.join(wh['platforms'])}"},
                    ]},
                ],
            }]
        }
    else:
        payload = {
            "embeds": [{
                "title": "🔔 RentMap 알림 테스트",
                "description": f"**{label}** webhook이 정상 연결됐습니다.",
                "color": 0x57F287,
                "fields": [
                    {"name": "이벤트", "value": ", ".join(wh["eventTypes"]), "inline": False},
                    {"name": "플랫폼", "value": ", ".join(wh["platforms"]), "inline": False},
                ],
                "footer": {"text": "RentMap"},
            }]
        }
    try:
        resp = _req.post(
            wh["webhookUrl"], json=payload, timeout=10,
            headers={"User-Agent": "RentMap-Webhook/1.0 (+rentmap)"},
        )
    except _req.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Webhook 연결 실패: {exc}")
    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=f"Webhook 응답 오류: HTTP {resp.status_code}",
        )
    return {"ok": True}


# Mount data directory for CSV and Photo access. Auth is enforced by the
# session_guard middleware above (including per-user user_id check on
# /data/photos/<uid>/...).
app.mount("/data", StaticFiles(directory="data"), name="data")

# Mount web directory at root for all other files (index.html, js, css etc.)
# html=True enables serving index.html automatically at /. The middleware
# turns away un-authenticated HTML requests before they reach this mount.
app.mount("/", StaticFiles(directory="web", html=True), name="web")

if __name__ == "__main__":
    print(f"{_ts()} RentMap Server starting at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
