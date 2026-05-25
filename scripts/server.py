import os
import re
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))


def _run_rentmap(args: list[str], label: str, timeout_s: int) -> int | None:
    started = time.monotonic()
    command = " ".join(args)
    print(f"[scheduler] {label}: START rentmap {command}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            check=False,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        print(f"[scheduler] {label}: {status} exit={result.returncode} elapsed={elapsed:.1f}s rentmap {command}", flush=True)
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(f"[scheduler] {label}: TIMEOUT after {elapsed:.1f}s limit={timeout_s}s rentmap {command}: {exc}", flush=True)
        return None
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[scheduler] {label}: ERROR after {elapsed:.1f}s rentmap {command}: {exc}", flush=True)
        return None


def run_hourly_crawl() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    area = os.environ.get("RENTMAP_AREA_NAME", "")
    center_lat = os.environ.get("RENTMAP_CENTER_LAT", "")
    center_lng = os.environ.get("RENTMAP_CENTER_LNG", "")
    radius_km = os.environ.get("RENTMAP_RADIUS_KM", "")
    max_deposit = os.environ.get("RENTMAP_MAX_DEPOSIT", "")
    max_rent = os.environ.get("RENTMAP_MAX_RENT", "")
    print(
        "[scheduler] hourly-crawl: target "
        f"date={today} area={area or '-'} center={center_lat},{center_lng} "
        f"radius_km={radius_km or '-'} max_deposit={max_deposit or '-'} max_rent={max_rent or '-'} "
        "sources=dabang,zigbang,daangn",
        flush=True,
    )
    exit_code = _run_rentmap(["crawl-all", "--skip-naver", "--date", today], label="hourly-crawl", timeout_s=50 * 60)
    if exit_code == 0:
        run_webhook_flush(trigger="hourly-crawl-complete")


def run_gen_web() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # gen_web is fault-tolerant: missing today's CSV falls back to most recent.
    print(f"[scheduler] gen-web: target date={today} sources=db-auto-fallback", flush=True)
    _run_rentmap(["gen-web", "--date", today], label="gen-web", timeout_s=5 * 60)


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
            print(f"[scheduler] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:
        # Worker failures must never kill the scheduler thread. Log and move on.
        print(f"[scheduler] webhook-flush: failed — {exc}", flush=True)


scheduler = BackgroundScheduler(timezone=TZ)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Every hour at :00 — crawl dabang/zigbang/daangn. Naver crawls in its own
    # container on the same :00 cron.
    scheduler.add_job(
        run_hourly_crawl,
        trigger=CronTrigger(minute=0, timezone=TZ),
        id="hourly_crawl",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    # Every hour at :50, after the hourly crawlers have had time to finish.
    # gen-web falls back to most-recent files for missing sources.
    scheduler.add_job(
        run_gen_web,
        trigger=CronTrigger(minute=50, timezone=TZ),
        id="gen_web_hourly_50",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10 * 60,
    )
    # Startup kicks crawl shortly after boot, then gen-web a bit later so a
    # fresh container has pages without waiting for the first :50 cron.
    now = datetime.now(TZ)
    scheduler.add_job(
        run_hourly_crawl, trigger="date",
        run_date=now + timedelta(seconds=15),
        id="startup_crawl", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        run_gen_web, trigger="date",
        run_date=now + timedelta(seconds=30),
        id="startup_gen_web", max_instances=1, coalesce=True,
    )
    scheduler.start()
    print("[scheduler] started - crawl at :00 hourly, gen-web at :50 hourly, webhook flush after crawl completion (KST)", flush=True)
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
from db import session as db_session  # noqa: E402

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


@app.middleware("http")
async def session_guard(request: Request, call_next):
    """HTML pages + API + /data require a valid session cookie.

    /login.html, /api/auth/*, and static JS/CSS assets are open so the login
    page can render. Photos in /data/photos/<uid>/... are double-checked
    against the caller's user.id to keep one user from probing another's
    folder by URL.
    """
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE_NAME)
    user = auth.lookup_session(token) if token else None

    if user is None:
        if path.startswith("/api/") or path.startswith("/data/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # Pages → bounce to login. Preserve where the user was headed via ?next=.
        target = "/login.html"
        if path and path != "/":
            target += f"?next={path}"
        return RedirectResponse(target, status_code=302)

    # Enforce per-user isolation on photo URLs. The folder layout is
    # /data/photos/<user_id>/<source>_<listing_no>/<filename>; any digit-only
    # segment in position 3 must equal the caller's user.id.
    if path.startswith("/data/photos/"):
        parts = path.split("/", 4)  # ['', 'data', 'photos', '<seg>', 'rest...']
        if len(parts) >= 4 and parts[3].isdigit() and int(parts[3]) != user.id:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

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


@app.post("/api/auth/signup")
async def auth_signup(body: SignupBody, request: Request, response: Response):
    expected = auth.signup_code_required()
    if body.code != expected:
        raise HTTPException(status_code=400, detail="Invalid signup code")

    username = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
        raise HTTPException(
            status_code=400,
            detail="Username may contain letters, digits, '.', '_', '-' only",
        )

    pw_hash = auth.hash_password(body.password)
    display_name = (body.display_name or "").strip() or username

    with db_session() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="Username already taken")
        # Decide admin: first user in the system becomes admin so the operator
        # can run migrate-globals immediately after first signup if they prefer
        # that over `users.py create-admin`.
        cur.execute("SELECT COUNT(*) AS n FROM users")
        is_first = (cur.fetchone()["n"] == 0)
        cur.execute(
            """
            INSERT INTO users (username, password_hash, display_name, is_admin, last_login_at)
            VALUES (%s, %s, %s, %s, now())
            RETURNING id, username, display_name, is_admin, is_active
            """,
            (username, pw_hash, display_name, is_first),
        )
        row = cur.fetchone()

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
        if not row or not row["is_active"]:
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
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    auth.clear_session_cookie(response, request)
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(user: auth.User = Depends(auth.current_user)):
    return {"user": _public_user(user)}


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
        print(f"Error reading favorites: {e}")
        return {"favorites": [], "deleted": {}}


@app.get("/api/favorites")
async def get_favorites(user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.load_state(user.id)["favorites"]
    except Exception as e:
        print(f"Error reading favorites: {e}")
        return []


@app.post("/api/favorites")
async def save_favorites(favorites: Any = Body(...),
                         user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.merge_payload(user.id, favorites)
    except Exception as e:
        print(f"Error saving favorites: {e}")
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
        print(f"Error reading area filter: {e}")
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
        print(f"Error saving area filter: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Mount data directory for CSV and Photo access. Auth is enforced by the
# session_guard middleware above (including per-user user_id check on
# /data/photos/<uid>/...).
app.mount("/data", StaticFiles(directory="data"), name="data")

# Mount web directory at root for all other files (index.html, js, css etc.)
# html=True enables serving index.html automatically at /. The middleware
# turns away un-authenticated HTML requests before they reach this mount.
app.mount("/", StaticFiles(directory="web", html=True), name="web")

if __name__ == "__main__":
    print("RentMap Server starting at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
