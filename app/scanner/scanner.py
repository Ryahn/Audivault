"""Library scanner: discovers book folders / loose files and keeps the database in sync."""

import hashlib
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..catalog import library
from ..core import db, events, tasks
from ..core.config import AUDIO_EXTS, EBOOK_EXTS, IMAGE_EXTS, METADATA_DIR, METADATA_EXTS
from . import cue, metafiles, probe
from .parse import book_data_from_dir, clean_sequence, fold_embedded_series, label_key, natural_key, parse_names, split_embedded_series

log = logging.getLogger("abstream.scanner")
PROBE_WORKERS = max(1, min(8, (os.cpu_count() or 4)))
SCAN_ITEM_COLS = (
    "id, library_id, folder_id, path, rel_path, ino, is_file, mtime_ms, ctime_ms, birthtime_ms, "
    "size, added_at, updated_at, last_scan, is_missing, title, cover_path, duration, "
    "audio_files, chapters, ebook_file, library_files, num_tracks, num_audio_files, num_chapters, num_files"
)

DISC_DIR = re.compile(r"^(cd|dis[ck]|part)\s*\d{1,3}$", re.I)
COVER_NAMES = ("cover", "folder", "front", "poster")


def _ext(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def file_type(name: str) -> str:
    ext = _ext(name)
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in EBOOK_EXTS:
        return "ebook"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in METADATA_EXTS:
        return "metadata" if ext in ("opf", "nfo", "json", "abs", "xml", "cue") else "text"
    return "unknown"


def ino_of(path: str, st: os.stat_result | None = None) -> str:
    try:
        st = st or os.stat(path)
        if st.st_ino:
            return str(st.st_ino)
    except OSError:
        pass
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def _stat_file(path: Path, root: Path, now: int) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", None) or st.st_ctime
    return {
        "ino": ino_of(str(path), st),
        "path": str(path),
        "rel_path": path.relative_to(root).as_posix(),
        "filename": path.name,
        "ext": path.suffix.lower(),
        "size": st.st_size,
        "mtime_ms": int(st.st_mtime * 1000),
        "ctime_ms": int(st.st_ctime * 1000),
        "birthtime_ms": int(birth * 1000),
        "added_at": now,
        "updated_at": now,
        "file_type": file_type(path.name),
    }


# ------------------------------------------------------------------ discovery

def discover(folder_path: str, scopes: list[str] | None = None) -> dict[str, dict]:
    """Return {item_rel_path: {"is_file": bool, "files": [abs paths]}} for a library folder.

    - A file of a media type directly in the folder root is a loose single-file item.
    - Any other directory holding audio/ebook files is an item; disc folders
      ("CD 1", "Disc 2") are merged into their parent.
    - `scopes` limits discovery to these top-level entries (partial scan).
    """
    root = Path(folder_path)
    items: dict[str, dict] = {}
    if not root.is_dir():
        return items
    if scopes is None:
        starts = [root]
    else:
        starts = []
        for scope in scopes:
            if scope.startswith("."):  # hidden folders such as .abstream-trash are never library content
                continue
            p = root / scope
            if p.is_file():
                if _ext(p.name) in AUDIO_EXTS or _ext(p.name) in EBOOK_EXTS:
                    items[p.name] = {"is_file": True, "files": [str(p)]}
            elif p.is_dir():
                starts.append(p)
    for start in starts:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("@eaDir", "#recycle", "$RECYCLE.BIN")]
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            if Path(dirpath, ".ignore").exists():
                dirnames[:] = []
                continue
            media = [f for f in filenames if _ext(f) in AUDIO_EXTS or _ext(f) in EBOOK_EXTS]
            if not media:
                continue
            if rel_dir == "":
                for f in media:
                    items[f] = {"is_file": True, "files": [str(root / f)]}
                continue
            item_rel = rel_dir
            parts = rel_dir.split("/")
            if len(parts) > 1 and DISC_DIR.match(parts[-1]):
                item_rel = "/".join(parts[:-1])
            entry = items.setdefault(item_rel, {"is_file": False, "files": []})
            entry["files"].extend(str(Path(dirpath) / f) for f in filenames if not f.startswith("."))
    # Pull non-media siblings (covers, opf...) of the item dir itself when the
    # media lives in disc subfolders.
    for rel, entry in items.items():
        if entry["is_file"]:
            continue
        item_dir = root / rel
        try:
            for f in item_dir.iterdir():
                if f.is_file() and not f.name.startswith(".") and str(f) not in entry["files"]:
                    entry["files"].append(str(f))
        except OSError:
            pass
    return items


_listing_guard = threading.Lock()
_listings: dict[str, dict] = {}
_listing_gen: dict[str, int] = {}
_listing_locks: dict[str, threading.Lock] = {}


def _listing_key(folder_path: str) -> str:
    return os.path.normcase(os.path.abspath(folder_path))


def _listing_lock(key: str) -> threading.Lock:
    with _listing_guard:
        lock = _listing_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _listing_locks[key] = lock
        return lock


def has_discover_cache(folder_path: str) -> bool:
    key = _listing_key(folder_path)
    with _listing_guard:
        return key in _listings


def invalidate_discover(folder_path: str) -> None:
    """Drop a cached folder listing. The next read walks the disk again."""
    key = _listing_key(folder_path)
    with _listing_guard:
        _listing_gen[key] = _listing_gen.get(key, 0) + 1
        _listings.pop(key, None)


def cached_discover(folder_path: str, scopes: list[str] | None = None, *, fresh: bool = False) -> dict:
    """Same result as `discover`, reused until the folder is invalidated.

    A partial scan (`scopes`) is never cached. Callers must not mutate the
    returned dict. Concurrent callers share one walk.
    """
    if scopes is not None:
        return discover(folder_path, scopes)
    key = _listing_key(folder_path)
    if fresh:
        invalidate_discover(folder_path)
    with _listing_lock(key):
        with _listing_guard:
            gen = _listing_gen.get(key, 0)
            hit = _listings.get(key)
        if hit is not None:
            return hit
        found = discover(folder_path)
        with _listing_guard:
            if _listing_gen.get(key, 0) == gen:
                _listings[key] = found
        return found


# ------------------------------------------------------------------ building

def _sort_audio(audio: list[dict]) -> list[dict]:
    keys = [(a.get("disc") or 0, a.get("track")) for a in audio]
    if all(k[1] for k in keys) and len(set(keys)) == len(keys):
        audio.sort(key=lambda a: (a.get("disc") or 0, a["track"], natural_key(a["rel_path"])))
    else:
        audio.sort(key=lambda a: natural_key(a["rel_path"]))
    for i, a in enumerate(audio, start=1):
        a["index"] = i
    return audio


def build_chapters(audio: list[dict]) -> list[dict]:
    chapters = []
    if len(audio) == 1 and audio[0].get("chapters"):
        chapters = [dict(c) for c in audio[0]["chapters"]]
    elif audio and all(a.get("chapters") for a in audio):
        offset = 0.0
        for a in audio:
            for c in a["chapters"]:
                chapters.append({"start": offset + c["start"], "end": offset + c["end"], "title": c["title"]})
            offset += a["duration"]
    else:
        offset = 0.0
        for a in audio:
            title = (a.get("tags") or {}).get("title") if len(audio) > 1 else None
            chapters.append({"start": offset, "end": offset + a["duration"], "title": title or Path(a["filename"]).stem})
            offset += a["duration"]
    out = []
    for i, c in enumerate(chapters):
        out.append({"id": i, "start": round(float(c["start"]), 3), "end": round(float(c["end"]), 3), "title": c["title"] or f"Chapter {i + 1}"})
    return out


def _metadata_from_tags(tags: dict, single_file: bool = True) -> dict:
    out: dict = {}
    if not tags:
        return out
    # A track title only names the book when the book is a single file.
    # Album tags are often "Title: Series, Book N" while the track title is the book.
    album = (tags.get("album") or "").strip()
    track = (tags.get("title") or "").strip() if single_file else ""
    short, emb_series, _seq = split_embedded_series(album)
    if emb_series and (not track or label_key(track) == label_key(short)):
        title = short
    else:
        title = album or track
    if title:
        out["title"] = title
    if tags.get("subtitle"):
        out["subtitle"] = tags["subtitle"]
    author = tags.get("album_artist") or tags.get("artist")
    if author:
        out["authors"] = parse_names(author)
    narr = tags.get("narrator") or tags.get("composer")
    if narr:
        out["narrators"] = parse_names(narr)
    if tags.get("date"):
        m = re.search(r"\d{4}", tags["date"])
        if m:
            out["published_year"] = m.group(0)
    for key in ("publisher", "description", "isbn", "asin", "language"):
        if tags.get(key):
            out[key] = tags[key]
    if tags.get("genre"):
        out["genres"] = [g.strip() for g in re.split(r"[;/,]", tags["genre"]) if g.strip()]
    series = tags.get("series")
    seq = tags.get("series_part")
    if not series and tags.get("grouping"):
        m = re.match(r"^(.*?)(?:,?\s*(?:#|book|vol\.?|volume)\s*([\d.]+))$", tags["grouping"], re.I)
        if m:
            series, seq = m.group(1).strip(), m.group(2)
    if series:
        out["series_list"] = [(series, clean_sequence(seq))]
    return out


def _sidecar_metadata(item_dir: Path, known_files: list[str] | None) -> list[dict]:
    """Sidecar metadata. `known_files` skips a directory listing when the walk already has the names."""
    sources = []
    if known_files is None:
        sources.append(metafiles.read_text_files(item_dir))
        opf = next(iter(sorted(item_dir.glob("*.opf"))), None)
        if opf:
            sources.append(metafiles.read_opf(opf))
        mj = item_dir / "metadata.json"
        if mj.is_file():
            sources.append(metafiles.read_abs_metadata_json(mj))
        return sources
    by_name = {Path(p).name.lower(): Path(p) for p in known_files}
    if "desc.txt" in by_name or "reader.txt" in by_name:
        sources.append(metafiles.read_text_files(item_dir))
    opf = next((path for name, path in sorted(by_name.items()) if name.endswith(".opf")), None)
    if opf:
        sources.append(metafiles.read_opf(opf))
    mj = by_name.get("metadata.json")
    if mj:
        sources.append(metafiles.read_abs_metadata_json(mj))
    return sources


def gather_metadata(lib_settings: dict, rel_path: str, is_file: bool, item_dir: Path | None, audio: list[dict],
                    known_files: list[str] | None = None) -> dict:
    """Merge metadata sources from lowest to highest priority."""
    folder_rel = rel_path
    if is_file:
        folder_rel = Path(rel_path).stem
    folder = book_data_from_dir(folder_rel, lib_settings.get("parseSubtitle", False))
    folder_md = {"title": folder["title"], "subtitle": folder["subtitle"], "asin": folder["asin"],
                 "authors": folder["authors"], "narrators": folder["narrators"], "published_year": folder["published_year"]}
    if folder["series"]:
        folder_md["series_list"] = [(folder["series"], folder["sequence"])]
    folder_md = {k: v for k, v in folder_md.items() if v}

    sources = []
    tag_md = _metadata_from_tags(audio[0].get("tags") if audio else {}, single_file=len(audio) == 1)
    if lib_settings.get("preferFolderMetadata"):
        sources += [tag_md, folder_md]
    else:
        sources += [folder_md, tag_md]
    if item_dir:
        sources.extend(_sidecar_metadata(item_dir, known_files))

    merged: dict = {}
    for src in sources:
        for k, v in src.items():
            if v not in (None, "", []):
                merged[k] = v
    if not merged.get("title"):
        merged["title"] = Path(rel_path).stem
    album = (audio[0].get("tags") or {}).get("album") if audio else None
    fold_embedded_series(merged, album)
    return merged


def _find_cover(files: list[dict]) -> str | None:
    images = [f for f in files if f["file_type"] == "image"]
    if not images:
        return None
    for name in COVER_NAMES:
        for f in images:
            if Path(f["filename"]).stem.lower() == name:
                return f["path"]
    return sorted(images, key=lambda f: (f["rel_path"].count("/"), natural_key(f["filename"])))[0]["path"]


def _extract_cover(item_id: str, audio: list[dict]) -> str | None:
    for a in audio:
        if not a.get("embedded_cover"):
            continue
        res = probe.extract_embedded_cover(a["path"])
        if res:
            data, ext = res
            target = METADATA_DIR / "items" / item_id / f"cover.{ext}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return str(target)
    return None


def _probe_one(st: dict) -> dict:
    info = probe.probe_audio(st["path"])
    tags = info["tags"]
    entry = dict(st)
    entry.update({
        "duration": info["duration"], "bit_rate": info["bit_rate"], "codec": info["codec"], "channels": info["channels"],
        "channel_layout": info["channel_layout"], "format": info["format"], "mime_type": info["mime_type"],
        "track": tags.get("track"), "disc": tags.get("disc"), "embedded_cover": info["embedded_cover"],
        "tags": tags, "chapters": info["chapters"], "error": info["error"], "exclude": False,
    })
    return entry


def _probe_files(audio_stats: list[dict], previous: list[dict]) -> list[dict]:
    prev = {(a["path"], a["size"], a["mtime_ms"]): a for a in previous}
    out: list[dict | None] = [None] * len(audio_stats)
    to_probe: list[tuple[int, dict]] = []
    for i, st in enumerate(audio_stats):
        cached = prev.get((st["path"], st["size"], st["mtime_ms"]))
        if cached:
            entry = dict(cached)
            entry.update({k: st[k] for k in ("ino", "rel_path", "filename", "ext")})
            out[i] = entry
        else:
            to_probe.append((i, st))
    if to_probe:
        workers = min(PROBE_WORKERS, len(to_probe))
        if workers == 1:
            for i, st in to_probe:
                out[i] = _probe_one(st)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_probe_one, st): i for i, st in to_probe}
                for fut in as_completed(futures):
                    out[futures[fut]] = fut.result()
    return out


def find_cue(files: list[dict]) -> Path | None:
    cues = sorted((f for f in files if f["ext"] == ".cue"), key=lambda f: (f["rel_path"].count("/"), natural_key(f["filename"])))
    return Path(cues[0]["path"]) if cues else None


def _cue_signature(files) -> list:
    return sorted((f["path"], f["size"], f["mtime_ms"]) for f in files if f.get("ext") == ".cue")


def compute_chapters(settings: dict, files: list[dict], included: list[dict], json_chapters: list[dict] | None) -> list[dict]:
    """Chapter priority: .cue (when the library enables it) > metadata.json > embedded / one per file."""
    if settings.get("useCueChapters"):
        cue_path = find_cue(files)
        if cue_path:
            chapters = cue.chapters_from_cue(cue_path, included)
            if chapters:
                return [{"id": i, **c} for i, c in enumerate(chapters)]
            log.info("Cue sheet %s did not match the audio files; using other chapter sources", cue_path)
    if json_chapters:
        return [{"id": i, "start": c["start"], "end": c["end"], "title": c["title"]} for i, c in enumerate(json_chapters)]
    return build_chapters(included)


def refresh_chapters(item_id: str) -> bool:
    """Re-derive an item's chapters from its files (used when the cue setting changes)."""
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))
    if not item:
        return False
    settings = library.library_settings(library.get_library(item["library_id"]))
    files = db.jload(item["library_files"], [])
    included = [a for a in db.jload(item["audio_files"], []) if not a.get("exclude")]
    json_chapters = None
    if not item["is_file"]:
        mj = Path(item["path"]) / "metadata.json"
        if mj.is_file():
            json_chapters = metafiles.read_abs_metadata_json(mj).get("chapters")
    chapters = compute_chapters(settings, files, included, json_chapters)
    if chapters == db.jload(item["chapters"], []):
        return False
    counts = db.compute_item_counts(
        db.jload(item["audio_files"], []), chapters, db.jload(item["library_files"], []),
        db.jload(item["ebook_file"], None))
    db.update("library_items", item_id, {"chapters": db.jdump(chapters), "updated_at": db.now_ms(), **counts})
    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(item["library_id"])
    return True


def refresh_library_chapters(task: dict | None, library_id: str) -> dict:
    rows = [r for r in db.q("SELECT id, library_files FROM library_items WHERE library_id = ? AND is_missing = 0", (library_id,))
            if any(f.get("ext") == ".cue" for f in db.jload(r["library_files"], []))]
    if task is not None:
        task["total"] = len(rows)
    changed = 0
    for i, r in enumerate(rows):
        changed += refresh_chapters(r["id"])
        if task is not None:
            task["progress"] = i + 1
    return {"updated": changed, "withCue": len(rows)}


def _signature(files: list[dict]) -> list:
    return sorted((f["path"], f["size"], f["mtime_ms"]) for f in files)


def _cover_cache_stale(existing: dict, cover: str | None, stats: list[dict], extracted: bool) -> bool:
    """True when cached grid thumbnails would show a different image than `cover`."""
    previous = existing.get("cover_path") or None
    current = cover or None
    if previous != current or extracted:
        return True
    if not current:
        return False
    old_files = {f["path"]: f for f in db.jload(existing.get("library_files"), [])}
    old = old_files.get(current)
    new = next((s for s in stats if s["path"] == current), None)
    if not old or not new:
        return False
    return old.get("size") != new.get("size") or old.get("mtime_ms") != new.get("mtime_ms")


def scan_item_entry(lib: dict, folder: dict, rel_path: str, entry: dict, existing: dict | None = None,
                    force: bool = False, invalidate_cache: bool = True) -> tuple[str, str]:
    """Create or refresh one item. Returns (item_id, action) where action is
    one of 'added', 'updated', 'unchanged'."""
    settings = library.library_settings(lib)
    root = Path(folder["fullPath"])
    now = db.now_ms()
    item_path = root / rel_path
    stats = [s for s in (_stat_file(Path(p), item_path if not entry["is_file"] else root, now) for p in entry["files"]) if s]
    if entry["is_file"]:
        for s in stats:
            s["rel_path"] = s["filename"]

    if existing and not force and not existing["is_missing"]:
        if _signature(db.jload(existing["library_files"], [])) == _signature(stats):
            return existing["id"], "unchanged"

    item_id = existing["id"] if existing else db.new_id()
    previous_audio = db.jload(existing["audio_files"], []) if existing else []
    prev_files = {f["path"]: f for f in (db.jload(existing["library_files"], []) if existing else [])}
    for s in stats:  # keep original added timestamps
        if s["path"] in prev_files:
            s["added_at"] = prev_files[s["path"]].get("added_at", now)

    audio = _sort_audio(_probe_files([s for s in stats if s["file_type"] == "audio"], previous_audio))
    # Preserve per-file exclusions made by the user
    excluded = {a["path"] for a in previous_audio if a.get("exclude")}
    for a in audio:
        a["exclude"] = a["path"] in excluded
    included = [a for a in audio if not a.get("exclude")]
    duration = round(sum(a["duration"] for a in included), 3)

    ebooks = [s for s in stats if s["file_type"] == "ebook"]
    ebook_file = None
    if ebooks:
        preferred = sorted(ebooks, key=lambda e: ["epub", "pdf", "mobi", "azw3", "cbz", "cbr"].index(e["ext"].lstrip(".")) if e["ext"].lstrip(".") in ["epub", "pdf", "mobi", "azw3", "cbz", "cbr"] else 99)
        ebook_file = dict(preferred[0]) | {"ebook_format": preferred[0]["ext"].lstrip(".")}

    try:
        dst = os.stat(item_path)
        dir_ino = ino_of(str(item_path), dst)
        mtime, ctime, birth = int(dst.st_mtime * 1000), int(dst.st_ctime * 1000), int((getattr(dst, "st_birthtime", None) or dst.st_ctime) * 1000)
    except OSError:
        dir_ino, mtime, ctime, birth = None, now, now, now

    audio_changed = not existing or [(a["path"], a["size"]) for a in previous_audio] != [(a["path"], a["size"]) for a in audio]
    chapters_json = None
    fields = {
        "library_id": lib["id"], "folder_id": folder["id"], "path": str(item_path), "rel_path": rel_path, "ino": dir_ino,
        "is_file": 1 if entry["is_file"] else 0, "mtime_ms": mtime, "ctime_ms": ctime, "birthtime_ms": birth,
        "size": sum(s["size"] for s in stats), "updated_at": now, "last_scan": now, "is_missing": 0,
        "duration": duration, "audio_files": db.jdump(audio), "library_files": db.jdump(stats),
        "ebook_file": db.jdump(ebook_file) if ebook_file else None,
    }

    item_dir = None if entry["is_file"] else item_path
    if not existing or force:
        md = gather_metadata(settings, rel_path, bool(entry["is_file"]), item_dir, included)
        fields.update({
            "title": md["title"], "subtitle": md.get("subtitle"), "publisher": md.get("publisher"),
            "published_year": md.get("published_year"), "published_date": md.get("published_date"),
            "description": md.get("description"), "isbn": md.get("isbn"), "asin": md.get("asin"),
            "language": md.get("language"), "explicit": 1 if md.get("explicit") else 0, "abridged": 1 if md.get("abridged") else 0,
            "narrators": db.jdump(md.get("narrators", [])), "genres": db.jdump(md.get("genres", [])), "tags": db.jdump(md.get("tags", [])),
        })
        from ..scanner.parse import title_ignore_prefix
        fields["title_sort"] = title_ignore_prefix(md["title"]).lower()
        chapters_json = compute_chapters(settings, stats, included, md.get("chapters"))
        fields["chapters"] = db.jdump(chapters_json)
    elif audio_changed or _cue_signature(prev_files.values()) != _cue_signature(stats):
        # metadata.json chapters are not re-read here: a chapter edit made in the app is
        # saved there, and must not be undone by the rescan its own write triggers.
        chapters_json = compute_chapters(settings, stats, included, None)
        fields["chapters"] = db.jdump(chapters_json)

    cover = existing.get("cover_path") if existing else None
    extracted = False
    if force or not cover or not os.path.exists(cover):
        found = _find_cover(stats)
        if found:
            cover = found
        elif cover and os.path.exists(cover):
            pass
        else:
            extracted_path = _extract_cover(item_id, included)
            cover = extracted_path
            extracted = extracted_path is not None
    fields["cover_path"] = cover
    if existing and _cover_cache_stale(existing, cover, stats, extracted):
        from ..catalog.media import clear_cover_thumbnails
        clear_cover_thumbnails(item_id)

    if chapters_json is None:
        chapters_json = db.jload(fields["chapters"], []) if "chapters" in fields else (
            db.jload(existing["chapters"], []) if existing else [])
    fields.update(db.compute_item_counts(audio, chapters_json, stats, ebook_file))

    with db.tx():
        if existing:
            db.update("library_items", item_id, fields)
        else:
            fields.update({"id": item_id, "added_at": now})
            db.insert("library_items", fields)
        if not existing or force:
            library.set_item_authors(item_id, lib["id"], md.get("authors", []))
            library.set_item_series(item_id, lib["id"], md.get("series_list", []))
    from ..catalog import fts
    fts.sync_item(item_id)
    if invalidate_cache:
        from ..core.library_cache import invalidate_library_cache
        invalidate_library_cache(lib["id"])
    return item_id, ("added" if not existing else "updated")


def _scope_of(rel_path: str) -> str:
    return rel_path.replace("\\", "/").split("/", 1)[0]


def scan_library(task: dict | None, library_id: str, force: bool = False, scopes: dict[str, list[str]] | None = None) -> dict:
    """Full scan, or a partial scan when `scopes` maps folder ids to the top-level
    entries (e.g. author folders or loose files) that changed. Either way only
    books whose files changed are re-read; `force` re-reads every book's metadata."""
    lib = library.get_library(library_id)
    if not lib:
        raise ValueError("Library not found")
    partial = scopes is not None
    if not partial:
        tasks.activity(f"Scanning library \"{lib['name']}\"")
    folders = db.jload(lib["folders"], [])
    # Slim columns for discovery/move detection; full blobs loaded when an entry is re-probed.
    rows = db.q(f"SELECT {SCAN_ITEM_COLS} FROM library_items WHERE library_id = ?", (library_id,))
    existing: dict[str, dict] = {}
    found: dict[str, tuple[dict, str, dict]] = {}
    for folder in folders:
        root = Path(folder["fullPath"])
        if not root.is_dir():
            # Unreachable folder (unmounted share, missing drive): leave its books alone
            # instead of marking every one of them missing.
            tasks.activity(f"Library folder not found: {root}", "warning")
            continue
        folder_scopes = None
        if partial:
            folder_scopes = scopes.get(folder["id"])
            if not folder_scopes:
                continue
        for r in rows:
            if r["folder_id"] == folder["id"] and (folder_scopes is None or _scope_of(r["rel_path"]) in folder_scopes):
                existing[r["path"]] = r
        for rel, entry in discover(folder["fullPath"], folder_scopes).items():
            found[str(root / rel)] = (folder, rel, entry)

    # Detect moved folders: a new path whose directory inode matches a missing item.
    missing_by_ino = {r["ino"]: r for p, r in existing.items() if p not in found and r["ino"]}
    stats = {"added": 0, "updated": 0, "missing": 0, "unchanged": 0, "organized": 0, "total": len(found), "partial": partial}
    touched: set[str] = set()
    if task is not None:
        task["total"] = len(found)

    for i, (path, (folder, rel, entry)) in enumerate(sorted(found.items())):
        current = existing.get(path)
        if current is None and not entry["is_file"]:
            ino = ino_of(path)
            moved = missing_by_ino.pop(ino, None) or (_moved_elsewhere(rows, ino, found) if partial else None)
            if moved:
                current = moved
                existing.pop(moved["path"], None)
        try:
            item_id, action = scan_item_entry(lib, folder, rel, entry, current, force=force, invalidate_cache=False)
            stats[action] += 1
            touched.add(item_id)
        except Exception as e:
            log.exception("Failed scanning %s", path)
            tasks.activity(f"Failed scanning {path}: {e}", "error")
        if task is not None:
            task["progress"] = i + 1
            task["message"] = rel

    for path, row in existing.items():
        if path not in found and not row["is_missing"]:
            db.ex("UPDATE library_items SET is_missing = 1, updated_at = ? WHERE id = ?", (db.now_ms(), row["id"]))
            stats["missing"] += 1

    if library.library_settings(lib).get("organizeOnScan"):
        from ..management import organizer  # organizer imports this module
        result = organizer.organize_items(library_id, touched if partial else None, task)
        stats["organized"] = result["moved"]
        if result["failed"]:
            tasks.activity(f"Auto-organize: {len(result['failed'])} folder(s) could not be moved", "warning")

    from ..core.library_cache import invalidate_library_cache
    invalidate_library_cache(library_id)

    if not partial:
        db.ex("UPDATE libraries SET last_scan = ? WHERE id = ?", (db.now_ms(), library_id))
    changed = stats["added"] + stats["updated"] + stats["missing"] + stats["organized"]
    if not partial or changed:
        what = "Folder change scan" if partial else "Scan"
        tasks.activity(f"{what} of \"{lib['name']}\" complete: {stats['added']} added, {stats['updated']} updated, {stats['missing']} missing"
                       + (f", {stats['organized']} organized" if stats["organized"] else ""))
    if changed:
        events.publish({"type": "library_changed", "libraryId": library_id, "added": stats["added"],
                        "updated": stats["updated"], "missing": stats["missing"]})
    if stats["added"] or stats["organized"] or not partial:
        from ..management import duplicates
        duplicates.notify_new()
    return stats


def _moved_elsewhere(rows: list[dict], ino: str, found: dict) -> dict | None:
    """In a partial scan the old location may be outside the scanned scopes:
    accept any item with this inode whose old path no longer exists."""
    for r in rows:
        if r["ino"] == ino and r["path"] not in found and not os.path.exists(r["path"]):
            return r
    return None


def rescan_item(item_id: str, force: bool = True) -> str:
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))
    if not item:
        raise ValueError("Item not found")
    lib = library.get_library(item["library_id"])
    folder = next((f for f in db.jload(lib["folders"], []) if f["id"] == item["folder_id"]), None)
    if not folder:
        raise ValueError("Library folder not found")
    if item["is_file"]:
        entry = {"is_file": True, "files": [item["path"]]}
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(item["path"]):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            files.extend(str(Path(dirpath) / f) for f in filenames if not f.startswith("."))
        entry = {"is_file": False, "files": sorted(files)}
    if not os.path.exists(item["path"]):
        db.ex("UPDATE library_items SET is_missing = 1 WHERE id = ?", (item_id,))
        return "missing"
    _, action = scan_item_entry(lib, folder, item["rel_path"], entry, item, force=force)
    return action


def remove_item_cache(item_id: str) -> None:
    shutil.rmtree(METADATA_DIR / "items" / item_id, ignore_errors=True)
    from ..catalog.media import clear_cover_thumbnails
    clear_cover_thumbnails(item_id)
