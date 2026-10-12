"""Tests for the default import operation and automatic import.

Needs internet access (books are matched against Audible). Run after api_test.py
on a fresh server started with ABSTREAM_WATCH_DELAY=2 and ABSTREAM_IMPORT_SETTLE=3:
    python tests/autoimport_test.py ./testdata
"""
import os
import shutil
import sys
import time
from pathlib import Path

import httpx
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TXXX

BASE = os.environ.get("ABSTREAM_TEST_URL", "http://127.0.0.1:13399")
TD = Path(sys.argv[1])
FAIL = []
imp = TD / "import"
src_mp3 = TD / "library/Andy Weir/Project Hail Mary/Project Hail Mary.mp3"


def check(cond, msg):
    print(("  ok:   " if cond else "  FAIL: ") + msg)
    if not cond:
        FAIL.append(msg)


def wait_for(fn, timeout=90, interval=1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


c = httpx.Client(base_url=BASE, timeout=120)
H = {"Authorization": "Bearer " + c.post("/login", json={"username": "admin", "password": "secret"}).json()["user"]["accessToken"]}
LIB = c.get("/api/libraries", headers=H).json()["libraries"][0]["id"]


def book(folder: str, title: str, artist: str, asin: str | None = None) -> Path:
    d = imp / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / "audio.mp3"
    shutil.copy(src_mp3, f)
    t = ID3(f)
    for frame in ("TALB", "TIT2", "TPE1", "TPE2", "TXXX"):
        t.delall(frame)
    t.add(TALB(encoding=3, text=title)); t.add(TIT2(encoding=3, text=title)); t.add(TPE1(encoding=3, text=artist))
    if asin:
        t.add(TXXX(encoding=3, desc="ASIN", text=asin))
    t.save()
    return d


def candidates():
    return {x["relPath"]: x for x in c.get(f"/api/abstream/libraries/{LIB}/import", headers=H).json()["candidates"]}


def items():
    return c.get(f"/api/libraries/{LIB}/items", headers=H, params={"limit": 500}).json()["results"]


def settings(**kw):
    r = c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": kw})
    assert r.status_code == 200, r.text


def task_done(kind="import"):
    ts = [t for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"] if t["kind"] == kind]
    return ts and all(t["status"] != "running" for t in ts) and ts[0]


print("default operation")
settings(importMode="copy", autoImport=False)
lib = c.get(f"/api/libraries/{LIB}", headers=H).json()
check(lib["settings"]["importMode"] == "copy", "importMode saved")
book("Manual Copy Book", "Manual Copy Book", "Test Author")
r = c.post(f"/api/abstream/libraries/{LIB}/import", headers=H, json={"relPath": "Manual Copy Book", "metadata": {"title": "Manual Copy Book", "authors": ["Test Author"]}})
check(r.status_code == 200 and (imp / "Manual Copy Book/audio.mp3").exists(), f"import without a mode uses the default (copy) ({r.text[:100]})")
cand = candidates().get("Manual Copy Book")
check(cand and cand["autoImport"] and cand["autoImport"]["status"] == "imported", "copied book remembered as imported")

print("auto-import (copy)")
book("Rip 01", "random rip name", "unknown uploader", asin="B0CMYLRB39")      # Arifureta Vol. 1 on Audible
book("Mystery Rip", "Zzqv Nonexistent Book Title", "Nobody Known")           # no provider match
settings(autoImport=True)                                                    # turning it on starts a run
t = wait_for(lambda: (x := task_done()) and x["result"] is not None and x, 180)
res = t["result"] if t else {}
print("   result:", {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()})
imported = {x["relPath"]: x for x in res.get("imported", [])}
review = {x["relPath"]: x for x in res.get("review", [])}
check("Rip 01" in imported and imported["Rip 01"]["match"] == "ASIN matches", f"ASIN match imported ({imported.get('Rip 01')})")
check("Arifureta" in imported.get("Rip 01", {}).get("to", ""), "imported under the matched metadata")
check("Mystery Rip" in review, f"unmatched book left for review ({review.get('Mystery Rip')})")
check("Manual Copy Book" not in imported, "manually copied book not imported again")
check((imp / "Rip 01/audio.mp3").exists(), "copy mode leaves the original")
check(any("Arifureta" in i["media"]["metadata"]["title"] for i in items()), "imported book is in the library")
cands = candidates()
check(cands["Mystery Rip"]["autoImport"]["status"] == "review" and "No confident match" in cands["Mystery Rip"]["autoImport"]["reason"],
      "Import page shows why it needs review")

r = c.post(f"/api/abstream/libraries/{LIB}/auto-import", headers=H)
t2 = wait_for(lambda: (x := task_done()) and x["id"] != t["id"] and x, 60)
check(t2 and not t2["result"]["imported"] and not t2["result"]["review"], "a second run doesn't re-import or re-search remembered books")

print("auto-import (move) via the folder watcher")
settings(importMode="move")
started = time.time()
d = book("Rip 02", "another rip", "someone", asin="B0DK41WV5K")             # The Primal Hunter 10 on Audible
moved = wait_for(lambda: not d.exists(), 180)
check(bool(moved), "watcher triggered auto-import and moved the book out of the import folder")
check(time.time() - started >= 3, "waited for the files to settle first")
check(wait_for(lambda: any("Primal Hunter" in i["media"]["metadata"]["title"] for i in items()), 30), "moved book is in the library")

print("length check")
book("Hobbit Sample", "The Hobbit", "J.R.R. Tolkien")                        # a 30-second file can't be the real book
t3 = wait_for(lambda: (x := task_done()) and any(r["relPath"] == "Hobbit Sample" for r in x["result"].get("review", [])) and x, 180)
reason = next((r["reason"] for r in (t3 or {"result": {"review": []}})["result"]["review"] if r["relPath"] == "Hobbit Sample"), "")
check(t3 is not None and ("length" in reason or "title" in reason), f"title/author match with the wrong length is not imported ({reason})")

settings(autoImport=False, importMode="move")
print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
