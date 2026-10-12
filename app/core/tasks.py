"""Background task tracking (scans, imports, organize runs) plus a small activity log.
Task lifecycle and progress are pushed to web clients through app.events."""

import logging
import threading
import time
import traceback
import uuid
from collections import deque

from . import events

log = logging.getLogger("abstream.tasks")

_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_activity: deque = deque(maxlen=300)
_pump_started = False
_hold_suppress = None
_hold_release = None


def set_library_hold(suppress, release) -> None:
    """Watcher registers these so a scan can ignore its own filesystem events.

    Core does not import the watcher. Until something registers, holds are no-ops.
    """
    global _hold_suppress, _hold_release
    _hold_suppress = suppress
    _hold_release = release


def activity(message: str, level: str = "info") -> None:
    entry = {"time": int(time.time() * 1000), "level": level, "message": message}
    _activity.appendleft(entry)
    getattr(log, level if level in ("info", "warning", "error") else "info")(message)
    events.publish({"type": "activity", "entry": entry, "adminOnly": True})


def recent_activity(limit: int = 100) -> list[dict]:
    return list(_activity)[:limit]


def public(task: dict) -> dict:
    return {k: v for k, v in task.items() if not k.startswith("_")}


def _emit(task: dict) -> None:
    task["_last_emit"] = (task["status"], task["progress"], task["message"])
    events.publish({"type": "task", "task": public(task), "libraryId": task.get("libraryId"),
                    "userId": task.get("userId"), "adminOnly": True})


def _progress_pump() -> None:
    """Push progress for running tasks at most once a second."""
    while True:
        time.sleep(1)
        for task in list(_tasks.values()):
            if task["status"] == "running" and not task["quiet"] and \
                    task.get("_last_emit") != (task["status"], task["progress"], task["message"]):
                _emit(task)


def start(kind: str, title: str, fn, *args, key: str | None = None, user_id: str | None = None,
          library_id: str | None = None, quiet: bool = False, **kwargs) -> dict:
    """Run fn(task, *args) in a thread. If `key` is given, only one task with
    that key may run at a time and the running one is returned.
    `quiet` tasks (e.g. watcher scans) only notify clients when something changed."""
    global _pump_started
    with _lock:
        if key:
            for t in _tasks.values():
                if t["key"] == key and t["status"] == "running":
                    return public(t)
        task = {"id": str(uuid.uuid4()), "key": key, "kind": kind, "title": title, "status": "running",
                "progress": 0, "total": 0, "message": "", "result": None, "error": None, "quiet": quiet,
                "libraryId": library_id, "userId": user_id,
                "started_at": int(time.time() * 1000), "finished_at": None}
        _tasks[task["id"]] = task
        if not _pump_started:
            _pump_started = True
            threading.Thread(target=_progress_pump, daemon=True, name="task-progress").start()

    hold = library_id if kind in ("scan", "organize", "duplicates", "series-match") and library_id else None
    if hold and _hold_suppress:
        _hold_suppress(hold)

    def run():
        try:
            task["result"] = fn(task, *args, **kwargs)
            task["status"] = "done"
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            activity(f"{title} failed: {e}", "error")
            log.error(traceback.format_exc())
        finally:
            if hold and _hold_release:
                _hold_release(hold)
            task["finished_at"] = int(time.time() * 1000)
            _emit(task)
            _prune()

    if not quiet:
        _emit(task)
    try:
        threading.Thread(target=run, daemon=True, name=f"task-{kind}").start()
    except Exception:
        if hold and _hold_release:
            _hold_release(hold)
        raise
    return public(task)


def _prune() -> None:
    with _lock:
        done = sorted((t for t in _tasks.values() if t["status"] != "running"), key=lambda t: t["finished_at"] or 0)
        for t in done[:-30]:
            _tasks.pop(t["id"], None)


def get(task_id: str) -> dict | None:
    t = _tasks.get(task_id)
    return public(t) if t else None


def all_tasks() -> list[dict]:
    return [public(t) for t in sorted(_tasks.values(), key=lambda t: t["started_at"], reverse=True)]


def running(key: str) -> bool:
    return any(t["key"] == key and t["status"] == "running" for t in _tasks.values())
