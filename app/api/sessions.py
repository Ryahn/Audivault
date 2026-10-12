"""Playback sessions, progress syncing and public track streaming."""

import time
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..catalog import library, serialize
from ..core import auth, db

router = APIRouter()

PLAY_DIRECT = 0
PLAY_LOCAL = 3


def _today() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%A")


def _finish_threshold(item: dict) -> tuple[float | None, float]:
    lib = library.get_library(item["library_id"])
    settings = library.library_settings(lib) if lib else {}
    return settings.get("markAsFinishedPercentComplete"), float(settings.get("markAsFinishedTimeRemaining") or 10)


def is_finished_at(item: dict, current: float, duration: float) -> bool:
    if duration <= 0:
        return False
    pct, remaining = _finish_threshold(item)
    if pct:
        return current / duration * 100 >= float(pct)
    return duration - current <= remaining


def upsert_progress(user_id: str, item: dict, data: dict) -> dict:
    """Create or update media progress from an Audiobookshelf-style payload."""
    now = db.now_ms()
    existing = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ''", (user_id, item["id"]))
    duration = float(data.get("duration") or (existing or {}).get("duration") or item["duration"] or 0)
    fields: dict = {"duration": duration, "last_update": int(data.get("lastUpdate") or now)}
    if "currentTime" in data and data["currentTime"] is not None:
        fields["current_pos"] = max(0.0, float(data["currentTime"]))
    if "progress" in data and data["progress"] is not None:
        fields["progress"] = min(1.0, max(0.0, float(data["progress"])))
    elif "current_pos" in fields and duration > 0:
        fields["progress"] = min(1.0, fields["current_pos"] / duration)
    if "ebookLocation" in data:
        fields["ebook_location"] = data["ebookLocation"]
    if "ebookProgress" in data and data["ebookProgress"] is not None:
        fields["ebook_progress"] = float(data["ebookProgress"])
    if "hideFromContinueListening" in data:
        fields["hide_from_continue"] = 1 if data["hideFromContinueListening"] else 0

    was_finished = bool(existing and existing["is_finished"])
    if "isFinished" in data and data["isFinished"] is not None:
        finished = bool(data["isFinished"])
    elif "current_pos" in fields and not was_finished:
        finished = is_finished_at(item, fields["current_pos"], duration)
    else:
        finished = was_finished
    fields["is_finished"] = 1 if finished else 0
    if finished and not was_finished:
        fields["finished_at"] = now
        fields["progress"] = 1.0
        fields["hide_from_continue"] = 0
    elif not finished and was_finished:
        fields["finished_at"] = None
        if "progress" not in fields and "current_pos" not in fields:
            fields["progress"] = 0.0
    if fields.get("current_pos", 0) > 0 and not finished and "hideFromContinueListening" not in data:
        fields["hide_from_continue"] = 0

    if existing:
        db.update("media_progress", existing["id"], fields)
        return db.q1("SELECT * FROM media_progress WHERE id = ?", (existing["id"],))
    row = {"id": db.new_id(), "user_id": user_id, "library_item_id": item["id"], "episode_id": "", "duration": duration,
           "progress": 0.0, "current_pos": 0.0, "is_finished": 0, "hide_from_continue": 0, "ebook_location": None,
           "ebook_progress": None, "started_at": int(data.get("startedAt") or now), "finished_at": None, "last_update": now}
    row.update(fields)
    db.insert("media_progress", row)
    return row


def start_session(user: dict, item: dict, body: dict) -> dict:
    authors = library.item_authors(item["id"])
    series = library.item_series(item["id"])
    item_payload = serialize.item_json(item, expanded=True, authors=authors, series=series)
    tracks = item_payload["media"]["tracks"]
    progress = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ''", (user["id"], item["id"]))
    start = float(progress["current_pos"]) if progress and not progress["is_finished"] else 0.0
    now = db.now_ms()
    date, dow = _today()
    device = body.get("deviceInfo") or {}
    device_info = {
        "id": device.get("deviceId") or "", "deviceId": device.get("deviceId"), "clientName": device.get("clientName"),
        "clientVersion": device.get("clientVersion"), "manufacturer": device.get("manufacturer"), "model": device.get("model"),
        "osName": device.get("osName"), "osVersion": device.get("osVersion"), "sdkVersion": device.get("sdkVersion"),
        "ipAddress": None, "browserName": device.get("browserName"), "deviceName": device.get("deviceName"),
    }
    row = {
        "id": db.new_id(), "user_id": user["id"], "library_id": item["library_id"], "library_item_id": item["id"], "episode_id": None,
        "media_type": "book", "display_title": item["title"], "display_author": ", ".join(a["name"] for a in authors),
        "cover_path": item["cover_path"], "duration": float(item["duration"] or 0), "play_method": PLAY_DIRECT,
        "media_player": body.get("mediaPlayer") or "unknown", "device_info": db.jdump(device_info), "session_date": date,
        "day_of_week": dow, "time_listening": 0.0, "start_time": start, "current_pos": start, "started_at": now, "updated_at": now,
        "media_metadata": db.jdump(item_payload["media"]["metadata"]), "chapters": item["chapters"], "is_open": 1,
    }
    db.insert("playback_sessions", row)
    return serialize.session_json(row, {"audioTracks": tracks, "videoTrack": None, "libraryItem": item_payload})


def sync_session(session: dict, data: dict) -> None:
    listened = float(data.get("timeListened") or 0)
    current = data.get("currentTime")
    fields = {"updated_at": db.now_ms()}
    if 0 < listened < 60 * 60 * 6:
        fields["time_listening"] = float(session["time_listening"] or 0) + listened
    if current is not None:
        fields["current_pos"] = float(current)
    if data.get("duration"):
        fields["duration"] = float(data["duration"])
    db.update("playback_sessions", session["id"], fields)
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (session["library_item_id"],))
    if item and current is not None:
        upsert_progress(session["user_id"], item, {"currentTime": float(current), "duration": fields.get("duration") or session["duration"]})


def _own_session(session_id: str, user: dict) -> dict:
    s = db.q1("SELECT * FROM playback_sessions WHERE id = ?", (session_id,))
    if not s or (s["user_id"] != user["id"] and not auth.is_admin(user)):
        raise HTTPException(404, "Session not found")
    return s


@router.post("/api/session/{session_id}/sync")
async def session_sync(session_id: str, request: Request):
    user = auth.current_user(request)
    session = _own_session(session_id, user)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    sync_session(session, data or {})
    return PlainTextResponse("OK")


@router.post("/api/session/{session_id}/close")
async def session_close(session_id: str, request: Request):
    user = auth.current_user(request)
    session = _own_session(session_id, user)
    try:
        data = await request.json()
    except ValueError:
        data = None
    if data:
        sync_session(session, data)
    db.ex("UPDATE playback_sessions SET is_open = 0, updated_at = ? WHERE id = ?", (db.now_ms(), session_id))
    return PlainTextResponse("OK")


def _sync_local(user: dict, s: dict) -> dict:
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (s.get("libraryItemId"),))
    if not item:
        return {"id": s.get("id"), "success": False, "error": "Item not found"}
    now = db.now_ms()
    date, dow = _today()
    existing = db.q1("SELECT * FROM playback_sessions WHERE id = ?", (s["id"],))
    fields = {
        "time_listening": float(s.get("timeListening") or 0), "current_pos": float(s.get("currentTime") or 0),
        "updated_at": int(s.get("updatedAt") or now), "duration": float(s.get("duration") or item["duration"] or 0),
    }
    if existing:
        if existing["user_id"] != user["id"]:
            return {"id": s["id"], "success": False, "error": "Forbidden"}
        db.update("playback_sessions", s["id"], fields)
    else:
        started = int(s.get("startedAt") or now)
        dt = datetime.fromtimestamp(started / 1000)
        authors = library.item_authors(item["id"])
        row = {
            "id": s["id"], "user_id": user["id"], "library_id": item["library_id"], "library_item_id": item["id"], "episode_id": None,
            "media_type": "book", "display_title": s.get("displayTitle") or item["title"],
            "display_author": s.get("displayAuthor") or ", ".join(a["name"] for a in authors), "cover_path": item["cover_path"],
            "play_method": int(s.get("playMethod") or PLAY_LOCAL), "media_player": s.get("mediaPlayer"),
            "device_info": db.jdump(s.get("deviceInfo") or {}), "session_date": dt.strftime("%Y-%m-%d"), "day_of_week": dt.strftime("%A"),
            "start_time": float(s.get("startTime") or 0), "started_at": started, "is_open": 0,
            "media_metadata": db.jdump(serialize.book_metadata(item, authors, library.item_series(item["id"]))), "chapters": item["chapters"],
        }
        row.update(fields)
        db.insert("playback_sessions", row)
    progress = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ''", (user["id"], item["id"]))
    synced = False
    if not progress or progress["last_update"] <= fields["updated_at"]:
        upsert_progress(user["id"], item, {"currentTime": fields["current_pos"], "duration": fields["duration"], "lastUpdate": fields["updated_at"]})
        synced = True
    return {"id": s["id"], "success": True, "progressSynced": synced}


@router.post("/api/session/local")
def session_local(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    result = _sync_local(user, body)
    if not result["success"]:
        raise HTTPException(400, result.get("error"))
    return PlainTextResponse("OK")


@router.post("/api/session/local-all")
def session_local_all(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    results = [_sync_local(user, s) for s in body.get("sessions") or [] if s.get("id")]
    return {"results": results}


@router.get("/api/sessions")
def all_sessions(request: Request, page: int = 0, itemsPerPage: int = 10, user: str | None = None):
    auth.require_admin(request)
    where, params = "", []
    if user:
        where, params = "WHERE user_id = ?", [user]
    total = db.scalar(f"SELECT COUNT(*) FROM playback_sessions {where}", params)
    rows = db.q(f"SELECT * FROM playback_sessions {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*params, itemsPerPage, page * itemsPerPage))
    return {"total": total, "numPages": max(1, -(-total // itemsPerPage)), "page": page, "itemsPerPage": itemsPerPage,
            "sessions": [serialize.session_json(r) for r in rows]}


@router.get("/api/sessions/open")
def open_sessions(request: Request):
    auth.require_admin(request)
    cutoff = db.now_ms() - 30 * 60 * 1000
    rows = db.q("SELECT * FROM playback_sessions WHERE is_open = 1 AND updated_at > ? ORDER BY updated_at DESC", (cutoff,))
    return {"sessions": [serialize.session_json(r) for r in rows]}


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    user = auth.current_user(request)
    _own_session(session_id, user)
    db.ex("DELETE FROM playback_sessions WHERE id = ?", (session_id,))
    return PlainTextResponse("OK")


@router.get("/public/session/{session_id}/track/{index}")
def public_track(session_id: str, index: int):
    """Unauthenticated audio stream for an active playback session (the
    random session id acts as the capability). Used by AudioBooth and the web player.
    Capability window is 24 hours from last session update."""
    from ..catalog.paths import is_allowed_media_path
    session = db.q1("SELECT * FROM playback_sessions WHERE id = ?", (session_id,))
    if not session or session["updated_at"] < (time.time() - 60 * 60 * 24) * 1000:
        raise HTTPException(404, "Session not found")
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (session["library_item_id"],))
    if not item:
        raise HTTPException(404, "Item not found")
    audio = [a for a in db.jload(item["audio_files"], []) if not a.get("exclude")]
    track = next((a for a in audio if a.get("index") == index), None)
    if not track or not is_allowed_media_path(track["path"]):
        raise HTTPException(404, "Track not found")
    return FileResponse(track["path"], media_type=track.get("mime_type") or "audio/mpeg")
