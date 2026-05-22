import os
import json
import shutil
import time
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List, Any
import uvicorn

app = FastAPI()

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
