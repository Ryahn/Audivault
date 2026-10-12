"""Unit tests for security / N+1 remediation helpers (no live server required)."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Use a throwaway data dir before importing app modules
_tmp = tempfile.mkdtemp(prefix="abstream-test-")
os.environ["AUDIVAULT_DATA_DIR"] = _tmp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import fts, library, serialize  # noqa: E402
from app.core import auth, db  # noqa: E402
from app.management import duplicates, organizer  # noqa: E402
from app.api.common import can_sql_page, sql_page_items  # noqa: E402
from app.catalog.paths import is_under  # noqa: E402
from app.providers import http as http_mod  # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def _item_row(item_id: str, title: str, title_sort: str, added_at: int, **extra):
    row = {
        "id": item_id, "library_id": "lib1", "folder_id": "f1", "path": f"/{item_id}", "rel_path": item_id,
        "ino": item_id, "is_file": 1, "mtime_ms": added_at, "ctime_ms": added_at, "birthtime_ms": added_at,
        "size": 1, "added_at": added_at, "updated_at": added_at, "is_missing": 0, "title": title,
        "title_sort": title_sort, "subtitle": None, "publisher": None, "published_year": None,
        "published_date": None, "description": None, "isbn": None, "asin": None, "language": None,
        "explicit": 0, "abridged": 0, "narrators": "[]", "genres": "[]", "tags": "[]", "cover_path": None,
        "duration": 0, "audio_files": "[]", "chapters": "[]", "ebook_file": None, "library_files": "[]",
        "num_tracks": 0, "num_audio_files": 0, "num_chapters": 0, "num_files": 0, "ebook_format": None,
        "last_scan": added_at,
    }
    row.update(extra)
    return row


print("sanitize / path confinement")
check(organizer.sanitize("..") == "", "sanitize rejects ..")
check(organizer.sanitize(".") == "", "sanitize rejects .")
check(".." not in organizer.render("{author}/{title}", {"author": "..", "title": "Book", "series": "", "sequence": "",
                                                        "subtitle": "", "year": "", "narrator": "", "asin": ""}).split("/"),
      "render drops .. author segment")
rel = organizer.render("{author}/{title}", {"author": "Normal", "title": "Book", "series": "", "sequence": "",
                                            "subtitle": "", "year": "", "narrator": "", "asin": ""})
check(rel == "Normal/Book", f"normal render ({rel})")

with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "lib"
    root.mkdir()
    inside = root / "Author" / "Book"
    check(organizer._target_under_root(inside, root), "target under root ok")
    escape = root / ".." / "outside"
    check(not organizer._target_under_root(escape, root), "target escape rejected")
    check(is_under(inside, root), "is_under inside")
    check(not is_under(Path(d) / "other", root), "is_under outside")

print("SSRF url_is_safe")
check(not http_mod.url_is_safe("http://127.0.0.1/x"), "block loopback")
check(not http_mod.url_is_safe("http://192.168.1.1/x"), "block private")
check(not http_mod.url_is_safe("http://169.254.169.254/latest"), "block link-local metadata")
check(not http_mod.url_is_safe("http://localhost/x"), "block localhost")
check(not http_mod.url_is_safe("file:///etc/passwd"), "block file scheme")
check(http_mod.url_is_safe("https://example.com/cover.jpg"), "allow public https")

print("passwords / api keys")
db.init_db()
try:
    auth.hash_password("")
    check(False, "empty password rejected")
except ValueError:
    check(True, "empty password rejected")
check(not auth.verify_password("", None), "null hash never verifies")
check(not auth.verify_password("x", None), "null hash never verifies non-empty")
user = auth.create_user("tester", "secret", "user")
token, row = auth.make_api_key(user, "k", expires_in=-10)  # already expired via JWT exp
# Force DB expiry in the past while minting a non-expiring-style check:
key_id = row["id"]
db.ex("UPDATE api_keys SET expires_at = ? WHERE id = ?", (1, key_id))
# JWT may already be expired from expires_in=-10; mint a fresh key and only expire in DB
fresh, row2 = auth.make_api_key(user, "k2", expires_in=3600)
db.ex("UPDATE api_keys SET expires_at = ? WHERE id = ?", (1, row2["id"]))
check(auth.user_from_token(fresh) is None, "API key past DB expires_at rejected")

print("FTS")
fts.ensure_fts()
db.ex(
    "INSERT INTO libraries (id, name, folders, display_order, icon, media_type, settings, created_at, updated_at) "
    "VALUES ('lib1', 'L', '[]', 0, 'database', 'book', '{}', 1, 1)"
)
now = db.now_ms()
db.insert("library_items", {
    "id": "item1", "library_id": "lib1", "folder_id": "f1", "path": "/x", "rel_path": "x", "ino": "1",
    "is_file": 1, "mtime_ms": now, "ctime_ms": now, "birthtime_ms": now, "size": 1, "added_at": now, "updated_at": now,
    "is_missing": 0, "title": "Mistborn", "title_sort": "mistborn", "subtitle": None, "publisher": None,
    "published_year": None, "published_date": None, "description": None, "isbn": None, "asin": "B000",
    "language": None, "explicit": 0, "abridged": 0, "narrators": "[]", "genres": "[]", "tags": "[]",
    "cover_path": None, "duration": 0, "audio_files": "[]", "chapters": "[]", "ebook_file": None, "library_files": "[]",
    "num_tracks": 0, "num_audio_files": 0, "num_chapters": 0, "num_files": 0, "ebook_format": None, "last_scan": now,
})
fts.sync_item("item1")
ids = fts.search_ids("lib1", "mist")
check(ids == ["item1"], f"FTS finds mistborn ({ids})")

print("admin promote guard (logic)")
# Mirror of update_user rule: only root may set type=admin
def promote_blocked(actor_type: str, new_type: str) -> bool:
    return new_type == "admin" and actor_type != "root"
check(promote_blocked("admin", "admin"), "non-root promote blocked")
check(not promote_blocked("root", "admin"), "root promote allowed")

print("signed download tokens")
dl = auth.make_download_token(user, "item1")
check(auth.user_from_download_token(dl, "item1")["id"] == user["id"], "download token valid for item")
check(auth.user_from_download_token(dl, "other") is None, "download token rejects wrong item")
check(auth.user_from_token(dl) is None, "download token is not a general access token")

print("SQL sort/LIMIT pushdown")
now = db.now_ms()
db.insert("library_items", _item_row("item2", "Zephyr", "zephyr", now + 2))
db.insert("library_items", _item_row("item3", "Alpha", "alpha", now + 3))
check(can_sql_page(False, "media.metadata.title", None, True), "can_sql_page title")
check(not can_sql_page(True, "media.metadata.title", None, True), "can_sql_page rejects collapse")
check(not can_sql_page(False, "progress", None, True), "can_sql_page rejects progress sort")
admin_user = {"id": user["id"], "type": "root", "permissions": "{}"}
paged = sql_page_items("lib1", admin_user, sort_by="media.metadata.title", desc=False, limit=2, page=0)
check(paged is not None and [r["id"] for r in paged[0]] == ["item3", "item1"], f"SQL page order ({paged})")
check(paged[1] == 3, f"SQL page total ({paged[1] if paged else None})")
page2 = sql_page_items("lib1", admin_user, sort_by="media.metadata.title", desc=False, limit=2, page=1)
check(page2 is not None and [r["id"] for r in page2[0]] == ["item2"], f"SQL page offset ({page2})")

print("series_json N+1 guard")
db.insert("authors", {"id": "a1", "library_id": "lib1", "name": "Author", "last_first": "Author",
                      "description": None, "image_path": None, "added_at": now, "updated_at": now})
db.insert("series", {"id": "s1", "library_id": "lib1", "name": "Series", "description": None,
                     "added_at": now, "updated_at": now})
db.insert("book_authors", {"item_id": "item1", "author_id": "a1", "position": 0})
db.insert("book_authors", {"item_id": "item2", "author_id": "a1", "position": 0})
db.insert("book_series", {"item_id": "item1", "series_id": "s1", "sequence": "1"})
db.insert("book_series", {"item_id": "item2", "series_id": "s1", "sequence": "2"})
books = [
    dict(db.q1("SELECT * FROM library_items WHERE id = ?", ("item1",)), series_sequence="1"),
    dict(db.q1("SELECT * FROM library_items WHERE id = ?", ("item2",)), series_sequence="2"),
]
srow = db.q1("SELECT * FROM series WHERE id = ?", ("s1",))
authors_by_id, series_by_id = library.bulk_authors_series([b["id"] for b in books])
n_calls = {"authors": 0, "series": 0}
_orig_authors, _orig_series = library.item_authors, library.item_series

def _count_authors(item_id):
    n_calls["authors"] += 1
    return _orig_authors(item_id)

def _count_series(item_id):
    n_calls["series"] += 1
    return _orig_series(item_id)

library.item_authors = _count_authors
library.item_series = _count_series
try:
    serialize.series_json(srow, books, authors_by_id=authors_by_id, series_by_id=series_by_id)
    check(n_calls["authors"] == 0 and n_calls["series"] == 0,
          f"series_json uses maps (authors={n_calls['authors']} series={n_calls['series']})")
finally:
    library.item_authors = _orig_authors
    library.item_series = _orig_series

print("duplicates count skips analyze")
analyze_calls = []
import app.management.dup_analysis as dup_analysis  # noqa: E402
_orig_analyze = dup_analysis.analyze
dup_analysis.analyze = lambda groups: analyze_calls.append(len(groups))
try:
    duplicates.find_groups(analyze=False)
    check(analyze_calls == [], f"find_groups(analyze=False) no analyze ({analyze_calls})")
finally:
    dup_analysis.analyze = _orig_analyze

print("CSRF + admin promote (HTTP)")
root = auth.create_user("rootuser", "secret", "root")
admin = auth.create_user("adminuser", "secret", "admin")
target = auth.create_user("promotee", "secret", "user")
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Avoid background scan/watcher noise during TestClient lifespan
with patch("app.main.tasks.start"), patch("app.main.watcher.start"), \
     patch("app.main.watcher.sync"), patch("app.management.autoimport.start"), \
     patch("app.api.items.warm_cover_thumbnails"):
    with TestClient(app) as client:
        login = client.post("/login", json={"username": "rootuser", "password": "secret"})
        check(login.status_code == 200, f"root login ({login.status_code})")
        # Cookie session without CSRF header on mutating route
        bad = client.post("/api/libraries", json={"name": "Lib", "folders": []})
        check(bad.status_code == 403, f"CSRF rejects cookie POST without header ({bad.status_code})")
        csrf = client.cookies.get("csrf_token")
        ok = client.post(
            "/api/libraries",
            json={"name": "Lib", "folders": []},
            headers={"X-CSRF-Token": csrf or ""},
        )
        # Empty folders may 400; CSRF itself must not 403 when header matches
        check(ok.status_code != 403, f"CSRF allows matching header ({ok.status_code})")

        # Non-root admin cannot promote
        client.cookies.clear()
        alogin = client.post("/login", json={"username": "adminuser", "password": "secret"})
        check(alogin.status_code == 200, f"admin login ({alogin.status_code})")
        acsrf = client.cookies.get("csrf_token")
        promo = client.patch(
            f"/api/users/{target['id']}",
            json={"type": "admin"},
            headers={"X-CSRF-Token": acsrf or ""},
        )
        check(promo.status_code == 403, f"non-root promote blocked HTTP ({promo.status_code})")
        still = db.q1("SELECT type FROM users WHERE id = ?", (target["id"],))
        check(still and still["type"] == "user", f"promotee remains user ({still})")

        # Cheap duplicates count endpoint does not call analyze
        analyze_calls.clear()
        dup_analysis.analyze = lambda groups: analyze_calls.append(len(groups))
        try:
            client.cookies.clear()
            client.post("/login", json={"username": "adminuser", "password": "secret"})
            cnt = client.get("/api/abstream/duplicates/count")
            check(cnt.status_code == 200 and "count" in cnt.json(), f"duplicates count ({cnt.status_code})")
            check(analyze_calls == [], f"count endpoint skips analyze ({analyze_calls})")
        finally:
            dup_analysis.analyze = _orig_analyze

print("media root cache")
from app.catalog import paths as paths_mod  # noqa: E402

paths_mod.invalidate_media_roots()
root_calls = {"n": 0}
_real_roots = paths_mod.library_folder_roots


def _counting_roots():
    root_calls["n"] += 1
    return _real_roots()


with patch.object(paths_mod, "library_folder_roots", _counting_roots):
    paths_mod.allowed_media_roots()
    paths_mod.allowed_media_roots()
    check(root_calls["n"] == 1, f"library roots resolved once ({root_calls['n']})")
    paths_mod.invalidate_media_roots()
    paths_mod.allowed_media_roots()
    check(root_calls["n"] == 2, "invalidate refreshes library roots")
paths_mod.invalidate_media_roots()

print("cover thumbnail cache")
import json  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402
from app.api.items import _FULL_COVER_CACHE, _GRID_COVER_CACHE, _cover_thumb_path, get_cover, warm_cover_thumbnails  # noqa: E402
from app.scanner import scanner  # noqa: E402

_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
cover_root = Path(tempfile.mkdtemp(prefix="abstream-cover-"))
book = cover_root / "Book"
book.mkdir()
cover_file = book / "cover.png"
Image.new("RGB", (80, 80), (180, 40, 40)).save(cover_file, "PNG")
(book / "a.mp3").write_bytes(_FRAME * 20)
cover_now = db.now_ms()
db.ex(
    "INSERT INTO libraries (id, name, folders, display_order, icon, media_type, settings, created_at, updated_at) "
    "VALUES ('coverlib', 'Covers', ?, 2, 'database', 'book', '{}', ?, ?)",
    (json.dumps([{"id": "coverfolder", "fullPath": str(cover_root), "addedAt": cover_now}]), cover_now, cover_now),
)
paths_mod.invalidate_media_roots()
found = scanner.discover(str(cover_root))
check(len(found) == 1, f"discovered cover book ({list(found)})")
rel, entry = next(iter(found.items()))
cover_lib = library.get_library("coverlib")
item_id, action = scanner.scan_item_entry(cover_lib, {"id": "coverfolder", "fullPath": str(cover_root)}, rel, entry, None)
check(action == "added" and item_id, f"scanned cover book ({action})")

try:
    get_cover("../secrets", 300)
    check(False, "cover id traversal is 404")
except HTTPException as exc:
    check(exc.status_code == 404, f"cover id traversal is 404 ({exc.status_code})")

first = get_cover(item_id, 300)
first_bytes = Path(first.path).read_bytes()
check(first.headers.get("cache-control") == _GRID_COVER_CACHE, f"grid cache header ({first.headers.get('cache-control')})")
check(first_bytes[:3] == b"\xff\xd8\xff", "grid cover is a jpeg")
full = get_cover(item_id, None)
check(full.headers.get("cache-control") == _FULL_COVER_CACHE, f"full cover cache header ({full.headers.get('cache-control')})")

real_getmtime = os.path.getmtime
real_allowed = paths_mod.is_allowed_media_path


def _guard_mtime(path):
    if os.path.normcase(str(path)) == os.path.normcase(str(cover_file)):
        raise AssertionError("statted original cover")
    return real_getmtime(path)


def _guard_allowed(path):
    if os.path.normcase(str(path)) == os.path.normcase(str(cover_file)):
        raise AssertionError("allowlisted original cover")
    return real_allowed(path)


with patch("os.path.getmtime", _guard_mtime), patch.object(paths_mod, "is_allowed_media_path", _guard_allowed):
    cached = get_cover(item_id, 300)
check(Path(cached.path).read_bytes() == first_bytes, "cached thumb served without the original")

Image.new("RGB", (30, 90), (20, 20, 200)).save(cover_file, "PNG")
still = get_cover(item_id, 300)
check(Path(still.path).read_bytes() == first_bytes, "replaced cover is ignored until a scan")
thumb_path = _cover_thumb_path(item_id, 300)
rescan = scanner.rescan_item(item_id, force=False)
check(rescan == "updated", f"cover change updates the item ({rescan})")
check(thumb_path is not None and not thumb_path.exists(), "scan drops the stale thumbnail")
warm_cover_thumbnails()
check(thumb_path.is_file(), "warmup rebuilds the missing thumbnail")
rebuilt = get_cover(item_id, 300)
check(Path(rebuilt.path).read_bytes() != first_bytes, "rebuilt thumbnail follows the new cover")
library.delete_item_records(item_id)
scanner.remove_item_cache(item_id)
try:
    get_cover(item_id, 300)
    check(False, "deleted item cover is gone")
except HTTPException as exc:
    check(exc.status_code == 404, f"deleted item cover is gone ({exc.status_code})")

print("organize case-only rename and leftover entries")
with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "lib"
    book = root / "Kiri Komori" / "Reincarnated as the Last of my Kind" / "1 - Book"
    sibling = root / "Kiri Komori" / "Reincarnated as the Last of my Kind" / "2 - Other"
    book.mkdir(parents=True)
    sibling.mkdir()
    (book / "a.mp3").write_bytes(b"x")
    taken = root / "Taken" / "Book"
    taken.mkdir(parents=True)
    loose = root / "Loose" / "Something"
    loose.mkdir(parents=True)
    now = db.now_ms()
    lib_id = "lib-case"
    db.insert("libraries", {
        "id": lib_id, "name": "Case", "media_type": "book",
        "folders": json.dumps([{"id": "fold-case", "fullPath": str(root), "addedAt": now}]),
        "settings": "{}", "display_order": 1, "icon": "audiobookshelf",
        "created_at": now, "updated_at": now,
    })
    ino = str(os.stat(book).st_ino or "shared-inode")
    live = _item_row("case-live", "Book", "book", now, library_id=lib_id, folder_id="fold-case",
                     path=str(book), rel_path="Kiri Komori/Reincarnated as the Last of my Kind/1 - Book",
                     ino=ino, is_file=0)
    other = _item_row("case-sib", "Other", "other", now, library_id=lib_id, folder_id="fold-case",
                      path=str(sibling), rel_path="Kiri Komori/Reincarnated as the Last of my Kind/2 - Other",
                      ino="sib-ino", is_file=0)
    ghost = _item_row("case-ghost", "Book", "book", now, library_id=lib_id, folder_id="fold-case",
                      path=str(root / "XKarnation" / "Reborn" / "3 - Book"),
                      rel_path="XKarnation/Reborn/3 - Book", ino=ino, is_file=0, is_missing=1)
    blocked = _item_row("case-block", "Book", "book", now, library_id=lib_id, folder_id="fold-case",
                        path=str(loose), rel_path="Loose/Something", ino="block-ino", is_file=0)
    for row in (live, other, ghost, blocked):
        db.insert("library_items", row)
    library.set_item_authors("case-live", lib_id, ["Kiri Komori"])
    library.set_item_series("case-live", lib_id, [("Reincarnated as the Last of My Kind", "1")])
    library.set_item_authors("case-sib", lib_id, ["Kiri Komori"])
    library.set_item_series("case-sib", lib_id, [("Reincarnated as the Last of My Kind", "2")])
    library.set_item_authors("case-block", lib_id, ["Taken"])

    plans = {p["id"]: p for p in organizer.plan_library(lib_id)}
    check(plans["case-live"]["status"] == "ok" and plans["case-live"]["caseOnly"],
          f"capitalization change is ready to rename ({plans['case-live']['status']} {plans['case-live']['detail']})")
    check(plans["case-ghost"]["status"] == "stale" and plans["case-ghost"]["keepId"] == "case-live",
          f"missing row that shares the folder is a leftover ({plans['case-ghost']['status']})")
    check(plans["case-block"]["status"] == "conflict" and not plans["case-block"]["caseOnly"],
          f"a different folder at the destination stays a conflict ({plans['case-block']['status']} {plans['case-block']['detail']})")
    check(organizer._dest_key("Author/My Kind", True) == organizer._dest_key("Author/my kind", True),
          "case-insensitive destinations share a key")

    organizer._apply_case_rename(lib_id, str(book), str(root / "Kiri Komori" / "Reincarnated as the Last of My Kind" / "1 - Book"), root)
    series_names = [p.name for p in (root / "Kiri Komori").iterdir()]
    check(series_names == ["Reincarnated as the Last of My Kind"], f"series folder capitalization fixed ({series_names})")
    sib = db.q1("SELECT path, rel_path FROM library_items WHERE id = ?", ("case-sib",))
    check("My Kind" in sib["path"] and sib["rel_path"].endswith("2 - Other"), f"sibling path follows the rename ({sib['rel_path']})")
    ghost_row = db.q1("SELECT path FROM library_items WHERE id = ?", ("case-ghost",))
    check("XKarnation" in ghost_row["path"], "leftover row is not retargeted onto the live book")

    organizer._drop_stale("case-ghost", "case-live")
    check(db.q1("SELECT id FROM library_items WHERE id = ?", ("case-ghost",)) is None, "leftover entry removed")
    check(book.exists() and (book / "a.mp3").exists(), "dropping the leftover does not delete the files")

if FAIL:
    print(f"\n{len(FAIL)} failure(s)")
    sys.exit(1)
print("\nAll remediation unit checks passed")
