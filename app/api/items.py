"""/api/items routes plus metadata provider search."""

import logging
import os
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .. import providers
from ..catalog import library, locks, media, serialize
from ..core import auth, db, tasks
from ..core.config import METADATA_DIR
from ..scanner import scanner
from .common import get_item_or_404, truthy
from .sessions import start_session

router = APIRouter()
log = logging.getLogger("abstream.covers")

COVER_GRID_WIDTH = 300
_WARM_WORKERS = 2
_GRID_COVER_CACHE = "public, max-age=31536000, immutable"
_FULL_COVER_CACHE = "private, max-age=86400"


def _item_response(item_id: str, user: dict) -> dict:
    return serialize.item_json(media.get_item(item_id), expanded=True, user=user, include_progress=True)


@router.get("/api/items/{item_id}")
def get_item(item_id: str, request: Request, expanded: str = "0", include: str = ""):
    user = auth.current_user(request)
    item = get_item_or_404(item_id, user)
    out = serialize.item_json(item, expanded=True, user=user, include_progress="progress" in include or truthy(expanded))
    lock = locks.item_lock(item)
    out["locked"] = bool(lock)
    out["lockReason"] = lock["reason"] if lock else None
    if "rssfeed" in include:
        out["rssFeed"] = None
    if "share" in include:
        out["mediaItemShare"] = None
    if "authors" in include:
        out["media"]["metadata"]["authors"] = [serialize.author_json(a) for a in db.q(
            "SELECT a.* FROM book_authors ba JOIN authors a ON a.id = ba.author_id WHERE ba.item_id = ? ORDER BY ba.position", (item_id,))]
    return out


@router.delete("/api/items/{item_id}")
def delete_item(item_id: str, request: Request, hard: str = "0"):
    user = auth.current_user(request)
    auth.require_permission(user, "delete")
    item = get_item_or_404(item_id, user)
    if truthy(hard):
        locks.check_item_delete(item)
    if truthy(hard) and os.path.exists(item["path"]):
        if item["is_file"]:
            os.remove(item["path"])
        else:
            shutil.rmtree(item["path"])
    library.delete_item_records(item_id)
    scanner.remove_item_cache(item_id)
    library.cleanup_orphans(item["library_id"])
    tasks.activity(f"Removed \"{item['title']}\"" + (" and deleted its files" if truthy(hard) else ""))
    return {"success": True}


@router.patch("/api/items/{item_id}/media")
def update_media(item_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    get_item_or_404(item_id, user)
    updated = media.update_media(item_id, body)
    return {"updated": updated, "libraryItem": _item_response(item_id, user)}


@router.post("/api/items/{item_id}/chapters")
def update_chapters(item_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    get_item_or_404(item_id, user)
    updated = media.update_media(item_id, {"chapters": body.get("chapters") or []})
    return {"success": True, "updated": updated}


@router.post("/api/items/{item_id}/match")
def match_item(item_id: str, request: Request, body: dict = Body(default={})):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    item = get_item_or_404(item_id, user)
    provider = body.get("provider") or library.library_settings(library.get_library(item["library_id"]))["metadataProvider"]
    if body.get("result"):
        updated = media.apply_match(item_id, body["result"], override=body.get("overrideDefaults", True),
                                    update_cover=body.get("updateCover", True))
        return {"updated": updated, "libraryItem": _item_response(item_id, user)}
    authors = library.item_authors(item_id)
    results = providers.search_books(provider, body.get("title") or item["title"],
                                     body.get("author") or (authors[0]["name"] if authors else None),
                                     asin=body.get("asin") or item["asin"], isbn=body.get("isbn"))
    if not results:
        return {"updated": False, "warning": "No match found", "libraryItem": _item_response(item_id, user)}
    updated = media.apply_match(item_id, results[0], override=truthy(body.get("overrideDefaults", False)))
    return {"updated": updated, "libraryItem": _item_response(item_id, user)}


@router.post("/api/items/{item_id}/scan")
def scan_item(item_id: str, request: Request):
    auth.require_admin(request)
    action = scanner.rescan_item(item_id, force=True)
    return {"result": action.upper()}


# ------------------------------------------------------------------ cover

def _valid_item_id(item_id: str) -> bool:
    try:
        return str(uuid.UUID(item_id)) == item_id
    except (ValueError, AttributeError, TypeError):
        return False


def _clamp_cover_width(width: int) -> int:
    return max(32, min(int(width), 800))


def _cover_thumb_path(item_id: str, width: int) -> Path | None:
    """Grid JPEG path, or None when item_id is not a library item id."""
    if not _valid_item_id(item_id):
        return None
    return METADATA_DIR / "cache" / "covers" / item_id / f"w{_clamp_cover_width(width)}.jpg"


def _existing_cover_thumb(item_id: str, width: int) -> Path | None:
    """Cached thumbnail under the metadata dir, without looking at the original cover."""
    dest = _cover_thumb_path(item_id, width)
    if dest is None or not dest.is_file():
        return None
    try:
        resolved = dest.resolve()
        root = (METADATA_DIR / "cache" / "covers").resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _cover_thumbnail(item_id: str, src: str, width: int) -> Path | None:
    """Return a cached JPEG thumbnail path, or None if resize fails (caller serves original)."""
    dest = _cover_thumb_path(item_id, width)
    if dest is None:
        return None
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{uuid.uuid4().hex}.part")
    try:
        from PIL import Image
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        clamped = _clamp_cover_width(width)
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > clamped:
                im = im.resize((clamped, max(1, int(h * (clamped / w)))), resample)
            im.save(tmp, "JPEG", quality=82)
        os.replace(tmp, dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def _warm_one(item_id: str, src: str) -> None:
    from ..catalog.paths import is_allowed_media_path
    try:
        if src and is_allowed_media_path(src):
            _cover_thumbnail(item_id, src, COVER_GRID_WIDTH)
    except Exception:
        log.exception("Cover thumbnail failed for %s", item_id)


def warm_cover_thumbnails() -> None:
    """Build missing 300px grid JPEGs off the request path. Two at a time, so playback keeps the disks."""
    try:
        rows = db.q(
            "SELECT id, cover_path FROM library_items WHERE cover_path IS NOT NULL AND cover_path != '' AND is_missing = 0"
        )
        pending = []
        for row in rows:
            dest = _cover_thumb_path(row["id"], COVER_GRID_WIDTH)
            if dest is not None and not dest.is_file():
                pending.append((row["id"], row["cover_path"]))
        if not pending:
            return
        log.info("Building %s cover thumbnail(s)", len(pending))
        with ThreadPoolExecutor(max_workers=_WARM_WORKERS) as pool:
            list(pool.map(lambda row: _warm_one(*row), pending))
    except Exception:
        log.exception("Cover thumbnail warmup failed")


@router.get("/api/items/{item_id}/cover")
def get_cover(item_id: str, width: int | None = None):
    """Public like Audiobookshelf: image loaders in apps don't send auth headers.
    Pass width= to get a cached grid-sized JPEG thumbnail.

    A thumbnail hit is served from the metadata cache and does not stat the
    original. That file is removed when the cover itself changes.
    """
    if width:
        thumb = _existing_cover_thumb(item_id, int(width))
        if thumb is not None:
            return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": _GRID_COVER_CACHE})
    from ..catalog.paths import is_allowed_media_path
    item = db.q1("SELECT cover_path FROM library_items WHERE id = ?", (item_id,))
    if not item or not item["cover_path"] or not is_allowed_media_path(item["cover_path"]):
        raise HTTPException(404, "No cover")
    path = item["cover_path"]
    if width:
        thumb = _cover_thumbnail(item_id, path, int(width))
        if thumb is not None:
            return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": _GRID_COVER_CACHE})
    return FileResponse(path, headers={"Cache-Control": _FULL_COVER_CACHE})


COVER_UPLOAD_MAX = 8 * 1024 * 1024
COVER_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}


def _validate_cover_bytes(data: bytes) -> str:
    """Return normalized extension from image magic / Pillow, or raise ValueError."""
    if not data or len(data) > COVER_UPLOAD_MAX:
        raise ValueError("Cover too large or empty")
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    try:
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            fmt = (im.format or "").upper()
    except Exception as e:
        raise ValueError("Invalid image") from e
    mapped = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt)
    if not mapped:
        raise ValueError("Unsupported image type")
    return mapped


@router.post("/api/items/{item_id}/cover")
async def upload_cover(item_id: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    get_item_or_404(item_id, user)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        upload: UploadFile = form.get("cover")
        if not upload:
            raise HTTPException(400, "No file")
        data = await upload.read(COVER_UPLOAD_MAX + 1)
        try:
            ext = _validate_cover_bytes(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        path = media.save_cover_bytes(item_id, data, ext)
    else:
        body = await request.json()
        url = body.get("url")
        if not url:
            raise HTTPException(400, "url required")
        path = media.set_cover_from_url(item_id, url)
        if not path:
            raise HTTPException(400, "Could not download cover")
    return {"success": True, "cover": path}


@router.patch("/api/items/{item_id}/cover")
def set_existing_cover(item_id: str, request: Request, body: dict = Body(...)):
    """Use one of the item's own image files as cover."""
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    item = get_item_or_404(item_id, user)
    cover = body.get("cover")
    files = {f["path"] for f in db.jload(item["library_files"], []) if f.get("file_type") == "image"}
    if cover not in files:
        raise HTTPException(400, "Cover must be an image file of this item")
    db.ex("UPDATE library_items SET cover_path = ?, updated_at = ? WHERE id = ?", (cover, db.now_ms(), item_id))
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(item["library_id"])
    media.clear_cover_thumbnails(item_id)
    return {"success": True, "cover": cover}


@router.delete("/api/items/{item_id}/cover")
def remove_cover(item_id: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    item = get_item_or_404(item_id, user)
    db.ex("UPDATE library_items SET cover_path = NULL, updated_at = ? WHERE id = ?", (db.now_ms(), item_id))
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(item["library_id"])
    media.clear_cover_thumbnails(item_id)
    return {"success": True}


# ------------------------------------------------------------------ files

def _library_file(item: dict, ino: str) -> dict:
    f = next((f for f in db.jload(item["library_files"], []) if f["ino"] == ino), None)
    if not f or not os.path.exists(f["path"]):
        raise HTTPException(404, "File not found")
    return f


@router.get("/api/items/{item_id}/file/{ino}")
def stream_file(item_id: str, ino: str, request: Request):
    user = auth.current_user(request)
    item = get_item_or_404(item_id, user)
    f = _library_file(item, ino)
    audio = next((a for a in db.jload(item["audio_files"], []) if a["ino"] == ino), None)
    return FileResponse(f["path"], media_type=(audio or {}).get("mime_type"))


@router.get("/api/items/{item_id}/file/{ino}/download")
def download_file(item_id: str, ino: str, request: Request):
    user = auth.current_user_for_download(request, item_id)
    auth.require_permission(user, "download")
    item = get_item_or_404(item_id, user)
    f = _library_file(item, ino)
    return FileResponse(f["path"], filename=f["filename"])


@router.get("/api/items/{item_id}/ebook")
@router.get("/api/items/{item_id}/ebook/{file_id}")
def get_ebook(item_id: str, request: Request, file_id: str | None = None):
    user = auth.current_user(request)
    item = get_item_or_404(item_id, user)
    if file_id:
        f = _library_file(item, file_id)
    else:
        f = db.jload(item["ebook_file"], None)
        if not f or not os.path.exists(f["path"]):
            raise HTTPException(404, "No ebook")
    return FileResponse(f["path"], filename=f["filename"])


class _ZipStream:
    """File-like sink that lets zipfile write into a generator."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self.pos = 0

    def write(self, data):
        self.chunks.append(bytes(data))
        self.pos += len(data)
        return len(data)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def drain(self) -> bytes:
        data = b"".join(self.chunks)
        self.chunks.clear()
        return data


@router.get("/api/items/{item_id}/download-url")
def download_url(item_id: str, request: Request):
    """Mint a short-lived signed download URL (web UI navigation; not a full access token)."""
    user = auth.current_user(request)
    auth.require_permission(user, "download")
    get_item_or_404(item_id, user)
    token = auth.make_download_token(user, item_id)
    return {
        "url": f"/api/items/{item_id}/download?dl={token}",
        "expiresIn": auth.DOWNLOAD_TOKEN_TTL,
    }


@router.get("/api/items/{item_id}/download")
def download_item(item_id: str, request: Request):
    user = auth.current_user_for_download(request, item_id)
    auth.require_permission(user, "download")
    item = get_item_or_404(item_id, user)
    files = [f for f in db.jload(item["library_files"], []) if os.path.exists(f["path"])]

    def generate():
        sink = _ZipStream()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for f in files:
                with open(f["path"], "rb") as src, zf.open(f["rel_path"], "w", force_zip64=True) as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                        yield sink.drain()
                yield sink.drain()
        yield sink.drain()

    name = "".join(c for c in item["title"] if c not in '<>:"/\\|?*') or "download"
    return StreamingResponse(generate(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


# ------------------------------------------------------------------ playback

@router.post("/api/items/{item_id}/play")
@router.post("/api/items/{item_id}/play/{episode_id}")
async def play(item_id: str, request: Request, episode_id: str | None = None):
    user = auth.current_user(request)
    item = get_item_or_404(item_id, user)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not any(not a.get("exclude") for a in db.jload(item["audio_files"], [])):
        raise HTTPException(400, "Item has no audio tracks")
    return start_session(user, item, body or {})


# ------------------------------------------------------------------ batch

@router.post("/api/items/batch/get")
def batch_get(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    ids = list(dict.fromkeys(body.get("libraryItemIds") or []))
    if not ids:
        return {"libraryItems": []}
    placeholders = ",".join("?" for _ in ids)
    rows = db.q(f"SELECT * FROM library_items WHERE id IN ({placeholders})", tuple(ids))
    by_id = {r["id"]: r for r in rows if auth.can_access_library(user, r["library_id"])}
    ordered = [by_id[i] for i in ids if i in by_id]
    authors_by_id, series_by_id = library.bulk_authors_series([r["id"] for r in ordered])
    progress = library.bulk_progress(user["id"], [r["id"] for r in ordered])
    out = [serialize.item_json(r, expanded=True, user=user, include_progress=True,
                               authors=authors_by_id.get(r["id"], []), series=series_by_id.get(r["id"], []),
                               progress=progress.get(r["id"]), progress_known=True) for r in ordered]
    return {"libraryItems": out}


@router.post("/api/items/batch/update")
def batch_update(request: Request, body: list = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    updates = 0
    for entry in body:
        item = media.get_item(entry.get("id"))
        if item and auth.can_access_library(user, item["library_id"]) and media.update_media(item["id"], entry.get("mediaPayload") or {}):
            updates += 1
    return {"success": True, "updates": updates}


@router.post("/api/items/batch/delete")
def batch_delete(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "delete")
    libs = set()
    for item_id in body.get("libraryItemIds") or []:
        item = media.get_item(item_id)
        if item and auth.can_access_library(user, item["library_id"]):
            library.delete_item_records(item_id)
            scanner.remove_item_cache(item_id)
            libs.add(item["library_id"])
    for lib_id in libs:
        library.cleanup_orphans(lib_id)
    return {"success": True}


@router.post("/api/items/batch/quickmatch")
def batch_quickmatch(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    ids = []
    for i in body.get("libraryItemIds") or []:
        item = media.get_item(i)
        if item and auth.can_access_library(user, item["library_id"]):
            ids.append(i)
    options = body.get("options") or {}

    def run(task):
        task["total"] = len(ids)
        matched = 0
        for n, item_id in enumerate(ids):
            try:
                if media.quick_match(item_id, options.get("provider"), truthy(options.get("overrideDefaults", False)))["updated"]:
                    matched += 1
            except Exception as e:
                tasks.activity(f"Quick match failed: {e}", "warning")
            task["progress"] = n + 1
        return {"matched": matched}

    task = tasks.start("match", f"Quick match {len(ids)} items", run, user_id=user["id"])
    return {"success": True, "task": task}


# ------------------------------------------------------------------ provider search

@router.get("/api/search/providers")
def search_providers(request: Request):
    auth.current_user(request)
    # podcasts: [] kept for Audiobookshelf / AudioBooth client shape
    return {"providers": {
        "books": providers.provider_list(),
        "booksCovers": [{"value": "best", "text": "Best"}] + [{"value": k, "text": v} for k, v in providers.COVER_PROVIDERS.items()],
        "podcasts": [],
    }}


@router.get("/api/search/books")
def search_books(request: Request, title: str = "", author: str | None = None, provider: str = "audible",
                 asin: str | None = None, isbn: str | None = None, id: str | None = None):
    auth.current_user(request)
    if not title and not asin:
        raise HTTPException(400, "title required")
    return providers.search_books(provider, title or asin, author, asin=asin, isbn=isbn)


@router.get("/api/search/covers")
def search_covers(request: Request, title: str = "", author: str | None = None, provider: str = "best"):
    auth.current_user(request)
    if not title:
        raise HTTPException(400, "title required")
    return {"results": providers.search_covers(provider, title, author)}


@router.get("/api/search/chapters")
def search_chapters(request: Request, asin: str = "", region: str = "us"):
    """Chapter lookup from Audnexus by ASIN (same as Audiobookshelf)."""
    auth.current_user(request)
    from ..providers import audible
    from ..providers.http import get_json
    if not audible.is_asin(asin):
        raise HTTPException(400, "Invalid ASIN")
    data = get_json(f"{audible.AUDNEXUS}/books/{asin.upper()}/chapters", {"region": region})
    if not data or not data.get("chapters"):
        return {"error": "Chapters not found"}
    return data


@router.get("/api/search/authors")
def search_authors(request: Request, q: str = "", region: str = "us"):
    auth.current_user(request)
    from ..providers import audible
    return audible.author_search(q, region) or {}
