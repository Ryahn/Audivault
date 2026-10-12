"""/api/libraries routes."""

import logging
import random
import threading
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from ..catalog import library, serialize
from ..catalog.paths import invalidate_media_roots
from ..core import auth, db, tasks
from ..management.watcher import watcher
from ..scanner import scanner
from ..scanner.parse import title_ignore_prefix
from .common import (Loaded, apply_filter, as_int, can_sql_page, collapse_series, decode_filter, encode_b64,
                     get_library_or_404, loaded_for_items, paginate, sort_items, sql_page_items, text_search, truthy)

log = logging.getLogger("abstream.libraries")
router = APIRouter(prefix="/api/libraries")


def _after_library_change(*, scan_id: str | None = None, scan_title: str | None = None, user_id: str | None = None) -> None:
    """Start scan / refresh watchers off the request thread so Save returns immediately."""
    def run():
        try:
            if scan_id:
                tasks.start("scan", scan_title or f"Scan", scanner.scan_library, scan_id,
                            key=f"scan:{scan_id}", library_id=scan_id, user_id=user_id)
            watcher.sync()
        except Exception:
            log.exception("Post-library-change work failed")

    threading.Thread(target=run, daemon=True, name="library-after-change").start()


def _folders_from_body(folders: list, existing: list | None = None) -> list[dict]:
    existing_by_path = {f["fullPath"]: f for f in existing or []}
    out = []
    for f in folders or []:
        path = (f.get("fullPath") if isinstance(f, dict) else str(f)).strip()
        if not path:
            continue
        path = str(Path(path).expanduser())
        if not Path(path).is_dir():
            raise HTTPException(400, f"Folder does not exist: {path}")
        folder = dict(existing_by_path.get(path) or {"id": db.new_id(), "fullPath": path, "addedAt": db.now_ms()})
        if isinstance(f, dict) and "locked" in f:
            folder["locked"] = bool(f["locked"])
        out.append(folder)
    return out


@router.get("")
def list_libraries(request: Request):
    user = auth.current_user(request)
    libs = [serialize.library_json(lib) for lib in library.all_libraries() if auth.can_access_library(user, lib["id"])]
    return {"libraries": libs}


@router.post("")
def create_library(request: Request, body: dict = Body(...)):
    user = auth.require_admin(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    folders = _folders_from_body(body.get("folders") or [])
    now = db.now_ms()
    settings = {k: v for k, v in (body.get("settings") or {}).items() if k in library.DEFAULT_LIBRARY_SETTINGS}
    lib = {"id": db.new_id(), "name": name, "media_type": "book", "folders": db.jdump(folders), "settings": db.jdump(settings),
           "display_order": (db.scalar("SELECT MAX(display_order) FROM libraries") or 0) + 1, "icon": body.get("icon") or "audiobookshelf",
           "last_scan": None, "created_at": now, "updated_at": now}
    db.insert("libraries", lib)
    invalidate_media_roots()
    # Persist first, then scan/watch in the background so the UI is not blocked.
    _after_library_change(scan_id=lib["id"], scan_title=f"Scan {name}", user_id=user["id"])
    return serialize.library_json(lib)


@router.get("/{library_id}")
def get_library(library_id: str, request: Request, include: str = ""):
    user = auth.current_user(request)
    lib = get_library_or_404(library_id, user)
    if "filterdata" in include:
        return {"filterdata": filter_data(library_id, user), "issues": db.scalar(
            "SELECT COUNT(*) FROM library_items WHERE library_id = ? AND is_missing = 1", (library_id,)),
            "numUserPlaylists": db.scalar("SELECT COUNT(*) FROM playlists WHERE library_id = ? AND user_id = ?", (library_id, user["id"])),
            "customMetadataProviders": [], "library": serialize.library_json(lib)}
    return serialize.library_json(lib)


@router.patch("/{library_id}")
def update_library(library_id: str, request: Request, body: dict = Body(...)):
    admin = auth.require_admin(request)
    lib = library.get_library(library_id)
    if not lib:
        raise HTTPException(404, "Library not found")
    fields = {}
    if body.get("name"):
        fields["name"] = body["name"].strip()
    if "folders" in body:
        fields["folders"] = db.jdump(_folders_from_body(body["folders"], db.jload(lib["folders"], [])))
    if "settings" in body and isinstance(body["settings"], dict):
        merged = (db.jload(lib["settings"], {}) or {}) | {k: v for k, v in body["settings"].items() if k in library.DEFAULT_LIBRARY_SETTINGS}
        if "importFolder" in merged and merged["importFolder"] and not Path(merged["importFolder"]).is_dir():
            raise HTTPException(400, "Import folder does not exist")
        fields["settings"] = db.jdump(merged)
    if "displayOrder" in body:
        fields["display_order"] = as_int(body["displayOrder"], lib["display_order"])
    if "icon" in body:
        fields["icon"] = body["icon"]
    fields["updated_at"] = db.now_ms()
    before = library.library_settings(lib)
    db.update("libraries", library_id, fields)
    if "folders" in body:
        invalidate_media_roots()
    updated = library.get_library(library_id)
    after = library.library_settings(updated)
    if after.get("autoImport") and (not before.get("autoImport") or before.get("importFolder") != after.get("importFolder")):
        from ..management import autoimport
        autoimport.start(library_id)
    if before.get("useCueChapters") != after.get("useCueChapters"):
        tasks.start("chapters", f"Refresh chapters in {updated['name']}", scanner.refresh_library_chapters, library_id,
                    key=f"chapters:{library_id}", library_id=library_id, user_id=admin["id"])
    _after_library_change()
    return serialize.library_json(updated)


@router.delete("/{library_id}")
def delete_library(library_id: str, request: Request):
    auth.require_admin(request)
    lib = library.get_library(library_id)
    if not lib:
        raise HTTPException(404, "Library not found")
    for item in db.q("SELECT id FROM library_items WHERE library_id = ?", (library_id,)):
        library.delete_item_records(item["id"])
        scanner.remove_item_cache(item["id"])
    for table in ("authors", "series", "collections", "playlists"):
        db.ex(f"DELETE FROM {table} WHERE library_id = ?", (library_id,))
    db.ex("DELETE FROM libraries WHERE id = ?", (library_id,))
    invalidate_media_roots()
    _after_library_change()
    return serialize.library_json(lib)


@router.post("/order")
def reorder(request: Request, body: list = Body(...)):
    auth.require_admin(request)
    for entry in body:
        db.ex("UPDATE libraries SET display_order = ? WHERE id = ?", (as_int(entry.get("newOrder")), entry.get("id")))
    return {"libraries": [serialize.library_json(lib) for lib in library.all_libraries()]}


@router.post("/{library_id}/scan")
@router.get("/{library_id}/scan")
def scan(library_id: str, request: Request, force: str = "0"):
    admin = auth.require_admin(request)
    lib = library.get_library(library_id)
    if not lib:
        raise HTTPException(404, "Library not found")
    title = f"{'Force rescan' if truthy(force) else 'Scan'} {lib['name']}"
    task = tasks.start("scan", title, scanner.scan_library, library_id, truthy(force), key=f"scan:{library_id}",
                       library_id=library_id, user_id=admin["id"])
    return {"success": True, "task": task}


@router.post("/{library_id}/refresh-metadata")
def refresh_metadata(library_id: str, request: Request):
    """Fill empty metadata from the library provider, in small batches. Does not replace values already set."""
    admin = auth.require_admin(request)
    lib = library.get_library(library_id)
    if not lib:
        raise HTTPException(404, "Library not found")
    from ..catalog import media
    task = tasks.start("match", f"Refresh metadata {lib['name']}", media.refresh_library_metadata, library_id,
                       key=f"refresh:{library_id}", library_id=library_id, user_id=admin["id"])
    return {"success": True, "task": task}


@router.delete("/{library_id}/issues")
def remove_missing(library_id: str, request: Request):
    auth.require_admin(request)
    missing = db.q("SELECT id FROM library_items WHERE library_id = ? AND is_missing = 1", (library_id,))
    for item in missing:
        library.delete_item_records(item["id"])
        scanner.remove_item_cache(item["id"])
    library.cleanup_orphans(library_id)
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(library_id)
    return {"removed": len(missing)}


# ------------------------------------------------------------------ items

@router.get("/{library_id}/items")
def library_items(library_id: str, request: Request, limit: int = 0, page: int = 0, sort: str | None = None, desc: str = "0",
                  filter: str | None = None, collapseseries: str = "0", minified: str = "0", include: str = "", q: str | None = None):
    user = auth.current_user(request)
    lib = get_library_or_404(library_id, user)
    group, value = decode_filter(filter)
    collapse = truthy(collapseseries) and group != "series"
    ignore_prefix = serialize.server_settings_json()["sortingIgnorePrefix"]
    expanded = not truthy(minified)
    want_progress = "progress" in include

    page_items = None
    total = 0
    loaded = None
    if can_sql_page(collapse, sort, group, ignore_prefix, value):
        paged = sql_page_items(
            library_id, user, sort_by=sort, desc=truthy(desc), limit=limit, page=page,
            group=group, value=value, q=q,
        )
        if paged is not None:
            page_items, total = paged
            loaded = loaded_for_items(library_id, page_items, user)

    if page_items is None:
        loaded = Loaded(library_id, user)
        items = apply_filter(loaded, loaded.items, group, value)
        if q:
            items = text_search(loaded, items, q)
        items = sort_items(loaded, items, sort, truthy(desc), ignore_prefix, series_id=value if group == "series" else None)
        entries = collapse_series(loaded, items, library.library_settings(lib).get("hideSingleBookSeries")) if collapse else [(i, None) for i in items]
        total = len(entries)
        page_entries = paginate(entries, limit, page)
    else:
        page_entries = [(i, None) for i in page_items]

    results = []
    for item, cs in page_entries:
        j = loaded.json(item, expanded=expanded, user=user, include_progress=want_progress)
        if group == "series":
            seq = next((x["sequence"] for x in loaded.series.get(item["id"], []) if x["id"] == value), None)
            j["media"]["metadata"]["series"] = {"id": value, "name": next((x["name"] for x in loaded.series.get(item["id"], []) if x["id"] == value), ""), "sequence": seq}
        if cs:
            j["collapsedSeries"] = cs
        if "rssfeed" in include:
            j["rssFeed"] = None
        if "share" in include:
            j["mediaItemShare"] = None
        results.append(j)
    return {"results": results, "total": total, "limit": limit, "page": page, "sortBy": sort, "sortDesc": truthy(desc),
            "filterBy": filter, "mediaType": "book", "minified": not expanded, "collapseseries": collapse, "include": include,
            "offset": page * limit}


# ------------------------------------------------------------------ series / authors / narrators

def _series_rows(library_id: str) -> list[dict]:
    return db.q("SELECT * FROM series WHERE library_id = ?", (library_id,))


@router.get("/{library_id}/series")
def library_series(library_id: str, request: Request, limit: int = 0, page: int = 0, sort: str = "name", desc: str = "0",
                   filter: str | None = None, include: str = "", minified: str = "0"):
    user = auth.current_user(request)
    lib = get_library_or_404(library_id, user)
    loaded = Loaded(library_id, user, include_missing=False)
    books_by_series: dict[str, list[dict]] = {}
    for item in loaded.items:
        for s in loaded.series.get(item["id"], []):
            row = dict(item)
            row["series_sequence"] = s["sequence"]
            books_by_series.setdefault(s["id"], []).append(row)
    rows = [dict(s) for s in _series_rows(library_id) if books_by_series.get(s["id"])]
    for s in rows:
        books = books_by_series[s["id"]]
        finished = sum(1 for b in books if (loaded.progress.get(b["id"]) or {}).get("is_finished"))
        s["finishedBooks"] = finished
        s["completion"] = finished / len(books) if books else 0.0
    if library.library_settings(lib).get("hideSingleBookSeries"):
        rows = [s for s in rows if len(books_by_series[s["id"]]) > 1]

    group, value = decode_filter(filter)
    if group:
        allowed = {i["id"] for i in apply_filter(loaded, loaded.items, group, value)}
        rows = [s for s in rows if any(b["id"] in allowed for b in books_by_series[s["id"]])]

    keys = {
        "name": lambda s: title_ignore_prefix(s["name"]).lower(),
        "numBooks": lambda s: len(books_by_series[s["id"]]),
        "addedAt": lambda s: s["added_at"],
        "lastBookAdded": lambda s: max(b["added_at"] for b in books_by_series[s["id"]]),
        "lastBookUpdated": lambda s: max(b["updated_at"] for b in books_by_series[s["id"]]),
        "totalDuration": lambda s: sum(b["duration"] or 0 for b in books_by_series[s["id"]]),
        "finished": lambda s: (s["finishedBooks"], title_ignore_prefix(s["name"]).lower()),
        "completion": lambda s: (s["completion"], s["finishedBooks"], title_ignore_prefix(s["name"]).lower()),
    }
    if sort == "random":
        random.shuffle(rows)
    else:
        rows.sort(key=keys.get(sort, keys["name"]), reverse=truthy(desc))
    total = len(rows)
    slim = truthy(minified)
    results = [serialize.series_json(s, serialize.sort_by_sequence(books_by_series[s["id"]]), minified=slim,
                                     authors_by_id=loaded.authors, series_by_id=loaded.series)
               for s in paginate(rows, limit, page)]
    return {"results": results, "total": total, "limit": limit, "page": page, "sortBy": sort, "sortDesc": truthy(desc),
            "filterBy": filter, "minified": slim, "include": include}


@router.get("/{library_id}/authors")
def library_authors(library_id: str, request: Request, limit: int = 0, page: int = 0, sort: str = "name", desc: str = "0"):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    rows = db.q("SELECT a.*, COUNT(li.id) AS num_books FROM authors a JOIN book_authors ba ON ba.author_id = a.id "
                "JOIN library_items li ON li.id = ba.item_id AND li.is_missing = 0 WHERE a.library_id = ? GROUP BY a.id", (library_id,))
    keys = {"name": lambda a: a["name"].lower(), "lastFirst": lambda a: (a["last_first"] or a["name"]).lower(),
            "numBooks": lambda a: a["num_books"], "addedAt": lambda a: a["added_at"], "updatedAt": lambda a: a["updated_at"]}
    rows.sort(key=keys.get(sort, keys["name"]), reverse=truthy(desc))
    total = len(rows)
    authors = [serialize.author_json(a, a["num_books"]) for a in paginate(rows, limit, page)]
    return {"authors": authors, "results": authors, "total": total, "limit": limit, "page": page, "sortBy": sort, "desc": truthy(desc)}


@router.get("/{library_id}/narrators")
def library_narrators(library_id: str, request: Request):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    counts = Counter()
    for row in db.q("SELECT narrators FROM library_items WHERE library_id = ? AND is_missing = 0", (library_id,)):
        counts.update(db.jload(row["narrators"], []))
    return {"narrators": [{"id": encode_b64(n), "name": n, "numBooks": c} for n, c in sorted(counts.items(), key=lambda kv: kv[0].lower())]}


@router.get("/{library_id}/collections")
def library_collections(library_id: str, request: Request, limit: int = 0, page: int = 0, minified: str = "1"):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    rows = db.q("SELECT * FROM collections WHERE library_id = ? ORDER BY name COLLATE NOCASE", (library_id,))
    # List view uses minified (expanded=False) books; detail endpoints keep full payloads.
    expanded = not truthy(minified)
    return {"results": [serialize.collection_json(c, user, expanded=expanded) for c in paginate(rows, limit, page)],
            "total": len(rows), "limit": limit, "page": page}


@router.get("/{library_id}/playlists")
def library_playlists(library_id: str, request: Request, limit: int = 0, page: int = 0):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    rows = db.q("SELECT * FROM playlists WHERE library_id = ? AND user_id = ? ORDER BY name COLLATE NOCASE", (library_id, user["id"]))
    return {"results": [serialize.playlist_json(p) for p in paginate(rows, limit, page)], "total": len(rows), "limit": limit, "page": page}


@router.get("/{library_id}/recent-episodes")
def recent_episodes(library_id: str, request: Request):
    """ABS client compat stub — Audivault has no podcasts; keep an empty list."""
    auth.current_user(request)
    return {"episodes": [], "total": 0, "limit": 0, "page": 0}


# ------------------------------------------------------------------ filter data / search / stats

def filter_data(library_id: str, user: dict) -> dict:
    loaded = Loaded(library_id, user, include_missing=False)
    authors, series = {}, {}
    genres, tags, narrators, languages, publishers, decades = set(), set(), set(), set(), set(), set()
    for i in loaded.items:
        for a in loaded.authors.get(i["id"], []):
            authors[a["id"]] = a["name"]
        for s in loaded.series.get(i["id"], []):
            series[s["id"]] = s["name"]
        genres.update(db.jload(i["genres"], []))
        tags.update(db.jload(i["tags"], []))
        narrators.update(db.jload(i["narrators"], []))
        if i["language"]:
            languages.add(i["language"])
        if i["publisher"]:
            publishers.add(i["publisher"])
        try:
            decades.add(str(int(str(i["published_year"])[:4]) // 10 * 10))
        except (TypeError, ValueError):
            pass

    def srt(values):
        return sorted(values, key=str.lower)

    return {
        "authors": [{"id": k, "name": v} for k, v in sorted(authors.items(), key=lambda kv: kv[1].lower())],
        "genres": srt(genres), "tags": srt(tags),
        "series": [{"id": k, "name": v} for k, v in sorted(series.items(), key=lambda kv: title_ignore_prefix(kv[1]).lower())],
        "narrators": srt(narrators), "languages": srt(languages), "publishers": srt(publishers),
        "publishedDecades": sorted(decades), "bookCount": len(loaded.items), "authorCount": len(authors),
        "seriesCount": len(series), "podcastCount": 0, "numIssues": 0, "loadedAt": db.now_ms(),
    }


@router.get("/{library_id}/filterdata")
def get_filter_data(library_id: str, request: Request):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    return filter_data(library_id, user)


@router.get("/{library_id}/search")
def search(library_id: str, request: Request, q: str = "", limit: int = 12):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    loaded = Loaded(library_id, user, include_missing=False)
    query = q.lower().strip()
    books = []
    for i in loaded.items:
        for key, text in (("title", i["title"]), ("subtitle", i["subtitle"]), ("authorName", loaded.author_name(i)),
                          ("isbn", i["isbn"]), ("asin", i["asin"])):
            if text and query in text.lower():
                books.append({"libraryItem": loaded.json(i, expanded=False), "matchKey": key, "matchText": text})
                break
        if len(books) >= limit:
            break
    series_out, author_out = [], []
    for s in _series_rows(library_id):
        if query in s["name"].lower():
            items = [dict(loaded.by_id[i["id"]], series_sequence=next(
                (x["sequence"] for x in loaded.series.get(i["id"], []) if x["id"] == s["id"]), None))
                     for i in loaded.items if any(x["id"] == s["id"] for x in loaded.series.get(i["id"], []))]
            items = serialize.sort_by_sequence(items)
            if items:
                series_out.append({
                    "series": serialize.series_json(s, items, authors_by_id=loaded.authors, series_by_id=loaded.series),
                    "books": [loaded.json(b) for b in items if b["id"] in loaded.by_id],
                })
        if len(series_out) >= limit:
            break
    for a in db.q("SELECT a.*, COUNT(ba.item_id) AS num_books FROM authors a JOIN book_authors ba ON ba.author_id = a.id "
                  "WHERE a.library_id = ? GROUP BY a.id", (library_id,)):
        if query in a["name"].lower():
            author_out.append(serialize.author_json(a, a["num_books"]))
        if len(author_out) >= limit:
            break
    narr, genre, tag = Counter(), Counter(), Counter()
    for i in loaded.items:
        narr.update(n for n in db.jload(i["narrators"], []) if query in n.lower())
        genre.update(g for g in db.jload(i["genres"], []) if query in g.lower())
        tag.update(t for t in db.jload(i["tags"], []) if query in t.lower())
    return {
        "book": books, "podcast": [], "episodes": [], "series": series_out, "authors": author_out,
        "narrators": [{"name": n, "numBooks": c} for n, c in narr.most_common(limit)],
        "tags": [{"name": n, "numItems": c} for n, c in tag.most_common(limit)],
        "genres": [{"name": n, "numItems": c} for n, c in genre.most_common(limit)],
    }


@router.get("/{library_id}/stats")
def stats(library_id: str, request: Request):
    user = auth.current_user(request)
    get_library_or_404(library_id, user)
    loaded = Loaded(library_id, user, include_missing=False)
    genres, authors, narrators = Counter(), Counter(), Counter()
    total_duration = 0.0
    total_size = 0
    audio_tracks = 0
    for i in loaded.items:
        total_duration += i["duration"] or 0
        total_size += i["size"] or 0
        audio_tracks += int(i.get("num_audio_files") or 0)
        genres.update(g for g in db.jload(i["genres"], []) if g)
        for a in loaded.authors.get(i["id"], []):
            authors[(a["id"], a["name"])] += 1
        narrators.update(n for n in db.jload(i["narrators"], []) if n)
    total_items = len(loaded.items)
    by_duration = sorted(loaded.items, key=lambda i: i["duration"] or 0, reverse=True)[:10]
    by_size = sorted(loaded.items, key=lambda i: i["size"] or 0, reverse=True)[:10]
    return {
        "totalItems": total_items, "totalAuthors": len(authors), "totalGenres": len(genres),
        "totalNarrators": len(narrators),
        "totalDuration": total_duration, "avgDuration": (total_duration / total_items) if total_items else 0,
        "totalSize": total_size, "numAudioTracks": audio_tracks,
        "longestItems": [{"id": i["id"], "title": i["title"], "duration": i["duration"]} for i in by_duration],
        "largestItems": [{"id": i["id"], "title": i["title"], "size": i["size"]} for i in by_size],
        "authorsWithCount": [{"id": k[0], "name": k[1], "count": c} for k, c in authors.most_common(10)],
        "narratorsWithCount": [{"id": encode_b64(n), "name": n, "count": c} for n, c in narrators.most_common(10)],
        "genresWithCount": [{"genre": g, "count": c} for g, c in genres.most_common(10)],
    }


# ------------------------------------------------------------------ personalized home shelves

@router.get("/{library_id}/personalized")
def personalized(library_id: str, request: Request, limit: int = 10, include: str = ""):
    user = auth.current_user(request)
    lib = get_library_or_404(library_id, user)
    loaded = Loaded(library_id, user, include_missing=False)
    prog = loaded.progress
    shelves = []

    def book_entities(items, with_progress=False):
        out = []
        for i in items:
            j = loaded.json(i, expanded=False)
            if with_progress and i["id"] in prog:
                j["progressLastUpdate"] = prog[i["id"]]["last_update"]
            out.append(j)
        return out

    in_progress = [i for i in loaded.items if i["id"] in prog and not prog[i["id"]]["is_finished"]
                   and not prog[i["id"]]["hide_from_continue"] and (prog[i["id"]]["current_pos"] > 0 or prog[i["id"]]["progress"] > 0)]
    in_progress.sort(key=lambda i: prog[i["id"]]["last_update"], reverse=True)
    audio_ip = [i for i in in_progress if int(i.get("num_tracks") or 0) > 0]
    ebook_ip = [i for i in in_progress if int(i.get("num_tracks") or 0) == 0 and i.get("ebook_format")]
    if audio_ip:
        shelves.append({"id": "continue-listening", "label": "Continue Listening", "labelStringKey": "LabelContinueListening",
                        "type": "book", "entities": book_entities(audio_ip[:limit], True), "total": len(audio_ip)})
    if ebook_ip:
        shelves.append({"id": "continue-reading", "label": "Continue Reading", "labelStringKey": "LabelContinueReading",
                        "type": "book", "entities": book_entities(ebook_ip[:limit], True), "total": len(ebook_ip)})

    # Continue series: next unfinished book in series where the user finished one and nothing is in progress
    series_books: dict[str, list[tuple[str | None, dict]]] = {}
    for i in loaded.items:
        for s in loaded.series.get(i["id"], []):
            series_books.setdefault(s["id"], []).append((s["sequence"], i))
    continue_series = []
    for sid, books in series_books.items():
        books.sort(key=lambda b: (_num(b[0]), b[1]["title"]))
        states = [prog.get(b[1]["id"]) for b in books]
        if not any(p and p["is_finished"] for p in states):
            continue
        if any(p and not p["is_finished"] and p["current_pos"] > 0 for p in states):
            continue
        last_finished = max(idx for idx, p in enumerate(states) if p and p["is_finished"])
        nxt = next((b[1] for idx, b in enumerate(books) if idx > last_finished and not (states[idx] and states[idx]["is_finished"])), None)
        if nxt and not (prog.get(nxt["id"]) or {}).get("hide_from_continue"):
            last_update = max(p["last_update"] for p in states if p)
            continue_series.append((last_update, nxt))
    continue_series.sort(key=lambda t: t[0], reverse=True)
    if continue_series:
        shelves.append({"id": "continue-series", "label": "Continue Series", "labelStringKey": "LabelContinueSeries", "type": "book",
                        "entities": book_entities([b for _, b in continue_series[:limit]]), "total": len(continue_series)})

    recent = sorted(loaded.items, key=lambda i: i["added_at"], reverse=True)
    if recent:
        shelves.append({"id": "recently-added", "label": "Recently Added", "labelStringKey": "LabelRecentlyAdded", "type": "book",
                        "entities": book_entities(recent[:limit]), "total": len(recent)})

    series_rows = {s["id"]: s for s in _series_rows(library_id)}
    recent_series = []
    for sid, books in series_books.items():
        if sid in series_rows:
            recent_series.append((max(b[1]["added_at"] for b in books), sid))
    recent_series.sort(reverse=True)
    if recent_series:
        entities = []
        for _, sid in recent_series[:5]:
            rows = []
            for seq, b in series_books[sid]:
                r = dict(b)
                r["series_sequence"] = seq
                rows.append(r)
            entities.append(serialize.series_json(series_rows[sid], serialize.sort_by_sequence(rows), minified=True,
                                                 authors_by_id=loaded.authors, series_by_id=loaded.series))
        shelves.append({"id": "recent-series", "label": "Recent Series", "labelStringKey": "LabelRecentSeries", "type": "series",
                        "entities": entities, "total": len(recent_series)})

    unstarted = [i for i in loaded.items if i["id"] not in prog]
    if unstarted:
        picks = random.sample(unstarted, min(limit, len(unstarted)))
        shelves.append({"id": "discover", "label": "Discover", "labelStringKey": "LabelDiscover", "type": "book",
                        "entities": book_entities(picks), "total": len(unstarted)})

    finished = sorted((i for i in loaded.items if i["id"] in prog and prog[i["id"]]["is_finished"]),
                      key=lambda i: prog[i["id"]]["finished_at"] or 0, reverse=True)
    if finished:
        shelves.append({"id": "listen-again", "label": "Listen Again", "labelStringKey": "LabelListenAgain", "type": "book",
                        "entities": book_entities(finished[:limit], True), "total": len(finished)})

    authors = db.q("SELECT a.*, COUNT(ba.item_id) AS num_books FROM authors a JOIN book_authors ba ON ba.author_id = a.id "
                   "WHERE a.library_id = ? GROUP BY a.id ORDER BY a.added_at DESC LIMIT ?", (library_id, limit))
    if authors:
        shelves.append({"id": "newest-authors", "label": "Newest Authors", "labelStringKey": "LabelNewestAuthors", "type": "authors",
                        "entities": [serialize.author_json(a, a["num_books"]) for a in authors],
                        "total": db.scalar("SELECT COUNT(*) FROM authors WHERE library_id = ?", (library_id,))})
    return shelves


def _num(seq):
    try:
        return float(seq)
    except (TypeError, ValueError):
        return float("inf")
