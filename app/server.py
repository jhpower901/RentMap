"""RentMap FastAPI application — entry point.

Responsibilities:
- Create FastAPI app with lifespan (scheduler start/stop)
- Register middleware (GZip, session guard)
- Include all API routers
- Mount static file directories
"""
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.api import auth
from app.api import invites as invite_store
from app.crawlers import region_runner
from app.crawlers import region_scheduler_sync
from app.scheduler.jobs import (
    run_region_sync, run_missing_retry_cycle,
    run_gen_web, run_webhook_flush,
    run_expired_session_cleanup,
    ALLOWED_SOURCES_SERVER, TZ,
)
from app.api.routes.auth import router as auth_router
from app.api.routes.admin import router as admin_router
from app.api.routes.regions import router as regions_router
from app.api.routes.listings import router as listings_router
from app.api.routes.favorites import router as favorites_router
from app.api.routes.webhooks import router as webhooks_router

ROOT = Path(__file__).resolve().parent.parent

def _ts() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S")


scheduler = BackgroundScheduler(timezone=TZ)

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

# gzip every response > 1KB. Caddy already negotiates zstd/gzip with the
# browser in front of us, but this also covers direct hits on :8000 (LAN dev,
# health checks) and is a no-op when Caddy strips/replaces Content-Encoding.
# The big win is the data_<source>_<slug>.js bundles (1–7 MB each, ~5–10× win
# on JSON-shaped text) which the map page pulls on every load.
app.add_middleware(GZipMiddleware, minimum_size=1024)


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ─── Routers ──────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(regions_router)
app.include_router(listings_router)
app.include_router(favorites_router)
app.include_router(webhooks_router)

# ─── Middleware ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Auth (sessions, signup/login/logout, middleware)
# ─────────────────────────────────────────────────────────────────────────────
from app.api import auth
from app.api import favorites as fav_store
from app.api import bookmarks as bookmark_store
from app.api import area_filters as area_store
from app.api import filter_preferences as filter_pref_store
from app.api import invites as invite_store
from app.api import user_webhooks as webhook_store
from app.api import regions as region_store
from app.api import region_schedules as schedule_store
from app.crawlers import region_runner
from app.crawlers import region_scheduler_sync
from app.db import session as db_session

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


# ─── Static files ─────────────────────────────────────────────────────────────
app.mount("/data", StaticFiles(directory="data"), name="data")
app.mount("/", StaticFiles(directory="web", html=True), name="web")

if __name__ == "__main__":
    print(f"{_ts()} RentMap Server starting at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
