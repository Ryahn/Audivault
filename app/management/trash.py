"""Trash folders (`<library folder>/.abstream-trash/<YYYYMMDD-HHMMSS>/<book path>`):
listing, restoring, deleting, and automatic emptying after a retention period.

Retention comes from ABSTREAM_TRASH_DAYS when it is set (the app setting is then
locked), otherwise from the app setting. 0 means keep forever.
"""

import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from ..catalog import library, locks
from ..core import db, events, fsops, tasks

log = logging.getLogger("abstream.trash")

TRASH_DIR = ".abstream-trash"
STAMP = re.compile(r"^\d{8}-\d{6}$")
DEFAULT_DAYS = 30
SETTING_KEY = "trash_retention_days"


# ------------------------------------------------------------------ retention setting

def env_days() -> int | None:
    raw = os.environ.get("ABSTREAM_TRASH_DAYS", "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("Ignoring invalid ABSTREAM_TRASH_DAYS=%r", raw)
        return None


def retention_days() -> int:
    from_env = env_days()
    if from_env is not None:
        return from_env
    value = db.get_setting(SETTING_KEY, DEFAULT_DAYS)
    return max(0, int(value)) if value is not None else DEFAULT_DAYS


def set_retention_days(days: int) -> None:
    if env_days() is not None:
        raise ValueError("Trash retention is set by ABSTREAM_TRASH_DAYS and can't be changed in the app")
    db.set_setting(SETTING_KEY, max(0, int(days)))


# ------------------------------------------------------------------ listing

def _roots() -> list[tuple[dict, dict, Path]]:
    out = []
    for lib in library.all_libraries():
        for folder in db.jload(lib["folders"], []):
            root = Path(folder["fullPath"]) / TRASH_DIR
            if root.is_dir():
                out.append((lib, folder, root))
    return out


def _batch_time(batch: Path) -> float:
    try:
        return datetime.strptime(batch.name, "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return batch.stat().st_mtime


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _books(batch: Path) -> list[str]:
    """Book paths (relative to the library folder) inside a trash batch."""
    from ..scanner import scanner
    found = sorted(scanner.discover(str(batch)).keys())
    if found:
        return found
    # Fallback for batches without recognizable media: their top-level entries
    return sorted(p.name for p in batch.iterdir())


def list_entries() -> list[dict]:
    days = retention_days()
    entries = []
    for lib, folder, root in _roots():
        try:
            batches = sorted(root.iterdir(), reverse=True)
        except OSError as e:
            log.warning("Skipping trash folder %s: %s", root, e)
            continue
        for batch in batches:
            if not batch.is_dir():
                continue
            try:
                deleted_at = _batch_time(batch)
                books = [{"relPath": rel, "size": _size(batch / rel)} for rel in _books(batch)]
                lock = locks.lock_for_path(batch)
                entries.append({
                    "locked": bool(lock), "lockReason": lock["reason"] if lock else None,
                    "libraryId": lib["id"], "libraryName": lib["name"], "folderId": folder["id"], "folderPath": folder["fullPath"],
                    "batch": batch.name, "deletedAt": int(deleted_at * 1000), "size": sum(b["size"] for b in books) or _size(batch),
                    "books": books, "expiresAt": int((deleted_at + days * 86400) * 1000) if days and not lock else None,
                })
            except Exception as e:
                log.warning("Skipping trash batch %s: %s", batch, e)
    entries.sort(key=lambda e: e["deletedAt"], reverse=True)
    return entries


# ------------------------------------------------------------------ actions

def _locate(folder_id: str, batch: str, rel: str | None = None) -> tuple[dict, dict, Path, Path]:
    if not STAMP.match(batch) and not re.match(r"^[\w .-]+$", batch):
        raise ValueError("Invalid trash batch")
    for lib, folder, root in _roots():
        if folder["id"] != folder_id:
            continue
        batch_dir = (root / batch).resolve()
        if batch_dir.parent != root.resolve() or not batch_dir.is_dir():
            break
        target = batch_dir
        if rel:
            target = (batch_dir / rel).resolve()
            if os.path.commonpath([str(target), str(batch_dir)]) != str(batch_dir) or target == batch_dir or not target.exists():
                raise ValueError("Not in this trash batch")
        return lib, folder, batch_dir, target
    raise ValueError("Trash batch not found")


def _rmtree(path: Path) -> None:
    if path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _prune_empty(path: Path, stop: Path) -> None:
    """Remove empty folders from `path` up to and including the batch folder."""
    while path != stop.parent and path.exists():
        try:
            if any(path.iterdir()):
                return
            path.rmdir()
        except OSError:
            return
        path = path.parent


def delete(folder_id: str, batch: str, rel: str | None = None) -> None:
    _, _, batch_dir, target = _locate(folder_id, batch, rel)
    locks.check_delete(target, "trash")
    _rmtree(target)
    if rel:
        _prune_empty(target.parent, batch_dir)
    root = batch_dir.parent
    if root.is_dir() and not any(root.iterdir()):
        root.rmdir()
    tasks.activity(f"Deleted from trash: {rel or batch}")
    events.publish({"type": "trash_changed", "adminOnly": True})


def restore(folder_id: str, batch: str, rel: str) -> dict:
    from ..scanner import scanner
    from .watcher import watcher

    lib, folder, batch_dir, source = _locate(folder_id, batch, rel)
    dest = Path(folder["fullPath"]) / rel
    if dest.exists():
        raise FileExistsError(f"Something already exists at {rel}")
    watcher.suppress(lib["id"])
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fsops.move(source, dest)
        _prune_empty(source.parent, batch_dir)
        root = batch_dir.parent
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
        scope = rel.replace("\\", "/").split("/", 1)[0]
        tasks.start("scan", f"Restore {rel}", scanner.scan_library, lib["id"], False, {folder["id"]: [scope]},
                    key=f"scan:{lib['id']}", library_id=lib["id"])
    finally:
        watcher.release(lib["id"])
    tasks.activity(f"Restored {rel} from the trash")
    events.publish({"type": "trash_changed", "adminOnly": True})
    return {"relPath": rel, "libraryId": lib["id"]}


def purge(task: dict | None = None, everything: bool = False) -> dict:
    """Delete trash batches older than the retention period (or all of them)."""
    from .watcher import watcher

    days = retention_days()
    if not everything and days == 0:
        return {"removed": 0, "freed": 0, "days": 0}
    cutoff = time.time() - days * 86400
    removed, freed, kept_locked = 0, 0, 0
    roots = _roots()
    held = []
    for lib, _, _ in roots:
        if lib["id"] not in held:
            watcher.suppress(lib["id"])
            held.append(lib["id"])
    try:
        return _purge_roots(roots, cutoff, everything, days, removed, freed, kept_locked)
    finally:
        for library_id in held:
            watcher.release(library_id)


def _purge_roots(roots, cutoff: float, everything: bool, days: int, removed: int, freed: int, kept_locked: int) -> dict:
    for _, _, root in roots:
        if locks.path_locked(root):
            kept_locked += sum(1 for b in root.iterdir() if b.is_dir())
            continue  # a locked folder's trash is never deleted
        for batch in list(root.iterdir()):
            if not batch.is_dir() or (not everything and _batch_time(batch) > cutoff):
                continue
            size = _size(batch)
            try:
                shutil.rmtree(batch)
                removed += 1
                freed += size
            except OSError as e:
                tasks.activity(f"Could not delete trash folder {batch}: {e}", "warning")
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    if removed:
        what = "Emptied the trash" if everything else f"Emptied trash older than {days} day(s)"
        tasks.activity(f"{what}: {removed} batch(es), {freed / 1e9:.2f} GB freed")
        events.publish({"type": "trash_changed", "adminOnly": True})
    return {"removed": removed, "freed": freed, "days": days, "everything": everything, "keptLocked": kept_locked}
