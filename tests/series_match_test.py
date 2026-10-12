"""Series match queue decisions. No server and no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.management.series_match import (  # noqa: E402
    already_in_library, chosen_sequence, file_sequence, pinned_series, queue_entries, same_series_name,
    sequence_from_label, title_key,
)

FAIL = []
SERIES = "Survival in Another World with My Mistress!"
LIGHT = "Survival in Another World with My Mistress! (Light Novel)"
SID = "series-main"
LIGHT_ID = "series-light"


def check(cond, msg):
    print(("  ok:   " if cond else "  FAIL: ") + msg)
    if not cond:
        FAIL.append(msg)


def item(item_id, title, series, sequence=None, asin=None, rel=None, folder_asins=None):
    return {
        "id": item_id,
        "title": title,
        "rel_path": rel or title,
        "asin": asin,
        "duration": 0,
        "author": "Ryuto",
        "series": series,
        "folder_asins": folder_asins or [],
    }


check(sequence_from_label("Survival in Another World with My Mistress!, Vol. 01") == "1",
      "Vol. 01 in a title becomes 1")
check(sequence_from_label("Title, Vol. 7") == "7", "Vol. 7")
check(sequence_from_label("Book 3") == "3", "Book 3")
check(file_sequence("7 - Title") == "7", "a leading folder number is a sequence")
check(sequence_from_label("101 Dalmatians") is None, "a number that is the title is not a volume")
check(sequence_from_label("The Harsh Mistress of the City") is None, "a shared word is not a volume")

check(same_series_name(SERIES, LIGHT), "(Light Novel) is the same series")
check(same_series_name("Animus", "Animus Series"), "a trailing Series word does not make a new series")
check(not same_series_name(SERIES, "Attack on Titan: The Harsh Mistress of the City"),
      "a different series that shares a word does not match")
check(not same_series_name("Art", "Arthur"), "a short name does not match a longer word")

check(chosen_sequence(
    {"series": [{"series": LIGHT, "sequence": "4"}]},
    SERIES,
    "Survival in Another World with My Mistress!, Vol. 01",
) == "4", "a matching provider series supplies the number")
check(chosen_sequence(
    {"series": [{"series": "Something Else", "sequence": None}]},
    SERIES,
    "Survival in Another World with My Mistress!, Vol. 01",
) == "1", "a provider result with no number keeps the number from the file")
check(chosen_sequence(
    {"series": [{"series": "Other", "sequence": "9"}, {"series": "Also", "sequence": "3"}]},
    SERIES,
    "Title, Vol. 01",
) == "1", "an unrelated provider series does not donate its number")

pinned = pinned_series([(LIGHT, None), ("Cosmere", "1")], SERIES, "2")
check(pinned == [(SERIES, "2"), ("Cosmere", "1")], f"the book stays on this series and keeps an unrelated one ({pinned})")

on_series = [{"id": SID, "name": SERIES, "sequence": "1"}]
light_series = [{"id": LIGHT_ID, "name": LIGHT, "sequence": None}]
harsh = [{"id": "other", "name": "Attack on Titan: The Harsh Mistress of the City", "sequence": None}]
items = [
    item("v1", f"{SERIES} (Light Novel), Vol. 1", on_series, sequence="1", asin="B0DK2BX4MZ"),
    item("v01", f"{SERIES}, Vol. 01", [{"id": SID, "name": SERIES, "sequence": None}],
         folder_asins=["B0DYVY5WF6"]),
    item("v2", f"{LIGHT}, Vol. 2", light_series),
    item("v7", f"{LIGHT}, Vol. 7", light_series),
    item("harsh", "Attack on Titan: The Harsh Mistress of the City, Vol. 02", harsh),
]
candidates = [
    {"relPath": "Ryuto/Series/Vol. 8 [B0FX5ZQ3JB]", "metadata": {"title": f"{SERIES} Vol. 8", "asin": "B0FX5ZQ3JB", "authors": ["Ryuto"]},
     "files": ["Vol. 8 [B0FX5ZQ3JB].m4b"], "autoImport": None},
    {"relPath": f"Ryuto/{SERIES}/Vol. 4", "metadata": {"title": f"{SERIES}, Vol. 4", "authors": ["Ryuto"]},
     "files": [], "autoImport": None},
    {"relPath": f"Ryuto/{SERIES}/Bonus", "metadata": {"title": f"{SERIES}, Vol. 10", "authors": ["Ryuto"]},
     "files": [], "autoImport": {"status": "skipped"}},
]
asins = {"B0FX5ZQ3JB", "B0DK2BX4MZ"}
titles = set()
queue = queue_entries(SERIES, SID, items, candidates, asins, titles)
by_id = {entry["itemId"] or entry["relPath"]: entry for entry in queue}

check("harsh" not in by_id, "an unrelated book is left out")
check(by_id["v2"]["source"] == "split" and by_id["v2"]["sourceName"] == LIGHT and by_id["v2"]["fileSequence"] == "2",
      f"volume 2 is pulled in from the split series ({by_id.get('v2')})")
check(by_id["v7"]["fileSequence"] == "7" and by_id["v7"]["sequence"] is None, "volume 7 has no stored number")
check(by_id["v01"]["source"] == "series" and by_id["v01"]["sequence"] is None and by_id["v01"]["fileSequence"] == "1",
      "the unlabeled Vol. 01 stays on this series")
check(by_id["v01"]["asins"] == ["B0DYVY5WF6"], f"an ASIN in the folder is searchable ({by_id['v01']['asins']})")
check("Ryuto/Series/Vol. 8 [B0FX5ZQ3JB]" not in by_id, "an import copy whose ASIN is already in the library is left out")
check(f"Ryuto/{SERIES}/Bonus" not in by_id, "a skipped import stays out")
check(any(entry["source"] == "import" and entry["fileSequence"] == "4" for entry in queue), "a new import volume is included")
check([entry["itemId"] or entry["fileSequence"] for entry in queue if entry["source"] != "import"][:3] == ["v01", "v2", "v7"],
      f"books with no number come first ({[e['itemId'] for e in queue]})")
check(queue[-1]["source"] == "import", "import files come after library books")

copy = {"relPath": "copy", "metadata": {"title": f"{SERIES} (Light Novel), Vol. 1 [B0NOTEXIST]"}, "files": [], "autoImport": None}
check(already_in_library(copy, set(), {title_key(f"{SERIES} (Light Novel), Vol. 1")}),
      "the same title with a bracketed ASIN is already in the library")

print()
print("FAILURES:", len(FAIL))
for failure in FAIL:
    print(" -", failure)
sys.exit(1 if FAIL else 0)
