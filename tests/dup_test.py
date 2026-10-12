"""Tests for organize-on-import / organize-on-scan and the duplicate manager.

Run after api_test.py against the same server:
    python tests/dup_test.py ./testdata
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = os.environ.get("ABSTREAM_TEST_URL", "http://127.0.0.1:13399")
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


c = httpx.Client(base_url=BASE, timeout=60)
H = {"Authorization": "Bearer " + c.post("/login", json={"username": "admin", "password": "secret"}).json()["user"]["accessToken"]}
LIB = c.get("/api/libraries", headers=H).json()["libraries"][0]["id"]
lib_root = TD / "library"


def items():
    return c.get(f"/api/libraries/{LIB}/items", headers=H, params={"limit": 500}).json()["results"]


def idle():
    return all(t["status"] != "running" for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"])


def scan():
    wait_for(idle, 60)
    t = c.post(f"/api/libraries/{LIB}/scan", headers=H).json()["task"]
    return wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{t['id']}", headers=H).json())["status"] != "running" and x, 120)


def settings(**kw):
    c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": kw})


# live events, to check the duplicate notification
received: list[dict] = []
ticket = c.post("/api/abstream/events/ticket", headers=H).json()["ticket"]


def listen():
    with httpx.Client(base_url=BASE, timeout=None) as sc, sc.stream("GET", "/api/abstream/events", params={"ticket": ticket}) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))


threading.Thread(target=listen, daemon=True).start()
wait_for(lambda: received, 10)

# ------------------------------------------------------------------ organize on import (off)
print("organize on import: off")
settings(organizeOnImport=False)
src_mp3 = lib_root / "Andy Weir/Project Hail Mary/Project Hail Mary.mp3"
raw = TD / "import/Raw Rip Folder 07"
raw.mkdir(parents=True, exist_ok=True)
shutil.copy(src_mp3, raw / "a.mp3")
r = c.post(f"/api/abstream/libraries/{LIB}/import", headers=H, json={"relPath": "Raw Rip Folder 07", "mode": "move",
                                                                        "metadata": {"title": "Unique Import Book", "authors": ["Test Author"]}})
check(r.status_code == 200 and r.json()["relPath"] == "Raw Rip Folder 07", f"kept original folder name ({r.text[:120]})")
check((lib_root / "Raw Rip Folder 07/a.mp3").exists(), "files placed under original name")
imported_id = r.json().get("id")
settings(organizeOnImport=True)

# ------------------------------------------------------------------ organize on scan
print("organize on scan")
misc = lib_root / "Misc Stuff/random name"
misc.mkdir(parents=True, exist_ok=True)
from mutagen.id3 import ID3, TALB, TPE1  # noqa: E402
shutil.copy(src_mp3, misc / "lw.mp3")
tags = ID3(misc / "lw.mp3")
tags.delall("TALB"); tags.delall("TPE1"); tags.delall("TIT2"); tags.delall("TXXX")
tags.add(TALB(encoding=3, text="Leviathan Wakes")); tags.add(TPE1(encoding=3, text="James S A Corey"))
tags.save()
settings(organizeOnScan=True)
res = scan()
print("   scan result:", res and res["result"])
paths = {i["media"]["metadata"]["title"]: i["relPath"] for i in items()}
check(paths.get("Leviathan Wakes") == "James S A Corey/Leviathan Wakes", f"misfiled book organized on scan ({paths.get('Leviathan Wakes')})")
check(paths.get("Unique Import Book") == "Test Author/Unique Import Book", f"un-organized import organized on scan ({paths.get('Unique Import Book')})")
check(not (lib_root / "Misc Stuff").exists(), "empty source folders removed")
check(res and res["result"].get("organized", 0) >= 2, "scan reports organized count")
settings(organizeOnScan=False)

# ------------------------------------------------------------------ duplicates
print("duplicates")
keep_dir = lib_root / "Andy Weir/Project Hail Mary"
dup_dir = lib_root / "Andy Weir/Project Hail Mary (2021 rip)"
dup_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(src_mp3, dup_dir / "Project Hail Mary.mp3")
(keep_dir / "metadata.json").write_text(json.dumps({"title": "Project Hail Mary", "authors": ["Andy Weir"]}), encoding="utf-8")
(dup_dir / "metadata.json").write_text(json.dumps({"title": "Project Hail Mary", "authors": ["Andy Weir"], "narrators": ["Ray Porter"],
                                                   "description": "A much longer description " * 20, "genres": ["Science Fiction"]}), encoding="utf-8")
(dup_dir / "extra.cue").write_text('FILE "Project Hail Mary.mp3" MP3\nTRACK 01 AUDIO\nTITLE "Dup Cue"\nINDEX 01 00:00:00\n', encoding="utf-8")
if (keep_dir / "Project Hail Mary.cue").exists():
    (keep_dir / "Project Hail Mary.cue").unlink()  # the kept copy has no cue sheet
received.clear()
scan()
groups = c.get("/api/abstream/duplicates", headers=H).json()["groups"]
phm = next((g for g in groups if any(i["relPath"] == "Andy Weir/Project Hail Mary (2021 rip)" for i in g["items"])), None)
# (the imported test book reuses this audio, so it also matches by ASIN: a 3-copy group is correct)
check(phm is not None and len(phm["items"]) >= 2, f"duplicate group detected ({[(g['reasons'], [i['relPath'] for i in g['items']]) for g in groups]})")
check(wait_for(lambda: any(e.get("type") == "duplicates_found" for e in received), 10), "duplicates_found notification pushed")

keep = next(i for i in phm["items"] if i["relPath"] == "Andy Weir/Project Hail Mary")
dup = next(i for i in phm["items"] if i["relPath"] == "Andy Weir/Project Hail Mary (2021 rip)")
check(dup["hasCue"] and not keep["hasCue"], "copy summaries show .cue presence")
cmp = c.get("/api/abstream/duplicates/compare", headers=H, params={"keep": keep["id"], "discard": dup["id"]}).json()
rows = {f["relPath"]: f for f in cmp["files"]}
check(rows["extra.cue"]["status"] == "missing" and rows["extra.cue"]["selected"], "missing .cue preselected to copy")
check(rows["metadata.json"]["status"] == "larger" and rows["metadata.json"]["selected"], "larger metadata.json preselected to replace")
check(rows["Project Hail Mary.mp3"]["status"] == "same" and not rows["Project Hail Mary.mp3"]["selected"], "identical audio not selected")

# progress on the discarded copy should move to the kept copy
c.patch(f"/api/me/progress/{dup['id']}", headers=H, json={"currentTime": 12, "duration": 30})
c.post(f"/api/me/item/{dup['id']}/bookmark", headers=H, json={"title": "dup mark", "time": 5})
r = c.post("/api/abstream/duplicates/resolve", headers=H, json={
    "keepId": keep["id"], "mode": "trash",
    "discards": [{"id": dup["id"], "copy": ["extra.cue"], "replace": ["metadata.json"]}]})
check(r.status_code == 200, f"resolve accepted ({r.text[:120]})")
t = wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{r.json()['task']['id']}", headers=H).json())["status"] != "running" and x, 60)
check(t and t["status"] == "done", f"resolve task done ({t and (t['error'] or t['result'])})")
check((keep_dir / "extra.cue").exists(), ".cue copied into kept folder")
check("much longer description" in (keep_dir / "metadata.json").read_text(encoding="utf-8"), "larger metadata.json replaced the kept one")
check(not dup_dir.exists(), "discarded folder removed from library")
trash = list((lib_root / ".abstream-trash").glob("*/Andy Weir/Project Hail Mary (2021 rip)"))
check(len(trash) == 1 and (trash[0] / "Project Hail Mary.mp3").exists(), "discarded copy moved to .abstream-trash")
check(c.get(f"/api/items/{dup['id']}", headers=H).status_code == 404, "discarded book removed from database")
kept = c.get(f"/api/items/{keep['id']}", headers=H, params={"include": "progress"}).json()
check(kept["userMediaProgress"] and abs(kept["userMediaProgress"]["currentTime"] - 12) < 0.1, "listening progress moved to kept copy")
me = c.get("/api/me", headers=H).json()
check(any(b["libraryItemId"] == keep["id"] and b["title"] == "dup mark" for b in me["bookmarks"]), "bookmark moved to kept copy")
check("Ray Porter" in kept["media"]["metadata"]["narratorName"], "kept copy re-read the replaced metadata.json")
groups = c.get("/api/abstream/duplicates", headers=H).json()["groups"]
check(not any(any(i["id"] == dup["id"] for i in g["items"]) for g in groups), "discarded copy gone from duplicate groups")
scan()
check(not any(".abstream-trash" in i["relPath"] for i in items()), "trash folder is not scanned back in")

print("decision support")
FRAME_128 = b"\xff\xfb\x90\x00" + b"\x00" * 413   # MPEG1 L3 128 kbps, 26.12 ms
FRAME_32 = b"\xff\xfb\x10\x00" + b"\x00" * 100    # MPEG1 L3 32 kbps, 26.12 ms


def silent(path: Path, frame: bytes, seconds: int = 610):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * int(seconds / 0.0261224))


base = lib_root / "Analysis Author"
silent(base / "Quality Book/qb.mp3", FRAME_128)                       # original
silent(base / "Low Rips/Quality Book/qb.mp3", FRAME_32)                # low-bitrate re-encode
silent(base / "Doubled/Quality Book/Part 01.mp3", FRAME_128)           # same book twice:
silent(base / "Doubled/Quality Book/Part 02.mp3", FRAME_32)            #   128k + 32k
(base / "Low Rips/Quality Book/qb.cue").write_text('FILE "qb.mp3" MP3\nTRACK 01 AUDIO\nTITLE "Rip Cue"\nINDEX 01 00:00:00\n', encoding="utf-8")
scan()
groups = c.get("/api/abstream/duplicates", headers=H).json()["groups"]
qg = next((g for g in groups if any(i["relPath"].startswith("Analysis Author/") for i in g["items"])), None)
check(qg is not None and len(qg["items"]) == 3, "quality test group found")
by_path = {i["relPath"]: i for i in qg["items"]}
orig, low, dbl = by_path["Analysis Author/Quality Book"], by_path["Analysis Author/Low Rips/Quality Book"], by_path["Analysis Author/Doubled/Quality Book"]
check(qg["recommendation"]["id"] == orig["id"], f"recommends the 128 kbps original ({qg['recommendation']['reason']})")
check("128 kbps vs 32 kbps" in qg["recommendation"]["reason"] or "contains the same audio" in qg["recommendation"]["reason"], "reason explains the choice")
check(orig["analysis"]["tier"] == "high" and low["analysis"]["tier"] == "low", "bitrate tiers")
check(orig["folderPath"].replace("\\", "/").rstrip("/").endswith("/library"), "copy shows its library folder")  # host or container path
notes = [n["text"] for n in dbl["analysis"]["notes"]]
check(any(t.startswith("Same audio saved 2×") for t in notes) and dbl["analysis"]["fix"]["keepFile"] == "Part 01.mp3", f"doubled copy flagged with a fix ({notes})")

# Applying the fix (exclude the 32k duplicate track) makes that copy clean
c.post(f"/api/abstream/items/{dbl['id']}/audio-files", headers=H, json={"exclude": dbl["analysis"]["fix"]["exclude"]})
groups = c.get("/api/abstream/duplicates", headers=H).json()["groups"]
dbl2 = next(i for g in groups for i in g["items"] if i["id"] == dbl["id"])
check(not dbl2["analysis"]["repeatedAudio"] and dbl2["analysis"]["kbps"] == 128, "after excluding the duplicate track the copy is clean 128 kbps")

# A cue sheet from a different encode is flagged and not preselected
cmp = c.get("/api/abstream/duplicates/compare", headers=H, params={"keep": orig["id"], "discard": low["id"]}).json()
cue_row = next(f for f in cmp["files"] if f["relPath"] == "qb.cue")
check(not cmp["sameAudio"] and not cue_row["selected"] and cue_row["warning"], "cue from a different encode not preselected, with warning")

# Batch resolve with the recommendation
r = c.post("/api/abstream/duplicates/resolve-batch", headers=H, json={"plans": [{"keepId": orig["id"], "discardIds": [low["id"], dbl["id"]]}], "mode": "trash"})
t = wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{r.json()['task']['id']}", headers=H).json())["status"] != "running" and x, 60)
check(t and t["status"] == "done" and t["result"]["resolved"] == 1, f"batch resolve done ({t and (t['error'] or t['result'])})")
check(not (base / "Low Rips").exists() and not (base / "Doubled").exists() and (base / "Quality Book/qb.mp3").exists(), "discarded copies trashed, original kept")
check(not (base / "Quality Book/qb.cue").exists(), "mismatched cue was not copied by the batch")
r = c.post("/api/abstream/duplicates/resolve-batch", headers=H, json={"plans": [{"keepId": orig["id"], "discardIds": [orig["id"]]}]})
check(r.status_code == 400, "batch rejects a plan that discards its own keep")

print("not duplicates")
other = lib_root / "Andy Weir/Project Hail Mary - Dramatized"
other.mkdir(parents=True, exist_ok=True)
shutil.copy(src_mp3, other / "d.mp3")
scan()
g = next((g for g in c.get("/api/abstream/duplicates", headers=H).json()["groups"]
          if any(i["relPath"] == "Andy Weir/Project Hail Mary - Dramatized" for i in g["items"])), None)
check(g is not None, "new copy detected")
c.post("/api/abstream/duplicates/ignore", headers=H, json={"itemIds": [i["id"] for i in g["items"]]})
check(not any(x["key"] == g["key"] for x in c.get("/api/abstream/duplicates", headers=H).json()["groups"]), "ignored group hidden")
c.delete("/api/abstream/duplicates/ignored", headers=H)
check(any(x["key"] == g["key"] for x in c.get("/api/abstream/duplicates", headers=H).json()["groups"]), "reset shows it again")

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
