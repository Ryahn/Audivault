"""In-process cache of slim library item snapshots for list endpoints."""

import threading

_lock = threading.Lock()
_snapshots: dict[tuple[str, bool], dict] = {}


def invalidate_library_cache(library_id: str | None = None) -> None:
    with _lock:
        if library_id is None:
            _snapshots.clear()
            return
        for key in [k for k in _snapshots if k[0] == library_id]:
            del _snapshots[key]


def get_snapshot(library_id: str, include_missing: bool) -> dict | None:
    with _lock:
        return _snapshots.get((library_id, include_missing))


def set_snapshot(library_id: str, include_missing: bool, snap: dict) -> None:
    with _lock:
        _snapshots[(library_id, include_missing)] = snap
