"""Convert database rows into Audiobookshelf-compatible JSON.

Timestamps are integer milliseconds everywhere (clients decode them as Int64).
"""

import html
import re

from .. import ABS_COMPAT_VERSION
from ..core import auth, db
from ..scanner.parse import last_first, title_ignore_prefix, title_prefix_at_end
from . import library


def strip_html(text: str | None) -> str | None:
    if not text:
        return text
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def normalize_chapters(chapters: list[dict] | None) -> list[dict]:
    """Clients (e.g. AudioBooth) require an integer id on every chapter."""
    return [{"id": i, "start": float(c.get("start") or 0), "end": float(c.get("end") or 0),
             "title": c.get("title") or f"Chapter {i + 1}"} for i, c in enumerate(chapters or [])]


def _file_meta(f: dict) -> dict:
    return {
        "filename": f["filename"], "ext": f["ext"], "path": f["path"], "relPath": f["rel_path"], "size": int(f["size"]),
        "mtimeMs": int(f.get("mtime_ms") or 0), "ctimeMs": int(f.get("ctime_ms") or 0), "birthtimeMs": int(f.get("birthtime_ms") or 0),
    }


def library_file_json(f: dict) -> dict:
    return {"ino": f["ino"], "metadata": _file_meta(f), "isSupplementary": None, "addedAt": int(f.get("added_at") or 0),
            "updatedAt": int(f.get("updated_at") or 0), "fileType": f.get("file_type", "unknown")}


def audio_file_json(a: dict) -> dict:
    tags = a.get("tags") or {}
    meta_tags = {k2: tags.get(k1) for k1, k2 in (
        ("album", "tagAlbum"), ("artist", "tagArtist"), ("album_artist", "tagAlbumArtist"), ("title", "tagTitle"),
        ("composer", "tagComposer"), ("genre", "tagGenre"), ("date", "tagDate"), ("publisher", "tagPublisher"),
        ("description", "tagDescription"), ("series", "tagSeries"), ("series_part", "tagSeriesPart"), ("asin", "tagASIN"),
        ("isbn", "tagISBN"), ("language", "tagLanguage"), ("subtitle", "tagSubtitle"), ("grouping", "tagGrouping"),
    ) if tags.get(k1)}
    return {
        "index": a.get("index", 1), "ino": a["ino"], "metadata": _file_meta(a), "addedAt": int(a.get("added_at") or 0),
        "updatedAt": int(a.get("updated_at") or 0), "trackNumFromMeta": a.get("track"), "discNumFromMeta": a.get("disc"),
        "trackNumFromFilename": None, "discNumFromFilename": None, "manuallyVerified": False, "exclude": bool(a.get("exclude")),
        "error": a.get("error"), "format": a.get("format"), "duration": float(a.get("duration") or 0), "bitRate": a.get("bit_rate"),
        "language": None, "codec": a.get("codec"), "timeBase": "1/1000", "channels": a.get("channels"),
        "channelLayout": a.get("channel_layout"), "chapters": [
            {"id": i, "start": c["start"], "end": c["end"], "title": c["title"]} for i, c in enumerate(a.get("chapters") or [])
        ], "embeddedCoverArt": "mjpeg" if a.get("embedded_cover") else None, "metaTags": meta_tags, "mimeType": a.get("mime_type"),
    }


def ebook_file_json(e: dict | None) -> dict | None:
    if not e:
        return None
    return {"ino": e["ino"], "metadata": _file_meta(e), "ebookFormat": e.get("ebook_format"),
            "addedAt": int(e.get("added_at") or 0), "updatedAt": int(e.get("updated_at") or 0)}


def tracks_json(item: dict, audio: list[dict]) -> list[dict]:
    tracks, offset = [], 0.0
    for a in audio:
        if a.get("exclude"):
            continue
        tracks.append({
            "index": a.get("index", len(tracks) + 1), "startOffset": round(offset, 3), "duration": float(a.get("duration") or 0),
            "title": a["filename"], "contentUrl": f"/api/items/{item['id']}/file/{a['ino']}", "mimeType": a.get("mime_type") or "audio/mpeg",
            "codec": a.get("codec"), "metadata": _file_meta(a), "ino": a["ino"],
        })
        offset += float(a.get("duration") or 0)
    return tracks


def book_metadata(item: dict, authors: list[dict], series: list[dict], include_description: bool = True) -> dict:
    author_names = [a["name"] for a in authors]
    narrators = db.jload(item["narrators"], [])
    out = {
        "title": item["title"],
        "titleIgnorePrefix": title_prefix_at_end(item["title"]),
        "subtitle": item["subtitle"],
        "authors": [{"id": a["id"], "name": a["name"]} for a in authors],
        "narrators": narrators,
        "series": [{"id": s["id"], "name": s["name"], "sequence": s["sequence"]} for s in series],
        "genres": db.jload(item["genres"], []),
        "publishedYear": item["published_year"],
        "publishedDate": item["published_date"],
        "publisher": item["publisher"],
        "isbn": item["isbn"],
        "asin": item["asin"],
        "language": item["language"],
        "explicit": bool(item["explicit"]),
        "abridged": bool(item["abridged"]),
        "authorName": ", ".join(author_names),
        "authorNameLF": ", ".join(last_first(n) for n in author_names),
        "narratorName": ", ".join(narrators),
        "seriesName": ", ".join(f"{s['name']} #{s['sequence']}" if s["sequence"] else s["name"] for s in series),
    }
    if include_description:
        out["description"] = item.get("description")
        out["descriptionPlain"] = strip_html(item.get("description"))
    else:
        out["description"] = None
        out["descriptionPlain"] = None
    return out


def item_json(item: dict, expanded: bool = True, user: dict | None = None, include_progress: bool = False,
              authors: list[dict] | None = None, series: list[dict] | None = None,
              progress: dict | None = None, progress_known: bool = False) -> dict:
    authors = authors if authors is not None else library.item_authors(item["id"])
    series = series if series is not None else library.item_series(item["id"])
    has_blobs = "audio_files" in item
    if has_blobs:
        audio = db.jload(item["audio_files"], [])
        included = [a for a in audio if not a.get("exclude")]
        chapters = normalize_chapters(db.jload(item["chapters"], []))
        ebook = db.jload(item["ebook_file"], None)
        lib_files = db.jload(item["library_files"], [])
        num_tracks, num_audio, num_chapters, num_files = len(included), len(audio), len(chapters), len(lib_files)
        ebook_format = ebook.get("ebook_format") if ebook else None
    else:
        audio, chapters, ebook, lib_files = [], [], None, []
        num_tracks = int(item.get("num_tracks") or 0)
        num_audio = int(item.get("num_audio_files") or 0)
        num_chapters = int(item.get("num_chapters") or 0)
        num_files = int(item.get("num_files") or 0)
        ebook_format = item.get("ebook_format")
        included = [None] * num_tracks  # only used for emptiness checks
    media = {
        "id": item["id"],
        "libraryItemId": item["id"],
        "metadata": book_metadata(item, authors, series, include_description=expanded),
        "coverPath": item["cover_path"],
        "tags": db.jload(item["tags"], []),
        "numTracks": num_tracks,
        "numAudioFiles": num_audio,
        "numChapters": num_chapters,
        "duration": float(item["duration"] or 0),
        "size": int(item["size"] or 0),
        "ebookFormat": ebook_format,
    }
    out = {
        "id": item["id"],
        "ino": item["ino"],
        "oldLibraryItemId": None,
        "libraryId": item["library_id"],
        "folderId": item["folder_id"],
        "path": item["path"],
        "relPath": item["rel_path"],
        "isFile": bool(item["is_file"]),
        "mtimeMs": int(item["mtime_ms"] or 0),
        "ctimeMs": int(item["ctime_ms"] or 0),
        "birthtimeMs": int(item["birthtime_ms"] or 0),
        "addedAt": int(item["added_at"]),
        "updatedAt": int(item["updated_at"]),
        "lastScan": int(item["last_scan"] or 0),
        "scanVersion": ABS_COMPAT_VERSION,
        "isMissing": bool(item["is_missing"]),
        "isInvalid": num_tracks == 0 and not ebook_format,
        "mediaType": "book",
        "media": media,
        "numFiles": num_files,
        "size": int(item["size"] or 0),
    }
    if expanded:
        if not has_blobs:
            # Detail views must load full row blobs.
            full = db.q1("SELECT audio_files, chapters, ebook_file, library_files, description FROM library_items WHERE id = ?",
                         (item["id"],))
            if full:
                audio = db.jload(full["audio_files"], [])
                chapters = normalize_chapters(db.jload(full["chapters"], []))
                ebook = db.jload(full["ebook_file"], None)
                lib_files = db.jload(full["library_files"], [])
                item = dict(item)
                item["description"] = full.get("description")
                media["metadata"] = book_metadata(item, authors, series, include_description=True)
        media.update({
            "audioFiles": [audio_file_json(a) for a in audio],
            "chapters": chapters,
            "ebookFile": ebook_file_json(ebook),
            "tracks": tracks_json(item, audio),
        })
        out["libraryFiles"] = [library_file_json(f) for f in lib_files]
    if include_progress:
        if progress_known:
            out["userMediaProgress"] = progress_json(progress) if progress else None
        elif user is not None:
            p = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ''",
                      (user["id"], item["id"]))
            out["userMediaProgress"] = progress_json(p) if p else None
    return out


def progress_json(p: dict) -> dict:
    return {
        "id": p["id"], "userId": p["user_id"], "libraryItemId": p["library_item_id"], "episodeId": p["episode_id"] or None,
        "mediaItemId": p["library_item_id"], "mediaItemType": "book", "duration": float(p["duration"] or 0),
        "progress": float(p["progress"] or 0), "currentTime": float(p["current_pos"] or 0), "isFinished": bool(p["is_finished"]),
        "hideFromContinueListening": bool(p["hide_from_continue"]), "ebookLocation": p["ebook_location"],
        "ebookProgress": p["ebook_progress"], "lastUpdate": int(p["last_update"]), "startedAt": int(p["started_at"]),
        "finishedAt": int(p["finished_at"]) if p["finished_at"] else None,
    }


def bookmark_json(b: dict) -> dict:
    return {"libraryItemId": b["library_item_id"], "title": b["title"] or "", "time": b["time"], "createdAt": int(b["created_at"])}


def library_json(lib: dict) -> dict:
    return {
        "id": lib["id"], "name": lib["name"],
        "folders": [{"id": f["id"], "fullPath": f["fullPath"], "libraryId": lib["id"], "addedAt": f.get("addedAt", lib["created_at"]),
                     "locked": bool(f.get("locked"))}
                    for f in db.jload(lib["folders"], [])],
        "displayOrder": lib["display_order"], "icon": lib["icon"], "mediaType": lib["media_type"], "provider": library.library_settings(lib)["metadataProvider"],
        "settings": library.library_settings(lib), "lastScan": lib["last_scan"], "lastScanVersion": ABS_COMPAT_VERSION,
        "createdAt": int(lib["created_at"]), "lastUpdate": int(lib["updated_at"]),
    }


def author_json(a: dict, num_books: int | None = None) -> dict:
    out = {"id": a["id"], "asin": a["asin"], "name": a["name"], "lastFirst": a.get("last_first") or last_first(a["name"]),
           "description": a["description"], "imagePath": a["image_path"], "libraryId": a["library_id"],
           "addedAt": int(a["added_at"]), "updatedAt": int(a["updated_at"])}
    if num_books is not None:
        out["numBooks"] = num_books
    return out


def series_items(series_id: str) -> list[dict]:
    rows = db.q("SELECT li.*, bs.sequence AS series_sequence FROM book_series bs JOIN library_items li ON li.id = bs.item_id "
                "WHERE bs.series_id = ? AND li.is_missing = 0", (series_id,))
    return sort_by_sequence(rows)


def sort_by_sequence(rows: list[dict]) -> list[dict]:
    def key(r):
        seq = r.get("series_sequence")
        try:
            return (0, float(seq), r["title"].lower())
        except (TypeError, ValueError):
            return (1, 0.0, (seq or "") + r["title"].lower())
    return sorted(rows, key=key)


def series_json(s: dict, books: list[dict] | None = None, user: dict | None = None, minified: bool = False,
                authors_by_id: dict | None = None, series_by_id: dict | None = None) -> dict:
    books = books if books is not None else series_items(s["id"])
    if authors_by_id is None or series_by_id is None:
        authors_by_id, series_by_id = library.bulk_authors_series([b["id"] for b in books])
    total_duration = sum(float(b["duration"] or 0) for b in books)
    num_books = len(books)

    def _book_json(b: dict) -> dict:
        j = item_json(b, expanded=False, authors=authors_by_id.get(b["id"], []), series=series_by_id.get(b["id"], []))
        j["media"]["metadata"]["series"] = {"id": s["id"], "name": s["name"], "sequence": b.get("series_sequence")}
        j["sequence"] = b.get("series_sequence")
        return j

    if minified:
        cover = next((b for b in books if b.get("cover_path")), books[0] if books else None)
        book_json = [_book_json(cover)] if cover else []
    else:
        book_json = [_book_json(b) for b in books]
    out = {
        "id": s["id"], "name": s["name"], "nameIgnorePrefix": title_ignore_prefix(s["name"]),
        "nameIgnorePrefixSort": title_ignore_prefix(s["name"]), "type": "series", "description": s.get("description"),
        "libraryId": s["library_id"], "addedAt": int(s["added_at"]), "updatedAt": int(s["updated_at"]),
        "books": book_json, "numBooks": num_books, "totalDuration": total_duration,
    }
    if "finishedBooks" in s:
        out["finishedBooks"] = s["finishedBooks"]
        out["completion"] = s.get("completion")
    return out


def collection_json(c: dict, user: dict | None = None, expanded: bool = True) -> dict:
    rows = db.q("SELECT li.* FROM collection_books cb JOIN library_items li ON li.id = cb.library_item_id "
                "WHERE cb.collection_id = ? ORDER BY cb.position", (c["id"],))
    authors_by_id, series_by_id = library.bulk_authors_series([r["id"] for r in rows])
    books = [item_json(r, expanded=expanded, authors=authors_by_id.get(r["id"], []), series=series_by_id.get(r["id"], []))
             for r in rows]
    return {"id": c["id"], "libraryId": c["library_id"], "name": c["name"], "description": c["description"], "cover": None,
            "coverFullPath": None, "books": books,
            "lastUpdate": int(c["updated_at"]), "createdAt": int(c["created_at"])}


def playlist_json(p: dict, expanded: bool = False) -> dict:
    rows = db.q("SELECT pi.episode_id, li.* FROM playlist_items pi JOIN library_items li ON li.id = pi.library_item_id "
                "WHERE pi.playlist_id = ? ORDER BY pi.position", (p["id"],))
    authors_by_id, series_by_id = library.bulk_authors_series([r["id"] for r in rows])
    items = [{"libraryItemId": r["id"], "episodeId": None,
              "libraryItem": item_json(r, expanded=expanded, authors=authors_by_id.get(r["id"], []),
                                       series=series_by_id.get(r["id"], []))} for r in rows]
    return {"id": p["id"], "libraryId": p["library_id"], "userId": p["user_id"], "name": p["name"], "description": p["description"],
            "coverPath": None, "items": items, "lastUpdate": int(p["updated_at"]), "createdAt": int(p["created_at"])}


def session_json(s: dict, item_payload: dict | None = None) -> dict:
    out = {
        "id": s["id"], "userId": s["user_id"], "libraryId": s["library_id"], "libraryItemId": s["library_item_id"],
        "bookId": s["library_item_id"], "episodeId": s["episode_id"], "mediaType": s["media_type"],
        "mediaMetadata": db.jload(s["media_metadata"], {}), "chapters": normalize_chapters(db.jload(s["chapters"], [])),
        "displayTitle": s["display_title"], "displayAuthor": s["display_author"], "coverPath": s["cover_path"],
        "duration": float(s["duration"] or 0), "playMethod": s["play_method"], "mediaPlayer": s["media_player"],
        "deviceInfo": db.jload(s["device_info"], None) or {}, "serverVersion": ABS_COMPAT_VERSION, "date": s["session_date"],
        "dayOfWeek": s["day_of_week"], "timeListening": float(s["time_listening"] or 0), "startTime": float(s["start_time"] or 0),
        "currentTime": float(s["current_pos"] or 0), "startedAt": int(s["started_at"]), "updatedAt": int(s["updated_at"]),
    }
    if item_payload is not None:
        out.update(item_payload)
    return out


def server_settings_json() -> dict:
    s = db.get_setting("server_settings", {}) or {}
    return {
        "id": "server-settings", "scannerFindCovers": False, "scannerCoverProvider": "google", "scannerParseSubtitle": False,
        "scannerPreferMatchedMetadata": False, "scannerDisableWatcher": True, "storeCoverWithItem": False,
        "storeMetadataWithItem": True, "metadataFileFormat": "json", "rateLimitLoginRequests": 10, "rateLimitLoginWindow": 600000,
        "allowIframe": False, "backupPath": "", "backupSchedule": False, "backupsToKeep": 2, "maxBackupSize": 1,
        "loggerDailyLogsToKeep": 7, "loggerScannerLogsToKeep": 2, "homeBookshelfView": 1, "bookshelfView": 1,
        "podcastEpisodeSchedule": "0 * * * *", "sortingIgnorePrefix": bool(s.get("sortingIgnorePrefix", True)),
        "sortingPrefixes": ["the", "a"], "chromecastEnabled": False, "dateFormat": "MM/dd/yyyy", "timeFormat": "HH:mm",
        "language": "en-us", "logLevel": 2, "version": ABS_COMPAT_VERSION, "buildNumber": 1,
        "authLoginCustomMessage": None, "authActiveAuthMethods": ["local"], "authOpenIDAutoLaunch": False,
        "authOpenIDAutoRegister": False, "authOpenIDButtonText": "Login with OpenId",
    }


def user_json(user: dict, minimal: bool = False) -> dict:
    progress = [] if minimal else [progress_json(p) for p in db.q("SELECT * FROM media_progress WHERE user_id = ?", (user["id"],))]
    bookmarks = [] if minimal else [bookmark_json(b) for b in db.q("SELECT * FROM bookmarks WHERE user_id = ? ORDER BY created_at", (user["id"],))]
    # Legacy non-expiring token: omit from list/admin views; include for full ABS client payloads.
    return {
        "id": user["id"], "username": user["username"], "email": None, "type": user["type"],
        "token": None if minimal else user["token"], "mediaProgress": progress, "seriesHideFromContinueListening": [], "bookmarks": bookmarks,
        "isActive": bool(user["is_active"]), "isLocked": False, "lastSeen": user["last_seen"], "createdAt": int(user["created_at"]),
        "permissions": auth.permissions(user), "librariesAccessible": db.jload(user["libraries_accessible"], []),
        "itemTagsSelected": [], "hasOpenIDLink": False,
    }
