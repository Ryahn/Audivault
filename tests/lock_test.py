"""Tests for locked libraries and folders (source of truth: files are never deleted).

Run after api_test.py on a fresh server:
    python tests/lock_test.py ./testdata
"""
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("AUDIVAULT_TEST_URL", "http://127.0.0.1:13399")
TD = Path(sys.argv[1])
SRV = os.environ.get("AUDIVAULT_TEST_SERVER_ROOT", str(TD)).rstrip("/\\")
SEP = "/" if SRV.startswith("/") else os.sep
FAIL = []
main_root = TD / "library"
locked_root = TD / "library_locked"


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
lib = c.get("/api/libraries", headers=H).json()["libraries"][0]
LIB = lib["id"]


def items():
    return c.get(f"/api/libraries/{LIB}/items", headers=H, params={"limit": 500}).json()["results"]


def idle():
    return all(t["status"] != "running" for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"])


def scan():
    wait_for(idle, 60)
    t = c.post(f"/api/libraries/{LIB}/scan", headers=H).json()["task"]
    wait_for(lambda: c.get(f"/api/abstream/tasks/{t['id']}", headers=H).json()["status"] != "running", 120)


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


# ------------------------------------------------------------------ setup: a second, locked folder
print("setup")
src = main_root / "Andy Weir/Project Hail Mary/Project Hail Mary.mp3"
copy_dir = locked_root / "Andy Weir/Project Hail Mary"
copy_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(src, copy_dir / "Project Hail Mary.mp3")
(copy_dir / "metadata.json").write_text('{"title": "Project Hail Mary", "authors": ["Andy Weir"]}', encoding="utf-8")
(copy_dir / "cover.png").write_bytes((main_root / "Brandon Sanderson/Mistborn/Book 1 - The Final Empire/cover.png").read_bytes()
                                     if (main_root / "Brandon Sanderson/Mistborn/Book 1 - The Final Empire/cover.png").exists()
                                     else b"\x89PNG\r\n\x1a\n")
misc = locked_root / "Misc Parent/Project Hail Mary"
misc.mkdir(parents=True, exist_ok=True)
shutil.copy(src, misc / "phm.mp3")
(locked_root / "Misc Parent/desktop.ini").write_text("[.ShellClassInfo]", encoding="utf-8")
# the unlocked copy gets a bigger metadata.json and an extra file to bring over
main_copy = main_root / "Andy Weir/Project Hail Mary"
(main_copy / "metadata.json").write_text('{"title": "Project Hail Mary", "authors": ["Andy Weir"], "description": "' + "longer " * 50 + '"}', encoding="utf-8")
(main_copy / "extra-notes.txt").write_text("notes from the unlocked copy", encoding="utf-8")

folders = [{"fullPath": f["fullPath"]} for f in lib["folders"]] + [{"fullPath": SRV + SEP + "library_locked", "locked": True}]
r = c.patch(f"/api/libraries/{LIB}", headers=H, json={"folders": folders})
check(r.status_code == 200 and any(f["locked"] for f in r.json()["folders"]), f"folder locked via library settings ({r.text[:120]})")
scan()
locked_items = [i for i in items() if i["path"].replace("\\", "/").startswith((SRV + SEP + "library_locked").replace("\\", "/"))]
check(len(locked_items) == 2, f"books in the locked folder scanned ({len(locked_items)})")
locks = c.get("/api/abstream/locks", headers=H).json()["locked"]
check(len(locks) == 1 and locks[0]["path"].endswith("library_locked"), "lock listed")

# ------------------------------------------------------------------ book: delete files
print("delete files")
li = next(i for i in locked_items if i["relPath"] == "Andy Weir/Project Hail Mary")
detail = c.get(f"/api/items/{li['id']}", headers=H).json()
check(detail["locked"] and "locked" in detail["lockReason"], "book reports locked")
r = c.delete(f"/api/items/{li['id']}", headers=H, params={"hard": 1})
check(r.status_code == 423 and (copy_dir / "Project Hail Mary.mp3").exists(), f"delete files refused with 423 ({r.status_code} {r.text[:80]})")
check(c.get(f"/api/items/{li['id']}", headers=H).status_code == 200, "book still in the library")

# ------------------------------------------------------------------ duplicates
print("duplicates")
before = snapshot(locked_root)
groups = c.get("/api/abstream/duplicates", headers=H).json()["groups"]
g = next(g for g in groups if any(i["id"] == li["id"] for i in g["items"]))
check(g["recommendation"]["id"] in {i["id"] for i in locked_items}, "a locked copy is recommended")
unlocked = next(i for i in g["items"] if not i["locked"] and i["relPath"] == "Andy Weir/Project Hail Mary")
locked_ids = [i["id"] for i in g["items"] if i["locked"]]
r = c.post("/api/abstream/duplicates/resolve", headers=H, json={"keepId": unlocked["id"], "discards": [{"id": x, "copy": [], "replace": []} for x in locked_ids]})
check(r.status_code == 423, f"discarding a locked copy refused ({r.status_code})")
check(snapshot(locked_root) == before, "refused resolve changed nothing in the locked folder")

cmp = c.get("/api/abstream/duplicates/compare", headers=H, params={"keep": li["id"], "discard": unlocked["id"]}).json()
rows = {f["relPath"]: f for f in cmp["files"]}
check(rows["metadata.json"]["blocked"] and not rows["metadata.json"]["selected"], "replacing an existing file in the locked keep is blocked")
check(rows["extra-notes.txt"]["status"] == "missing" and not rows["extra-notes.txt"]["blocked"], "adding a new file to the locked keep is allowed")
r = c.post("/api/abstream/duplicates/resolve", headers=H, json={"keepId": li["id"], "discards": [{"id": unlocked["id"], "copy": [], "replace": ["metadata.json"]}]})
check(r.status_code == 423, "explicit replace into a locked keep refused")
other_locked = [x for x in locked_ids if x != li["id"]]
if other_locked:
    r = c.post("/api/abstream/duplicates/resolve", headers=H, json={"keepId": li["id"], "discards": [{"id": other_locked[0], "copy": [], "replace": []}]})
    check(r.status_code == 423, "another locked copy in the group can't be discarded either")
r = c.post("/api/abstream/duplicates/resolve", headers=H, json={"keepId": li["id"], "discards": [{"id": unlocked["id"], "copy": ["extra-notes.txt"], "replace": []}]})
check(r.status_code == 200, f"keeping the locked copy and discarding the unlocked one works ({r.text[:100]})")
t = wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{r.json()['task']['id']}", headers=H).json())["status"] != "running" and x, 60)
check(t and t["status"] == "done", f"resolve finished ({t and (t['error'] or 'ok')})")
check((copy_dir / "extra-notes.txt").exists(), "new file added to the locked copy")
check(before["Andy Weir/Project Hail Mary/metadata.json"] == snapshot(locked_root)["Andy Weir/Project Hail Mary/metadata.json"],
      "locked copy's existing metadata.json untouched")
check(not main_copy.exists(), "unlocked duplicate moved to the trash")

# ------------------------------------------------------------------ trash in a locked folder
print("trash")
old = locked_root / ".abstream-trash/20200101-000000/Old Author/Old Book"
old.mkdir(parents=True, exist_ok=True)
(old / "a.mp3").write_bytes(b"\x00" * 100)
entry = next(e for e in c.get("/api/abstream/trash", headers=H).json()["entries"] if e["batch"] == "20200101-000000")
check(entry["locked"] and entry["expiresAt"] is None, "locked folder's trash is listed as kept")
r = c.delete(f"/api/abstream/trash/{entry['folderId']}/{entry['batch']}", headers=H)
check(r.status_code == 423 and old.exists(), "deleting locked trash refused")
c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": 7})
wait_for(idle, 30)
check(old.exists(), "retention purge skips the locked folder")
tk = c.post("/api/abstream/trash/empty", headers=H).json()["task"]
res = wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{tk['id']}", headers=H).json())["status"] != "running" and x, 30)
check(old.exists() and res["result"]["keptLocked"] >= 1, f"empty trash keeps locked batches ({res['result']})")
c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": 30})

# ------------------------------------------------------------------ covers
print("covers")
li_detail = c.get(f"/api/items/{li['id']}", headers=H).json()
png = (copy_dir / "cover.png").read_bytes()
r = c.post(f"/api/items/{li['id']}/cover", headers=H, files={"cover": ("new.png", png, "image/png")})
check(r.status_code == 200 and (copy_dir / "cover.png").exists(), "new cover keeps the old cover file in the locked folder")
check(".abstream" not in r.json()["cover"] and "library_locked" not in r.json()["cover"].replace("\\", "/"), "new cover stored outside the locked folder")

# ------------------------------------------------------------------ organize still renames, but deletes nothing
print("organize")
misc_item = next(i for i in items() if i["relPath"] == "Misc Parent/Project Hail Mary")
plan = c.get(f"/api/abstream/items/{misc_item['id']}/organize", headers=H).json()
r = c.post(f"/api/abstream/items/{misc_item['id']}/organize", headers=H).json()
check(r["status"] in ("moved", "conflict"), f"organize allowed in a locked folder ({r['status']} -> {plan['to']})")
if r["status"] == "moved":
    check((locked_root / "Misc Parent/desktop.ini").exists(), "desktop.ini left in place (no file deleted)")

# ------------------------------------------------------------------ library-wide lock
print("library lock")
c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": {"locked": True}})
plain = next(i for i in items() if i["relPath"] == "Brandon Sanderson/Mistborn/Book 1 - The Final Empire" or i["relPath"].endswith("The Final Empire"))
r = c.delete(f"/api/items/{plain['id']}", headers=H, params={"hard": 1})
check(r.status_code == 423, "library lock protects every folder")
c.patch(f"/api/libraries/{LIB}", headers=H, json={"settings": {"locked": False}})
check(not c.get(f"/api/items/{plain['id']}", headers=H).json()["locked"], "unlocking the library lifts it")

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
