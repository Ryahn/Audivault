"""Watches library and import folders and triggers partial scans.

Changes are collected per library and grouped by top-level entry (usually an
author folder, or a loose file). Once a library has been quiet for
ABSTREAM_WATCH_DELAY seconds (so a book that is still being copied is not
scanned half-way), only those entries are rescanned. Import-folder changes are
announced to web clients; importing still needs a person to confirm metadata.

Native file events don't work everywhere (network shares, Docker Desktop on
Windows/macOS), so a library can use polling instead; `auto` picks polling when
ABSTREAM_WATCH_POLLING is set.
"""

import logging
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from ..catalog import library
from ..core import db, events, tasks
from ..scanner import scanner

log = logging.getLogger("abstream.watcher")

FORCE_POLLING = os.environ.get("ABSTREAM_WATCH_POLLING", "").lower() in ("1", "true", "yes")
POLL_INTERVAL = float(os.environ.get("ABSTREAM_WATCH_INTERVAL", "30"))
QUIET_SECONDS = float(os.environ.get("ABSTREAM_WATCH_DELAY", "15"))

IGNORED_NAMES = {"Thumbs.db", ".DS_Store", "desktop.ini"}
IGNORED_SUFFIXES = (".abstream-tmp", ".part", ".tmp", ".crdownload", ".partial", "~")
# Read-only events: the scanner itself opens files, which must not trigger more scans
IGNORED_EVENTS = {"opened", "closed_no_write"}


def resolve_mode(settings: dict) -> str:
    mode = settings.get("watchMode") or "auto"
    if mode == "auto":
        return "polling" if FORCE_POLLING else "native"
    return mode if mode in ("native", "polling") else "native"


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "Watcher", library_id: str, target: str, root: str):
        self.watcher, self.library_id, self.target, self.root = watcher, library_id, target, root

    def on_any_event(self, event):
        if event.event_type in IGNORED_EVENTS:
            return
        for path in (event.src_path, getattr(event, "dest_path", "") or ""):
            if path:
                self.watcher.note(self.library_id, self.target, self.root, os.fsdecode(path))


class Watcher:
    def __init__(self):
        self.lock = threading.Lock()
        self.observers: dict[tuple, object] = {}
        self.pending: dict[str, dict] = {}
        self.import_counts: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}
        self.started = False

    def suppress(self, library_id: str) -> None:
        """Ignore filesystem events for this library until the matching release()."""
        if not library_id:
            return
        with self.lock:
            self._suppressed[library_id] = self._suppressed.get(library_id, 0) + 1

    def release(self, library_id: str) -> None:
        """Stop ignoring events. Notes taken while suppressed never entered pending."""
        if not library_id:
            return
        with self.lock:
            left = self._suppressed.get(library_id, 0) - 1
            if left > 0:
                self._suppressed[library_id] = left
            else:
                self._suppressed.pop(library_id, None)

    # ------------------------------------------------------------ events in
    def note(self, library_id: str, target: str, root: str, path: str) -> None:
        name = os.path.basename(path)
        if name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES) or ".abstream-tmp" in path:
            return
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            return
        if rel in (".", "") or rel.startswith("."):  # also skips hidden folders like .abstream-trash
            return
        with self.lock:
            if self._suppressed.get(library_id):
                return
            entry = self.pending.setdefault(library_id, {"scopes": {}, "import": False, "last": 0.0})
            if target == "import":
                entry["import"] = True
                scanner.invalidate_discover(root)
            else:
                entry["scopes"].setdefault(target, set()).add(rel.replace("\\", "/").split("/", 1)[0])
            entry["last"] = time.time()

    # ------------------------------------------------------------ observers
    def _desired(self) -> dict[tuple, tuple[str, str, str]]:
        want = {}
        for lib in library.all_libraries():
            settings = library.library_settings(lib)
            if settings.get("disableWatcher"):
                continue
            mode = resolve_mode(settings)
            for folder in db.jload(lib["folders"], []):
                if os.path.isdir(folder["fullPath"]):
                    want[(lib["id"], folder["id"], folder["fullPath"], mode)] = (lib["id"], folder["id"], folder["fullPath"])
            imp = settings.get("importFolder")
            if imp and settings.get("watchImportFolder") and os.path.isdir(imp):
                want[(lib["id"], "import", imp, mode)] = (lib["id"], "import", imp)
        return want

    def sync(self) -> None:
        """Start/stop observers to match the current library settings."""
        want = self._desired()
        with self.lock:
            for key in [k for k in self.observers if k not in want]:
                obs = self.observers.pop(key)
                try:
                    obs.stop()
                except Exception:
                    pass
            for key, (library_id, target, path) in want.items():
                if key in self.observers:
                    continue
                mode = key[3]
                obs = PollingObserver(timeout=POLL_INTERVAL) if mode == "polling" else Observer()
                try:
                    obs.schedule(_Handler(self, library_id, target, path), path, recursive=True)
                    obs.daemon = True
                    obs.start()
                    self.observers[key] = obs
                    log.info("Watching %s (%s, %s)", path, "import folder" if target == "import" else "library", mode)
                except Exception as e:
                    tasks.activity(f"Could not watch {path}: {e}", "warning")

    def pending_count(self) -> int:
        """Libraries with changes waiting for their quiet period (or for a running job)."""
        with self.lock:
            return len(self.pending)

    def status(self) -> list[dict]:
        with self.lock:
            return [{"libraryId": k[0], "target": "import" if k[1] == "import" else "library", "path": k[2], "mode": k[3]}
                    for k in self.observers]

    # ------------------------------------------------------------ dispatch
    def _dispatch(self) -> None:
        now = time.time()
        with self.lock:
            pending = dict(self.pending)
        ready = [lid for lid, p in pending.items() if now - p["last"] >= self._quiet_seconds(lid)]
        for library_id in ready:
            with self.lock:
                if self._suppressed.get(library_id):
                    continue  # our own moves; leave any earlier pending until the task ends
            if tasks.running(f"scan:{library_id}") or tasks.running(f"organize:{library_id}") or tasks.running("duplicates:batch"):
                continue  # retry after the running job; its own changes get picked up then
            with self.lock:
                if self._suppressed.get(library_id):
                    continue
                entry = self.pending.pop(library_id, None)
            if not entry:
                continue
            lib = library.get_library(library_id)
            if not lib:
                continue
            if entry["scopes"]:
                scopes = {fid: sorted(s) for fid, s in entry["scopes"].items()}
                log.info("Partial scan of %s: %s", lib["name"], scopes)
                tasks.start("scan", f"Folder change in {lib['name']}", scanner.scan_library, library_id, False, scopes,
                            key=f"scan:{library_id}", library_id=library_id, quiet=True)
            if entry["import"]:
                self._announce_import(lib)

    def _quiet_seconds(self, library_id: str) -> float:
        """Polling only notices changes once per interval, so wait at least one full interval:
        otherwise a slow copy can look finished between two polls."""
        lib = library.get_library(library_id)
        if lib and resolve_mode(library.library_settings(lib)) == "polling":
            return max(QUIET_SECONDS, POLL_INTERVAL + 5)
        return QUIET_SECONDS

    def _announce_import(self, lib: dict) -> None:
        folder = library.library_settings(lib).get("importFolder")
        if not folder or not os.path.isdir(folder):
            return
        count = len(scanner.cached_discover(folder))
        previous = self.import_counts.get(lib["id"], 0)
        self.import_counts[lib["id"]] = count
        if count > previous:
            tasks.activity(f"{count - previous} new item(s) in the import folder of \"{lib['name']}\"")
        events.publish({"type": "import_changed", "libraryId": lib["id"], "count": count, "new": max(0, count - previous)})
        if library.library_settings(lib).get("autoImport") and count:
            from . import autoimport
            autoimport.start(lib["id"])

    def _loop(self) -> None:
        last_sync = time.time()
        while True:
            time.sleep(2)
            try:
                self._dispatch()
                if time.time() - last_sync > 300:  # pick up folders that appeared (e.g. a share mounted late)
                    self.sync()
                    last_sync = time.time()
            except Exception:
                log.exception("Watcher loop error")

    def _boot(self) -> None:
        """Count import folders and start observers. This walks every audiobook
        file, which on a Docker Desktop bind mount can take minutes — it must
        not run before the HTTP server is listening."""
        try:
            for lib in library.all_libraries():
                folder = library.library_settings(lib).get("importFolder")
                if folder and os.path.isdir(folder):
                    self.import_counts[lib["id"]] = len(scanner.cached_discover(folder))
            self.sync()
        except Exception:
            log.exception("Watcher startup failed")
        self._loop()

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._boot, daemon=True, name="watcher").start()


watcher = Watcher()
tasks.set_library_hold(watcher.suppress, watcher.release)
