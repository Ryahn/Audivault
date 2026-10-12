"""Duplicate titles like 'Advance' and 'Advance: Animus, Book 4' share a folder name."""

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="abstream-naming-")
os.environ["ABSTREAM_DATA_DIR"] = _tmp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import library  # noqa: E402
from app.core import db  # noqa: E402
from app.management.duplicates import find_groups  # noqa: E402
from app.management.organizer import render, template_values  # noqa: E402
from app.scanner.parse import canonical_author_name, fold_embedded_series, split_embedded_series  # noqa: E402
from app.scanner.scanner import gather_metadata  # noqa: E402

FAIL = []
TEMPLATE = "{author}/[{series}/][{sequence} - ]{title}"
KNOWN = ["Animus Series", "Advance", "Infiltration", "Master", "Payback"]


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def path_for(title, authors, series):
    return render(TEMPLATE, template_values(title, authors, series, known_series=KNOWN))


db.init_db()

print("embedded series title")
check(split_embedded_series("Advance: Animus, Book 4") == ("Advance", "Animus", "4"), "colon form")
check(split_embedded_series("Advance - Animus, Book 4") == ("Advance", "Animus", "4"), "organizer form")
check(split_embedded_series("Project Hail Mary") == ("Project Hail Mary", None, None), "plain title stays")
vol_title = "Classroom of the Elite: Year 2, Vol. 01"
check(split_embedded_series(vol_title) == (vol_title, None, None), "volume subtitle is not a series")
check(split_embedded_series("Hell Mode: The Hardcore Gamer, Volume 02") == (
    "Hell Mode: The Hardcore Gamer, Volume 02", None, None), "volume word is not a series")
check(canonical_author_name("Michael T Anderle") == "Michael Anderle", "middle initial dropped")
check(canonical_author_name("Michael t. Anderle") == "Michael Anderle", "lowercase initial dropped")
check(canonical_author_name("Mary Ann Evans") == "Mary Ann Evans", "middle name kept")

print("organizer names both copies the same")
authors_t = ["Joshua Anderle", "Michael T Anderle"]
authors = ["Joshua Anderle", "Michael Anderle"]
advance = "Joshua Anderle & Michael Anderle/Animus Series/4 - Advance"
check(path_for("Advance", authors_t, [("Animus Series", "4")]) == advance, "matched Advance")
check(path_for("Advance: Animus, Book 4", authors, [("Advance", None)]) == advance, "album Advance")
victor = "Joshua Anderle & Michael Anderle/Animus Series/12 - Victor"
check(path_for("Victor", authors, [("Animus Series", "12")]) == victor, "matched Victor")
check(path_for("Victor: Animus, Book 12", authors, [("Animus Series", "2")]) == victor, "mislabeled Victor")
invasion = "Joshua Anderle & Michael Anderle/Animus Series/10 - Invasion"
check(path_for("Invasion: Animus, Book 10", authors, [("Animus Series", "10")]) == invasion, "long Invasion")
check(path_for("Project Hail Mary", ["Andy Weir"], []) == "Andy Weir/Project Hail Mary", "unrelated title")
elite = "Shogo Kinugasa/Classroom of the Elite Year 2/Classroom of the Elite - Year 2, Vol. 01"
check(path_for(vol_title, ["Shogo Kinugasa"], [("Classroom of the Elite Year 2", None)]) == elite,
      "volume title is not shortened")
kept = {"title": vol_title, "series_list": [("Classroom of the Elite Year 2", "1")]}
fold_embedded_series(kept, vol_title)
check(kept["title"] == vol_title and kept["series_list"] == [("Classroom of the Elite Year 2", "1")],
      f"scanner keeps a volume title ({kept})")

print("scanner stores the short title and the book number from the album")
tagged = gather_metadata(
    {},
    "Joshua Anderle & Michael Anderle/Advance/Advance - Animus, Book 4",
    False, None,
    [{"tags": {"album": "Advance: Animus, Book 4", "title": "Advance",
               "album_artist": "Joshua Anderle, Michael Anderle"}}],
)
check(tagged["title"] == "Advance", f"tag title ({tagged['title']})")
check(tagged["series_list"] == [("Animus", "4")], f"bogus series replaced ({tagged['series_list']})")

victor_md = {"title": "Victor - Animus, Book 12", "series_list": [("Animus Series", "2")]}
fold_embedded_series(victor_md, "Victor: Animus, Book 12")
check(victor_md["title"] == "Victor" and victor_md["series_list"] == [("Animus Series", "12")],
      f"sequence taken from the title ({victor_md})")

print("duplicates group the two copies")
lib_id = db.new_id()
now = db.now_ms()
db.insert("libraries", {"id": lib_id, "name": "Audiobooks", "folders": "[]", "created_at": now, "updated_at": now})
author_id = db.new_id()
db.insert("authors", {"id": author_id, "library_id": lib_id, "name": "Joshua Anderle",
                      "added_at": now, "updated_at": now})


def add_book(title, duration, asin=None):
    item_id = db.new_id()
    db.insert("library_items", {
        "id": item_id, "library_id": lib_id, "folder_id": "folder", "path": item_id, "rel_path": title,
        "added_at": now, "updated_at": now, "title": title, "duration": duration, "asin": asin,
    })
    db.ex("INSERT INTO book_authors (item_id, author_id, position) VALUES (?, ?, 0)", (item_id, author_id))
    return item_id


add_book("Advance", 24310, "1774240890")
add_book("Advance: Animus, Book 4", 24311)
add_book("Co-op", 35062)
add_book("Advance: Other Series, Book 1", 1000)
groups = find_groups()
paired = [g for g in groups if {i["title"] for i in g["items"]} == {"Advance", "Advance: Animus, Book 4"}]
check(len(paired) == 1, f"Advance pair grouped ({[g['items'] for g in groups]})")
check(all("Advance: Other Series, Book 1" not in {i["title"] for i in g["items"]} or len(g["items"]) == 1
          for g in groups), "different length is not the same book")
check(library.get_library(lib_id)["name"] == "Audiobooks", "library still readable")

print("same recording, different title")
from app.management.duplicates import _chapters_align  # noqa: E402

bound = [0, 567.8, 3195.9, 10895.8, 15459.1, 22735.6, 23470.1, 24422.3]
volume = [0, 23.9, 569.5, 3197.6, 10897.2, 15460.4, 22736.6, 23471.1]
invasion = [0, 18.5, 497.9, 921.8, 1350.6, 1896.6, 2480.3, 3001.3]
check(_chapters_align(bound, volume), "volume subtitle shares chapter times")
check(not _chapters_align(bound, invasion), "different book in the series does not align")


def add_chapters(title, duration, starts, author=None):
    item_id = db.new_id()
    who = author or author_id
    db.insert("library_items", {
        "id": item_id, "library_id": lib_id, "folder_id": "folder", "path": item_id, "rel_path": title,
        "added_at": now, "updated_at": now, "title": title, "duration": duration, "num_chapters": len(starts),
        "chapters": db.jdump([{"start": s, "end": s + 1, "title": "Chapter"} for s in starts]),
    })
    db.ex("INSERT INTO book_authors (item_id, author_id, position) VALUES (?, ?, 0)", (item_id, who))
    return item_id


add_chapters("The Bound Demon", 25621, bound)
add_chapters("Black Summoner: Volume 1", 25622, volume)
add_chapters("Infiltration", 23050, [0, 18.4, 611.7, 1219.8, 1813.3, 2405.8, 2990.2, 3532.3])
add_chapters("Invasion", 23054, invasion)
groups = find_groups()
titles_of = lambda name: next((g for g in groups if name in {i["title"] for i in g["items"]}), None)
bound_group = titles_of("The Bound Demon")
check(bound_group and {i["title"] for i in bound_group["items"]} == {"The Bound Demon", "Black Summoner: Volume 1"},
      f"subtitle copy grouped ({bound_group})")
check("Same recording" in (bound_group or {}).get("reasons", []), "reason is the recording")
invasion_group = titles_of("Invasion")
check(invasion_group is None or "Infiltration" not in {i["title"] for i in invasion_group["items"]},
      "nearby series books stay separate")

if FAIL:
    print(f"\n{len(FAIL)} failed")
    sys.exit(1)
print("\nall passed")
