"""Duplicate manager: finds copies of the same book (across all libraries),
compares their files, and resolves a group by keeping one copy, optionally
bringing over files from the discarded copies, moving listening history to the
kept copy, and trashing (or deleting) the rest.
"""

import logging
import os
import re
import shutil
import time
from pathlib import Path

from ..catalog import library, locks
from ..core import db, events, fsops, tasks
from ..scanner.parse import split_embedded_series

log = logging.getLogger("abstream.duplicates")

from .trash import TRASH_DIR  # noqa: E402  (shared trash folder name)
# File types that are safe to bring across by default. Audio is never preselected:
# mixing tracks from two different rips would corrupt the kept book.
SIDECAR_TYPES = {"metadata", "text", "ebook"}


# ------------------------------------------------------------------ detection

def _norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\((un)?abridged\)|\[[^\]]*\]", " ", t)
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"[\W_]+", " ", t).strip()


def _core_title(title: str) -> str:
    """'Advance: Animus, Book 4' matches a copy titled 'Advance'."""
    short, series, _seq = split_embedded_series(title)
    return _norm_title(short if series else title)


def _durations_match(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return True
    return abs(a - b) <= max(60.0, 0.02 * min(a, b))


def _chapter_starts(chapters) -> list[float]:
    starts = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        try:
            starts.append(float(chapter.get("start", 0)))
        except (TypeError, ValueError):
            continue
    starts.sort()
    return starts


def _chapters_align(left: list[float], right: list[float], tolerance: float = 8.0) -> bool:
    """True when two copies share the same chapter boundaries.

    One rip often has a short opening credit the other folds into chapter 1,
    so the lists need not be the same length. Most starts in the shorter list
    still land on a start in the longer one.
    """
    left = [s for s in left if s >= 1]
    right = [s for s in right if s >= 1]
    if len(left) < 4 or len(right) < 4:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    matched = 0
    cursor = 0
    for start in shorter:
        while cursor < len(longer) and longer[cursor] < start - tolerance:
            cursor += 1
        if cursor < len(longer) and abs(longer[cursor] - start) <= tolerance:
            matched += 1
            cursor += 1
    return matched >= 4 and matched / len(shorter) >= 0.6


def _recording_candidates(items: list[dict], first_author: dict[str, str], titles: dict[str, str]) -> list[tuple[str, str]]:
    """Books by one author whose runtimes are close enough to compare chapters."""
    by_author: dict[str, list[dict]] = {}
    for item in items:
        author = _norm_name(first_author.get(item["id"], ""))
        duration = float(item["duration"] or 0)
        if author and duration >= 600 and (item.get("num_chapters") or 0) >= 4:
            by_author.setdefault(author, []).append(item)
    pairs = []
    for books in by_author.values():
        books.sort(key=lambda book: float(book["duration"] or 0))
        for index, left in enumerate(books):
            left_dur = float(left["duration"])
            window = max(30.0, 0.005 * left_dur)
            for right in books[index + 1:]:
                if float(right["duration"]) - left_dur > window:
                    break
                if titles.get(left["id"]) == titles.get(right["id"]):
                    continue
                pairs.append((left["id"], right["id"]))
    return pairs


def _norm_name(name: str) -> str:
    return re.sub(r"[\W_]+", "", (name or "").lower())


def _ignored_sets() -> list[set[str]]:
    return [set(g) for g in (db.get_setting("duplicates_ignored", []) or [])]


def _group_key(ids) -> str:
    return ",".join(sorted(ids))


_DUP_MATCH_COLS = (
    "id, library_id, folder_id, path, rel_path, is_file, title, subtitle, asin, narrators, "
    "cover_path, duration, size, added_at, updated_at, num_tracks, num_audio_files, num_chapters, num_files"
)


def find_groups(include_ignored: bool = False, analyze: bool = False) -> list[dict]:
    items = db.q(f"SELECT {_DUP_MATCH_COLS} FROM library_items WHERE is_missing = 0")
    if not items:
        return []
    first_author: dict[str, str] = {}
    for r in db.q("SELECT ba.item_id, a.name FROM book_authors ba JOIN authors a ON a.id = ba.author_id ORDER BY ba.position DESC"):
        first_author[r["item_id"]] = r["name"]  # DESC order: position 0 written last

    parent = {i["id"]: i["id"] for i in items}
    reasons: dict[tuple, set] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(ids, reason):
        ids = list(ids)
        for other in ids[1:]:
            a, b = find(ids[0]), find(other)
            if a != b:
                parent[b] = a
        reasons.setdefault(tuple(sorted(ids)), set()).add(reason)

    by_asin: dict[str, list[str]] = {}
    by_title: dict[tuple, list[str]] = {}
    by_core: dict[tuple, list[str]] = {}
    titles: dict[str, str] = {}
    durations: dict[str, float] = {}
    for i in items:
        if i["asin"]:
            by_asin.setdefault(i["asin"].strip().upper(), []).append(i["id"])
        author = _norm_name(first_author.get(i["id"], ""))
        title = _norm_title(i["title"])
        titles[i["id"]] = title
        durations[i["id"]] = float(i["duration"] or 0)
        if author and title:  # title alone is too weak ("Book 1", "Chapter 1"...)
            by_title.setdefault((title, author), []).append(i["id"])
            core = _core_title(i["title"])
            if core:
                by_core.setdefault((core, author), []).append(i["id"])
    for ids in by_asin.values():
        if len(ids) > 1:
            union(ids, "Same ASIN")
    for ids in by_title.values():
        if len(ids) > 1:
            union(ids, "Same title and author")
    for ids in by_core.values():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if titles[ids[i]] == titles[ids[j]]:
                    continue
                if _durations_match(durations[ids[i]], durations[ids[j]]):
                    union([ids[i], ids[j]], "Same title and author")

    recording_pairs = _recording_candidates(items, first_author, titles)
    if recording_pairs:
        needed = list({item_id for pair in recording_pairs for item_id in pair})
        starts: dict[str, list[float]] = {}
        for start in range(0, len(needed), 400):
            chunk = needed[start:start + 400]
            marks = ",".join("?" * len(chunk))
            for row in db.q(f"SELECT id, chapters FROM library_items WHERE id IN ({marks})", tuple(chunk)):
                starts[row["id"]] = _chapter_starts(db.jload(row["chapters"], []))
        for left_id, right_id in recording_pairs:
            if _chapters_align(starts.get(left_id, []), starts.get(right_id, [])):
                union([left_id, right_id], "Same recording")

    groups: dict[str, list[str]] = {}
    for i in items:
        groups.setdefault(find(i["id"]), []).append(i["id"])
    ignored = [] if include_ignored else _ignored_sets()
    by_id = {i["id"]: i for i in items}
    member_ids = [i for ids in groups.values() if len(ids) >= 2 for i in ids]
    heavy: dict[str, dict] = {}
    for start in range(0, len(member_ids), 400):
        chunk = member_ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in db.q(
            f"SELECT id, audio_files, library_files, chapters FROM library_items WHERE id IN ({marks})",
            tuple(chunk),
        ):
            heavy[row["id"]] = row
    _, series_by_id = library.bulk_authors_series(member_ids)
    all_libs = library.all_libraries()
    libs = {l["id"]: l["name"] for l in all_libs}
    folders = {f["id"]: f["fullPath"] for l in all_libs for f in db.jload(l["folders"], [])}
    out = []
    for ids in groups.values():
        if len(ids) < 2 or any(set(ids) <= ig for ig in ignored):
            continue
        why = sorted({r for key, rs in reasons.items() if set(key) <= set(ids) for r in rs})
        copies = sorted((_summary({**by_id[x], **heavy.get(x, {})}, libs, first_author.get(x), folders, series_by_id.get(x, []))
                         for x in ids), key=lambda c: c["addedAt"])
        out.append({"key": _group_key(ids), "reasons": why, "items": copies})
    out.sort(key=lambda g: g["items"][0]["title"].lower())
    if analyze:
        from . import dup_analysis
        dup_analysis.analyze(out)
    return out


def _summary(item: dict, libs: dict, author: str | None, folders: dict | None = None, series: list | None = None) -> dict:
    files = db.jload(item.get("library_files"), [])
    audio = [a for a in db.jload(item.get("audio_files"), []) if not a.get("exclude")]
    names = {f["filename"].lower() for f in files}
    series = series if series is not None else library.item_series(item["id"])
    return {
        "id": item["id"], "libraryId": item["library_id"], "libraryName": libs.get(item["library_id"], "?"),
        "folderPath": (folders or {}).get(item["folder_id"]),
        "locked": bool(locks.item_lock(item)),
        "title": item["title"], "subtitle": item["subtitle"], "authorName": author or "",
        "seriesName": ", ".join(f"{s['name']} #{s['sequence']}" if s["sequence"] else s["name"] for s in series),
        "narrators": db.jload(item["narrators"], []), "asin": item["asin"], "path": item["path"], "relPath": item["rel_path"],
        "isFile": bool(item["is_file"]), "duration": item["duration"] or 0, "size": item["size"] or 0,
        "numAudioFiles": int(item.get("num_audio_files") or len(audio)),
        "numFiles": int(item.get("num_files") or len(files)),
        "numChapters": int(item.get("num_chapters") or len(db.jload(item.get("chapters"), []))),
        "hasCue": any(n.endswith(".cue") for n in names), "hasMetadataJson": "metadata.json" in names,
        "hasCover": bool(item["cover_path"]), "updatedAt": int(item["updated_at"]), "addedAt": int(item["added_at"]),
        "codecs": sorted({a.get("codec") or "?" for a in audio}),
    }


def notify_new() -> None:
    """Called after scans: announce duplicate groups that weren't known before."""
    try:
        groups = find_groups()
    except Exception:
        log.exception("Duplicate detection failed")
        return
    keys = {g["key"] for g in groups}
    known = set(db.get_setting("duplicates_known", []) or [])
    new = keys - known
    db.set_setting("duplicates_known", sorted(keys))
    if new:
        tasks.activity(f"Found {len(new)} new set(s) of duplicate books ({len(groups)} in total)", "warning")
        events.publish({"type": "duplicates_found", "count": len(groups), "new": len(new), "adminOnly": True})


def ignore(item_ids: list[str]) -> None:
    ignored = db.get_setting("duplicates_ignored", []) or []
    ignored.append(sorted(set(item_ids)))
    db.set_setting("duplicates_ignored", ignored)


def reset_ignored() -> None:
    db.set_setting("duplicates_ignored", [])


# ------------------------------------------------------------------ comparison

def _item(item_id: str) -> dict:
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))
    if not item:
        raise ValueError("Book not found")
    return item


def compare(keep_id: str, discard_id: str) -> dict:
    keep, discard = _item(keep_id), _item(discard_id)
    keep_files = {f["rel_path"].lower(): f for f in db.jload(keep["library_files"], [])}
    discard_files = db.jload(discard["library_files"], [])
    keep_has_image = any(f["file_type"] == "image" for f in keep_files.values())
    can_copy = not keep["is_file"] and os.path.isdir(keep["path"])
    same_audio = _audio_signature(keep) == _audio_signature(discard)
    keep_lock = locks.item_lock(keep)
    discard_lock = locks.item_lock(discard)
    rows = []
    for f in sorted(discard_files, key=lambda f: f["rel_path"].lower()):
        other = keep_files.get(f["rel_path"].lower())
        ftype = f.get("file_type", "unknown")
        if other is None:
            status = "missing"
            default = ftype in SIDECAR_TYPES or (ftype == "image" and not keep_has_image)
        elif f["size"] > other["size"]:
            status = "larger"
            default = ftype in SIDECAR_TYPES
        elif f["size"] < other["size"]:
            status = "smaller"
            default = False
        else:
            status = "same"
            default = False
        warning = None
        replace_blocked = keep_lock is not None and other is not None
        if replace_blocked:
            default = False
            warning = "The kept copy is locked: its existing files can't be replaced"
        elif f["rel_path"].lower().endswith(".cue") and not same_audio:
            # A cue sheet's timings belong to the audio it was made for
            default = False
            warning = "Made for a different encode of this book: chapter times may not line up"
        rows.append({"relPath": f["rel_path"], "fileType": ftype, "status": status, "size": f["size"],
                     "keepSize": other["size"] if other else None, "selected": bool(default and can_copy), "warning": warning,
                     "blocked": replace_blocked})
    discard_names = {f["rel_path"].lower() for f in discard_files}
    only_keep = [{"relPath": f["rel_path"], "fileType": f.get("file_type"), "size": f["size"]}
                 for k, f in sorted(keep_files.items()) if k not in discard_names]
    return {"keepId": keep_id, "discardId": discard_id, "canCopy": can_copy, "sameAudio": same_audio,
            "keepLocked": keep_lock is not None, "discardLocked": discard_lock is not None,
            "discardLockReason": discard_lock["reason"] if discard_lock else None,
            "reason": None if can_copy else "The kept copy is a single loose file. Organize it into a folder first to bring files over.",
            "files": rows, "onlyInKeep": only_keep}


def _audio_signature(item: dict) -> tuple:
    return tuple(sorted((round(a.get("duration") or 0), a.get("size") or 0)
                        for a in db.jload(item["audio_files"], []) if not a.get("exclude")))


# ------------------------------------------------------------------ resolution

def resolve_batch(task: dict | None, plans: list[dict], mode: str = "trash") -> dict:
    """Resolve several groups using the default file selections from compare()."""
    from .watcher import watcher

    if task is not None:
        task["total"] = len(plans)
    library_ids = set()
    for plan in plans:
        for item_id in [plan.get("keepId"), *(plan.get("discardIds") or [])]:
            row = db.q1("SELECT library_id FROM library_items WHERE id = ?", (item_id,))
            if row:
                library_ids.add(row["library_id"])
    for library_id in library_ids:
        watcher.suppress(library_id)
    resolved, failed, copied, replaced = 0, [], 0, 0
    changed, trashed = set(), False
    try:
        for n, plan in enumerate(plans):
            keep_id = plan["keepId"]
            try:
                discards = []
                for discard_id in plan["discardIds"]:
                    cmp = compare(keep_id, discard_id)
                    discards.append({
                        "id": discard_id,
                        "copy": [f["relPath"] for f in cmp["files"] if f["selected"] and f["status"] == "missing"],
                        "replace": [f["relPath"] for f in cmp["files"] if f["selected"] and f["status"] != "missing"],
                    })
                if task is not None:
                    task["message"] = _item(keep_id)["title"]
                r = resolve(None, keep_id, discards, mode, notify=False)
                resolved += 1
                copied += r["copied"]
                replaced += r["replaced"]
                changed.update(r["libraryIds"])
                trashed = trashed or r["trashed"]
            except Exception as e:
                log.exception("Batch resolve failed for %s", keep_id)
                failed.append({"keepId": keep_id, "error": str(e)})
            if task is not None:
                task["progress"] = n + 1
    finally:
        for library_id in library_ids:
            watcher.release(library_id)
    for library_id in changed:
        events.publish({"type": "library_changed", "libraryId": library_id, "added": 0, "updated": resolved, "missing": 0})
    if trashed:
        events.publish({"type": "trash_changed", "adminOnly": True})
    return {"resolved": resolved, "failed": failed, "copied": copied, "replaced": replaced, "mode": mode, "batch": True}


def _safe_target(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if os.path.commonpath([str(target), str(base.resolve())]) != str(base.resolve()):
        raise ValueError(f"Refusing to write outside the book folder: {rel}")
    return target


def _merge_user_data(keep_id: str, discard_id: str) -> None:
    for p in db.q("SELECT * FROM media_progress WHERE library_item_id = ?", (discard_id,)):
        mine = db.q1("SELECT * FROM media_progress WHERE user_id = ? AND library_item_id = ? AND episode_id = ?",
                     (p["user_id"], keep_id, p["episode_id"]))
        if mine and mine["last_update"] >= p["last_update"]:
            continue  # the kept copy's progress is newer
        if mine:
            db.ex("DELETE FROM media_progress WHERE id = ?", (mine["id"],))
        db.ex("UPDATE media_progress SET library_item_id = ? WHERE id = ?", (keep_id, p["id"]))
    db.ex("UPDATE bookmarks SET library_item_id = ? WHERE library_item_id = ?", (keep_id, discard_id))
    db.ex("UPDATE playback_sessions SET library_item_id = ? WHERE library_item_id = ?", (keep_id, discard_id))
    db.ex("INSERT OR IGNORE INTO collection_books (collection_id, library_item_id, position) "
          "SELECT collection_id, ?, position FROM collection_books WHERE library_item_id = ?", (keep_id, discard_id))
    db.ex("INSERT OR IGNORE INTO playlist_items (playlist_id, library_item_id, episode_id, position) "
          "SELECT playlist_id, ?, episode_id, position FROM playlist_items WHERE library_item_id = ?", (keep_id, discard_id))


def _folder_root(item: dict) -> Path:
    lib = library.get_library(item["library_id"])
    folder = next((f for f in db.jload(lib["folders"], []) if f["id"] == item["folder_id"]), None)
    if not folder:
        raise ValueError("Library folder not found")
    return Path(folder["fullPath"])


def resolve(task: dict | None, keep_id: str, discards: list[dict], mode: str = "trash", notify: bool = True) -> dict:
    from . import organizer
    from ..scanner import scanner

    keep = _item(keep_id)
    keep_dir = Path(keep["path"])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copied, replaced, disposed, metadata_changed = 0, 0, [], False
    libraries_changed = {keep["library_id"]}
    # Check locks up front so a refused resolve changes nothing
    for d in discards:
        if d["id"] != keep_id:
            locks.check_item_delete(_item(d["id"]), "copy")
    keep_lock = locks.item_lock(keep)
    if keep_lock and any(d.get("replace") for d in discards):
        raise locks.LockedError(f"{keep_lock['reason']}: files in the kept copy can't be replaced")
    if task is not None:
        task["total"] = len(discards)

    for n, d in enumerate(discards):
        discard = _item(d["id"])
        if discard["id"] == keep_id:
            continue
        libraries_changed.add(discard["library_id"])
        a, b = os.path.normcase(keep["path"]), os.path.normcase(discard["path"])
        if a.startswith(b + os.sep) or b.startswith(a + os.sep):
            raise ValueError("One copy is inside the other's folder; resolve this by hand")
        discard_files = {f["rel_path"]: f for f in db.jload(discard["library_files"], [])}
        if task is not None:
            task["message"] = discard["rel_path"]

        wanted = [(rel, False) for rel in d.get("copy") or []] + [(rel, True) for rel in d.get("replace") or []]
        if wanted and (keep["is_file"] or not keep_dir.is_dir()):
            raise ValueError("The kept copy is a loose file; organize it into a folder before bringing files over")
        for rel, overwrite in wanted:
            f = discard_files.get(rel)
            if not f:
                raise ValueError(f"Not a file of the discarded copy: {rel}")
            target = _safe_target(keep_dir, rel)
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fsops.copy_file(f["path"], target)
            if overwrite:
                replaced += 1
            else:
                copied += 1
            if f.get("file_type") == "metadata" and not rel.lower().endswith(".cue"):
                metadata_changed = True

        _merge_user_data(keep_id, discard["id"])

        root = _folder_root(discard)
        if mode == "delete":
            if discard["is_file"]:
                os.remove(discard["path"])
            else:
                shutil.rmtree(discard["path"])
            disposed.append({"title": discard["title"], "relPath": discard["rel_path"], "deleted": True})
        else:
            dest = root / TRASH_DIR / stamp / discard["rel_path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            fsops.move(discard["path"], dest)
            disposed.append({"title": discard["title"], "relPath": discard["rel_path"], "trashedTo": str(dest)})
        organizer._remove_empty_parents(Path(discard["path"]).parent, root)
        library.delete_item_records(discard["id"])
        scanner.remove_item_cache(discard["id"])
        library.cleanup_orphans(discard["library_id"])
        if task is not None:
            task["progress"] = n + 1

    # Re-read the kept copy: a full re-read when metadata files came across, since the user chose them
    if copied or replaced:
        scanner.rescan_item(keep_id, force=metadata_changed)
    where = "deleted" if mode == "delete" else f"moved to {TRASH_DIR}"
    tasks.activity(f"Resolved duplicates of \"{keep['title']}\": kept {keep['rel_path']}, {len(disposed)} copy(ies) {where}"
                   + (f", {copied} file(s) copied, {replaced} replaced" if copied or replaced else ""))
    db.set_setting("duplicates_known", [g["key"] for g in find_groups()])
    trashed = mode != "delete" and bool(disposed)
    if notify:
        for library_id in libraries_changed:
            events.publish({"type": "library_changed", "libraryId": library_id, "added": 0, "updated": 1, "missing": 0})
        if trashed:
            events.publish({"type": "trash_changed", "adminOnly": True})
    return {"kept": keep["rel_path"], "copied": copied, "replaced": replaced, "disposed": disposed, "mode": mode,
            "trashed": trashed, "libraryIds": sorted(libraries_changed)}
