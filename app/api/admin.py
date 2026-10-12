"""Users, API keys, server settings, plus Audivault's import/organize tools."""

import asyncio
import json
import os
import string
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..catalog import library, locks, serialize
from ..core import auth, db, events, tasks
from ..management import duplicates, organizer, trash
from ..management.watcher import watcher
from .common import truthy

router = APIRouter()

SSE_END = chr(10) * 2  # each Server-Sent Event ends with a blank line


def sse(data: dict) -> str:
    return "data: " + json.dumps(data) + SSE_END


# ------------------------------------------------------------------ users

@router.get("/api/users")
def list_users(request: Request):
    auth.require_admin(request)
    return {"users": [serialize.user_json(u, minimal=True) for u in db.q("SELECT * FROM users ORDER BY created_at")]}


@router.post("/api/users")
def create_user(request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "Username required")
    if db.q1("SELECT id FROM users WHERE username = ?", (username,)):
        raise HTTPException(400, "Username already taken")
    user_type = body.get("type") if body.get("type") in ("admin", "user", "guest") else "user"
    if user_type == "admin" and admin["type"] != "root":
        raise HTTPException(403, "Only root can create admins")
    perms = auth.DEFAULT_PERMISSIONS | (body.get("permissions") or {})
    try:
        user = auth.create_user(username, body.get("password") or "", user_type, perms, body.get("librariesAccessible") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"user": serialize.user_json(user, minimal=True)}


@router.get("/api/users/{user_id}")
def get_user(user_id: str, request: Request):
    auth.require_admin(request)
    u = db.q1("SELECT * FROM users WHERE id = ?", (user_id,))
    if not u:
        raise HTTPException(404, "User not found")
    return serialize.user_json(u)


@router.patch("/api/users/{user_id}")
def update_user(user_id: str, request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    u = db.q1("SELECT * FROM users WHERE id = ?", (user_id,))
    if not u:
        raise HTTPException(404, "User not found")
    if u["type"] == "root" and admin["type"] != "root":
        raise HTTPException(403, "Cannot edit root user")
    fields = {}
    if body.get("username") and body["username"] != u["username"]:
        if db.q1("SELECT id FROM users WHERE username = ? AND id != ?", (body["username"], user_id)):
            raise HTTPException(400, "Username already taken")
        fields["username"] = body["username"].strip()
    if body.get("password"):
        try:
            fields["password_hash"] = auth.hash_password(body["password"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if u["type"] != "root" and body.get("type") in ("admin", "user", "guest"):
        new_type = body["type"]
        if new_type == "admin" and admin["type"] != "root":
            raise HTTPException(403, "Only root can promote admins")
        if u["type"] == "admin" and new_type != "admin" and admin["type"] != "root":
            raise HTTPException(403, "Only root can demote admins")
        fields["type"] = new_type
    if "isActive" in body and u["type"] != "root":
        fields["is_active"] = 1 if body["isActive"] else 0
    if isinstance(body.get("permissions"), dict):
        fields["permissions"] = db.jdump(auth.DEFAULT_PERMISSIONS | body["permissions"])
    if isinstance(body.get("librariesAccessible"), list):
        fields["libraries_accessible"] = db.jdump(body["librariesAccessible"])
    db.update("users", user_id, fields)
    if "password_hash" in fields or fields.get("is_active") == 0:
        auth.revoke_user_sessions(user_id)
    return {"success": True, "user": serialize.user_json(db.q1("SELECT * FROM users WHERE id = ?", (user_id,)), minimal=True)}


@router.delete("/api/users/{user_id}")
def delete_user(user_id: str, request: Request):
    admin = auth.require_admin(request)
    u = db.q1("SELECT * FROM users WHERE id = ?", (user_id,))
    if not u:
        raise HTTPException(404, "User not found")
    if u["type"] == "root" or u["id"] == admin["id"]:
        raise HTTPException(400, "Cannot delete this user")
    for table in ("media_progress", "bookmarks", "playback_sessions", "refresh_tokens", "api_keys"):
        db.ex(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    for p in db.q("SELECT id FROM playlists WHERE user_id = ?", (user_id,)):
        db.ex("DELETE FROM playlist_items WHERE playlist_id = ?", (p["id"],))
    db.ex("DELETE FROM playlists WHERE user_id = ?", (user_id,))
    db.ex("DELETE FROM users WHERE id = ?", (user_id,))
    return {"success": True}


# ------------------------------------------------------------------ api keys

def _key_json(k: dict) -> dict:
    user = db.q1("SELECT id, username, type FROM users WHERE id = ?", (k["user_id"],))
    return {"id": k["id"], "name": k["name"], "userId": k["user_id"], "user": user, "createdAt": k["created_at"],
            "expiresAt": k["expires_at"], "lastUsedAt": k["last_used"], "isActive": True}


@router.get("/api/api-keys")
def list_keys(request: Request):
    auth.require_admin(request)
    return {"apiKeys": [_key_json(k) for k in db.q("SELECT * FROM api_keys ORDER BY created_at DESC")]}


@router.post("/api/api-keys")
def create_key(request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    owner = db.q1("SELECT * FROM users WHERE id = ?", (body.get("userId") or admin["id"],))
    if not owner:
        raise HTTPException(404, "User not found")
    expires = int(body["expiresIn"]) if body.get("expiresIn") else None
    token, row = auth.make_api_key(owner, name, expires)
    return {"apiKey": _key_json(row) | {"apiKey": token}}


@router.delete("/api/api-keys/{key_id}")
def delete_key(key_id: str, request: Request):
    auth.require_admin(request)
    db.ex("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return {"success": True}


# ------------------------------------------------------------------ server settings

@router.get("/api/settings")
def get_settings(request: Request):
    auth.require_admin(request)
    return {"serverSettings": serialize.server_settings_json()}


@router.patch("/api/settings")
def patch_settings(request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    current = db.get_setting("server_settings", {}) or {}
    if "sortingIgnorePrefix" in body:
        current["sortingIgnorePrefix"] = bool(body["sortingIgnorePrefix"])
    db.set_setting("server_settings", current)
    return {"success": True, "serverSettings": serialize.server_settings_json()}


# ------------------------------------------------------------------ Audivault tools

@router.get("/api/abstream/tasks")
def list_tasks(request: Request):
    auth.current_user(request)
    return {"tasks": tasks.all_tasks()}


@router.get("/api/abstream/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    auth.current_user(request)
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return t


@router.post("/api/abstream/events/ticket")
def event_ticket(request: Request):
    user = auth.current_user(request)
    return {"ticket": events.issue_ticket(user["id"])}


@router.get("/api/abstream/events")
async def event_stream(request: Request, ticket: str = ""):
    """Server-Sent Events stream. Authenticated with a one-time ticket from
    POST /api/abstream/events/ticket (EventSource can't send headers)."""
    user_id = events.redeem_ticket(ticket)
    user = db.q1("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)) if user_id else None
    if not user:
        raise HTTPException(401, "Invalid or expired ticket")
    sub = events.subscribe(user, asyncio.get_running_loop())

    async def generate():
        try:
            yield sse({"type": "hello"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(sub["queue"].get(), timeout=15)
                    yield f"data: {payload}" + SSE_END
                except asyncio.TimeoutError:
                    yield ": keep-alive" + SSE_END
        finally:
            events.unsubscribe(sub)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/abstream/duplicates")
def list_duplicates(request: Request, analyze: str = "1"):
    auth.require_admin(request)
    do_analyze = truthy(analyze)
    groups = duplicates.find_groups(analyze=do_analyze)
    return {"groups": groups, "count": len(groups)}


@router.get("/api/abstream/duplicates/count")
def duplicates_count(request: Request):
    """Cheap count for the navbar — no Audible/runtime analysis."""
    auth.require_admin(request)
    groups = duplicates.find_groups(analyze=False)
    return {"count": len(groups)}


@router.post("/api/abstream/duplicates/resolve-batch")
def resolve_duplicates_batch(request: Request, body: dict = Body(...)):
    """Resolve many groups at once: [{keepId, discardIds}], using default file selections."""
    admin = auth.require_admin(request)
    plans = [p for p in body.get("plans") or [] if p.get("keepId") and p.get("discardIds")]
    if not plans:
        raise HTTPException(400, "Nothing to resolve")
    seen = set()
    for p in plans:
        ids = {p["keepId"], *p["discardIds"]}
        if ids & seen or p["keepId"] in p["discardIds"]:
            raise HTTPException(400, "A book appears in more than one plan")
        seen |= ids
    mode = "delete" if body.get("mode") == "delete" else "trash"
    task = tasks.start("duplicates", f"Resolve {len(plans)} duplicate groups", duplicates.resolve_batch, plans, mode,
                       key="duplicates:batch", user_id=admin["id"])
    return {"task": task}


@router.post("/api/abstream/duplicates/ignore")
def ignore_duplicates(request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    ids = [i for i in body.get("itemIds") or [] if isinstance(i, str)]
    if len(ids) < 2:
        raise HTTPException(400, "Need at least two books")
    duplicates.ignore(ids)
    return {"success": True}


@router.delete("/api/abstream/duplicates/ignored")
def reset_ignored_duplicates(request: Request):
    auth.require_admin(request)
    duplicates.reset_ignored()
    return {"success": True}


@router.get("/api/abstream/duplicates/compare")
def compare_duplicates(request: Request, keep: str, discard: str):
    auth.require_admin(request)
    try:
        return duplicates.compare(keep, discard)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/api/abstream/duplicates/resolve")
def resolve_duplicates(request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    keep_id = body.get("keepId")
    discards = [d for d in body.get("discards") or [] if isinstance(d, dict) and d.get("id") and d["id"] != keep_id]
    if not keep_id or not discards:
        raise HTTPException(400, "keepId and at least one discarded copy are required")
    mode = "delete" if body.get("mode") == "delete" else "trash"
    keep = db.q1("SELECT * FROM library_items WHERE id = ?", (keep_id,))
    if not keep:
        raise HTTPException(404, "Book not found")
    # Refuse right away (423) instead of failing in the background
    for d in discards:
        discard = db.q1("SELECT * FROM library_items WHERE id = ?", (d["id"],))
        if discard:
            locks.check_item_delete(discard, "copy")
    if locks.item_lock(keep) and any(d.get("replace") for d in discards):
        raise locks.LockedError(f"{locks.item_lock(keep)['reason']}: files in the kept copy can't be replaced")
    task = tasks.start("duplicates", "Resolve duplicates", duplicates.resolve, keep_id, discards, mode,
                       key=f"duplicates:{keep_id}", library_id=keep["library_id"], user_id=admin["id"])
    return {"task": task}


@router.get("/api/abstream/trash")
def list_trash(request: Request):
    auth.require_admin(request)
    return {"retentionDays": trash.retention_days(), "lockedByEnv": trash.env_days() is not None,
            "entries": trash.list_entries()}


@router.patch("/api/abstream/trash/settings")
def trash_settings(request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    try:
        days = int(body.get("retentionDays"))
    except (TypeError, ValueError):
        raise HTTPException(400, "retentionDays must be a whole number of days (0 = never)")
    try:
        trash.set_retention_days(days)
    except ValueError as e:
        raise HTTPException(409, str(e))
    # Apply the new limit right away
    tasks.start("trash", "Empty old trash", trash.purge, key="trash", quiet=True)
    return {"retentionDays": trash.retention_days()}


@router.post("/api/abstream/trash/empty")
def empty_trash(request: Request):
    admin = auth.require_admin(request)
    task = tasks.start("trash", "Empty trash", trash.purge, True, key="trash", user_id=admin["id"])
    return {"task": task}


@router.post("/api/abstream/trash/restore")
def restore_from_trash(request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    try:
        return trash.restore(body.get("folderId") or "", body.get("batch") or "", body.get("relPath") or "")
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/api/abstream/trash/{folder_id}/{batch}")
def delete_from_trash(folder_id: str, batch: str, request: Request, rel: str | None = None):
    auth.require_admin(request)
    try:
        trash.delete(folder_id, batch, rel)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"success": True}


@router.get("/api/abstream/locks")
def list_locks(request: Request):
    auth.require_admin(request)
    return {"locked": locks.locked_roots()}


@router.get("/api/abstream/watcher")
def watcher_status(request: Request):
    auth.require_admin(request)
    return {"watching": watcher.status(), "pending": watcher.pending_count()}


@router.get("/api/abstream/activity")
def activity(request: Request):
    auth.require_admin(request)
    return {"activity": tasks.recent_activity()}


@router.get("/api/abstream/fs")
def browse(request: Request, path: str = ""):
    """Directory picker for library/import folders."""
    auth.require_admin(request)
    if not path:
        if os.name == "nt":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives]}
        path = "/"
    p = Path(path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, "Not a directory")
    try:
        dirs = sorted((c for c in p.iterdir() if c.is_dir() and not c.name.startswith((".", "$"))), key=lambda c: c.name.lower())
    except PermissionError:
        dirs = []
    parent = str(p.parent) if p.parent != p else ("" if os.name == "nt" else None)
    return {"path": str(p), "parent": parent, "dirs": [{"name": d.name, "path": str(d)} for d in dirs[:500]]}


@router.post("/api/abstream/template-preview")
def template_preview(request: Request, body: dict = Body(...)):
    auth.current_user(request)
    template = body.get("template") or library.DEFAULT_LIBRARY_SETTINGS["namingTemplate"]
    samples = [
        organizer.template_values("The Final Empire", ["Brandon Sanderson"], [("Mistborn", "1")], None, "2006", ["Michael Kramer"], "B002UZMLXM"),
        organizer.template_values("Project Hail Mary", ["Andy Weir"], [], None, "2021", ["Ray Porter"], "B08G9PRS1K"),
    ]
    return {"examples": [organizer.render(template, v) for v in samples]}


@router.get("/api/abstream/libraries/{library_id}/import")
def import_candidates(library_id: str, request: Request, fresh: str = "0"):
    auth.require_admin(request)
    if not library.get_library(library_id):
        raise HTTPException(404, "Library not found")
    return organizer.list_import_candidates(library_id, fresh=truthy(fresh))


@router.post("/api/abstream/libraries/{library_id}/import")
def import_item(library_id: str, request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    if not library.get_library(library_id):
        raise HTTPException(404, "Library not found")
    try:
        mode = body.get("mode") if body.get("mode") in ("move", "copy") else None  # None: the library's default
        return organizer.import_candidate(library_id, body.get("relPath") or "", body.get("metadata") or {}, body.get("folderId"),
                                          mode, body.get("coverUrl"))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/abstream/libraries/{library_id}/import/batch")
def import_batch(library_id: str, request: Request, body: dict = Body(...)):
    """Import several books from the Import page in one background task."""
    admin = auth.require_admin(request)
    if not library.get_library(library_id):
        raise HTTPException(404, "Library not found")
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "No books to import")
    mode = body.get("mode") if body.get("mode") in ("move", "copy") else None
    task = tasks.start("import-batch", f"Import {len(items)} book(s)", organizer.import_batch, library_id, items, mode,
                       key=f"import-batch:{library_id}", library_id=library_id, user_id=admin["id"])
    return {"task": task}


@router.get("/api/abstream/libraries/{library_id}/organize")
def organize_preview(library_id: str, request: Request, template: str | None = None, all: str = "0"):
    auth.require_admin(request)
    if not library.get_library(library_id):
        raise HTTPException(404, "Library not found")
    plans = organizer.plan_library(library_id, template or None)
    if not truthy(all):
        plans = [p for p in plans if p["status"] != "unchanged"]
    return {"plans": plans}


@router.post("/api/abstream/libraries/{library_id}/auto-import")
def run_auto_import(library_id: str, request: Request):
    """Run auto-import now (e.g. after fixing books that needed review)."""
    auth.require_admin(request)
    from ..management import autoimport
    lib = library.get_library(library_id)
    if not lib:
        raise HTTPException(404, "Library not found")
    if not library.library_settings(lib).get("autoImport"):
        raise HTTPException(400, "Auto-import is off for this library")
    return {"task": autoimport.start(library_id)}


@router.post("/api/abstream/libraries/{library_id}/organize")
def organize_apply(library_id: str, request: Request, body: dict = Body(default={})):
    auth.require_admin(request)
    if not library.get_library(library_id):
        raise HTTPException(404, "Library not found")
    admin = auth.current_user(request)
    task = tasks.start("organize", "Organize library", organizer.organize_library, library_id, body.get("itemIds") or None,
                       body.get("template") or None, key=f"organize:{library_id}", library_id=library_id, user_id=admin["id"])
    return {"task": task}


@router.get("/api/abstream/items/{item_id}/organize")
def organize_item_preview(item_id: str, request: Request):
    auth.require_admin(request)
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404, "Item not found")
    return organizer.plan_item(item)


@router.post("/api/abstream/items/{item_id}/organize")
def organize_item(item_id: str, request: Request):
    auth.require_admin(request)
    try:
        return organizer.apply_item(item_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/abstream/items/{item_id}/audio-files")
def set_excluded_files(item_id: str, request: Request, body: dict = Body(...)):
    """Exclude/include audio files from playback: {"exclude": [ino, ...]}"""
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    from .common import get_item_or_404
    item = get_item_or_404(item_id, user)
    from ..scanner.scanner import compute_chapters
    excluded = set(body.get("exclude") or [])
    audio = db.jload(item["audio_files"], [])
    for a in audio:
        a["exclude"] = a["ino"] in excluded
    included = [a for a in audio if not a["exclude"]]
    if not included:
        raise HTTPException(400, "At least one audio file must stay included")
    settings = library.library_settings(library.get_library(item["library_id"]))
    chapters = compute_chapters(settings, db.jload(item["library_files"], []), included, None)
    counts = db.compute_item_counts(audio, chapters, db.jload(item["library_files"], []),
                                    db.jload(item["ebook_file"], None))
    db.update("library_items", item_id, {"audio_files": db.jdump(audio), "duration": sum(a["duration"] for a in included),
                                        "chapters": db.jdump(chapters), "updated_at": db.now_ms(), **counts})
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(item["library_id"])
    return PlainTextResponse("OK")
