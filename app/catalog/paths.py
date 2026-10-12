"""Path confinement helpers: keep served/moved files inside allowed roots."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ..core import db
from ..core.config import METADATA_DIR
from . import library

_roots_lock = threading.Lock()
_roots_cache: list[Path] | None = None


def is_under(child: str | Path, parent: str | Path) -> bool:
    """True if child resolves to a path inside parent (or is parent)."""
    try:
        c = Path(child).resolve()
        p = Path(parent).resolve()
        return os.path.commonpath([str(c), str(p)]) == str(p)
    except (OSError, ValueError):
        return False


def library_folder_roots() -> list[Path]:
    roots: list[Path] = []
    for lib in library.all_libraries():
        for folder in db.jload(lib["folders"], []):
            try:
                roots.append(Path(folder["fullPath"]).resolve())
            except (OSError, TypeError, KeyError):
                continue
    return roots


def invalidate_media_roots() -> None:
    """Drop the cached library-folder list. Call when folders are saved or deleted."""
    global _roots_cache
    with _roots_lock:
        _roots_cache = None


def allowed_media_roots() -> list[Path]:
    """Library folders plus the metadata dir. Resolved once until folders change.

    Resolving every folder on each cover request walks the library disks (often a
    Docker bind mount) and dominates grid load time.
    """
    global _roots_cache
    with _roots_lock:
        cached = _roots_cache
    if cached is not None:
        return list(cached)
    built = [METADATA_DIR.resolve(), *library_folder_roots()]
    with _roots_lock:
        if _roots_cache is None:
            _roots_cache = built
        return list(_roots_cache)


def is_allowed_media_path(path: str | Path) -> bool:
    """True if path is a real file under METADATA_DIR or a library folder."""
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    if not target.is_file():
        return False
    # Reject if any path component is a symlink that escapes (resolve already follows;
    # also refuse when the non-resolved parent chain leaves allowed roots).
    for root in allowed_media_roots():
        if is_under(target, root):
            return True
    return False


def require_under(path: str | Path, root: str | Path) -> Path:
    """Resolve path and raise ValueError if it escapes root."""
    target = Path(path).resolve()
    base = Path(root).resolve()
    if not is_under(target, base):
        raise ValueError("Path escapes allowed root")
    return target
