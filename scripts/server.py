import os
import json
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, UploadFile, File
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
    scheduler.start()
    print("[scheduler] started — crawl at :00 hourly, gen-web at :00/:30 (KST), plus startup kicks", flush=True)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

# Data storage paths
DATA_DIR = "data"
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites_persistent.json")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")

os.makedirs(PHOTOS_DIR, exist_ok=True)

def get_fav_dir(source: str, id: str):
    # Sanitize path
    folder_name = f"{source}_{id}".replace(":", "_").replace("/", "_")
    path = os.path.join(PHOTOS_DIR, folder_name)
    os.makedirs(path, exist_ok=True)
    return path

@app.get("/api/favorites")
async def get_favorites():
    if not os.path.exists(FAVORITES_FILE):
        return []
    try:
        with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading favorites: {e}")
        return []

@app.post("/api/favorites")
async def save_favorites(favorites: List[Any]):
    try:
        os.makedirs(os.path.dirname(FAVORITES_FILE), exist_ok=True)
        with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites, f, ensure_ascii=False, indent=2)
        return {"status": "success"}
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
    # Create a unique filename
    timestamp = int(time.time() * 1000)
    filename = f"{timestamp}_{file.filename}"
    file_path = os.path.join(fav_dir, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    rel_path = os.path.relpath(file_path, DATA_DIR).replace("\\", "/")
    return {"photoKey": filename, "url": f"/data/{rel_path}"}

@app.delete("/api/photos")
async def delete_photo(id: str, source: str, photoKey: str):
    fav_dir = get_fav_dir(source, id)
    file_path = os.path.join(fav_dir, photoKey)
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
