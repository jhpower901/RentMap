"""Favorites, bookmarks, photos, user-filters, area-filter routes."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api import auth
from app.api import favorites as fav_store
from app.api import bookmarks as bookmark_store
from app.api import area_filters as area_store
from app.api import filter_preferences as filter_pref_store
from app.db import session as db_session

router = APIRouter()

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
DATA_DIR = "data"
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

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


@router.get("/api/favorites/state")
async def get_favorites_state(user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.load_state(user.id)
    except Exception as e:
        # Don't 500 the client over a DB blip — empty state lets local cache win.
        print(f"{_ts()} Error reading favorites: {e}")
        return {"favorites": [], "deleted": {}}


@router.get("/api/favorites")
async def get_favorites(user: auth.User = Depends(auth.current_user)):
    try:
        return fav_store.load_state(user.id)["favorites"]
    except Exception as e:
        print(f"{_ts()} Error reading favorites: {e}")
        return []


@router.post("/api/favorites")
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


# ─────────────────────────────────────────────────────────────────────────────
# Bookmarks — separate axis from favorites. See scripts/bookmarks.py for the
# wire format. The endpoints mirror /api/favorites so the client can use the
# same "POST current state, server merges, returns canonical" loop. The
# user-id header check is the same guard against a cross-account write.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/bookmarks/state")
async def get_bookmarks_state(user: auth.User = Depends(auth.current_user)):
    try:
        return bookmark_store.load_state(user.id)
    except Exception as e:
        print(f"{_ts()} Error reading bookmarks: {e}")
        return {"bookmarks": [], "deleted": {}}


@router.post("/api/bookmarks")
async def save_bookmarks(request: Request, bookmarks: Any = Body(...),
                         user: auth.User = Depends(auth.current_user)):
    posted_user_id = request.headers.get("x-rentmap-user-id")
    if posted_user_id != str(user.id):
        raise HTTPException(status_code=409, detail="Bookmarks sync user changed; reload required")
    try:
        return bookmark_store.merge_payload(user.id, bookmarks)
    except Exception as e:
        print(f"{_ts()} Error saving bookmarks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/photos")
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


@router.post("/api/photos")
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


@router.delete("/api/photos")
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


@router.get("/api/user-filters/{context}")
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


@router.put("/api/user-filters/{context}")
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


@router.get("/api/area-filter")
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


@router.put("/api/area-filter")
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
