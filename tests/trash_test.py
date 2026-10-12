"""Tests for the trash: listing, retention-based emptying, restore, delete, empty.

Run after dup_test.py (which leaves discarded copies in the trash), against the same server:
    python tests/trash_test.py ./testdata
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("ABSTREAM_TEST_URL", "http://127.0.0.1:13399")
TD = Path(sys.argv[1])
FAIL = []
lib_root = TD / "library"
trash_root = lib_root / ".abstream-trash"


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


def trash():
    return c.get("/api/abstream/trash", headers=H).json()


def idle():
    return all(t["status"] != "running" for t in c.get("/api/abstream/tasks", headers=H).json()["tasks"])


print("listing")
t = trash()
check(t["retentionDays"] == 30 and not t["lockedByEnv"], f"default retention 30 days, not locked ({t['retentionDays']}, {t['lockedByEnv']})")
check(len(t["entries"]) >= 1, f"discarded duplicates are listed ({len(t['entries'])} batch(es))")
recent = t["entries"][0]
check(recent["expiresAt"] and recent["expiresAt"] > time.time() * 1000, "recent batch expires in the future")
check(all(b["relPath"] for b in recent["books"]) and recent["size"] > 0, "books and sizes listed")

# An old batch (2020) that the retention limit should remove
old = trash_root / "20200101-000000" / "Old Author" / "Old Book"
old.mkdir(parents=True, exist_ok=True)
shutil.copy(lib_root / "Andy Weir/Project Hail Mary/Project Hail Mary.mp3", old / "x.mp3")
t = trash()
old_entry = next((e for e in t["entries"] if e["batch"] == "20200101-000000"), None)
check(old_entry is not None and old_entry["books"][0]["relPath"] == "Old Author/Old Book", "old batch listed with its book")

print("retention")
r = c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": "abc"})
check(r.status_code == 400, "invalid retention rejected")
r = c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": 7})
check(r.status_code == 200 and r.json()["retentionDays"] == 7, "retention saved")
check(wait_for(lambda: not (trash_root / "20200101-000000").exists(), 30), "batch older than 7 days emptied right away")
wait_for(idle, 30)
check(any(e["batch"] == recent["batch"] for e in trash()["entries"]), "recent batch kept")
c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": 0})
check(trash()["retentionDays"] == 0, "0 = keep forever")

print("restore")
book = next(b for e in trash()["entries"] for b in e["books"] if b["relPath"] == "Andy Weir/Project Hail Mary (2021 rip)")
entry = next(e for e in trash()["entries"] if any(b["relPath"] == book["relPath"] for b in e["books"]))
r = c.post("/api/abstream/trash/restore", headers=H, json={"folderId": entry["folderId"], "batch": entry["batch"], "relPath": book["relPath"]})
check(r.status_code == 200, f"restore accepted ({r.text[:120]})")
check((lib_root / "Andy Weir/Project Hail Mary (2021 rip)/Project Hail Mary.mp3").exists(), "files moved back into the library")


def restored_item():
    items = c.get(f"/api/libraries/{LIB}/items", headers=H, params={"limit": 500}).json()["results"]
    return next((i for i in items if i["relPath"] == "Andy Weir/Project Hail Mary (2021 rip)" and not i["isMissing"]), None)


check(wait_for(restored_item, 60) is not None, "restored book scanned back in")
r = c.post("/api/abstream/trash/restore", headers=H, json={"folderId": entry["folderId"], "batch": entry["batch"], "relPath": book["relPath"]})
check(r.status_code == 404, "restoring it twice fails cleanly")

print("delete")
r = c.delete(f"/api/abstream/trash/{entry['folderId']}/..", headers=H)
check(r.status_code in (404, 405), "path traversal in batch name rejected")
r = c.delete(f"/api/abstream/trash/{entry['folderId']}/{entry['batch']}", headers=H, params={"rel": "../../Andy Weir"})
check(r.status_code == 404 and (lib_root / "Andy Weir").exists(), "path traversal in book path rejected")
remaining = [e for e in trash()["entries"] if e["books"]]
if remaining:
    e = remaining[0]
    b = e["books"][0]
    r = c.delete(f"/api/abstream/trash/{e['folderId']}/{e['batch']}", headers=H, params={"rel": b["relPath"]})
    check(r.status_code == 200 and not any(x["relPath"] == b["relPath"] for x in
                                           next((y for y in trash()["entries"] if y["batch"] == e["batch"]), {"books": []})["books"]),
          "single book deleted from the trash")

print("empty")
extra = trash_root / time.strftime("%Y%m%d-%H%M%S") / "Extra Author" / "Extra Book"
extra.mkdir(parents=True, exist_ok=True)
(extra / "a.mp3").write_bytes(b"\x00" * 1000)
r = c.post("/api/abstream/trash/empty", headers=H)
task = r.json()["task"]
done = wait_for(lambda: (x := c.get(f"/api/abstream/tasks/{task['id']}", headers=H).json())["status"] != "running" and x, 30)
check(done and done["result"]["removed"] >= 1 and done["result"]["everything"], f"empty trash task ({done and done['result']})")
check(not trash_root.exists() and trash()["entries"] == [], "trash folder removed")
c.patch("/api/abstream/trash/settings", headers=H, json={"retentionDays": 30})

print("environment variable lock")
code = ("import os; os.environ['ABSTREAM_TRASH_DAYS']='14'; from app.management import trash; "
        "assert trash.retention_days() == 14 and trash.env_days() == 14\n"
        "try:\n    trash.set_retention_days(3); print('NOT LOCKED')\nexcept ValueError:\n    print('LOCKED')")
env = dict(os.environ, ABSTREAM_DATA_DIR=str(TD / "_envcheck"))
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parent.parent)
check(out.stdout.strip() == "LOCKED", f"ABSTREAM_TRASH_DAYS overrides and locks the app setting ({out.stdout.strip()} {out.stderr[-200:]})")
shutil.rmtree(TD / "_envcheck", ignore_errors=True)

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
