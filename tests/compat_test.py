"""Compatibility checks that do not need the generated audiobook tree.

Covers login and token refresh, /status naming, a byte-range audio request,
progress and bookmarks, collection and playlist membership, and opening a
database that predates a library_items column.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_root = tempfile.mkdtemp(prefix="abstream-compat-")
os.environ["ABSTREAM_DATA_DIR"] = _root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import auth, db  # noqa: E402
from app.main import app  # noqa: E402


def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        sys.exit(1)


def _item_row(item_id, path, library_files, audio_files):
    now = db.now_ms()
    return {
        "id": item_id, "library_id": "lib1", "folder_id": "f1", "path": path, "rel_path": item_id,
        "ino": item_id, "is_file": 1, "mtime_ms": now, "ctime_ms": now, "birthtime_ms": now,
        "size": 8, "added_at": now, "updated_at": now, "is_missing": 0,
        "title": "Compat", "title_sort": "compat", "subtitle": None, "publisher": None,
        "published_year": None, "published_date": None, "description": None, "isbn": None,
        "asin": None, "language": None, "explicit": 0, "abridged": 0, "narrators": "[]",
        "genres": "[]", "tags": "[]", "cover_path": None, "duration": 10,
        "audio_files": audio_files, "chapters": "[]", "ebook_file": None,
        "library_files": library_files,
    }


def _without_count_column():
    cols = [r["name"] for r in db.q("PRAGMA table_info(library_items)") if r["name"] != "num_tracks"]
    listed = ", ".join(cols)
    db.ex(f"CREATE TABLE library_items_oldshape AS SELECT {listed} FROM library_items")
    db.ex("DROP TABLE library_items")
    db.ex("ALTER TABLE library_items_oldshape RENAME TO library_items")


print("existing database missing a later column")
db.init_db()
db.insert("library_items", _item_row("old", str(Path(_root) / "old"), "[]", "[]"))
_without_count_column()
db.init_db()
names = {r["name"] for r in db.q("PRAGMA table_info(library_items)")}
check("num_tracks" in names, "num_tracks added when an older file is opened")
old = db.q1("SELECT num_tracks FROM library_items WHERE id = 'old'")
check(old is not None and old["num_tracks"] == 0, "counts backfilled for the existing row")

db.ex(
    "INSERT INTO libraries (id, name, folders, display_order, icon, media_type, settings, created_at, updated_at) "
    "VALUES ('lib1', 'L', '[]', 0, 'database', 'book', '{}', 1, 1)"
)
audio = Path(_root) / "clip.mp3"
audio.write_bytes(b"01234567")
file_json = (
    '[{"ino":"a1","path":%s,"rel_path":"clip.mp3","filename":"clip.mp3","ext":".mp3","size":8,"file_type":"audio"}]'
    % __import__("json").dumps(str(audio))
)
audio_json = (
    '[{"ino":"a1","index":1,"duration":10,"mime_type":"audio/mpeg","filename":"clip.mp3","ext":".mp3",'
    '"path":%s,"rel_path":"clip.mp3","size":8,"added_at":0,"updated_at":0}]'
    % __import__("json").dumps(str(audio))
)
db.insert("library_items", _item_row("item1", str(Path(_root) / "book"), file_json, audio_json))
user = auth.create_user("compat", "secret", "root")

with patch("app.main.tasks.start"), patch("app.main.watcher.start"), patch("app.main.watcher.sync"), \
        patch("app.management.autoimport.start"), patch("app.api.items.warm_cover_thumbnails"):
    with TestClient(app) as client:
        print("status and login")
        status = client.get("/status").json()
        check(status["app"] == "audiobookshelf", "status app stays audiobookshelf")
        check(status["appName"] == "Audivault", "status appName is Audivault")
        login = client.post("/login", json={"username": "compat", "password": "secret"},
                            headers={"x-return-tokens": "true"}).json()
        access = login["user"]["accessToken"]
        refresh = login["user"]["refreshToken"]
        check(bool(access and refresh), "login returns access and refresh tokens")
        auth_headers = {"Authorization": f"Bearer {access}"}
        rotated = client.post("/auth/refresh", headers={"x-refresh-token": refresh}).json()
        new_access = rotated["user"]["accessToken"]
        new_refresh = rotated["user"]["refreshToken"]
        check(bool(new_access and new_refresh and new_refresh != refresh), "refresh rotates the refresh token")
        stale = client.post("/auth/refresh", headers={"x-refresh-token": refresh})
        check(stale.status_code == 401, "spent refresh token is rejected")
        auth_headers = {"Authorization": f"Bearer {new_access}"}

        print("byte range")
        ranged = client.get(f"/api/items/item1/file/a1", headers={**auth_headers, "Range": "bytes=0-3"})
        check(ranged.status_code == 206, f"range status {ranged.status_code}")
        check(ranged.content == b"0123", "range body is the first four bytes")

        print("progress and bookmarks")
        progress = client.patch("/api/me/progress/item1", headers=auth_headers,
                                json={"currentTime": 4, "duration": 10, "progress": 0.4}).json()
        check(progress["currentTime"] == 4, "progress stored")
        mark = client.post("/api/me/item/item1/bookmark", headers=auth_headers,
                           json={"time": 4, "title": "here"}).json()
        check(mark["title"] == "here", "bookmark created")
        renamed = client.patch("/api/me/item/item1/bookmark", headers=auth_headers,
                               json={"time": 4, "title": "there"}).json()
        check(renamed["title"] == "there", "bookmark updated")
        removed = client.delete("/api/me/item/item1/bookmark/4", headers=auth_headers)
        check(removed.status_code == 200, "bookmark deleted")
        check(db.q1("SELECT id FROM bookmarks WHERE user_id = ?", (user["id"],)) is None, "bookmark row is gone")

        print("collections and playlists")
        collection = client.post("/api/collections", headers=auth_headers,
                                 json={"name": "C", "libraryId": "lib1", "books": ["item1", "missing"]}).json()
        check([b["id"] for b in collection["books"]] == ["item1"], "collection skips an unknown book")
        cid = collection["id"]
        cleared = client.patch(f"/api/collections/{cid}", headers=auth_headers, json={"books": []}).json()
        check(cleared["books"] == [], "collection batch update can empty the list")
        batched = client.post(f"/api/collections/{cid}/batch/add", headers=auth_headers, json={"books": ["item1", "missing"]}).json()
        check([b["id"] for b in batched["books"]] == ["item1"], "collection batch add skips an unknown book")
        added = client.post(f"/api/collections/{cid}/book", headers=auth_headers, json={"id": "item1"}).json()
        check([b["id"] for b in added["books"]] == ["item1"], "collection add does not duplicate")
        client.delete(f"/api/collections/{cid}/book/item1", headers=auth_headers)
        check(db.q("SELECT library_item_id FROM collection_books WHERE collection_id = ?", (cid,)) == [], "collection remove")

        playlist = client.post("/api/playlists", headers=auth_headers, json={
            "name": "P", "libraryId": "lib1", "items": [{"libraryItemId": "item1"}, "missing"],
        }).json()
        check([i["libraryItemId"] for i in playlist["items"]] == ["item1"], "playlist accepts libraryItemId and skips unknown ids")
        pid = playlist["id"]
        batch = client.post(f"/api/playlists/{pid}/batch/add", headers=auth_headers,
                             json={"items": [{"libraryItemId": "item1"}]}).json()
        check(len(batch["items"]) == 1, "playlist batch add does not duplicate")
        episode = client.delete(f"/api/playlists/{pid}/item/item1/ep1", headers=auth_headers)
        check(episode.status_code == 200, "playlist episode delete route")
        check(db.q("SELECT library_item_id FROM playlist_items WHERE playlist_id = ?", (pid,)) == [],
              "episode route removes the playlist item")
        again = client.post(f"/api/playlists/{pid}/item", headers=auth_headers,
                            json={"libraryItemId": "item1"}).json()
        check(len(again["items"]) == 1, "playlist add")
        client.delete(f"/api/playlists/{pid}/item/item1", headers=auth_headers)
        check(db.q("SELECT library_item_id FROM playlist_items WHERE playlist_id = ?", (pid,)) == [], "playlist remove")

print("\nAll compatibility checks passed")
