"""File operations that work on mounts where the server doesn't own existing files.

On Docker Desktop's Windows mounts (and some NAS/SMB mounts) files created
outside the container belong to another user. The server may write their
contents but not change their timestamps or permissions, so `shutil.copy2`
fails with "Operation not permitted" *after* the data was copied. These helpers:

- copy into a temporary file the server owns, copy timestamps onto it (best
  effort), then atomically rename it over the destination, so a replaced file
  is never left half-written;
- fall back to copy-then-delete for moves across mounts, using the same copy.
"""

import errno
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("abstream.fsops")

TMP_SUFFIX = ".abstream-tmp"  # the folder watcher ignores these names
_replace_guard = None


def set_replace_guard(guard) -> None:
    """Catalog locks register this so a copy never overwrites a locked file.

    Core does not import catalog. Until something registers, copies are not locked.
    """
    global _replace_guard
    _replace_guard = guard


def copy_file(src, dst) -> None:
    src, dst = Path(src), Path(dst)
    if dst.exists() and _replace_guard:
        _replace_guard(dst, "existing files")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}{TMP_SUFFIX}")
    try:
        shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)
        except OSError as e:  # timestamps/permissions are nice to have, not required
            log.debug("Could not copy file times for %s: %s", dst, e)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_tree(src, dst) -> None:
    src, dst = Path(src), Path(dst)
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            copy_file(Path(dirpath) / name, target_dir / name)
    for dirpath, dirnames, _ in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        try:
            shutil.copystat(dirpath, dst / rel)
        except OSError:
            pass


def move(src, dst) -> None:
    """Move a file or folder to `dst` (which must not exist yet)."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:  # only "different filesystem" falls back to copying
            raise
    if src.is_dir():
        copy_tree(src, dst)
        shutil.rmtree(src)
    else:
        copy_file(src, dst)
        src.unlink()
