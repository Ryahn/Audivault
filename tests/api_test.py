"""End-to-end API test mimicking the AudioBooth iOS client's requests and its
Swift Decodable requirements (required keys, Int64 timestamps, etc.)."""
import base64
import os
import json
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("ABSTREAM_TEST_URL", "http://127.0.0.1:13399")
TD = Path(sys.argv[1])
# Where the server sees the test data (differs when the server runs in Docker)
SRV = os.environ.get("ABSTREAM_TEST_SERVER_ROOT", str(TD)).rstrip("/\\")
SEP = "/" if SRV.startswith("/") else os.sep
FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def opt(d, k, pred):
    return k not in d or d[k] is None or pred(d[k])


# --------------------------------------------------------------- Swift model validators

def v_chapter(c, p):
    check(is_int(c.get("id")) and num(c.get("start")) and num(c.get("end")) and isinstance(c.get("title"), str), f"{p}: bad Chapter {c}")


def v_track(t, p):
    check(is_int(t.get("index")) and num(t.get("startOffset")) and num(t.get("duration")), f"{p}: bad AudioTrack")
    check(opt(t, "updatedAt", is_int), f"{p}: track.updatedAt not int")


def v_libfile(f, p):
    m = f.get("metadata") or {}
    check(isinstance(f.get("ino"), str), f"{p}: libfile.ino")
    for k in ("filename", "ext", "path", "relPath"):
        check(isinstance(m.get(k), str), f"{p}: libfile.metadata.{k}")
    check(is_int(m.get("size")) and is_int(m.get("birthtimeMs")), f"{p}: libfile size/birthtime ints")
    check(is_int(f.get("addedAt")) and is_int(f.get("updatedAt")), f"{p}: libfile dates")


def v_book(b, p="book"):
    check(isinstance(b.get("id"), str) and isinstance(b.get("libraryId"), str), f"{p}: id/libraryId")
    check(is_int(b.get("addedAt")) and is_int(b.get("updatedAt")), f"{p}: addedAt/updatedAt must be Int64")
    media = b.get("media") or {}
    md = media.get("metadata") or {}
    check(isinstance(md.get("title"), str), f"{p}: metadata.title")
    for k in ("subtitle", "publishedYear", "authorName", "narratorName", "seriesName", "publisher", "description", "isbn", "asin", "language"):
        check(opt(md, k, lambda v: isinstance(v, str)), f"{p}: metadata.{k} type")
    check(opt(md, "authors", lambda v: all(isinstance(a.get("id"), str) and isinstance(a.get("name"), str) for a in v)), f"{p}: authors")
    s = md.get("series")
    if isinstance(s, list):
        for x in s:
            check(isinstance(x.get("id"), str) and isinstance(x.get("name"), str), f"{p}: series entry")
    elif isinstance(s, dict):
        check(isinstance(s.get("id"), str) and isinstance(s.get("name"), str), f"{p}: series obj")
    check(opt(media, "duration", num) and opt(media, "size", is_int) and opt(media, "numTracks", is_int), f"{p}: media nums")
    for c in media.get("chapters") or []:
        v_chapter(c, p)
    for t in media.get("tracks") or []:
        v_track(t, p)
    for f in b.get("libraryFiles") or []:
        v_libfile(f, p)
    cs = b.get("collapsedSeries")
    if cs:
        check(isinstance(cs.get("id"), str) and is_int(cs.get("numBooks")) and isinstance(cs.get("libraryItemIds"), list), f"{p}: collapsedSeries")


def v_author(a, p="author"):
    check(isinstance(a.get("id"), str) and isinstance(a.get("name"), str), f"{p}: id/name")
    check(opt(a, "addedAt", is_int) and opt(a, "updatedAt", is_int) and opt(a, "numBooks", is_int), f"{p}: ints")


def v_series(s, p="series"):
    inner = s.get("series", s)
    check(isinstance(inner.get("id"), str) and isinstance(inner.get("name"), str), f"{p}: id/name")
    check(opt(inner, "addedAt", is_int), f"{p}: addedAt int")
    check(isinstance(s.get("books"), list), f"{p}: books required")
    for b in s.get("books", []):
        v_book(b, p + ".book")


def v_progress(mp, p="progress"):
    for k in ("id", "libraryItemId"):
        check(isinstance(mp.get(k), str), f"{p}: {k}")
    check(num(mp.get("progress")) and isinstance(mp.get("isFinished"), bool) and num(mp.get("currentTime")), f"{p}: values")
    check(is_int(mp.get("startedAt")) and is_int(mp.get("lastUpdate")), f"{p}: startedAt/lastUpdate Int64")


def v_session_sync(s, p="sessionSync"):
    for k in ("id", "libraryItemId", "mediaType"):
        check(isinstance(s.get(k), str), f"{p}: {k}")
    for k in ("duration", "startTime", "currentTime"):
        check(num(s.get(k)), f"{p}: {k}")
    check(is_int(s.get("playMethod")) and is_int(s.get("startedAt")) and is_int(s.get("updatedAt")), f"{p}: ints")
    check(isinstance(s.get("deviceInfo"), dict), f"{p}: deviceInfo must be an object")


def v_page(d, p):
    check(is_int(d.get("total")) and is_int(d.get("page")), f"{p}: Page needs total+page")


# --------------------------------------------------------------- run

c = httpx.Client(base_url=BASE, timeout=30)

print("status / init / login")
st = c.get("/status").json()
check("audiobookshelf" in st["app"], "status.app must contain audiobookshelf")
if not st["isInit"]:
    check(c.post("/init", json={"newRoot": {"username": "admin", "password": "secret"}}).status_code == 200, "init")
r = c.post("/login", json={"username": "admin", "password": "wrong"})
check(r.status_code == 401, "bad login -> 401")
r = c.post("/login", json={"username": "admin", "password": "secret"}, headers={"x-return-tokens": "true"})
check(r.status_code == 200, f"login {r.status_code} {r.text[:200]}")
login = r.json()
user = login["user"]
for k in ("userDefaultLibraryId", "ereaderDevices", "serverSettings"):
    check(k in login, f"login.{k}")
check(all(k in login["serverSettings"] for k in ("id", "version", "sortingIgnorePrefix")), "serverSettings keys")
check(all(k in user["permissions"] for k in ("update", "delete", "download")), "permissions keys")
access, refresh = user["accessToken"], user["refreshToken"]
payload = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "=="))
check(payload.get("type") == "access" and is_int(payload.get("exp")), "access JWT has type+exp")
H = {"Authorization": f"Bearer {access}"}

print("create library")
lib = c.post("/api/libraries", headers=H, json={"name": "Books", "folders": [{"fullPath": SRV + SEP + "library"}],
                                                 "settings": {"importFolder": SRV + SEP + "import"}}).json()
lib_id = lib["id"]
for _ in range(60):
    tasks = c.get("/api/abstream/tasks", headers=H).json()["tasks"]
    if tasks and all(t["status"] != "running" for t in tasks):
        break
    time.sleep(0.5)
print("  tasks:", [(t["title"], t["status"], t.get("result"), t.get("error")) for t in tasks])

r = c.post("/api/authorize", headers=H).json()
check(r["userDefaultLibraryId"] == lib_id, "authorize default library")
libs = c.get("/api/libraries", headers=H).json()["libraries"]
check(len(libs) == 1 and libs[0]["mediaType"] == "book", "libraries list")

print("items")
page = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"minified": 1, "limit": 50, "page": 0, "sort": "media.metadata.title"}).json()
v_page(page, "items")
titles = sorted(b["media"]["metadata"]["title"] for b in page["results"])
print("  ", page["total"], titles)
check(page["total"] == 5, "expected 5 items")
for b in page["results"]:
    v_book(b, "items[]")
by_title = {b["media"]["metadata"]["title"]: b for b in page["results"]}
fe = by_title.get("The Final Empire")
check(fe and fe["media"]["metadata"]["seriesName"] == "Mistborn #1", "Final Empire series parsed from folder")
check(fe and fe["media"]["numTracks"] == 3, "Final Empire has 3 tracks")
wa = by_title.get("Witches Abroad")
check(wa and wa["media"]["numTracks"] == 4 and wa["media"]["metadata"]["seriesName"] == "Discworld #12", "Disc folders merged + series seq 12")
phm = by_title.get("Project Hail Mary")
check(phm and phm["media"]["coverPath"], "embedded cover extracted")
check(phm and phm["media"]["metadata"]["narratorName"] == "Ray Porter", "narrator from composer tag")
check("A Loose Single" in by_title and by_title["A Loose Single"]["isFile"], "loose file item")

col = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"minified": 1, "collapseseries": 1, "limit": 50}).json()
cs = [b for b in col["results"] if b.get("collapsedSeries")]
check(any(x["collapsedSeries"]["name"] == "Mistborn" and x["collapsedSeries"]["numBooks"] == 2 for x in cs), "collapsed Mistborn series")
for b in col["results"]:
    v_book(b, "collapsed[]")

desc = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"sort": "addedAt", "desc": 1, "limit": 2, "page": 1}).json()
check(len(desc["results"]) == 2 and desc["page"] == 1, "paging")

print("item expanded")
for b in page["results"]:
    v_book(c.get(f"/api/items/{b['id']}", headers=H, params={"expanded": 1}).json(), f"expanded {b['media']['metadata']['title']}")
woa = c.get(f"/api/items/{by_title['The Well of Ascension']['id']}", headers=H, params={"expanded": 1}).json()
check([ch["title"] for ch in woa["media"]["chapters"]] == ["Prologue", "Chapter 1"], "chapters from metadata.json")
item = c.get(f"/api/items/{phm['id']}", headers=H, params={"expanded": 1}).json()
v_book(item, "item")
check(len(item["media"]["chapters"]) == 3 and item["media"]["chapters"][1]["title"] == "Chapter 1", "ID3 chapters")
check(item["media"]["tracks"][0]["contentUrl"].startswith(f"/api/items/{phm['id']}/file/"), "track contentUrl")
check(c.get(f"/api/items/{phm['id']}/cover").status_code == 200, "cover public")
fe_item = c.get(f"/api/items/{fe['id']}", headers=H, params={"expanded": 1}).json()
check([t["startOffset"] > 0 for t in fe_item["media"]["tracks"]] == [False, True, True], "track offsets")
check(fe_item["media"]["coverPath"].endswith("cover.png"), "folder cover image used")
ino = fe_item["media"]["tracks"][1]["metadata"] and fe_item["media"]["audioFiles"][1]["ino"]
r = c.get(f"/api/items/{fe['id']}/file/{ino}/download", headers=H)
check(r.status_code == 200 and len(r.content) > 1000, "file download")
r = c.get(f"/api/items/{fe['id']}/file/{ino}", params={"token": access}, headers={"Range": "bytes=0-99"})
check(r.status_code == 206 and len(r.content) == 100, f"range request via ?token ({r.status_code})")
check(c.get(f"/api/items/{fe['id']}/file/{ino}").status_code == 401, "file needs auth")

print("filterdata / personalized / series / authors / search")
fd = c.get(f"/api/libraries/{lib_id}", headers=H, params={"include": "filterdata"}).json()["filterdata"]
check(any(a["name"] == "Brandon Sanderson" for a in fd["authors"]) and "Fantasy" in fd["genres"], "filterdata")
shelves = c.get(f"/api/libraries/{lib_id}/personalized", headers=H).json()
check(isinstance(shelves, list) and {"recently-added", "recent-series", "newest-authors"} <= {s["id"] for s in shelves}, "shelves")
for s in shelves:
    check(all(k in s for k in ("id", "label", "type", "entities")), "shelf keys")
    for e in s["entities"]:
        {"book": v_book, "series": v_series, "authors": v_author}[s["type"]](e, f"shelf {s['id']}")
sr = c.get(f"/api/libraries/{lib_id}/series", headers=H, params={"limit": 20, "page": 0, "sort": "name"}).json()
v_page(sr, "series")
for s in sr["results"]:
    v_series(s)
mist = next(s for s in sr["results"] if s["name"] == "Mistborn")
check([b["media"]["metadata"]["title"] for b in mist["books"]] == ["The Final Empire", "The Well of Ascension"], "series order")
check(mist.get("numBooks") == 2, "series numBooks on full list")
# Minified series list: one cover book + numBooks (web UI / large libraries)
sr_min = c.get(f"/api/libraries/{lib_id}/series", headers=H, params={"limit": 20, "page": 0, "sort": "name", "minified": 1}).json()
mist_min = next(s for s in sr_min["results"] if s["name"] == "Mistborn")
check(mist_min.get("numBooks") == 2 and len(mist_min["books"]) == 1, "minified series slim books")
check(mist_min["books"][0]["media"]["metadata"].get("description") in (None, ""), "minified omits description")
# Progress include uses preloaded map (no N+1); response still includes userMediaProgress when asked
page_prog = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"minified": 1, "limit": 5, "page": 0, "include": "progress"}).json()
check(all("userMediaProgress" in b for b in page_prog["results"]), "include=progress on list")
check(c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"limit": 0, "minified": 1}).json()["total"] >= page["total"], "limit=0 returns all")
au = c.get(f"/api/libraries/{lib_id}/authors", headers=H, params={"minified": 1, "limit": 50, "page": 0}).json()
v_page(au, "authors")
for a in au["authors"]:
    v_author(a)
bs = next(a for a in au["authors"] if a["name"] == "Brandon Sanderson")
check(bs["numBooks"] == 2, "author numBooks")
ad = c.get(f"/api/authors/{bs['id']}", headers=H, params={"include": "items,series"}).json()
for k in ("libraryId", "addedAt", "updatedAt", "series", "libraryItems"):
    check(k in ad, f"AuthorDetails.{k}")
check(len(ad["series"]) == 1 and len(ad["series"][0]["items"]) == 2, "author series items")
for b in ad["libraryItems"]:
    v_book(b, "authorDetails.item")
filt = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"filter": "series." + base64.b64encode(mist["id"].encode()).decode(), "minified": 1}).json()
check(filt["total"] == 2, "series filter")
filt = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"filter": "authors." + base64.b64encode(bs["id"].encode()).decode()}).json()
check(filt["total"] == 2, "author filter")
nar = c.get(f"/api/libraries/{lib_id}/narrators", headers=H).json()["narrators"]
check(any(n["name"] == "Ray Porter" for n in nar) and all("id" in n for n in nar), "narrators")
lib_stats = c.get(f"/api/libraries/{lib_id}/stats", headers=H).json()
for k in ("totalItems", "totalAuthors", "totalGenres", "totalNarrators", "totalDuration", "avgDuration", "totalSize",
          "numAudioTracks", "longestItems", "largestItems", "authorsWithCount", "narratorsWithCount", "genresWithCount"):
    check(k in lib_stats, f"libraryStats.{k}")
check(lib_stats["totalItems"] == page["total"] and lib_stats["avgDuration"] > 0, "avg book length")
check(abs(lib_stats["avgDuration"] - lib_stats["totalDuration"] / lib_stats["totalItems"]) < 0.01, "avg matches total")
check(any(n["name"] == "Ray Porter" and n["count"] >= 1 and n["id"] for n in lib_stats["narratorsWithCount"]), "narrator counts")
check(lib_stats["longestItems"] and lib_stats["longestItems"][0]["duration"] >= lib_stats["longestItems"][-1]["duration"], "longest books sorted")
srch = c.get(f"/api/libraries/{lib_id}/search", headers=H, params={"q": "mist", "limit": 10}).json()
check(srch["series"] and "books" in srch["series"][0] and "series" in srch["series"][0], "search series shape")
for x in srch["series"]:
    v_series(x, "search.series")
srch = c.get(f"/api/libraries/{lib_id}/search", headers=H, params={"q": "hail", "limit": 10}).json()
check(srch["book"] and srch["book"][0]["libraryItem"]["id"] == phm["id"], "search books")

print("play session")
ps = c.post(f"/api/items/{phm['id']}/play", headers=H, json={
    "forceDirectPlay": True, "forceTranscode": False, "supportedMimeTypes": ["audio/mpeg"], "mediaPlayer": "ios",
    "deviceInfo": {"clientName": "AudioBooth iOS", "clientVersion": "1.0", "deviceId": "dev-1"}}).json()
for k in ("id", "userId", "libraryItemId", "currentTime", "duration", "libraryItem"):
    check(k in ps, f"PlaySession.{k}")
v_book(ps["libraryItem"], "session.libraryItem")
for t in ps["audioTracks"]:
    v_track(t, "session.track")
r = c.get(f"/public/session/{ps['id']}/track/1", headers={"Range": "bytes=0-1023"})
check(r.status_code == 206 and r.headers["content-type"].startswith("audio/mpeg"), f"public track stream ({r.status_code})")
check(c.post(f"/api/session/{ps['id']}/sync", headers=H, json={"timeListened": 12, "currentTime": 12.5}).status_code == 200, "sync")
mp = c.get(f"/api/me/progress/{phm['id']}", headers=H).json()
v_progress(mp)
check(abs(mp["currentTime"] - 12.5) < 0.01 and not mp["isFinished"], "progress after sync")
check(c.post(f"/api/session/{ps['id']}/close", headers=H).status_code == 200, "close")
shelves = c.get(f"/api/libraries/{lib_id}/personalized", headers=H).json()
cl = next((s for s in shelves if s["id"] == "continue-listening"), None)
check(cl and cl["entities"][0]["id"] == phm["id"], "continue listening shelf")

print("progress api")
check(c.patch(f"/api/me/progress/{fe['id']}", headers=H, json={"isFinished": True}).status_code == 200, "mark finished")
fin = c.get(f"/api/me/progress/{fe['id']}", headers=H).json()
check(fin["isFinished"] and fin["progress"] == 1, "finished progress")
shelves = c.get(f"/api/libraries/{lib_id}/personalized", headers=H).json()
cs_shelf = next((s for s in shelves if s["id"] == "continue-series"), None)
check(cs_shelf and cs_shelf["entities"][0]["media"]["metadata"]["title"] == "The Well of Ascension", "continue series shelf")
r = c.get(f"/api/me/progress/{mp['id']}/remove-from-continue-listening", headers=H)
check(r.status_code == 200 and "mediaProgress" in r.json(), "remove from continue listening")

print("bookmarks")
bm = c.post(f"/api/me/item/{phm['id']}/bookmark", headers=H, json={"title": "Cool bit", "time": 15}).json()
check(bm["libraryItemId"] == phm["id"] and is_int(bm["createdAt"]), "bookmark create")
check(c.patch(f"/api/me/item/{phm['id']}/bookmark", headers=H, json={"libraryItemId": phm["id"], "time": 15.0, "title": "Renamed", "createdAt": bm["createdAt"]}).status_code == 200, "bookmark update")
auth_user = c.post("/api/authorize", headers=H).json()["user"]
check(auth_user["bookmarks"][0]["title"] == "Renamed", "bookmark in authorize")
check(c.delete(f"/api/me/item/{phm['id']}/bookmark/15.0", headers=H).status_code == 200, "bookmark delete")

print("local sessions / history / stats")
now = int(time.time() * 1000)
local = {"id": "local-sess-1", "libraryItemId": wa["id"], "mediaType": "book", "duration": 32, "startTime": 0, "currentTime": 20,
         "timeListening": 20, "playMethod": 3, "mediaPlayer": "ios", "deviceInfo": {"deviceId": "dev-1", "clientName": "AudioBooth iOS"},
         "startedAt": now - 30000, "updatedAt": now}
check(c.post("/api/session/local", headers=H, json=local).status_code == 200, "local session")
r = c.post("/api/session/local-all", headers=H, json={"sessions": [local | {"id": "local-sess-2", "currentTime": 25, "updatedAt": now + 1000}], "deviceInfo": local["deviceInfo"]}).json()
check(r["results"][0]["success"], "local-all")
hist = c.get("/api/me/listening-sessions", headers=H, params={"page": 0, "itemsPerPage": 10}).json()
for k in ("total", "numPages", "page", "itemsPerPage", "sessions"):
    check(k in hist, f"history.{k}")
for s in hist["sessions"]:
    check(isinstance(s["id"], str) and isinstance(s["libraryItemId"], str) and num(s["updatedAt"]), "ListeningHistorySession")
item_sessions = c.get(f"/api/me/item/listening-sessions/{wa['id']}", headers=H, params={"itemsPerPage": 10, "page": 0}).json()
for s in item_sessions["sessions"]:
    v_session_sync(s)
check(item_sessions["numPages"] >= 1 and len(item_sessions["sessions"]) == 2, "item sessions")
stats = c.get("/api/me/listening-stats", headers=H).json()
check(num(stats["totalTime"]) and isinstance(stats["days"], dict) and num(stats["today"]), "listening stats")
prev = 0
for key in ("24h", "3d", "7d", "1m", "3m", "6m", "12m", "all"):
    w = stats["windows"][key]
    for field in ("time", "booksFinished", "booksListened", "genres", "authors", "narrators", "books", "dayOfWeek"):
        check(field in w, f"window.{key}.{field}")
    check(w["time"] + 0.01 >= prev, f"window {key} includes shorter spans")
    prev = w["time"]
    check(len(w["dayOfWeek"]) == 7, f"window.{key} weekdays")
check(stats["windows"]["24h"]["time"] > 0 and stats["windows"]["all"]["time"] >= stats["windows"]["24h"]["time"], "recent session in 24h and all")
check(any(g["genre"] == "Science Fiction" and g["time"] > 0 for g in stats["windows"]["all"]["genres"]), "genre listening time")
check(stats["windows"]["all"]["booksFinished"] >= 1, "finished books in all")
ys = c.get(f"/api/me/stats/year/{time.localtime().tm_year}", headers=H).json()
for k in ("totalListeningSessions", "totalListeningTime", "totalBookListeningTime", "totalPodcastListeningTime", "topAuthors", "topGenres",
          "numBooksFinished", "numBooksListened", "booksWithCovers", "finishedBooksWithCovers"):
    check(k in ys, f"yearStats.{k}")

print("collections / playlists")
colr = c.post("/api/collections", headers=H, json={"books": [fe["id"]], "libraryId": lib_id, "name": "Favs", "description": None}).json()
for k in ("id", "name", "libraryId", "books", "lastUpdate", "createdAt"):
    check(k in colr, f"Collection.{k}")
check(is_int(colr["lastUpdate"]), "collection lastUpdate int")
colr = c.post(f"/api/collections/{colr['id']}/batch/add", headers=H, json={"books": [phm["id"]]}).json()
check(len(colr["books"]) == 2, "collection batch add")
colr = c.delete(f"/api/collections/{colr['id']}/book/{fe['id']}", headers=H).json()
check(len(colr["books"]) == 1, "collection remove")
check(c.get(f"/api/libraries/{lib_id}/collections", headers=H, params={"limit": 10, "page": 0}).json()["total"] == 1, "library collections")
pl = c.post("/api/playlists", headers=H, json={"libraryId": lib_id, "name": "Queue", "items": [{"libraryItemId": phm["id"], "episodeId": None}]}).json()
for k in ("id", "name", "libraryId", "userId", "items", "lastUpdate", "createdAt"):
    check(k in pl, f"Playlist.{k}")
check(pl["items"][0]["libraryItemId"] == phm["id"] and "libraryItem" in pl["items"][0], "playlist item")
pl = c.post(f"/api/playlists/{pl['id']}/batch/add", headers=H, json={"items": [{"libraryItemId": fe["id"]}]}).json()
check(len(pl["items"]) == 2, "playlist batch add")
pl = c.post(f"/api/playlists/{pl['id']}/batch/remove", headers=H, json={"items": [{"libraryItemId": fe["id"]}]}).json()
check(len(pl["items"]) == 1, "playlist batch remove")
check(c.get(f"/api/libraries/{lib_id}/playlists", headers=H).json()["total"] == 1, "library playlists")

print("tokens: refresh / api key / legacy")
r = c.post("/auth/refresh", headers={"x-refresh-token": refresh})
check(r.status_code == 200 and r.json()["user"]["refreshToken"] and r.json()["user"]["accessToken"], "refresh rotates tokens")
check(c.post("/auth/refresh", headers={"x-refresh-token": refresh}).status_code == 401, "old refresh token revoked")
key = c.post("/api/api-keys", headers=H, json={"name": "phone"}).json()["apiKey"]["apiKey"]
check(json.loads(base64.urlsafe_b64decode(key.split(".")[1] + "=="))["type"] == "api", "api key JWT type")
check(c.post("/api/authorize", headers={"Authorization": f"Bearer {key}"}).status_code == 200, "api key auth")
check(c.post("/api/authorize", headers={"Authorization": f"Bearer {user['token']}"}).status_code == 200, "legacy token auth")
check(c.post("/api/authorize", headers={"Authorization": "Bearer nope"}).status_code == 401, "bad token 401")

print("edit / chapters / organize / import")
r = c.patch(f"/api/items/{by_title['A Loose Single']['id']}/media", headers=H, json={"metadata": {
    "title": "The Loose Single", "authors": [{"name": "Jane Doe"}], "series": [{"name": "Loose Ends", "sequence": "2"}]}}).json()
check(r["updated"] and r["libraryItem"]["media"]["metadata"]["seriesName"] == "Loose Ends #2", "media patch")
plans = c.get(f"/api/abstream/libraries/{lib_id}/organize", headers=H).json()["plans"]
print("  plans:", [(p["from"], p["to"], p["status"]) for p in plans])
loose_plan = next(p for p in plans if p["id"] == by_title["A Loose Single"]["id"])
check(loose_plan["to"] == "Jane Doe/Loose Ends/2 - The Loose Single", "loose plan target")
res = c.post(f"/api/abstream/items/{loose_plan['id']}/organize", headers=H).json()
check(res["status"] == "moved", f"organize item {res}")
moved = c.get(f"/api/items/{loose_plan['id']}", headers=H).json()
check(moved["relPath"] == "Jane Doe/Loose Ends/2 - The Loose Single" and not moved["isFile"] and moved["media"]["numTracks"] == 1, "moved item updated")
check((TD / "library/Jane Doe/Loose Ends/2 - The Loose Single/metadata.json").exists(), "metadata.json written")
r = c.post(f"/api/items/{loose_plan['id']}/chapters", headers=H, json={"chapters": [{"start": 0, "end": 5, "title": "A"}, {"start": 5, "end": 12, "title": "B"}]})
check(r.status_code == 200 and len(c.get(f"/api/items/{loose_plan['id']}", headers=H).json()["media"]["chapters"]) == 2, "chapters update")

imp = c.get(f"/api/abstream/libraries/{lib_id}/import", headers=H).json()
print("  import candidates:", [(x["relPath"], x["metadata"]["title"], x["metadata"]["authors"], x["metadata"]["series"]) for x in imp["candidates"]])
dune = next(x for x in imp["candidates"] if x["relPath"] == "random_rip_folder")
check(dune["metadata"]["title"] == "Dune" and dune["metadata"]["authors"] == ["Frank Herbert"] and dune["metadata"]["series"] == "Dune", "import guess from tags")
r = c.post(f"/api/abstream/libraries/{lib_id}/import", headers=H, json={"relPath": "random_rip_folder", "mode": "move", "metadata": {
    "title": "Dune", "authors": ["Frank Herbert"], "series": "Dune", "sequence": "1", "narrators": ["Scott Brick"], "publishedYear": "1965"}})
check(r.status_code == 200 and r.json()["relPath"] == "Frank Herbert/Dune/1 - Dune", f"import {r.status_code} {r.text[:200]}")
check(not (TD / "import/random_rip_folder").exists(), "import source removed")
newid = r.json()["id"]
ni = c.get(f"/api/items/{newid}", headers=H).json()
check(ni["media"]["numTracks"] == 2 and ni["media"]["metadata"]["narratorName"] == "Scott Brick", "imported item")
r = c.post(f"/api/abstream/libraries/{lib_id}/import", headers=H, json={"relPath": "Some Loose Import.mp3", "mode": "copy", "metadata": {"title": "The Hobbit", "authors": ["J.R.R. Tolkien"]}})
check(r.status_code == 200 and (TD / "import/Some Loose Import.mp3").exists(), "copy import of loose file")

print("rescan keeps edits")
c.post(f"/api/libraries/{lib_id}/scan", headers=H)
time.sleep(0.5)
for _ in range(60):
    if all(t["status"] != "running" for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"]):
        break
    time.sleep(0.5)
page = c.get(f"/api/libraries/{lib_id}/items", headers=H, params={"limit": 50}).json()
titles = sorted(b["media"]["metadata"]["title"] for b in page["results"])
print("  ", titles, [b["isMissing"] for b in page["results"]])
check(page["total"] == 7 and not any(b["isMissing"] for b in page["results"]), "after rescan: 7 items, none missing")
check(c.get(f"/api/items/{newid}", headers=H).json()["media"]["metadata"]["narratorName"] == "Scott Brick", "edit survived rescan")

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
