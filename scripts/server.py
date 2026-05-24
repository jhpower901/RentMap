import os
import re
import json
import shutil
import sqlite3
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
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))


def _run_rentmap(args: list[str], label: str, timeout_s: int) -> None:
    print(f"[scheduler] {label}: rentmap {' '.join(args)}", flush=True)
    try:
        subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            check=False,
            timeout=timeout_s,
        )
        print(f"[scheduler] {label}: done", flush=True)
    except Exception as exc:
        print(f"[scheduler] {label}: failed — {exc}", flush=True)


def run_hourly_crawl() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    _run_rentmap(["crawl-all", "--skip-naver", "--date", today], label="hourly-crawl", timeout_s=50 * 60)


def run_gen_web() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # gen_web is fault-tolerant: missing today's CSV falls back to most recent.
    _run_rentmap(["gen-web", "--date", today], label="gen-web", timeout_s=5 * 60)


def run_webhook_flush() -> None:
    """Drain pending listing_status_events to Discord. Safe to call frequently
    — exits cheaply when there's nothing to send or no URL configured.
    """
    try:
        # Local import keeps DB / requests out of server startup if the worker
        # module ever gains heavier imports.
        sys.path.insert(0, str(ROOT / "scripts"))
        from webhook_worker import flush_once  # noqa: WPS433 — intentional late import
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"[scheduler] webhook-flush: {nonzero}", flush=True)
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
    # Every 30 minutes (:00 and :30) — regenerate web pages from whatever CSVs
    # are present (gen-web falls back to most-recent files for missing sources).
    scheduler.add_job(
        run_gen_web,
        trigger=CronTrigger(minute="0,30", timezone=TZ),
        id="gen_web_30m",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10 * 60,
    )
    # Startup kicks — crawl shortly after boot, then gen-web a bit later so a
    # fresh container ends up with rendered pages without waiting for the cron.
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
    # Every minute — flush pending Discord notifications. The worker self-caps
    # at 25 events per pass, so even a large backlog drains gracefully rather
    # than bursting through Discord's 30/min rate limit.
    scheduler.add_job(
        run_webhook_flush,
        trigger=CronTrigger(minute="*", timezone=TZ),
        id="webhook_flush",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    print("[scheduler] started — crawl at :00 hourly, gen-web at :00/:30, webhook-flush every minute (KST)", flush=True)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


_VALID_SOURCES = {"dabang", "daangn", "zigbang", "naver"}
_SOURCE_TO_PLATFORM_CODE = {
    # UI uses short codes; DB platforms table stores "naver_land" for naver.
    "dabang": "dabang",
    "daangn": "daangn",
    "zigbang": "zigbang",
    "naver": "naver_land",
}


@app.get("/api/listings/{source}/{listing_no}/price-history")
def price_history(source: str, listing_no: str, limit: int = 60) -> dict[str, Any]:
    """Return up to ``limit`` price snapshots for one listing, oldest first.

    Powers the sparkline that the detail row lazy-loads when a user expands
    a listing. The UI cap is small (≈20 points), but we default the API at
    60 to leave room for future "show full history" UIs without a schema
    change.

    Returns ``{ "points": [{t, deposit, rent, maint, total}, ...] }``. Empty
    points list is a valid response when the DB is empty or the listing was
    never matched (e.g. a CSV-only environment).
    """
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source: {source}")
    if not listing_no or len(listing_no) > 100:
        raise HTTPException(status_code=400, detail="invalid listing_no")
    limit = max(1, min(int(limit), 500))

    platform_code = _SOURCE_TO_PLATFORM_CODE[source]
    # Late import keeps server boot independent of DB availability — a fresh
    # container can serve static pages even before db-stack is up.
    sys.path.insert(0, str(ROOT / "scripts"))
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


# Data storage paths
DATA_DIR = "data"
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites_persistent.json")
FAVORITES_DB_FILE = os.path.join(DATA_DIR, "rentmap.db")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")

os.makedirs(PHOTOS_DIR, exist_ok=True)

def normalize_favorites_payload(payload: Any) -> dict[str, Any]:
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

def iso_time(value: Any) -> float:
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0

def merge_deleted(*states: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for state in states:
        for key, value in state.get("deleted", {}).items():
            if isinstance(key, str) and isinstance(value, str):
                if iso_time(value) >= iso_time(merged.get(key)):
                    merged[key] = value
    return merged

def filter_deleted(favorites: list[Any], deleted: dict[str, str]) -> list[Any]:
    filtered = []
    for entry in favorites:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key:
            continue
        if iso_time(deleted.get(key)) >= iso_time(entry.get("savedAt")):
            continue
        filtered.append(entry)
    return filtered

def merge_favorites(*states: dict[str, Any], deleted: dict[str, str]) -> list[Any]:
    by_key: dict[str, dict[str, Any]] = {}
    for state in states:
        for entry in state.get("favorites", []):
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            if iso_time(deleted.get(key)) >= iso_time(entry.get("savedAt")):
                continue
            prev = by_key.get(key)
            if prev is None or iso_time(entry.get("savedAt")) >= iso_time(prev.get("savedAt")):
                by_key[key] = entry
    return sorted(by_key.values(), key=lambda entry: iso_time(entry.get("savedAt")), reverse=True)

def db_connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(FAVORITES_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_favorites_db() -> None:
    conn = db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                key TEXT PRIMARY KEY,
                id TEXT NOT NULL,
                source TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                saved_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorite_deleted (
                key TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            )
        """)
        fav_count = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        del_count = conn.execute("SELECT COUNT(*) FROM favorite_deleted").fetchone()[0]
        if fav_count == 0 and del_count == 0 and os.path.exists(FAVORITES_FILE):
            try:
                with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                    legacy = normalize_favorites_payload(json.load(f))
                write_favorites_state(legacy, conn=conn)
                print(f"[favorites] migrated legacy JSON to SQLite: {FAVORITES_DB_FILE}", flush=True)
            except Exception as exc:
                print(f"[favorites] legacy JSON migration skipped: {exc}", flush=True)
    finally:
        conn.close()

def write_favorites_state(state: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
    close_conn = conn is None
    if conn is None:
        conn = db_connect()
    try:
        conn.execute("DELETE FROM favorites")
        for entry in state.get("favorites", []):
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            id_value = entry.get("id")
            source = entry.get("source")
            if not key or id_value is None or source is None:
                continue
            conn.execute(
                """
                INSERT INTO favorites (key, id, source, entry_json, saved_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    id=excluded.id,
                    source=excluded.source,
                    entry_json=excluded.entry_json,
                    saved_at=excluded.saved_at
                """,
                (str(key), str(id_value), str(source), json.dumps(entry, ensure_ascii=False), entry.get("savedAt") or ""),
            )
        conn.execute("DELETE FROM favorite_deleted")
        for key, deleted_at in state.get("deleted", {}).items():
            if isinstance(key, str) and isinstance(deleted_at, str):
                conn.execute(
                    "INSERT INTO favorite_deleted (key, deleted_at) VALUES (?, ?)",
                    (key, deleted_at),
                )
        conn.commit()
    finally:
        if close_conn:
            conn.close()

def load_favorites_state_from_db() -> dict[str, Any]:
    ensure_favorites_db()
    conn = db_connect()
    try:
        deleted = {
            row["key"]: row["deleted_at"]
            for row in conn.execute("SELECT key, deleted_at FROM favorite_deleted")
        }
        favorites = []
        for row in conn.execute("SELECT entry_json FROM favorites"):
            try:
                entry = json.loads(row["entry_json"])
            except json.JSONDecodeError:
                continue
            key = entry.get("key") if isinstance(entry, dict) else None
            if key and iso_time(deleted.get(key)) < iso_time(entry.get("savedAt")):
                favorites.append(entry)
        favorites.sort(key=lambda entry: iso_time(entry.get("savedAt")), reverse=True)
        return {"favorites": favorites, "deleted": deleted}
    finally:
        conn.close()

_SAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_folder_segment(value: str) -> str:
    return _SAFE_FOLDER_RE.sub("_", value or "")


def _sanitize_filename(value: str) -> str:
    base = os.path.basename(value or "")
    cleaned = _SAFE_FILE_RE.sub("_", base)
    # Block "." / ".." / leading-dot names — whitelist allows dots for extensions.
    if not cleaned or cleaned.startswith("."):
        cleaned = "_" + cleaned
    return cleaned


def get_fav_dir(source: str, id: str):
    folder_name = f"{_sanitize_folder_segment(source)}_{_sanitize_folder_segment(id)}"
    path = os.path.join(PHOTOS_DIR, folder_name)
    resolved = os.path.realpath(path)
    photos_root = os.path.realpath(PHOTOS_DIR)
    if not resolved.startswith(photos_root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")
    os.makedirs(resolved, exist_ok=True)
    return resolved

def load_favorites_state() -> dict[str, Any]:
    return load_favorites_state_from_db()

@app.get("/api/favorites/state")
async def get_favorites_state():
    try:
        return load_favorites_state()
    except Exception as e:
        print(f"Error reading favorites: {e}")
        return {"favorites": [], "deleted": {}}

@app.get("/api/favorites")
async def get_favorites():
    try:
        return load_favorites_state()["favorites"]
    except Exception as e:
        print(f"Error reading favorites: {e}")
        return []

@app.post("/api/favorites")
async def save_favorites(favorites: Any = Body(...)):
    try:
        existing = load_favorites_state()
        incoming = normalize_favorites_payload(favorites)
        deleted = merge_deleted(existing, incoming)
        payload = {
            "favorites": merge_favorites(existing, incoming, deleted=deleted),
            "deleted": deleted,
        }
        write_favorites_state(payload)
        return payload
    except Exception as e:
        print(f"Error saving favorites: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/photos")
async def list_photos(id: str, source: str):
    fav_dir = get_fav_dir(source, id)
    photos = []
    for filename in sorted(os.listdir(fav_dir)):
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            # We return metadata including the web-accessible URL
            # The URL points to /data/photos/folder/filename
            rel_path = os.path.relpath(os.path.join(fav_dir, filename), DATA_DIR).replace("\\", "/")
            photos.append({
                "photoKey": filename,
                "url": f"/data/{rel_path}",
                "addedAt": os.path.getctime(os.path.join(fav_dir, filename))
            })
    return photos

@app.post("/api/photos")
async def upload_photo(id: str, source: str, file: UploadFile = File(...)):
    fav_dir = get_fav_dir(source, id)
    timestamp = int(time.time() * 1000)
    filename = f"{timestamp}_{_sanitize_filename(file.filename or '')}"
    file_path = os.path.join(fav_dir, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    rel_path = os.path.relpath(file_path, DATA_DIR).replace("\\", "/")
    return {"photoKey": filename, "url": f"/data/{rel_path}"}

@app.delete("/api/photos")
async def delete_photo(id: str, source: str, photoKey: str):
    fav_dir = get_fav_dir(source, id)
    file_path = os.path.join(fav_dir, _sanitize_filename(photoKey))
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Photo not found")

# Mount data directory for CSV and Photo access
app.mount("/data", StaticFiles(directory="data"), name="data")

# Mount web directory at root for all other files (index.html, js, css etc.)
# html=True enables serving index.html automatically at /
app.mount("/", StaticFiles(directory="web", html=True), name="web")

if __name__ == "__main__":
    print("RentMap Server starting at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
