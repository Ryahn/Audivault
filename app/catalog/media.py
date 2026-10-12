"""Editing book metadata, applying provider matches, covers and author info."""

import logging
import shutil
from pathlib import Path

from .. import providers
from ..core import db, tasks
from ..core.config import METADATA_DIR
from ..providers import audible
from ..providers.http import get_bytes
from ..scanner import metafiles
from ..scanner.parse import clean_sequence, parse_names, title_ignore_prefix
from . import library

log = logging.getLogger("abstream.media")


def clear_cover_thumbnails(item_id: str) -> None:
    """Drop cached grid JPEGs so the next view rebuilds them from the current cover."""
    shutil.rmtree(METADATA_DIR / "cache" / "covers" / item_id, ignore_errors=True)

SIMPLE_FIELDS = {
    "title": "title", "subtitle": "subtitle", "publisher": "publisher", "publishedYear": "published_year",
    "publishedDate": "published_date", "description": "description", "isbn": "isbn", "asin": "asin", "language": "language",
}


def get_item(item_id: str) -> dict | None:
    return db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))


def update_media(item_id: str, payload: dict) -> bool:
    """Apply an Audiobookshelf-style media update payload. Returns True if anything changed."""
    item = get_item(item_id)
    if not item:
        raise ValueError("Item not found")
    md = payload.get("metadata") or {}
    fields: dict = {}
    for src, col in SIMPLE_FIELDS.items():
        if src in md:
            value = md[src]
            value = str(value).strip() if value is not None else None
            if col == "title" and not value:
                continue
            if (value or None) != item[col]:
                fields[col] = value or None
    for key in ("explicit", "abridged"):
        if key in md and int(bool(md[key])) != item[key]:
            fields[key] = int(bool(md[key]))
    for key in ("narrators", "genres"):
        if key in md and md[key] is not None:
            value = [str(v).strip() for v in md[key] if str(v).strip()]
            if value != db.jload(item[key], []):
                fields[key] = db.jdump(value)
    if "tags" in payload and payload["tags"] is not None:
        tags = [str(t).strip() for t in payload["tags"] if str(t).strip()]
        if tags != db.jload(item["tags"], []):
            fields["tags"] = db.jdump(tags)
    if "chapters" in payload and payload["chapters"] is not None:
        chapters = [{"id": i, "start": float(c["start"]), "end": float(c["end"]), "title": c.get("title") or f"Chapter {i + 1}"}
                    for i, c in enumerate(sorted(payload["chapters"], key=lambda c: float(c["start"])))]
        fields["chapters"] = db.jdump(chapters)
        fields["num_chapters"] = len(chapters)
    if "title" in fields:
        fields["title_sort"] = title_ignore_prefix(fields["title"]).lower()

    changed = bool(fields)
    with db.tx():
        if "authors" in md and md["authors"] is not None:
            names = [a["name"] if isinstance(a, dict) else str(a) for a in md["authors"]]
            names = [n.strip() for n in names if n and n.strip()]
            if names != [a["name"] for a in library.item_authors(item_id)]:
                library.set_item_authors(item_id, item["library_id"], names)
                changed = True
        if "series" in md and md["series"] is not None:
            series = []
            for s in md["series"]:
                if isinstance(s, dict) and s.get("name"):
                    series.append((s["name"].strip(), clean_sequence(s.get("sequence"))))
            current = [(s["name"], s["sequence"]) for s in library.item_series(item_id)]
            if sorted(series, key=str) != sorted(current, key=str):
                library.set_item_series(item_id, item["library_id"], series)
                changed = True
        if changed:
            fields["updated_at"] = db.now_ms()
            db.update("library_items", item_id, fields)
    if changed:
        library.cleanup_orphans(item["library_id"])
        write_metadata_file(item_id)
        from . import fts
        fts.sync_item(item_id)
        from ..core.library_cache import invalidate_library_cache
        invalidate_library_cache(item["library_id"])
    return changed


def write_metadata_file(item_id: str) -> None:
    item = get_item(item_id)
    if not item or item["is_file"] or item["is_missing"]:
        return
    lib = library.get_library(item["library_id"])
    if not library.library_settings(lib).get("writeMetadataJson"):
        return
    path = Path(item["path"]) / "metadata.json"
    try:
        metafiles.write_abs_metadata_json(
            item, [a["name"] for a in library.item_authors(item_id)],
            [(s["name"], s["sequence"]) for s in library.item_series(item_id)], path)
        _sync_file_list(item_id)
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)


def _sync_file_list(item_id: str) -> None:
    from ..scanner.scanner import rescan_item
    try:
        rescan_item(item_id, force=False)
    except Exception as e:
        log.warning("File list refresh failed for %s: %s", item_id, e)


def save_cover_bytes(item_id: str, data: bytes, ext: str) -> str:
    item = get_item(item_id)
    lib = library.get_library(item["library_id"])
    ext = ext.lower().lstrip(".")
    ext = "jpg" if ext in ("jpeg", "") else ext
    if ext not in ("jpg", "png", "webp", "gif"):
        raise ValueError("Unsupported cover type")
    if not data or len(data) > 8 * 1024 * 1024:
        raise ValueError("Cover too large or empty")
    from . import locks
    if (not item["is_file"] and library.library_settings(lib).get("storeCoverWithItem") and Path(item["path"]).is_dir()
            and not locks.path_locked(item["path"])):
        for old in Path(item["path"]).glob("cover.*"):
            try:
                old.unlink()
            except OSError:
                pass
        target = Path(item["path"]) / f"cover.{ext}"
    else:
        target_dir = METADATA_DIR / "items" / item_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for old in target_dir.glob("cover.*"):
            old.unlink(missing_ok=True)
        target = target_dir / f"cover.{ext}"
    target.write_bytes(data)
    db.ex("UPDATE library_items SET cover_path = ?, updated_at = ? WHERE id = ?", (str(target), db.now_ms(), item_id))
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(item["library_id"])
    clear_cover_thumbnails(item_id)
    if target.parent == Path(item["path"]):
        _sync_file_list(item_id)
    return str(target)


def _ext_from(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    for key, ext in (("png", "png"), ("webp", "webp"), ("gif", "gif"), ("jpeg", "jpg"), ("jpg", "jpg")):
        if key in ct:
            return ext
    suffix = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "jpeg", "png", "webp", "gif") else "jpg"


def set_cover_from_url(item_id: str, url: str) -> str | None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Invalid cover URL")
    res = get_bytes(url)
    if not res:
        return None
    data, ctype = res
    if not ctype.startswith("image/") and not data[:4] in (b"\x89PNG", b"RIFF") and not data[:2] == b"\xff\xd8":
        return None
    return save_cover_bytes(item_id, data, _ext_from(ctype, url))


def match_payload(result: dict, item: dict, override: bool = True) -> dict:
    """Build an update payload from a provider result."""
    md: dict = {}
    current_authors = library.item_authors(item["id"])
    current_series = library.item_series(item["id"])

    def take(key, value, current):
        if value in (None, "", []):
            return
        if override or not current:
            md[key] = value

    take("title", result.get("title"), item["title"])
    take("subtitle", result.get("subtitle"), item["subtitle"])
    take("publisher", result.get("publisher"), item["publisher"])
    take("publishedYear", result.get("publishedYear"), item["published_year"])
    take("description", result.get("description"), item["description"])
    take("isbn", result.get("isbn"), item["isbn"])
    take("asin", result.get("asin"), item["asin"])
    take("language", result.get("language"), item["language"])
    if result.get("author"):
        take("authors", [{"name": n} for n in parse_names(result["author"])], current_authors)
    if result.get("narrator"):
        take("narrators", parse_names(result["narrator"]), db.jload(item["narrators"], []))
    if result.get("genres"):
        take("genres", result["genres"], db.jload(item["genres"], []))
    if result.get("series"):
        take("series", [{"name": s["series"], "sequence": s.get("sequence")} for s in result["series"]], current_series)
    if "explicit" in result and override:
        md["explicit"] = bool(result.get("explicit"))
    if "abridged" in result and override:
        md["abridged"] = bool(result.get("abridged"))
    payload = {"metadata": md}
    if result.get("tags") and (override or not db.jload(item["tags"], [])):
        payload["tags"] = result["tags"]
    return payload


def apply_match(item_id: str, result: dict, override: bool = True, update_cover: bool = True) -> bool:
    item = get_item(item_id)
    changed = update_media(item_id, match_payload(result, item, override))
    if update_cover and result.get("cover") and (override or not item["cover_path"]):
        try:
            if set_cover_from_url(item_id, result["cover"]):
                changed = True
        except Exception as e:
            log.warning("Cover download failed: %s", e)
    return changed


def quick_match(item_id: str, provider: str | None = None, override: bool = False, announce: bool = True) -> dict:
    item = get_item(item_id)
    if not item:
        raise ValueError("Item not found")
    lib = library.get_library(item["library_id"])
    provider = provider or library.library_settings(lib)["metadataProvider"]
    authors = library.item_authors(item_id)
    results = providers.search_books(provider, item["title"], authors[0]["name"] if authors else None, asin=item["asin"])
    if not results:
        return {"updated": False, "warning": "No match found"}
    updated = apply_match(item_id, results[0], override=override)
    if announce:
        tasks.activity(f"Matched \"{item['title']}\" via {provider}")
    return {"updated": updated, "result": results[0]}


REFRESH_BATCH = 10
REFRESH_PAUSE = 5


def _missing_metadata(item: dict, series_ids: set[str]) -> bool:
    if not (item["description"] or "").strip():
        return True
    if not db.jload(item["genres"], []):
        return True
    if not db.jload(item["narrators"], []):
        return True
    if not item["cover_path"]:
        return True
    return item["id"] not in series_ids


def refresh_library_metadata(task, library_id: str) -> dict:
    """Fill empty description, genres, narrator, series, and cover. Existing values stay."""
    import time

    from ..core import events

    rows = db.q(
        "SELECT id, description, genres, narrators, cover_path FROM library_items WHERE library_id = ? AND is_missing = 0",
        (library_id,),
    )
    series_ids = {r["item_id"] for r in db.q(
        "SELECT DISTINCT bs.item_id FROM book_series bs JOIN library_items li ON li.id = bs.item_id "
        "WHERE li.library_id = ? AND li.is_missing = 0",
        (library_id,),
    )}
    todo = [r["id"] for r in rows if _missing_metadata(r, series_ids)]
    task["total"] = len(todo)
    updated = 0
    for n, item_id in enumerate(todo):
        try:
            if quick_match(item_id, override=False, announce=False).get("updated"):
                updated += 1
        except Exception as e:
            log.warning("Metadata refresh failed for %s: %s", item_id, e)
        task["progress"] = n + 1
        task["message"] = f"{n + 1} of {len(todo)}"
        if (n + 1) % REFRESH_BATCH == 0 and n + 1 < len(todo):
            time.sleep(REFRESH_PAUSE)
    lib = library.get_library(library_id)
    if lib:
        raw = db.jload(lib["settings"], {}) or {}
        raw["lastMetadataRefresh"] = db.now_ms()
        db.update("libraries", library_id, {"settings": db.jdump(raw), "updated_at": db.now_ms()})
    if updated:
        events.publish({"type": "library_changed", "libraryId": library_id, "added": 0, "updated": updated, "missing": 0})
    tasks.activity(f"Metadata refresh filled {updated} of {len(todo)} book(s)")
    return {"checked": len(todo), "updated": updated}


def match_author(author_id: str, region: str = "us", asin: str | None = None, name: str | None = None) -> dict | None:
    author = db.q1("SELECT * FROM authors WHERE id = ?", (author_id,))
    if not author:
        raise ValueError("Author not found")
    info = audible.author_by_asin(asin, region) if asin else audible.author_search(name or author["name"], region)
    if not info:
        return None
    fields = {"asin": info["asin"], "description": info.get("description") or author["description"], "updated_at": db.now_ms()}
    if info.get("image"):
        res = get_bytes(info["image"])
        if res:
            data, ctype = res
            target = METADATA_DIR / "authors" / f"{author_id}.{_ext_from(ctype, info['image'])}"
            target.write_bytes(data)
            fields["image_path"] = str(target)
    db.update("authors", author_id, fields)
    return db.q1("SELECT * FROM authors WHERE id = ?", (author_id,))
