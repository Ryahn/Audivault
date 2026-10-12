"""Locked libraries and folders: a source of truth whose files are never deleted.

A whole library can be locked (library setting `locked`), or individual library
folders (`"locked": true` on the folder). Inside a locked folder Audivault
never deletes or overwrites a file: no "delete files", no discarding duplicates,
no emptying trash, no replacing existing files. Renaming, moving, adding new
files and updating its own metadata.json are still allowed.

Every code path that deletes or overwrites user files calls `check_delete()` (or
asks `path_locked()`) right before doing so, so a lock holds even if the UI is
bypassed.
"""

import os
from pathlib import Path

from ..core import db, fsops
from . import library


class LockedError(Exception):
    """Raised when an operation would delete or overwrite files in a locked folder."""


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def locked_roots() -> list[dict]:
    """[{path, libraryId, libraryName, folderId, reason}] for every locked folder."""
    out = []
    for lib in library.all_libraries():
        lib_locked = bool(library.library_settings(lib).get("locked"))
        for folder in db.jload(lib["folders"], []):
            if lib_locked or folder.get("locked"):
                reason = (f'Library "{lib["name"]}" is locked' if lib_locked
                          else f'Folder {folder["fullPath"]} is locked')
                out.append({"path": folder["fullPath"], "libraryId": lib["id"], "libraryName": lib["name"],
                            "folderId": folder["id"], "reason": reason})
    return out


def lock_for_path(path) -> dict | None:
    target = _norm(path)
    for root in locked_roots():
        base = _norm(root["path"])
        if target == base or target.startswith(base + os.sep):
            return root
    return None


def path_locked(path) -> bool:
    return lock_for_path(path) is not None


def item_lock(item: dict) -> dict | None:
    """The lock that applies to a library item (by its folder), if any."""
    lib = library.get_library(item["library_id"])
    if not lib:
        return None
    lib_locked = bool(library.library_settings(lib).get("locked"))
    folder = next((f for f in db.jload(lib["folders"], []) if f["id"] == item["folder_id"]), None)
    if lib_locked:
        return {"reason": f'Library "{lib["name"]}" is locked', "path": folder["fullPath"] if folder else None}
    if folder and folder.get("locked"):
        return {"reason": f'Folder {folder["fullPath"]} is locked', "path": folder["fullPath"]}
    return None


def check_delete(path, what: str = "files") -> None:
    lock = lock_for_path(path)
    if lock:
        raise LockedError(f"{lock['reason']}: its {what} can't be deleted or overwritten")


def check_item_delete(item: dict, what: str = "files") -> None:
    lock = item_lock(item)
    if lock:
        raise LockedError(f"{lock['reason']}: \"{item['title']}\" is a source-of-truth copy; its {what} can't be deleted")
    # Belt and braces: also check the path itself (covers locks on overlapping folders)
    check_delete(item["path"], what)


def can_overwrite(path) -> bool:
    """False when `path` exists inside a locked folder."""
    return not (Path(path).exists() and path_locked(path))


fsops.set_replace_guard(check_delete)
