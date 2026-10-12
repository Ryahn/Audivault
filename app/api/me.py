"""/api/me routes: progress, bookmarks, listening history and stats."""

from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..catalog import serialize
from ..core import auth, db
from .common import get_item_or_404
from .sessions import upsert_progress

router = APIRouter(prefix="/api/me")


@router.get("")
def me(request: Request):
    return serialize.user_json(auth.current_user(request))


@router.patch("/password")
def change_password(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    if not auth.verify_password(body.get("password") or "", user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    new = body.get("newPassword") or ""
    try:
        pw_hash = auth.hash_password(new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.ex("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user["id"]))
    auth.revoke_user_sessions(user["id"])
    return {"success": True}


# ------------------------------------------------------------------ progress

@router.get("/progress/{item_id}")
def get_progress(item_id: str, request: Request):
    user = auth.current_user(request)
    p = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ''", (user["id"], item_id))
    if not p:
        raise HTTPException(404, "No progress")
    return serialize.progress_json(p)


@router.patch("/progress/batch/update")
def batch_progress(request: Request, body: list = Body(...)):
    user = auth.current_user(request)
    for entry in body:
        item = db.q1("SELECT * FROM library_items WHERE id = ?", (entry.get("libraryItemId"),))
        if item and auth.can_access_library(user, item["library_id"]):
            upsert_progress(user["id"], item, entry)
    return PlainTextResponse("OK")


@router.patch("/progress/{item_id}")
@router.patch("/progress/{item_id}/{episode_id}")
def update_progress(item_id: str, request: Request, body: dict = Body(...), episode_id: str | None = None):
    user = auth.current_user(request)
    item = get_item_or_404(item_id, user)
    p = upsert_progress(user["id"], item, body)
    return serialize.progress_json(p)


@router.delete("/progress/{progress_id}")
def delete_progress(progress_id: str, request: Request):
    user = auth.current_user(request)
    db.ex("DELETE FROM media_progress WHERE id = ? AND user_id = ?", (progress_id, user["id"]))
    return PlainTextResponse("OK")


@router.get("/progress/{progress_id}/remove-from-continue-listening")
def hide_from_continue(progress_id: str, request: Request):
    user = auth.current_user(request)
    db.ex("UPDATE media_progress SET hide_from_continue = 1 WHERE id = ? AND user_id = ?", (progress_id, user["id"]))
    return serialize.user_json(user)


@router.get("/items-in-progress")
def items_in_progress(request: Request, limit: int = 25):
    from ..catalog import library
    user = auth.current_user(request)
    rows = db.q("SELECT li.*, mp.last_update AS progress_last_update FROM media_progress mp JOIN library_items li ON li.id = mp.library_item_id "
                "WHERE mp.user_id = ? AND mp.is_finished = 0 AND mp.current_pos > 0 ORDER BY mp.last_update DESC LIMIT ?", (user["id"], limit))
    rows = [r for r in rows if auth.can_access_library(user, r["library_id"])]
    authors_by_id, series_by_id = library.bulk_authors_series([r["id"] for r in rows])
    out = []
    for r in rows:
        j = serialize.item_json(r, expanded=False, authors=authors_by_id.get(r["id"], []), series=series_by_id.get(r["id"], []))
        j["progressLastUpdate"] = r["progress_last_update"]
        out.append(j)
    return {"libraryItems": out}


# ------------------------------------------------------------------ bookmarks

@router.post("/item/{item_id}/bookmark")
def create_bookmark(item_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    get_item_or_404(item_id, user)
    row = {"id": db.new_id(), "user_id": user["id"], "library_item_id": item_id, "time": float(body.get("time") or 0),
           "title": body.get("title") or "", "created_at": db.now_ms()}
    db.insert("bookmarks", row)
    return serialize.bookmark_json(row)


@router.patch("/item/{item_id}/bookmark")
def update_bookmark(item_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    time_ = float(body.get("time") or 0)
    row = db.q1("SELECT * FROM bookmarks WHERE user_id = ? AND library_item_id = ? AND ABS(time - ?) < 0.5", (user["id"], item_id, time_))
    if not row:
        raise HTTPException(404, "Bookmark not found")
    db.ex("UPDATE bookmarks SET title = ? WHERE id = ?", (body.get("title") or "", row["id"]))
    row["title"] = body.get("title") or ""
    return serialize.bookmark_json(row)


@router.delete("/item/{item_id}/bookmark/{time_value}")
def delete_bookmark(item_id: str, time_value: str, request: Request):
    user = auth.current_user(request)
    try:
        t = float(time_value)
    except ValueError:
        raise HTTPException(400, "Invalid time")
    db.ex("DELETE FROM bookmarks WHERE user_id = ? AND library_item_id = ? AND ABS(time - ?) < 0.5", (user["id"], item_id, t))
    return PlainTextResponse("OK")


# ------------------------------------------------------------------ listening sessions / stats

# A session counts in a window when it started inside that span. Listening time is not split mid-session.
_WINDOW_SPANS = (
    ("24h", 24 * 60 * 60),
    ("3d", 3 * 24 * 60 * 60),
    ("7d", 7 * 24 * 60 * 60),
    ("1m", 30 * 24 * 60 * 60),
    ("3m", 90 * 24 * 60 * 60),
    ("6m", 180 * 24 * 60 * 60),
    ("12m", 365 * 24 * 60 * 60),
    ("all", None),
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_RANK = 10


def _meta_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def _new_window() -> dict:
    return {
        "time": 0.0, "ids": set(), "genres": Counter(), "authors": Counter(), "narrators": Counter(),
        "books": {}, "dow": [0.0] * 7,
    }


def _add_listen(acc: dict, session: dict, seconds: float, meta: dict) -> None:
    acc["time"] += seconds
    item_id = session["library_item_id"]
    acc["ids"].add(item_id)
    for genre in meta.get("genres") or []:
        name = _meta_name(genre)
        if name:
            acc["genres"][name] += seconds
    for author in meta.get("authors") or []:
        if isinstance(author, dict):
            name = _meta_name(author)
            author_id = author.get("id")
        else:
            name = _meta_name(author)
            author_id = None
        if name:
            acc["authors"][(author_id, name)] += seconds
    for narrator in meta.get("narrators") or []:
        name = _meta_name(narrator)
        if name:
            acc["narrators"][name] += seconds
    book = acc["books"].get(item_id)
    if book is None:
        book = {
            "id": item_id,
            "title": session["display_title"] or meta.get("title") or "Untitled",
            "author": session["display_author"] or meta.get("authorName") or "",
            "coverPath": session["cover_path"],
            "time": 0.0,
        }
        acc["books"][item_id] = book
    book["time"] += seconds
    if session["cover_path"] and not book["coverPath"]:
        book["coverPath"] = session["cover_path"]
    started = int(session["started_at"] or 0)
    if started > 0:
        try:
            acc["dow"][datetime.fromtimestamp(started / 1000).weekday()] += seconds
        except (OSError, OverflowError, ValueError):
            pass


def _window_json(acc: dict, finished: int) -> dict:
    books = sorted(acc["books"].values(), key=lambda b: b["time"], reverse=True)[:_RANK]
    return {
        "time": acc["time"],
        "booksFinished": finished,
        "booksListened": len(acc["ids"]),
        "genres": [{"genre": name, "time": t} for name, t in acc["genres"].most_common(_RANK)],
        "authors": [{"id": author_id, "name": name, "time": t} for (author_id, name), t in acc["authors"].most_common(_RANK)],
        "narrators": [{"name": name, "time": t} for name, t in acc["narrators"].most_common(_RANK)],
        "books": books,
        "dayOfWeek": [{"day": day, "time": acc["dow"][i]} for i, day in enumerate(_WEEKDAYS)],
    }


def _paged_sessions(user_id: str, item_id: str | None, page: int, per_page: int) -> dict:
    where, params = "user_id = ?", [user_id]
    if item_id:
        where += " AND library_item_id = ?"
        params.append(item_id)
    where += " AND time_listening > 0"
    total = db.scalar(f"SELECT COUNT(*) FROM playback_sessions WHERE {where}", params)
    rows = db.q(f"SELECT * FROM playback_sessions WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*params, per_page, page * per_page))
    return {"total": total, "numPages": max(1, -(-total // per_page)) if total else 0, "page": page, "itemsPerPage": per_page,
            "sessions": [serialize.session_json(r) for r in rows]}


@router.get("/listening-sessions")
def listening_sessions(request: Request, page: int = 0, itemsPerPage: int = 10):
    user = auth.current_user(request)
    return _paged_sessions(user["id"], None, page, max(1, itemsPerPage))


@router.get("/item/listening-sessions/{item_id}")
@router.get("/item/listening-sessions/{item_id}/{episode_id}")
def item_listening_sessions(item_id: str, request: Request, page: int = 0, itemsPerPage: int = 10, episode_id: str | None = None):
    user = auth.current_user(request)
    return _paged_sessions(user["id"], item_id, page, max(1, itemsPerPage))


@router.get("/listening-stats")
def listening_stats(request: Request):
    user = auth.current_user(request)
    sessions = db.q("SELECT * FROM playback_sessions WHERE user_id = ? AND time_listening > 0 ORDER BY updated_at DESC", (user["id"],))
    days, dow, items = defaultdict(float), defaultdict(float), {}
    total = 0.0
    today = datetime.now().strftime("%Y-%m-%d")
    now_ms = db.now_ms()
    windows = {key: _new_window() for key, _ in _WINDOW_SPANS}
    for s in sessions:
        t = float(s["time_listening"] or 0)
        total += t
        if s["session_date"]:
            days[s["session_date"]] += t
        if s["day_of_week"]:
            dow[s["day_of_week"]] += t
        md = db.jload(s["media_metadata"], {}) or {}
        entry = items.setdefault(s["library_item_id"], {"id": s["library_item_id"], "timeListening": 0.0, "mediaMetadata": md})
        entry["timeListening"] += t
        started = int(s["started_at"] or 0)
        age = ((now_ms - started) / 1000.0) if started > 0 else None
        for key, span in _WINDOW_SPANS:
            if span is None or (age is not None and age <= span):
                _add_listen(windows[key], s, t, md)
    finished_at = [int(r["finished_at"]) for r in db.q(
        "SELECT finished_at FROM media_progress WHERE user_id = ? AND is_finished = 1 AND finished_at IS NOT NULL",
        (user["id"],),
    )]

    def finished_in(span):
        if span is None:
            return len(finished_at)
        cutoff = now_ms - span * 1000
        return sum(1 for ts in finished_at if ts >= cutoff)

    return {"totalTime": total, "items": items, "days": dict(days), "dayOfWeek": dict(dow), "today": days.get(today, 0.0),
            "recentSessions": [serialize.session_json(s) for s in sessions[:10]],
            "windows": {key: _window_json(windows[key], finished_in(span)) for key, span in _WINDOW_SPANS}}


@router.get("/stats/year/{year}")
def year_stats(year: int, request: Request):
    user = auth.current_user(request)
    start = int(datetime(year, 1, 1).timestamp() * 1000)
    end = int(datetime(year + 1, 1, 1).timestamp() * 1000)
    sessions = db.q("SELECT * FROM playback_sessions WHERE user_id = ? AND started_at >= ? AND started_at < ? AND time_listening > 0",
                    (user["id"], start, end))
    authors, genres, narrators, months = Counter(), Counter(), Counter(), Counter()
    item_ids = set()
    total = 0.0
    for s in sessions:
        t = float(s["time_listening"] or 0)
        total += t
        item_ids.add(s["library_item_id"])
        md = db.jload(s["media_metadata"], {}) or {}
        for a in md.get("authors") or []:
            authors[a.get("name") if isinstance(a, dict) else a] += t
        for g in md.get("genres") or []:
            genres[g] += t
        for n in md.get("narrators") or []:
            narrators[n] += t
        months[datetime.fromtimestamp(s["started_at"] / 1000).month - 1] += t
    finished = db.q("SELECT mp.*, li.title, li.duration AS item_duration, li.cover_path FROM media_progress mp "
                    "JOIN library_items li ON li.id = mp.library_item_id WHERE mp.user_id = ? AND mp.is_finished = 1 "
                    "AND mp.finished_at >= ? AND mp.finished_at < ?", (user["id"], start, end))
    longest = max(finished, key=lambda r: r["item_duration"] or 0, default=None)
    with_covers = [r["id"] for r in db.q(f"SELECT id FROM library_items WHERE cover_path IS NOT NULL AND id IN ({','.join('?' * len(item_ids))})",
                                          tuple(item_ids))] if item_ids else []
    return {
        "totalListeningSessions": len(sessions), "totalListeningTime": total, "totalBookListeningTime": total,
        "totalPodcastListeningTime": 0, "topAuthors": [{"name": n, "time": t} for n, t in authors.most_common(5)],
        "topGenres": [{"genre": g, "time": t} for g, t in genres.most_common(5)],
        "mostListenedNarrator": ({"name": narrators.most_common(1)[0][0], "time": narrators.most_common(1)[0][1]} if narrators else None),
        "mostListenedMonth": ({"month": months.most_common(1)[0][0], "time": months.most_common(1)[0][1]} if months else None),
        "numBooksFinished": len(finished), "numBooksListened": len(item_ids),
        "longestAudiobookFinished": ({"id": longest["library_item_id"], "title": longest["title"], "duration": longest["item_duration"] or 0,
                                      "finishedAt": datetime.fromtimestamp(longest["finished_at"] / 1000).isoformat()} if longest else None),
        "booksWithCovers": with_covers, "finishedBooksWithCovers": [r["library_item_id"] for r in finished if r["cover_path"]],
    }
