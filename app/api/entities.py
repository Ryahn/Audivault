"""Authors, series, collections and playlists."""

import os
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..management import series_match
from ..catalog import library, media, serialize
from ..core import auth, db, tasks
from ..core.config import METADATA_DIR
from ..providers.http import get_bytes
from ..scanner.parse import last_first

router = APIRouter()


# ------------------------------------------------------------------ authors

def _author_or_404(author_id: str, user: dict) -> dict:
    a = db.q1("SELECT * FROM authors WHERE id = ?", (author_id,))
    if not a or not auth.can_access_library(user, a["library_id"]):
        raise HTTPException(404, "Author not found")
    return a


@router.get("/api/authors/{author_id}")
def get_author(author_id: str, request: Request, include: str = ""):
    user = auth.current_user(request)
    a = _author_or_404(author_id, user)
    items = db.q("SELECT li.* FROM book_authors ba JOIN library_items li ON li.id = ba.item_id WHERE ba.author_id = ? AND li.is_missing = 0 "
                 "ORDER BY li.title_sort", (author_id,))
    out = serialize.author_json(a, len(items))
    authors_by_id, series_by_id = library.bulk_authors_series([i["id"] for i in items])
    if "items" in include:
        out["libraryItems"] = [serialize.item_json(i, expanded=False, authors=authors_by_id.get(i["id"], []),
                                                   series=series_by_id.get(i["id"], [])) for i in items]
    if "series" in include:
        series: dict[str, dict] = {}
        for i in items:
            for s in series_by_id.get(i["id"], []):
                entry = series.setdefault(s["id"], {"id": s["id"], "name": s["name"], "items": []})
                row = dict(i)
                row["series_sequence"] = s["sequence"]
                entry["items"].append(row)
        out["series"] = []
        for s in sorted(series.values(), key=lambda s: s["name"].lower()):
            books = []
            for b in serialize.sort_by_sequence(s["items"]):
                j = serialize.item_json(b, expanded=False, authors=authors_by_id.get(b["id"], []),
                                        series=series_by_id.get(b["id"], []))
                j["media"]["metadata"]["series"] = {"id": s["id"], "name": s["name"], "sequence": b["series_sequence"]}
                books.append(j)
            out["series"].append({"id": s["id"], "name": s["name"], "items": books})
    if "items" in include and "series" not in include:
        out["series"] = []
    if "series" in include and "items" not in include:
        out["libraryItems"] = []
    return out


@router.patch("/api/authors/{author_id}")
def update_author(author_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    a = _author_or_404(author_id, user)
    fields = {}
    merged = False
    new_name = (body.get("name") or "").strip()
    if new_name and new_name != a["name"]:
        other = db.q1("SELECT * FROM authors WHERE library_id = ? AND name = ? AND id != ?", (a["library_id"], new_name, author_id))
        if other:
            # Merge into the existing author
            for row in db.q("SELECT item_id, position FROM book_authors WHERE author_id = ?", (author_id,)):
                db.ex("INSERT OR IGNORE INTO book_authors (item_id, author_id, position) VALUES (?, ?, ?)", (row["item_id"], other["id"], row["position"]))
            db.ex("DELETE FROM book_authors WHERE author_id = ?", (author_id,))
            db.ex("DELETE FROM authors WHERE id = ?", (author_id,))
            author_id, merged = other["id"], True
        else:
            fields.update({"name": new_name, "last_first": last_first(new_name)})
    for key, col in (("description", "description"), ("asin", "asin")):
        if key in body:
            fields[col] = body[key] or None
    if "imagePath" in body and not body["imagePath"]:
        fields["image_path"] = None
    if fields:
        fields["updated_at"] = db.now_ms()
        db.update("authors", author_id, fields)
    if fields.get("name") or merged:
        for row in db.q("SELECT item_id FROM book_authors WHERE author_id = ?", (author_id,)):
            media.write_metadata_file(row["item_id"])
    return {"author": serialize.author_json(db.q1("SELECT * FROM authors WHERE id = ?", (author_id,))), "updated": bool(fields) or merged,
            "merged": merged}


@router.delete("/api/authors/{author_id}")
def delete_author(author_id: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "delete")
    _author_or_404(author_id, user)
    db.ex("DELETE FROM book_authors WHERE author_id = ?", (author_id,))
    db.ex("DELETE FROM authors WHERE id = ?", (author_id,))
    return PlainTextResponse("OK")


@router.post("/api/authors/{author_id}/match")
def match_author(author_id: str, request: Request, body: dict = Body(default={})):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _author_or_404(author_id, user)
    updated = media.match_author(author_id, body.get("region") or "us", asin=body.get("asin"), name=body.get("q"))
    a = db.q1("SELECT * FROM authors WHERE id = ?", (author_id,))
    return {"updated": bool(updated), "author": serialize.author_json(a)}


@router.get("/api/authors/{author_id}/image")
def author_image(author_id: str):
    from ..catalog.paths import is_allowed_media_path
    a = db.q1("SELECT image_path FROM authors WHERE id = ?", (author_id,))
    if not a or not a["image_path"] or not is_allowed_media_path(a["image_path"]):
        raise HTTPException(404, "No image")
    return FileResponse(a["image_path"], headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/authors/{author_id}/image")
def set_author_image(author_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _author_or_404(author_id, user)
    url = body.get("url") or ""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid url")
    res = get_bytes(url)
    if not res:
        raise HTTPException(400, "Download failed")
    data, ctype = res
    ext = "png" if "png" in ctype else "webp" if "webp" in ctype else "jpg"
    target = METADATA_DIR / "authors" / f"{author_id}.{ext}"
    target.write_bytes(data)
    db.ex("UPDATE authors SET image_path = ?, updated_at = ? WHERE id = ?", (str(target), db.now_ms(), author_id))
    return {"author": serialize.author_json(db.q1("SELECT * FROM authors WHERE id = ?", (author_id,)))}


@router.delete("/api/authors/{author_id}/image")
def delete_author_image(author_id: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    a = _author_or_404(author_id, user)
    if a["image_path"] and Path(a["image_path"]).is_relative_to(METADATA_DIR):
        Path(a["image_path"]).unlink(missing_ok=True)
    db.ex("UPDATE authors SET image_path = NULL, updated_at = ? WHERE id = ?", (db.now_ms(), author_id))
    return {"author": serialize.author_json(db.q1("SELECT * FROM authors WHERE id = ?", (author_id,)))}


@router.get("/api/narrators/{narrator_id}/image")
def narrator_image(narrator_id: str):
    raise HTTPException(404, "No image")


# ------------------------------------------------------------------ series

@router.get("/api/series/{series_id}")
def get_series(series_id: str, request: Request, include: str = ""):
    user = auth.current_user(request)
    s = db.q1("SELECT * FROM series WHERE id = ?", (series_id,))
    if not s or not auth.can_access_library(user, s["library_id"]):
        raise HTTPException(404, "Series not found")
    books = serialize.series_items(s["id"])
    authors_by_id, series_by_id = library.bulk_authors_series([b["id"] for b in books])
    out = serialize.series_json(s, books, authors_by_id=authors_by_id, series_by_id=series_by_id)
    if "progress" in include:
        ids = [b["id"] for b in books]
        prog = library.bulk_progress(user["id"], ids)
        out["progress"] = {
            "libraryItemIds": ids,
            "libraryItemIdsFinished": [i for i in ids if prog.get(i, {}).get("is_finished")],
            "isFinished": bool(ids) and all(prog.get(i, {}).get("is_finished") for i in ids),
        }
    if "rssfeed" in include:
        out["rssFeed"] = None
    return out


@router.patch("/api/series/{series_id}")
def update_series(series_id: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    s = db.q1("SELECT * FROM series WHERE id = ?", (series_id,))
    if not s:
        raise HTTPException(404, "Series not found")
    fields = {}
    name = (body.get("name") or "").strip()
    if name and name != s["name"]:
        if db.q1("SELECT id FROM series WHERE library_id = ? AND name = ? AND id != ?", (s["library_id"], name, series_id)):
            raise HTTPException(400, "A series with that name already exists")
        fields["name"] = name
    if "description" in body:
        fields["description"] = body["description"] or None
    if fields:
        fields["updated_at"] = db.now_ms()
        db.update("series", series_id, fields)
        if "name" in fields:
            for row in db.q("SELECT item_id FROM book_series WHERE series_id = ?", (series_id,)):
                media.write_metadata_file(row["item_id"])
    return serialize.series_json(db.q1("SELECT * FROM series WHERE id = ?", (series_id,)))


def _series_or_404(series_id: str) -> dict:
    series = db.q1("SELECT * FROM series WHERE id = ?", (series_id,))
    if not series:
        raise HTTPException(404, "Series not found")
    return series


@router.get("/api/series/{series_id}/match-queue")
def series_match_queue(series_id: str, request: Request):
    auth.require_admin(request)
    series = _series_or_404(series_id)
    return series_match.load_queue(series_id)


@router.post("/api/series/{series_id}/match")
def series_match_apply(series_id: str, request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    series = _series_or_404(series_id)
    picks = body.get("picks") or []
    if not isinstance(picks, list) or not picks:
        raise HTTPException(400, "No books selected")
    task = tasks.start("series-match", f"Match {series['name']}", series_match.run, series_id, picks,
                       key=f"series-match:{series_id}", library_id=series["library_id"], user_id=admin["id"])
    return {"task": task}


# ------------------------------------------------------------------ collections

def _replace_members(parent_id: str, ids: list, *, table: str, parent_col: str, parent_table: str,
                     episode: bool, drop_empty: bool) -> None:
    """Replace an ordered list of books. Unknown ids are skipped; their slots still count."""
    db.ex(f"DELETE FROM {table} WHERE {parent_col} = ?", (parent_id,))
    source = (i for i in ids if i) if drop_empty else ids
    unique = list(dict.fromkeys(source))
    if unique:
        placeholders = ",".join("?" for _ in unique)
        existing = {r["id"] for r in db.q(f"SELECT id FROM library_items WHERE id IN ({placeholders})", tuple(unique))}
        for pos, item_id in enumerate(unique):
            if item_id not in existing:
                continue
            if episode:
                db.ex(
                    f"INSERT INTO {table} ({parent_col}, library_item_id, episode_id, position) VALUES (?, ?, '', ?)",
                    (parent_id, item_id, pos),
                )
            else:
                db.ex(
                    f"INSERT INTO {table} ({parent_col}, library_item_id, position) VALUES (?, ?, ?)",
                    (parent_id, item_id, pos),
                )
    db.ex(f"UPDATE {parent_table} SET updated_at = ? WHERE id = ?", (db.now_ms(), parent_id))


def _member_ids(table: str, parent_col: str, parent_id: str) -> list[str]:
    return [r["library_item_id"] for r in db.q(
        f"SELECT library_item_id FROM {table} WHERE {parent_col} = ? ORDER BY position", (parent_id,))]


def _collection_or_404(cid: str, user: dict) -> dict:
    c = db.q1("SELECT * FROM collections WHERE id = ?", (cid,))
    if not c or not auth.can_access_library(user, c["library_id"]):
        raise HTTPException(404, "Collection not found")
    return c


def _set_collection_books(cid: str, ids: list[str]) -> None:
    _replace_members(cid, ids, table="collection_books", parent_col="collection_id", parent_table="collections",
                     episode=False, drop_empty=False)


def _collection_ids(cid: str) -> list[str]:
    return _member_ids("collection_books", "collection_id", cid)


@router.get("/api/collections")
def list_collections(request: Request):
    user = auth.current_user(request)
    rows = [c for c in db.q("SELECT * FROM collections ORDER BY name COLLATE NOCASE") if auth.can_access_library(user, c["library_id"])]
    return {"collections": [serialize.collection_json(c, expanded=False) for c in rows]}


@router.post("/api/collections")
def create_collection(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    if not body.get("name") or not body.get("libraryId"):
        raise HTTPException(400, "name and libraryId required")
    if not auth.can_access_library(user, body["libraryId"]):
        raise HTTPException(404, "Library not found")
    now = db.now_ms()
    c = {"id": db.new_id(), "library_id": body["libraryId"], "name": body["name"].strip(), "description": body.get("description"),
         "created_at": now, "updated_at": now}
    db.insert("collections", c)
    _set_collection_books(c["id"], body.get("books") or [])
    return serialize.collection_json(_collection_or_404(c["id"], user))


@router.get("/api/collections/{cid}")
def get_collection(cid: str, request: Request):
    user = auth.current_user(request)
    return serialize.collection_json(_collection_or_404(cid, user))


@router.patch("/api/collections/{cid}")
def update_collection(cid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _collection_or_404(cid, user)
    fields = {k: body[k] for k in ("name", "description") if k in body}
    if fields:
        fields["updated_at"] = db.now_ms()
        db.update("collections", cid, fields)
    if isinstance(body.get("books"), list):
        _set_collection_books(cid, body["books"])
    return serialize.collection_json(_collection_or_404(cid, user))


@router.delete("/api/collections/{cid}")
def delete_collection(cid: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "delete")
    _collection_or_404(cid, user)
    db.ex("DELETE FROM collection_books WHERE collection_id = ?", (cid,))
    db.ex("DELETE FROM collections WHERE id = ?", (cid,))
    return PlainTextResponse("OK")


@router.post("/api/collections/{cid}/book")
def add_collection_book(cid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _collection_or_404(cid, user)
    _set_collection_books(cid, _collection_ids(cid) + [body.get("id")])
    return serialize.collection_json(_collection_or_404(cid, user))


@router.delete("/api/collections/{cid}/book/{book_id}")
def remove_collection_book(cid: str, book_id: str, request: Request):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _collection_or_404(cid, user)
    _set_collection_books(cid, [i for i in _collection_ids(cid) if i != book_id])
    return serialize.collection_json(_collection_or_404(cid, user))


@router.post("/api/collections/{cid}/batch/add")
def batch_add_collection(cid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _collection_or_404(cid, user)
    _set_collection_books(cid, _collection_ids(cid) + list(body.get("books") or []))
    return serialize.collection_json(_collection_or_404(cid, user))


@router.post("/api/collections/{cid}/batch/remove")
def batch_remove_collection(cid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    auth.require_permission(user, "update")
    _collection_or_404(cid, user)
    remove = set(body.get("books") or [])
    _set_collection_books(cid, [i for i in _collection_ids(cid) if i not in remove])
    return serialize.collection_json(_collection_or_404(cid, user))


# ------------------------------------------------------------------ playlists

def _playlist_or_404(pid: str, user: dict) -> dict:
    p = db.q1("SELECT * FROM playlists WHERE id = ?", (pid,))
    if not p or p["user_id"] != user["id"]:
        raise HTTPException(404, "Playlist not found")
    return p


def _playlist_ids(pid: str) -> list[str]:
    return _member_ids("playlist_items", "playlist_id", pid)


def _set_playlist_items(pid: str, ids: list[str]) -> None:
    _replace_members(pid, ids, table="playlist_items", parent_col="playlist_id", parent_table="playlists",
                     episode=True, drop_empty=True)


def _item_ids(entries) -> list[str]:
    return [e.get("libraryItemId") if isinstance(e, dict) else e for e in entries or []]


@router.get("/api/playlists")
def list_playlists(request: Request):
    user = auth.current_user(request)
    return {"playlists": [serialize.playlist_json(p) for p in db.q("SELECT * FROM playlists WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user["id"],))]}


@router.post("/api/playlists")
def create_playlist(request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    if not body.get("name") or not body.get("libraryId"):
        raise HTTPException(400, "name and libraryId required")
    now = db.now_ms()
    p = {"id": db.new_id(), "library_id": body["libraryId"], "user_id": user["id"], "name": body["name"].strip(),
         "description": body.get("description"), "created_at": now, "updated_at": now}
    db.insert("playlists", p)
    _set_playlist_items(p["id"], _item_ids(body.get("items")))
    return serialize.playlist_json(_playlist_or_404(p["id"], user))


@router.get("/api/playlists/{pid}")
def get_playlist(pid: str, request: Request):
    user = auth.current_user(request)
    return serialize.playlist_json(_playlist_or_404(pid, user))


@router.patch("/api/playlists/{pid}")
def update_playlist(pid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    fields = {k: body[k] for k in ("name", "description") if k in body and body[k] is not None}
    if fields:
        fields["updated_at"] = db.now_ms()
        db.update("playlists", pid, fields)
    if isinstance(body.get("items"), list):
        _set_playlist_items(pid, _item_ids(body["items"]))
    return serialize.playlist_json(_playlist_or_404(pid, user))


@router.delete("/api/playlists/{pid}")
def delete_playlist(pid: str, request: Request):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    db.ex("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
    db.ex("DELETE FROM playlists WHERE id = ?", (pid,))
    return PlainTextResponse("OK")


@router.post("/api/playlists/{pid}/item")
def add_playlist_item(pid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    _set_playlist_items(pid, _playlist_ids(pid) + [body.get("libraryItemId")])
    return serialize.playlist_json(_playlist_or_404(pid, user))


@router.delete("/api/playlists/{pid}/item/{item_id}")
@router.delete("/api/playlists/{pid}/item/{item_id}/{episode_id}")
def remove_playlist_item(pid: str, item_id: str, request: Request, episode_id: str | None = None):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    _set_playlist_items(pid, [i for i in _playlist_ids(pid) if i != item_id])
    return serialize.playlist_json(_playlist_or_404(pid, user))


@router.post("/api/playlists/{pid}/batch/add")
def batch_add_playlist(pid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    _set_playlist_items(pid, _playlist_ids(pid) + _item_ids(body.get("items")))
    return serialize.playlist_json(_playlist_or_404(pid, user))


@router.post("/api/playlists/{pid}/batch/remove")
def batch_remove_playlist(pid: str, request: Request, body: dict = Body(...)):
    user = auth.current_user(request)
    _playlist_or_404(pid, user)
    remove = set(_item_ids(body.get("items")))
    _set_playlist_items(pid, [i for i in _playlist_ids(pid) if i not in remove])
    return serialize.playlist_json(_playlist_or_404(pid, user))
