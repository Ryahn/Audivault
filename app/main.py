"""FastAPI application: mounts the Audiobookshelf-compatible API and the web UI."""

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import admin, auth_routes, entities, items, libraries, me, sessions
from .api_spec import DESCRIPTION, TAGS, install_openapi
from .catalog import library
from .catalog.locks import LockedError
from .core import auth, db, tasks
from .core.config import CORS_ORIGINS
from .management import trash
from .management.watcher import watcher
from .scanner import probe, scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("abstream")

WEB_DIR = Path(__file__).parent / "web"
_CSRF_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_SKIP_PREFIXES = (
    "/login", "/logout", "/auth/refresh", "/init", "/status", "/ping", "/healthcheck",
    "/public/", "/api/authorize",
)


def _scheduler() -> None:
    """Periodic library scans (per-library `scanIntervalMinutes`) and hourly trash emptying."""
    last_purge = 0.0
    while True:
        time.sleep(60)
        if time.time() - last_purge >= 3600:
            last_purge = time.time()
            tasks.start("trash", "Empty old trash", trash.purge, key="trash", quiet=True)
        try:
            for lib in library.all_libraries():
                settings = library.library_settings(lib)
                minutes = int(settings.get("scanIntervalMinutes") or 0)
                if minutes > 0 and (lib["last_scan"] or 0) < db.now_ms() - minutes * 60_000:
                    tasks.start("scan", f"Scheduled scan {lib['name']}", scanner.scan_library, lib["id"], key=f"scan:{lib['id']}",
                                library_id=lib["id"], quiet=True)
                hours = int(settings.get("metadataRefreshHours") or 0)
                last_refresh = int(settings.get("lastMetadataRefresh") or 0)
                if hours > 0 and last_refresh < db.now_ms() - hours * 3_600_000:
                    from .catalog import media
                    tasks.start("match", f"Refresh metadata {lib['name']}", media.refresh_library_metadata, lib["id"],
                                key=f"refresh:{lib['id']}", library_id=lib["id"], quiet=True)
        except Exception:
            log.exception("Scheduler error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    from .catalog import fts
    fts.ensure_fts()
    log.info("Audivault %s starting (ffprobe %s)", __version__, "found" if probe.FFPROBE else "not found, using mutagen only")
    for lib in library.all_libraries():
        tasks.start("scan", f"Startup scan {lib['name']}", scanner.scan_library, lib["id"], key=f"scan:{lib['id']}", library_id=lib["id"])
    watcher.start()
    tasks.start("trash", "Empty old trash", trash.purge, key="trash", quiet=True)
    from .management import autoimport
    for lib in library.all_libraries():
        autoimport.start(lib["id"])
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=items.warm_cover_thumbnails, daemon=True, name="cover-warmup").start()
    yield


app = FastAPI(
    title="Audivault",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
    docs_url="/api-docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    openapi_tags=TAGS,
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "filter": True,
        "docExpansion": "none",
    },
)
_cors_credentials = CORS_ORIGINS != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range"],
)


@app.middleware("http")
async def csrf_cookie_auth(request: Request, call_next):
    """Require X-CSRF-Token for mutating requests authenticated only via access cookie."""
    if request.method not in _CSRF_SAFE and auth.auth_via_cookie(request):
        path = request.url.path
        if not any(path == p or path.startswith(p) for p in _CSRF_SKIP_PREFIXES):
            if not auth.csrf_ok(request):
                return PlainTextResponse("CSRF token missing or invalid", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        # Revalidate with ETag on every load so UI updates are picked up immediately
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(LockedError)
async def locked_error(request: Request, exc: LockedError):
    return PlainTextResponse(str(exc), status_code=423)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    # Audiobookshelf answers API errors with plain text bodies
    if request.url.path.startswith(("/api", "/login", "/auth", "/public", "/init", "/logout")):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    if exc.status_code == 404 and request.method == "GET":
        return _index_html()
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


for module in (auth_routes, libraries, items, me, sessions, entities, admin):
    app.include_router(module.router)

install_openapi(app)

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


def _index_html() -> HTMLResponse:
    """index.html with asset URLs stamped by file mtime, so browsers never run stale CSS/JS."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("css/app.css", "js/main.js"):
        stamp = int((WEB_DIR / "static" / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={stamp}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def index():
    return _index_html()
