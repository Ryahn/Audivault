"""Live refresh, trash listing, sorts, and provider pacing."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.mkdtemp(prefix="abstream-plan-")
os.environ["AUDIVAULT_DATA_DIR"] = _tmp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import fts, library, media  # noqa: E402
from app.core import db  # noqa: E402
from app.management import trash  # noqa: E402
from app.api.common import can_sql_page, sort_items  # noqa: E402
from app.providers import http as http_mod  # noqa: E402
from app.management.watcher import watcher  # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


class _Loaded:
    def __init__(self):
        self.progress = {
            "done": {"is_finished": 1, "progress": 0.2, "last_update": 1},
            "mid": {"is_finished": 0, "progress": 0.5, "last_update": 50},
            "fresh": {"is_finished": 0, "progress": 0.1, "last_update": 99},
        }
        self.authors = {}

    def author_name(self, item):
        return ""


def _books():
    return [
        {"id": "done", "title": "Done", "added_at": 1},
        {"id": "mid", "title": "Mid", "added_at": 1},
        {"id": "fresh", "title": "New", "added_at": 1},
    ]


db.init_db()

print("watcher ignores events caused by the app")
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "Author" / "Book").mkdir(parents=True)
    (root / "Author2").mkdir()
    watcher.pending.clear()
    watcher._suppressed.clear()
    watcher.note("lib-w", "fold", str(root), str(root / "Author" / "Book"))
    check("lib-w" in watcher.pending, "a real arrival is pending")
    watcher.suppress("lib-w")
    watcher.note("lib-w", "fold", str(root), str(root / "Author2"))
    scopes = watcher.pending["lib-w"]["scopes"]["fold"]
    check(scopes == {"Author"}, f"moves during a task are not recorded ({scopes})")
    watcher.note("lib-other", "fold", str(root), str(root / "Author2"))
    check("lib-other" in watcher.pending, "another library is still watched")
    watcher.pending["held"] = {"scopes": {"fold": {"Book"}}, "import": False, "last": 0.0}
    watcher.suppress("held")
    watcher._dispatch()
    check("held" in watcher.pending, "a suppressed library is not scanned")
    watcher.release("held")
    watcher.release("lib-w")
    watcher.note("lib-w", "fold", str(root), str(root / "Author2"))
    check("Author2" in watcher.pending["lib-w"]["scopes"]["fold"], "events after the task are recorded")
    watcher.pending.clear()
    watcher._suppressed.clear()

print("progress sorts stay out of SQL and order by how far along the book is")
loaded = _Loaded()
percent = [i["id"] for i in sort_items(loaded, _books(), "progress.percent", True)]
finished = [i["id"] for i in sort_items(loaded, _books(), "progress.isFinished", True)]
check(percent == ["done", "mid", "fresh"], f"percent complete order {percent}")
check(finished[0] == "done" and set(finished[1:]) == {"mid", "fresh"}, f"finished order {finished}")
check(not can_sql_page(False, "progress.percent", None, True), "percent complete is not a SQL sort")
check(not can_sql_page(False, "progress.isFinished", None, True), "finished is not a SQL sort")

print("genre and tag search")
db.ex("DROP TABLE IF EXISTS library_items_fts")
db.ex("""CREATE VIRTUAL TABLE library_items_fts USING fts5(
    item_id UNINDEXED, library_id UNINDEXED, title, subtitle, authors, narrators, series, asin, isbn)""")
fts.ensure_fts()
sql = db.q1("SELECT sql FROM sqlite_master WHERE name = 'library_items_fts'")["sql"]
check("genres" in sql and "tags" in sql, "an old search index is rebuilt with genres and tags")
now = db.now_ms()
db.insert("libraries", {
    "id": "lib-fts", "name": "FTS", "media_type": "book", "folders": "[]", "settings": "{}",
    "display_order": 1, "icon": "audiobookshelf", "created_at": now, "updated_at": now,
})
db.insert("library_items", {
    "id": "g1", "library_id": "lib-fts", "folder_id": "f", "path": "/g1", "rel_path": "g1",
    "is_file": 1, "size": 1, "added_at": now, "updated_at": now, "title": "Plain Title",
    "genres": json.dumps(["Space Opera"]), "tags": json.dumps(["award"]),
})
fts.sync_item("g1")
check(fts.search_ids("lib-fts", "space") == ["g1"], "a genre name matches")
check(fts.search_ids("lib-fts", "award") == ["g1"], "a tag matches")
check(fts.search_ids("lib-fts", "mystery") == [], "an unrelated genre does not match")

print("trash listing skips a bad batch and still returns the rest")
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    good = root / ".abstream-trash" / "20260101-120000" / "Author" / "Book"
    bad = root / ".abstream-trash" / "20260102-120000" / "Broken"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "book.mp3").write_bytes(b"x")
    now = db.now_ms()
    db.insert("libraries", {
        "id": "lib-trash", "name": "Trash", "media_type": "book",
        "folders": json.dumps([{"id": "fold-trash", "fullPath": str(root), "addedAt": now}]),
        "settings": "{}", "display_order": 2, "icon": "audiobookshelf",
        "created_at": now, "updated_at": now,
    })

    real_books = trash._books

    def flaky(batch):
        if batch.name == "20260102-120000":
            raise OSError("unreadable batch")
        return real_books(batch)

    with patch.object(trash, "_books", flaky):
        entries = trash.list_entries()
    mine = [e for e in entries if e["folderId"] == "fold-trash"]
    check(len(mine) == 1, f"one readable batch ({len(mine)})")
    check(mine and mine[0]["batch"] == "20260101-120000", "the batch under the library folder is listed")
    check(mine and any(b["relPath"].endswith("Book") for b in mine[0]["books"]), "the discarded book path is listed")

print("provider backoff")
check(library.DEFAULT_LIBRARY_SETTINGS["metadataRefreshHours"] == 0, "scheduled metadata refresh is off by default")


class _Resp:
    def __init__(self, status, body=None, retry_after=None):
        self.status_code = status
        self.headers = {"retry-after": retry_after} if retry_after is not None else {}
        self._body = body or {}
        self.content = b""

    def json(self):
        return self._body


class _Client:
    calls = 0
    script = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, *args, **kwargs):
        _Client.calls += 1
        return _Client.script.pop(0)


http_mod._gate._next.clear()
_Client.calls = 0
_Client.script = [_Resp(404)]
with patch.object(http_mod, "url_is_safe", return_value=True), patch.object(http_mod.httpx, "Client", _Client):
    missed = http_mod.get_json("https://api.audnex.us/books/x")
check(missed is None and _Client.calls == 1, "a normal failure is not retried")

http_mod._gate._next.clear()
_Client.calls = 0
_Client.script = [_Resp(429, retry_after="0"), _Resp(200, {"title": "Ok"})]
with patch.object(http_mod, "url_is_safe", return_value=True), patch.object(http_mod.httpx, "Client", _Client):
    got = http_mod.get_json("https://api.audnex.us/books/x")
check(got == {"title": "Ok"} and _Client.calls == 2, f"429 is retried ({_Client.calls} calls)")

http_mod._gate.penalize("api.audnex.us", 0.05)
started = time.monotonic()
http_mod._gate.wait("api.audnex.us")
check(time.monotonic() - started >= 0.04, "a penalized host waits before the next call")

empty = {"id": "e", "description": "", "genres": "[]", "narrators": "[]", "cover_path": None}
full = {"id": "f", "description": "text", "genres": '["Fantasy"]', "narrators": '["Ada"]', "cover_path": "/c.jpg"}
check(media._missing_metadata(empty, set()), "a book with empty fields needs a refresh")
check(not media._missing_metadata(full, {"f"}), "a complete book is left alone")
check(media._missing_metadata(full, set()), "a book missing its series still needs a refresh")

if FAIL:
    print(f"\n{len(FAIL)} failed")
    sys.exit(1)
print("\nall checks passed")
