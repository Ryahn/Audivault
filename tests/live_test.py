"""Tests for .cue chapters, folder watching (partial scans) and the live event stream.

Run after api_test.py against the same server, started with AUDIVAULT_WATCH_DELAY=2
so watcher-triggered scans happen quickly:
    python tests/live_test.py ./testdata
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = os.environ.get("AUDIVAULT_TEST_URL", "http://127.0.0.1:13399")
TD = Path(sys.argv[1])
FAIL = []


def check(cond, msg):
    print(("  ok:   " if cond else "  FAIL: ") + msg)
    if not cond:
        FAIL.append(msg)


def wait_for(fn, timeout=60, interval=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


c = httpx.Client(base_url=BASE, timeout=30)
H = {"Authorization": "Bearer " + c.post("/login", json={"username": "admin", "password": "secret"}).json()["user"]["accessToken"]}
lib = c.get("/api/libraries", headers=H).json()["libraries"][0]
LIB = lib["id"]


def items():
    return c.get(f"/api/libraries/{LIB}/items", headers=H, params={"limit": 200}).json()["results"]


def by_title(title):
    return next((i for i in items() if i["media"]["metadata"]["title"] == title), None)


def tasks_idle():
    return all(t["status"] != "running" for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"])


# ------------------------------------------------------------------ event stream
print("event stream")
received: list[dict] = []
ticket = c.post("/api/abstream/events/ticket", headers=H).json()["ticket"]
stop = threading.Event()


def listen(tk):
    with httpx.Client(base_url=BASE, timeout=None) as sc:
        with sc.stream("GET", "/api/abstream/events", params={"ticket": tk}) as r:
            received.append({"type": "_status", "code": r.status_code})
            for line in r.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))


threading.Thread(target=listen, args=(ticket,), daemon=True).start()
check(wait_for(lambda: any(e.get("type") == "hello" for e in received), 10), "stream connects with ticket")
r = c.get("/api/abstream/events", params={"ticket": ticket})
check(r.status_code == 401, "ticket cannot be reused")
check(c.get("/api/abstream/events", params={"ticket": "bogus"}).status_code == 401, "bogus ticket rejected")

c.post(f"/api/libraries/{LIB}/scan", headers=H)
done = wait_for(lambda: next((e for e in received if e.get("type") == "task" and e["task"]["kind"] == "scan"
                               and e["task"]["status"] == "done"), None), 30)
check(done is not None and "added" in done["task"]["result"], "scan completion pushed with counts")

# ------------------------------------------------------------------ cue chapters
print("cue chapters")
phm = by_title("Project Hail Mary")
titles = lambda: [ch["title"] for ch in c.get(f"/api/items/{phm['id']}", headers=H).json()["media"]["chapters"]]
check(titles() == ["Opening Credits", "Chapter 1", "Chapter 2"], "cue ignored while setting is off")
c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": {"useCueChapters": True}})
check(wait_for(lambda: titles() == ["Cue Intro", "Cue Part Two"], 20), "enabling setting applies .cue chapters")
ch = c.get(f"/api/items/{phm['id']}", headers=H).json()["media"]["chapters"]
check(abs(ch[1]["start"] - (15 + 37 / 75)) < 0.01 and ch[-1]["end"] > 29, "cue frame timing + last chapter ends at book end")
check(wait_for(lambda: any(e.get("type") == "task" and e["task"]["kind"] == "chapters" and e["task"]["status"] == "done" for e in received), 10),
      "chapter refresh task pushed")
c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": {"useCueChapters": False}})
check(wait_for(lambda: titles() == ["Opening Credits", "Chapter 1", "Chapter 2"], 20), "disabling setting restores embedded chapters")
c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": {"useCueChapters": True}})
wait_for(lambda: titles()[0] == "Cue Intro", 20)

# ------------------------------------------------------------------ watcher: partial scans
print("folder watcher")
watching = c.get("/api/abstream/watcher", headers=H).json()["watching"]
check(any(w["target"] == "library" for w in watching) and any(w["target"] == "import" for w in watching), f"watching library + import {watching}")
# Let changes made by earlier tests (renames, imports) finish their own partial scans first
wait_for(lambda: tasks_idle() and c.get("/api/abstream/watcher", headers=H).json()["pending"] == 0, 60)
wait_for(tasks_idle, 30)
received.clear()

src = TD / "library/Andy Weir/Project Hail Mary/Project Hail Mary.mp3"
new_dir = TD / "library/Becky Chambers/A Psalm for the Wild-Built"
new_dir.mkdir(parents=True)
shutil.copy(src, new_dir / "psalm.mp3")
added = wait_for(lambda: by_title("Project Hail Mary") and any(i["relPath"] == "Becky Chambers/A Psalm for the Wild-Built" for i in items()), 60)
check(bool(added), "new book folder picked up without a manual scan")
ev = wait_for(lambda: next((e for e in received if e.get("type") == "library_changed"), None), 10)
check(ev is not None and ev["added"] >= 1, "library_changed event pushed")
scan_ev = next((e for e in received if e.get("type") == "task" and e["task"]["kind"] == "scan" and e["task"]["status"] == "done"), None)
# Changes queued while earlier jobs ran may be merged in, but it must never be a full-library walk
check(scan_ev is not None and scan_ev["task"]["result"]["partial"] and scan_ev["task"]["result"]["total"] < len(items()),
      f"partial scan covered only changed folders ({scan_ev and scan_ev['task']['result']})")

received.clear()
(TD / "library/Becky Chambers/A Psalm for the Wild-Built/psalm.cue").write_text(
    'FILE "psalm.mp3" MP3\nTRACK 01 AUDIO\nTITLE "Watched Cue"\nINDEX 01 00:00:00\nTRACK 02 AUDIO\nTITLE "Second"\nINDEX 01 00:10:00\n')
psalm_id = next(i["id"] for i in items() if i["relPath"] == "Becky Chambers/A Psalm for the Wild-Built")
check(wait_for(lambda: [x["title"] for x in c.get(f"/api/items/{psalm_id}", headers=H).json()["media"]["chapters"]] == ["Watched Cue", "Second"], 60),
      "adding a .cue file to a book updates its chapters")

received.clear()
shutil.rmtree(TD / "library/Becky Chambers")
check(wait_for(lambda: c.get(f"/api/items/{psalm_id}", headers=H).json().get("isMissing"), 60), "deleted folder marked missing")

# ------------------------------------------------------------------ watcher: import folder
print("import folder watcher")
received.clear()
(TD / "import/New Drop").mkdir(parents=True, exist_ok=True)
shutil.copy(src, TD / "import/New Drop/new.mp3")
ev = wait_for(lambda: next((e for e in received if e.get("type") == "import_changed"), None), 60)
check(ev is not None and ev["new"] >= 1, f"import_changed pushed ({ev})")

stop.set()
print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
