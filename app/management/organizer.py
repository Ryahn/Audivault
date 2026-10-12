"""Import and organize: moves books into the Author/Series/Title layout.

Naming template tokens:
    {author} {series} {sequence} {title} {subtitle} {year} {narrator} {asin}
Square brackets mark an optional group that is dropped when any token inside
it is empty, e.g. the default:
    {author}/[{series}/][{sequence} - ]{title}
The default produces folder names the scanner parses back to the same metadata.
"""

import hashlib
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..catalog import library, media
from ..core import db, fsops, tasks
from ..scanner import scanner
from ..scanner.parse import (
    ASIN_IN_NAME, canonical_author_name, natural_key, prefer_series_name, series_key, split_embedded_series,
)

log = logging.getLogger("abstream.organizer")
_import_snapshots: dict[str, list] = {}

TOKEN = re.compile(r"\{(\w+)\}")
GROUP = re.compile(r"\[([^\[\]]*)\]")
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _replace_invalid(m: re.Match) -> str:
    ch = m.group(0)
    if ch in "/\\|":
        return "-"
    if ch == ":":
        return " -"
    return ""


def sanitize(segment: str, max_len: int = 150) -> str:
    s = INVALID.sub(_replace_invalid, segment or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    # Reject path traversal segments (literal ".." would escape the library root)
    if s in ("", ".", "..") or set(s) <= {"."}:
        return ""
    if s.upper() in RESERVED:
        s += "_"
    s = s[:max_len].rstrip(". ")
    if s in ("", ".", "..") or set(s) <= {"."}:
        return ""
    return s


def _series_names(library_id: str) -> list[str]:
    return [r["name"] for r in db.q("SELECT name FROM series WHERE library_id = ?", (library_id,))]


def _naming_series(title: str, series: list[tuple[str, str | None]], known_series: list[str] | None):
    """Turn 'Advance: Animus, Book 4' into title Advance, series Animus, book 4.

    When the library already calls that series 'Animus Series', keep that name
    so both copies of a book land in the same folder.
    """
    short, emb_series, emb_seq = split_embedded_series(title or "")
    series = list(series or [])
    if not emb_series:
        return short, series
    current_name, current_seq = series[0] if series else ("", None)
    named_after_book = bool(current_name) and series_key(current_name) == series_key(short)
    same_series = bool(current_name) and series_key(current_name) == series_key(emb_series)
    if not current_name or named_after_book:
        series = [(prefer_series_name(emb_series, known_series), emb_seq)]
        return short, series
    if same_series:
        name = prefer_series_name(current_name, known_series)
        seq = emb_seq if emb_seq and str(current_seq or "") != str(emb_seq) else (current_seq or emb_seq)
        series = [(name, seq)] + series[1:]
        return short, series
    # "Classroom of the Elite: Year 2, Vol. 01" must keep its full title when
    # the text after the colon is not the series this book already belongs to.
    return title or "", series


def template_values(title: str, authors: list[str], series: list[tuple[str, str | None]], subtitle=None, year=None,
                    narrators: list[str] | None = None, asin=None, known_series: list[str] | None = None) -> dict:
    title, series = _naming_series(title, series, known_series)
    # Prefer a series with a sequence number (e.g. "Mistborn Saga #1" over an unnumbered "Cosmere")
    first_series = next((s for s in series if s[1]), series[0] if series else (None, None))
    author_names = [canonical_author_name(a) for a in (authors or [])]
    return {
        "author": " & ".join(author_names[:3]) if author_names else "Unknown Author",
        "series": first_series[0] or "",
        "sequence": first_series[1] or "" if first_series[0] else "",
        "title": title or "",
        "subtitle": subtitle or "",
        "year": year or "",
        "narrator": ", ".join(narrators or []),
        "asin": asin or "",
    }


def render(template: str, values: dict) -> str:
    clean = {k: sanitize(str(v)) for k, v in values.items()}

    def group(m):
        body = m.group(1)
        tokens = TOKEN.findall(body)
        if any(not clean.get(t) for t in tokens):
            return ""
        return TOKEN.sub(lambda t: clean.get(t.group(1), ""), body)

    out = GROUP.sub(group, template)
    out = TOKEN.sub(lambda t: clean.get(t.group(1), ""), out)
    segments = [sanitize(s) for s in out.replace("\\", "/").split("/")]
    segments = [s for s in segments if s and s not in (".", "..")]
    if not segments:
        title = clean.get("title") or "Untitled"
        segments = [title if title not in (".", "..", "") else "Untitled"]
    return "/".join(segments)


def item_values(item: dict, known_series: list[str] | None = None) -> dict:
    if known_series is None and item.get("library_id"):
        known_series = _series_names(item["library_id"])
    return template_values(
        item["title"], [a["name"] for a in library.item_authors(item["id"])],
        [(s["name"], s["sequence"]) for s in library.item_series(item["id"])],
        item["subtitle"], item["published_year"], db.jload(item["narrators"], []), item["asin"],
        known_series=known_series)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _exact_path(a: str, b: str) -> bool:
    """Path equality that keeps capitalization. normcase folds case only on Windows."""
    return os.path.normpath(a) == os.path.normpath(b)


def _same_directory(a: str, b: str) -> bool:
    """True when both paths are the same directory, including a case-only difference.

    On a case-insensitive volume (a Windows disk, or that disk mounted into the
    Linux container) `My Kind` and `my Kind` are one folder. os.path.exists says
    the target is already there, which used to look like a conflict.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _folds_case(root: str) -> bool:
    """True when this volume treats `My` and `my` as the same folder."""
    path = Path(root)
    if not path.exists():
        return os.path.normcase("A") != os.path.normcase("a")
    for i, ch in enumerate(path.name):
        if ch.isalpha() and ch.swapcase() != ch:
            alt = path.with_name(path.name[:i] + ch.swapcase() + path.name[i + 1:])
            try:
                return os.path.samefile(path, alt)
            except OSError:
                return False
    return os.path.normcase("A") != os.path.normcase("a")


def _dest_key(path: str, fold: bool) -> str:
    key = os.path.normcase(os.path.normpath(path))
    return key.casefold() if fold else key


def _live_item_for_inode(item: dict) -> dict | None:
    """Another library row for this same folder, if the missing row is a leftover."""
    ino = item.get("ino")
    if not ino:
        return None
    return db.q1(
        "SELECT id, title, rel_path FROM library_items "
        "WHERE library_id = ? AND ino = ? AND id != ? AND is_missing = 0",
        (item["library_id"], ino, item["id"]),
    )


def _is_inside(child: str, parent: str) -> bool:
    from ..catalog.paths import is_under
    return is_under(child, parent)


def _target_under_root(target: str | Path, root: str | Path) -> bool:
    """True if target (file or dir to create) resolves under root."""
    from ..catalog.paths import is_under
    try:
        t = Path(target)
        # For paths that do not exist yet, resolve parents then append name
        if t.exists():
            return is_under(t, root)
        parent = t.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        resolved_parent = parent.resolve()
        remainder = t.relative_to(parent) if parent != t else Path(".")
        candidate = (resolved_parent / remainder).resolve()
        return is_under(candidate, root)
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ organize existing items

def _live_by_inode(library_id: str, items: list[dict]) -> dict:
    """One query for every missing row's inode, instead of a query per book."""
    inos = list(dict.fromkeys(i["ino"] for i in items if i.get("is_missing") and i.get("ino")))
    found: dict = {}
    for start in range(0, len(inos), 400):
        chunk = inos[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows = db.q(
            f"SELECT id, title, rel_path, ino FROM library_items "
            f"WHERE library_id = ? AND is_missing = 0 AND ino IN ({marks})",
            (library_id, *chunk),
        )
        found.update({r["ino"]: r for r in rows})
    return found


def _classify_target(current: str, target: str, root: str) -> dict:
    if not _target_under_root(target, root):
        return {"status": "error", "caseOnly": False, "detail": "The new path would leave the library folder."}
    if _same_directory(current, target):
        return {
            "status": "ok", "caseOnly": True,
            "detail": "Only the capitalization changes. The folder will be renamed to match.",
        }
    if os.path.exists(target):
        return {
            "status": "conflict", "caseOnly": False,
            "detail": "A different folder is already at the new location, so this one is left where it is.",
        }
    return {"status": "ok", "caseOnly": False, "detail": ""}


def plan_item(item: dict, template: str | None = None, authors: list | None = None, series: list | None = None,
              lib: dict | None = None, folders: list | None = None, settings: dict | None = None,
              lives: dict | None = None, check_target: bool = True, known_series: list[str] | None = None) -> dict:
    if lib is None:
        lib = library.get_library(item["library_id"])
    if settings is None:
        settings = library.library_settings(lib)
    if folders is None:
        folders = db.jload(lib["folders"], [])
    folder = next((f for f in folders if f["id"] == item["folder_id"]), None)
    if known_series is None and item.get("library_id"):
        known_series = _series_names(item["library_id"])
    if authors is not None and series is not None:
        values = template_values(
            item["title"], [a["name"] for a in authors],
            [(s["name"], s["sequence"]) for s in series],
            item["subtitle"], item["published_year"], db.jload(item["narrators"], []), item["asin"],
            known_series=known_series)
    else:
        values = item_values(item, known_series)
    rel = render(template or settings["namingTemplate"], values)
    target = str(Path(folder["fullPath"]) / rel) if folder else None
    current = item["path"]
    plan = {"id": item["id"], "title": item["title"], "from": item["rel_path"], "to": rel, "fromPath": current, "toPath": target,
            "folderId": item["folder_id"], "isFile": bool(item["is_file"]), "status": "ok", "caseOnly": False, "detail": ""}
    if item["is_missing"]:
        if lives is None:
            live = _live_item_for_inode(item)
        else:
            live = lives.get(item.get("ino"))
            if live and live["id"] == item["id"]:
                live = None
        if live:
            plan["status"] = "stale"
            plan["keepId"] = live["id"]
            plan["detail"] = (
                f"These files are already in the library at {live['rel_path']}. "
                "This entry is left over from an older path and can be removed without touching the files."
            )
        else:
            plan["status"] = "missing"
            plan["detail"] = (
                "The folder is not on disk, so it can't be renamed. "
                "Remove it from the library in Settings if it is gone for good."
            )
    elif target is None or not folder:
        plan["status"] = "error"
        plan["detail"] = "This book is not in a library folder."
    elif _exact_path(current, target):
        plan["status"] = "unchanged"
    elif check_target:
        plan.update(_classify_target(current, target, folder["fullPath"]))
    else:
        plan["_check"] = True
    return plan


def plan_library(library_id: str, template: str | None = None) -> list[dict]:
    lib = library.get_library(library_id)
    if not lib:
        return []
    items = db.q(
        "SELECT id, library_id, folder_id, path, rel_path, ino, is_file, is_missing, title, subtitle, "
        "published_year, narrators, asin FROM library_items WHERE library_id = ? ORDER BY rel_path",
        (library_id,))
    settings = library.library_settings(lib)
    folders = db.jload(lib["folders"], [])
    use_template = template or settings["namingTemplate"]
    authors_by_id, series_by_id = library.bulk_authors_series([i["id"] for i in items])
    known_series = _series_names(library_id)
    lives = _live_by_inode(library_id, items)
    plans = [plan_item(i, use_template, authors=authors_by_id.get(i["id"], []), series=series_by_id.get(i["id"], []),
                       lib=lib, folders=folders, settings=settings, lives=lives, check_target=False,
                       known_series=known_series)
             for i in items]
    pending = [p for p in plans if p.get("_check")]
    if pending:
        roots = {f["id"]: f["fullPath"] for f in folders}
        workers = min(scanner.PROBE_WORKERS, len(pending))

        def classify(plan: dict) -> dict:
            return _classify_target(plan["fromPath"], plan["toPath"], roots.get(plan["folderId"], ""))

        if workers == 1:
            updates = [classify(p) for p in pending]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                updates = list(pool.map(classify, pending))
        for plan, update in zip(pending, updates):
            plan.update(update)
    for plan in plans:
        plan.pop("_check", None)
    # Two items mapping to the same destination is a conflict too. On a
    # case-insensitive volume, paths that differ only by capitalization are
    # the same destination even though Linux normcase does not fold case.
    fold_by_folder = {f["id"]: _folds_case(f["fullPath"]) for f in folders}
    seen: dict[tuple, dict] = {}
    for p in plans:
        if p["status"] != "ok" or not p.get("toPath"):
            continue
        key = (p.get("folderId"), _dest_key(p["toPath"], fold_by_folder.get(p.get("folderId"), False)))
        resident = seen.get(key)
        if resident is None:
            seen[key] = p
            continue
        if resident.get("caseOnly") and not p.get("caseOnly"):
            p["status"] = "conflict"
            p["caseOnly"] = False
            p["detail"] = f"'{resident['title']}' is already in this folder."
        elif p.get("caseOnly") and not resident.get("caseOnly"):
            resident["status"] = "conflict"
            resident["caseOnly"] = False
            resident["detail"] = f"'{p['title']}' is already in this folder."
            seen[key] = p
        else:
            p["status"] = "conflict"
            p["caseOnly"] = False
            p["detail"] = f"'{resident['title']}' would be renamed to the same folder."
            if resident["status"] == "ok":
                resident["status"] = "conflict"
                resident["caseOnly"] = False
                resident["detail"] = f"'{p['title']}' would be renamed to the same folder."
    return plans


def _remove_empty_parents(path: Path, stop: Path) -> None:
    from ..catalog import locks
    path = path.resolve()
    stop = stop.resolve()
    junk = () if locks.path_locked(path) else ("Thumbs.db", ".DS_Store", "desktop.ini")  # locked: delete no files at all
    while path != stop and _is_inside(str(path), str(stop)):
        try:
            leftovers = [p for p in path.iterdir() if p.name not in junk]
            if leftovers:
                return
            for p in path.iterdir():
                p.unlink()
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _move(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    if os.path.normcase(src) == os.path.normcase(dst) or _is_inside(dst, src) or _same_directory(src, dst):
        tmp = src.rstrip("/\\") + ".abstream-tmp"
        os.rename(src, tmp)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        fsops.move(tmp, dst)
    else:
        fsops.move(src, dst)


def _rename_case(old: Path, new: Path) -> None:
    """Rename a file or folder when only its capitalization changes.

    A direct rename is a no-op or an error on a case-insensitive volume, so
    the name goes through a temporary the folder watcher ignores.
    """
    if old.name == new.name:
        return
    tmp = Path(str(old) + ".abstream-tmp")
    if tmp.exists():
        tmp = Path(str(old) + f".abstream-tmp-{os.getpid()}")
    os.rename(old, tmp)
    try:
        os.rename(tmp, new)
    except OSError:
        os.rename(tmp, old)
        raise


def _replace_prefix(path: str, old: str, new: str) -> str | None:
    path_n = os.path.normpath(path)
    old_n = os.path.normpath(old)
    if path_n == old_n:
        return str(Path(new))
    prefix = old_n + os.sep
    if path_n.startswith(prefix):
        return str(Path(new) / Path(path_n[len(prefix):]))
    return None


def _retarget_prefix(library_id: str, old: Path, new: Path, root: Path) -> list[str]:
    """Point every book stored under `old` at `new`. A row that would land on
    another book's path is left alone (it is a leftover entry, not this folder)."""
    rows = db.q("SELECT id, path FROM library_items WHERE library_id = ?", (library_id,))
    taken = {os.path.normpath(r["path"]): r["id"] for r in rows}
    updated = []
    for row in rows:
        rewritten = _replace_prefix(row["path"], str(old), str(new))
        if not rewritten or os.path.normpath(rewritten) == os.path.normpath(row["path"]):
            continue
        owner = taken.get(os.path.normpath(rewritten))
        if owner and owner != row["id"]:
            continue
        rel = Path(os.path.relpath(rewritten, root)).as_posix()
        db.update("library_items", row["id"], {"path": rewritten, "rel_path": rel, "updated_at": db.now_ms()})
        taken.pop(os.path.normpath(row["path"]), None)
        taken[os.path.normpath(rewritten)] = row["id"]
        updated.append(row["id"])
    return updated


def _apply_case_rename(library_id: str, src: str, dst: str, root: Path) -> list[str]:
    """Fix capitalization along `src` so it matches `dst`. Both are the same folder.

    The difference is often a parent (the series or author folder), and that
    folder is shared. Renaming it updates every book stored under it, so the
    next scan does not mark the siblings missing and add them again.
    """
    src_parts = Path(src).parts
    dst_parts = Path(dst).parts
    if len(src_parts) != len(dst_parts):
        raise ValueError("Case-only rename paths do not line up")
    current = list(src_parts)
    updated: list[str] = []
    for i, (old_name, new_name) in enumerate(zip(src_parts, dst_parts)):
        if old_name == new_name:
            continue
        if old_name.lower() != new_name.lower():
            raise ValueError("Refusing to case-rename a path that changes more than capitalization")
        old = Path(*current[:i + 1])
        new = Path(*current[:i], new_name)
        _rename_case(old, new)
        updated.extend(_retarget_prefix(library_id, old, new, root))
        current[i] = new_name
    return updated


def _drop_stale(item_id: str, keep_id: str) -> None:
    """Remove a library row whose folder is already represented by `keep_id`."""
    from .duplicates import _merge_user_data
    keep = db.q1("SELECT library_id, title, rel_path FROM library_items WHERE id = ?", (keep_id,))
    if not keep:
        raise ValueError("The copy to keep is no longer in the library")
    _merge_user_data(keep_id, item_id)
    library.delete_item_records(item_id)
    scanner.remove_item_cache(item_id)
    library.cleanup_orphans(keep["library_id"])
    tasks.activity(f"Removed a leftover entry for \"{keep['title']}\" (files stay at {keep['rel_path']})")


def _rename_audio_files(item_dir: Path, item: dict) -> None:
    audio = [a for a in db.jload(item["audio_files"], [])]
    if not audio:
        return
    series_rows = db.q(
        "SELECT s.name, bs.sequence FROM book_series bs JOIN series s ON s.id = bs.series_id "
        "WHERE bs.item_id = ? ORDER BY s.name",
        (item["id"],),
    )
    named, _series = _naming_series(
        item["title"],
        [(r["name"], r["sequence"]) for r in series_rows],
        _series_names(item["library_id"]) if item.get("library_id") else None,
    )
    base = sanitize(named)
    audio.sort(key=lambda a: a.get("index", 0))
    width = max(2, len(str(len(audio))))
    for i, a in enumerate(audio, start=1):
        old = Path(a["path"])
        rel = Path(a["rel_path"])
        # File paths in the db point at the old location; map onto the new dir
        current = item_dir / rel
        if not current.exists():
            continue
        name = f"{base}{old.suffix.lower()}" if len(audio) == 1 else f"{base} - Part {i:0{width}d}{old.suffix.lower()}"
        target = current.with_name(name)
        if target != current and not target.exists():
            current.rename(target)


def apply_item(item_id: str, template: str | None = None, rename_files: bool | None = None) -> dict:
    item = media.get_item(item_id)
    if not item:
        raise ValueError("Item not found")
    plan = plan_item(item, template)
    if plan["status"] == "stale":
        _drop_stale(item_id, plan["keepId"])
        plan["status"] = "removed"
        return plan
    if plan["status"] not in ("ok",):
        return plan
    lib = library.get_library(item["library_id"])
    settings = library.library_settings(lib)
    folder = next(f for f in db.jload(lib["folders"], []) if f["id"] == item["folder_id"])
    root = Path(folder["fullPath"])
    src, dst = item["path"], plan["toPath"]
    if not _target_under_root(dst, root):
        plan["status"] = "error"
        plan["detail"] = "The new path would leave the library folder."
        return plan
    old_parent = Path(src).parent

    retargeted: list[str] = []
    if _same_directory(src, dst):
        retargeted = _apply_case_rename(item["library_id"], src, dst, root)
    elif item["is_file"]:
        Path(dst).mkdir(parents=True, exist_ok=True)
        fsops.move(src, Path(dst) / Path(src).name)
    else:
        _move(src, dst)
    db.update("library_items", item_id, {"path": dst, "rel_path": plan["to"], "is_file": 0, "updated_at": db.now_ms()})
    for other_id in dict.fromkeys(retargeted):
        if other_id == item_id:
            continue
        try:
            scanner.rescan_item(other_id, force=False)
        except Exception:
            log.exception("Rescan after case rename failed for %s", other_id)

    if rename_files if rename_files is not None else settings.get("renameAudioFiles"):
        _rename_audio_files(Path(dst), item)
    _remove_empty_parents(old_parent, root)

    # Refresh file list/paths without touching metadata
    scanner.rescan_item(item_id, force=False)
    media.write_metadata_file(item_id)
    tasks.activity(f"Organized \"{item['title']}\" -> {plan['to']}")
    plan["status"] = "moved"
    return plan


def organize_library(task: dict, library_id: str, item_ids: list[str] | None = None, template: str | None = None) -> dict:
    plans = [p for p in plan_library(library_id, template)
             if p["status"] in ("ok", "stale") and (not item_ids or p["id"] in item_ids)]
    task["total"] = len(plans)
    moved, removed, failed = 0, 0, []
    for i, p in enumerate(plans):
        task["message"] = p["to"]
        try:
            result = apply_item(p["id"], template)
            if result["status"] == "moved":
                moved += 1
            elif result["status"] == "removed":
                removed += 1
            else:
                failed.append({"id": p["id"], "title": p["title"], "error": result.get("detail") or result["status"]})
        except Exception as e:
            log.exception("Organize failed for %s", p["fromPath"])
            failed.append({"id": p["id"], "title": p["title"], "error": str(e)})
        task["progress"] = i + 1
    return {"moved": moved, "removed": removed, "failed": failed}


def organize_items(library_id: str, item_ids: set[str] | None = None, task: dict | None = None) -> dict:
    """Automatic organize (used after scans when the library enables it).
    Clean moves are made, including a rename that only fixes capitalization.
    Conflicts, folders that are really gone, leftover entries, and books
    without an author (which would land in "Unknown Author") are left alone.
    Leftover entries are removed from the Organize page, where that is explicit."""
    plans = [p for p in plan_library(library_id) if p["status"] == "ok" and (item_ids is None or p["id"] in item_ids)]
    authors_by_id, _ = library.bulk_authors_series([p["id"] for p in plans])
    moved, skipped, failed = 0, 0, []
    for i, p in enumerate(plans):
        if not authors_by_id.get(p["id"]):
            skipped += 1
            continue
        if task is not None:
            task["message"] = f"Organizing {p['to']}"
        try:
            if apply_item(p["id"])["status"] == "moved":
                moved += 1
        except Exception as e:
            log.exception("Auto-organize failed for %s", p["fromPath"])
            failed.append({"id": p["id"], "title": p["title"], "error": str(e)})
    return {"moved": moved, "skipped": skipped, "failed": failed}


# ------------------------------------------------------------------ import

def candidate_id(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()[:16]


def _inspect_import_file(path: str, probe_it: bool, cache: dict) -> dict:
    """Stat one import file, and read its tags when they are not already cached."""
    try:
        st = os.stat(path)
        size = st.st_size
        mtime_ms = int(st.st_mtime * 1000)
    except OSError:
        return {"path": path, "size": 0, "info": None, "key": None, "store": False}
    if not probe_it:
        return {"path": path, "size": size, "info": None, "key": None, "store": False}
    key = f"{path}|{size}|{mtime_ms}"
    info = cache.get(key)
    if info:
        return {"path": path, "size": size, "info": info, "key": key, "store": False}
    probed = scanner.probe.probe_audio(path)
    info = {"duration": probed["duration"], "tags": probed["tags"], "embedded_cover": probed["embedded_cover"]}
    return {"path": path, "size": size, "info": info, "key": key, "store": True}


def list_import_candidates(library_id: str, *, fresh: bool = False) -> dict:
    lib = library.get_library(library_id)
    settings = library.library_settings(lib)
    import_dir = settings.get("importFolder") or ""
    if not import_dir:
        return {"importFolder": "", "candidates": [], "error": "No import folder configured for this library"}
    if not Path(import_dir).is_dir():
        return {"importFolder": import_dir, "candidates": [], "error": "Import folder does not exist"}
    from . import autoimport
    state = autoimport.load_state(library_id)
    snap_key = scanner._listing_key(import_dir)
    if fresh:
        scanner.invalidate_discover(import_dir)
        _import_snapshots.pop(snap_key, None)
    elif scanner.has_discover_cache(import_dir) and snap_key in _import_snapshots:
        candidates = []
        for c in _import_snapshots[snap_key]:
            item = dict(c)
            item["autoImport"] = autoimport.present_outcome(state.get(c["signature"]))
            candidates.append(item)
        return {"importFolder": import_dir, "candidates": candidates}
    probe_cache = autoimport.load_probe_cache(library_id)
    discovered = scanner.cached_discover(import_dir, fresh=False)
    books = []
    jobs = []
    for rel, entry in sorted(discovered.items(), key=lambda kv: natural_key(kv[0])):
        files = entry["files"]
        audio = sorted((f for f in files if scanner.file_type(f) == "audio"), key=natural_key)
        books.append((rel, entry, files, audio))
        audio_set = set(audio)
        for f in files:
            jobs.append((f, f in audio_set))
    inspected: dict[str, dict] = {}
    if jobs:
        workers = min(scanner.PROBE_WORKERS, len(jobs))
        if workers == 1:
            rows = [_inspect_import_file(path, probe_it, probe_cache) for path, probe_it in jobs]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                rows = list(pool.map(lambda job: _inspect_import_file(job[0], job[1], probe_cache), jobs))
        dirty = False
        for row in rows:
            inspected[row["path"]] = row
            if row["store"] and row["key"]:
                probe_cache[row["key"]] = row["info"]
                dirty = True
        if dirty:
            autoimport.save_probe_cache(library_id, probe_cache)
    out = []
    for rel, entry, files, audio in books:
        merged_tags = {}
        duration = 0.0
        embedded_cover = False
        for f in audio:
            info = (inspected.get(f) or {}).get("info") or {}
            duration += info.get("duration") or 0
            # Author, ASIN, and series are often missing on the first file of a multi-file book.
            for k, v in (info.get("tags") or {}).items():
                if v and not merged_tags.get(k):
                    merged_tags[k] = v
            if info.get("embedded_cover"):
                embedded_cover = True
        first_tags = merged_tags
        audio_meta = [{"path": a, "rel_path": a, "duration": 0, "tags": first_tags if i == 0 else {}} for i, a in enumerate(audio)]
        item_dir = None if entry["is_file"] else Path(import_dir) / rel
        md = scanner.gather_metadata(settings, rel, entry["is_file"], item_dir, audio_meta, files)
        series = md.get("series_list") or []
        if not md.get("asin"):  # e.g. "Book Title [B0CMYLRB39].m4b"
            for f in files:
                m = ASIN_IN_NAME.search(Path(f).name)
                if m and m.group(1).startswith("B0"):
                    md["asin"] = m.group(1)
                    break
        sig = autoimport.signature(rel, files)
        out.append({
            "id": candidate_id(rel), "relPath": rel, "isFile": entry["is_file"], "numAudioFiles": len(audio),
            "files": files, "signature": sig, "autoImport": autoimport.present_outcome(state.get(sig)),
            "numFiles": len(files),
            "size": sum((inspected.get(f) or {}).get("size") or 0 for f in files),
            "duration": duration,
            "hasCover": embedded_cover or any(scanner.file_type(f) == "image" for f in files),
            "metadata": {
                "title": md.get("title"), "subtitle": md.get("subtitle"), "authors": md.get("authors", []),
                "narrators": md.get("narrators", []), "series": series[0][0] if series else "", "sequence": series[0][1] if series else "",
                "publishedYear": md.get("published_year"), "asin": md.get("asin"), "isbn": md.get("isbn"),
                "description": md.get("description"), "publisher": md.get("publisher"), "genres": md.get("genres", []),
                "language": md.get("language"),
            },
        })
    if scanner.has_discover_cache(import_dir):
        _import_snapshots[snap_key] = out
    return {"importFolder": import_dir, "candidates": out}


def import_candidate(library_id: str, rel: str, metadata: dict, folder_id: str | None = None, mode: str | None = None,
                     cover_url: str | None = None, source_signature: str | None = None) -> dict:
    lib = library.get_library(library_id)
    settings = library.library_settings(lib)
    if mode not in ("move", "copy"):
        mode = "copy" if settings.get("importMode") == "copy" else "move"
    import_dir = Path(settings.get("importFolder") or "")
    src = import_dir / rel
    if not import_dir.is_dir() or not src.exists() or not _is_inside(str(src), str(import_dir)):
        raise ValueError("Import source not found")
    folders = db.jload(lib["folders"], [])
    folder = next((f for f in folders if f["id"] == folder_id), folders[0] if folders else None)
    if not folder:
        raise ValueError("Library has no folders")
    from . import autoimport
    source_sig = source_signature
    if not source_sig:
        source_entry = scanner.cached_discover(str(import_dir)).get(Path(rel).as_posix())
        source_sig = autoimport.signature(Path(rel).as_posix(), source_entry["files"]) if source_entry else None

    authors = metadata.get("authors") or []
    if isinstance(authors, str):
        from ..scanner.parse import parse_names
        authors = parse_names(authors)
    series = [(metadata["series"].strip(), (str(metadata.get("sequence") or "").strip() or None))] if metadata.get("series") else []
    values = template_values(metadata.get("title") or Path(rel).stem, authors, series, metadata.get("subtitle"),
                             metadata.get("publishedYear"), metadata.get("narrators"), metadata.get("asin"),
                             known_series=_series_names(library_id))
    organize = settings.get("organizeOnImport", True)
    # Organized: the naming template. Otherwise keep the name/relative path it had in the import folder.
    target_rel = render(settings["namingTemplate"], values) if organize else Path(rel).as_posix()
    if ".." in Path(target_rel).parts or any(p in (".", "..") for p in Path(target_rel).parts):
        raise ValueError("Import destination escapes library folder")
    target = Path(folder["fullPath"]) / target_rel
    if not _target_under_root(target, folder["fullPath"]):
        raise ValueError("Import destination escapes library folder")
    if target.exists():
        raise FileExistsError(f"Destination already exists: {target_rel}")

    op = fsops.move if mode == "move" else fsops.copy_file
    if not organize and src.is_file():
        # Loose file stays a loose single-file book at the library root
        target.parent.mkdir(parents=True, exist_ok=True)
        op(str(src), str(target))
        item_id, _ = scanner.scan_item_entry(lib, folder, target.name, {"is_file": True, "files": [str(target)]}, None, force=True)
        _apply_import_metadata(item_id, metadata, authors, series, cover_url)
        tasks.activity(f"Imported \"{metadata.get('title')}\" -> {target_rel}")
        if source_sig:
            autoimport._save_outcome(library_id, source_sig, {"status": "imported", "to": target_rel, "itemId": item_id, "reason": "imported"})
        if mode == "move":
            scanner.invalidate_discover(str(import_dir))
        return {"id": item_id, "relPath": target_rel}

    target.mkdir(parents=True, exist_ok=False)
    if src.is_file():
        op(str(src), str(target / src.name))
    else:
        for child in src.iterdir():
            if child.is_dir():
                if mode == "move":
                    fsops.move(child, target / child.name)
                else:
                    fsops.copy_tree(child, target / child.name)
            else:
                op(str(child), str(target / child.name))
        if mode == "move":
            _remove_empty_parents(src, import_dir)

    entry = scanner.discover(str(target))
    files = sorted({f for e in entry.values() for f in e["files"]} | {str(p) for p in target.iterdir() if p.is_file()})
    item_id, _ = scanner.scan_item_entry(lib, folder, target_rel, {"is_file": False, "files": files}, None, force=True)
    _apply_import_metadata(item_id, metadata, authors, series, cover_url)
    if settings.get("renameAudioFiles") and organize:
        item = media.get_item(item_id)
        _rename_audio_files(target, item)
        scanner.rescan_item(item_id, force=False)
    tasks.activity(f"Imported \"{metadata.get('title')}\" -> {target_rel}")
    if source_sig:
        autoimport._save_outcome(library_id, source_sig, {"status": "imported", "to": target_rel, "itemId": item_id, "reason": "imported"})
    if mode == "move":
        scanner.invalidate_discover(str(import_dir))
    return {"id": item_id, "relPath": target_rel}


def import_batch(task: dict | None, library_id: str, items: list[dict], mode: str | None = None) -> dict:
    """Import many candidates. One failure does not stop the rest."""
    from ..core import events
    from . import autoimport
    imported, skipped, failed = [], [], []
    if task is not None:
        task["total"] = len(items)
    for i, item in enumerate(items):
        rel = (item.get("relPath") or "").replace("\\", "/")
        if task is not None:
            task["message"] = rel
        meta = item.get("metadata") or {}
        if not str(meta.get("title") or "").strip():
            failed.append({"relPath": rel, "error": "Title is required"})
        else:
            try:
                res = import_candidate(library_id, rel, meta, item.get("folderId"), mode, item.get("coverUrl"),
                                       item.get("signature"))
                imported.append({"relPath": rel, "to": res["relPath"]})
            except FileExistsError as e:
                target = str(e).split(": ", 1)[-1]
                reason = f"Already in the library at {target}. This copy was left in the import folder."
                sig = item.get("signature")
                if sig:
                    autoimport._save_outcome(library_id, sig, {"status": "skipped", "to": target, "reason": reason})
                skipped.append({"relPath": rel, "to": target, "reason": reason})
            except Exception as e:
                log.exception("Batch import failed for %s", rel)
                failed.append({"relPath": rel, "error": str(e)})
        if task is not None:
            task["progress"] = i + 1
    lib = library.get_library(library_id)
    name = lib["name"] if lib else "the library"
    if imported:
        tasks.activity(f"Imported {len(imported)} book(s) into \"{name}\"")
        events.publish({"type": "library_changed", "libraryId": library_id, "added": len(imported), "updated": 0, "missing": 0})
    if skipped:
        tasks.activity(f"{len(skipped)} book(s) already in \"{name}\" were left in the import folder")
    if imported or skipped or failed:
        remaining = len(list_import_candidates(library_id)["candidates"])
        events.publish({"type": "import_changed", "libraryId": library_id, "count": remaining, "new": 0})
    return {"imported": imported, "skipped": skipped, "failed": failed, "libraryId": library_id}


def _apply_import_metadata(item_id: str, metadata: dict, authors: list[str], series: list, cover_url: str | None) -> None:
    payload = {"metadata": {
        "title": metadata.get("title"), "subtitle": metadata.get("subtitle"), "authors": [{"name": a} for a in authors],
        "series": [{"name": s, "sequence": q} for s, q in series], "narrators": metadata.get("narrators") or [],
        "publishedYear": metadata.get("publishedYear"), "asin": metadata.get("asin"), "isbn": metadata.get("isbn"),
        "description": metadata.get("description"), "publisher": metadata.get("publisher"),
        "genres": metadata.get("genres") or [], "language": metadata.get("language"),
    }}
    payload["metadata"] = {k: v for k, v in payload["metadata"].items() if v not in (None, "")}
    media.update_media(item_id, payload)
    if cover_url:
        try:
            media.set_cover_from_url(item_id, cover_url)
        except Exception as e:
            log.warning("Cover download failed during import: %s", e)
    media.write_metadata_file(item_id)
