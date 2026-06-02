"""FastAPI entry point for the standalone DB-tool.

Why this is a separate ASGI app from scripts/server.py:

* Process isolation — a crash in this admin UI must not 5xx the public
  RentMap site. They share the same image and the same Postgres but
  nothing else.
* Distinct cookie name — see deps.COOKIE_NAME.
* Tightened bind — uvicorn binds 127.0.0.1 only; reaching the tool
  from outside the host requires an SSH tunnel.

Run via::

    python scripts/dbtool/server.py            # 127.0.0.1:8001
    PORT=9001 python scripts/dbtool/server.py  # override port

Inside Docker, the dbtool service in docker-compose.override.yml uses
the same image and runs the same command. See docs/dbtool.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dbtool import deps  # noqa: E402
from dbtool.routes_auth import router as auth_router  # noqa: E402
from dbtool.routes_users import router as users_router  # noqa: E402
from dbtool.routes_favorites import router as favorites_router  # noqa: E402
from dbtool.routes_listings import router as listings_router  # noqa: E402
from dbtool.routes_events import router as events_router  # noqa: E402
from dbtool.routes_regions import router as regions_router  # noqa: E402
from dbtool.routes_audit import router as audit_router  # noqa: E402


WEB_ROOT = ROOT / "web" / "dbtool"

app = FastAPI(title="RentMap DB-tool", docs_url=None, redoc_url=None)


# ──────────────────────────────────────────────────────────────────────────────
# Same-origin write guard. The tool is a single-page app on the same host,
# so any cross-origin write is by definition not coming from our UI.
# ──────────────────────────────────────────────────────────────────────────────

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC_EXACT = {"/login.html", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/api/tool/auth/",)
_PUBLIC_ASSET_EXTS = (".js", ".css", ".ico", ".png", ".jpg", ".jpeg",
                      ".svg", ".webp", ".woff", ".woff2", ".map")


def _same_origin(request: Request) -> bool:
    host = request.headers.get("host")
    if not host:
        return False

    def host_of(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return urlparse(value).netloc or None
        except ValueError:
            return None

    origin = host_of(request.headers.get("origin"))
    if origin is not None:
        return origin == host
    referer = host_of(request.headers.get("referer"))
    if referer is not None:
        return referer == host
    # No Origin and no Referer on a write — refuse. Same policy as the
    # main service. The tool only ever runs under same-origin XHR/fetch
    # so this is a clean reject for any external curl/script.
    return False


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    if path.endswith(_PUBLIC_ASSET_EXTS):
        return True
    return False


@app.middleware("http")
async def guard(request: Request, call_next):
    # CSRF first so a stray write to /api/tool/auth/login can't side-step.
    if request.method in _WRITE_METHODS and not _same_origin(request):
        return JSONResponse(
            {"detail": "Cross-origin write rejected"}, status_code=403
        )

    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    token = request.cookies.get(deps.COOKIE_NAME)
    user = deps.lookup_session(token) if token else None
    if user is None or not user.is_admin:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # Bounce HTML requests back to login, preserving destination.
        target = "/login.html"
        if path and path != "/":
            target += f"?next={quote(path, safe='/')}"
        return RedirectResponse(target, status_code=302)
    request.state.user = user
    return await call_next(request)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(favorites_router)
app.include_router(listings_router)
app.include_router(events_router)
app.include_router(regions_router)
app.include_router(audit_router)


# Root → SPA shell. Anything else under "/" tries the static mount.
@app.get("/")
def root():
    return FileResponse(WEB_ROOT / "index.html")


# Static page bundle. Serves login.html, index.html (SPA), JS, CSS.
if WEB_ROOT.exists():
    app.mount("/", StaticFiles(directory=str(WEB_ROOT), html=True), name="static")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    port = int(os.environ.get("PORT", "8001"))
    # 127.0.0.1 only by default — operator must SSH-tunnel to reach it.
    # RENTMAP_DBTOOL_BIND=0.0.0.0 to override (don't, unless you know
    # what you're doing).
    host = os.environ.get("RENTMAP_DBTOOL_BIND", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
