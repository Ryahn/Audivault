"""Matcher decisions for auto-import. No server and no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.management.autoimport import (  # noqa: E402
    MATCHER_VERSION, _choose, _decided, confident, present_outcome,
)
from app.providers import clean_title  # noqa: E402
from app.scanner.parse import ASIN_IN_NAME  # noqa: E402

FAIL = []


def check(cond, msg):
    print(("  ok:   " if cond else "  FAIL: ") + msg)
    if not cond:
        FAIL.append(msg)


HOBBIT = {"title": "The Hobbit", "author": "J.R.R. Tolkien", "narrator": "Andy Serkis", "duration": 660, "asin": "B009EXAMPLE"}
HOURS = 660 * 60


check(confident({"title": "The Hobbit", "authors": ["J.R.R. Tolkien"], "asin": "B0CMYLRB39"},
                {"title": "Something Else", "asin": "B0CMYLRB39"}, 30)[0], "ASIN match wins")

ok, why = confident({"title": "The Hobbit", "authors": ["J.R.R. Tolkien"]}, HOBBIT, 30)
check(not ok and "length" in why, f"right book with the wrong length stays in review ({why})")

ok, why = confident({"title": "The Hobbit", "authors": ["Sanderson"]}, HOBBIT, HOURS)
check(not ok and "author" in why, f"a different author is not imported just because the length matches ({why})")

ok, why = confident({"title": "The Hobbit", "authors": []}, HOBBIT, HOURS)
check(ok and "author filled" in why, f"missing author is filled when title and length agree ({why})")

ok, why = confident({"title": "The Hobbit", "authors": ["Unknown"]}, HOBBIT, HOURS)
check(ok, f"'Unknown' is ignored and the provider supplies the author ({why})")

ok, why = confident({"title": "The Hobbit", "authors": []}, HOBBIT, 30)
check(not ok and "length" in why, f"missing author plus the wrong length is not imported ({why})")

ok, why = confident({"title": "The Hobbit", "authors": ["Tolkien"]}, HOBBIT, 0)
check(ok and why == "title and author match", f"a last name is enough when the title agrees ({why})")

ok, why = confident({"title": "01 - The Hobbit (Unabridged)", "authors": ["J.R.R. Tolkien"]}, HOBBIT, HOURS)
check(ok, f"track numbers and '(Unabridged)' still match ({why})")

ok, why = confident({"title": "The Hobbit", "authors": ["Andy Serkis"]}, HOBBIT, HOURS)
check(ok and "narrator" in why, f"narrator tagged as the author is corrected ({why})")

ok, why = confident({"title": "Zzqv Nonexistent", "authors": ["Nobody Known"]}, HOBBIT, HOURS)
check(not ok and "closest title" in why, f"an unrelated title is not forced ({why})")

match, reason, near = _choose(
    {"title": "The Hobbit", "authors": []},
    [{"title": "Hobbit Holes", "author": "Someone", "duration": 660}, HOBBIT],
    HOURS,
)
check(match and match["title"] == "The Hobbit" and near is None, f"the length-confirmed book is chosen ({reason})")

match, reason, near = _choose({"title": "The Hobbit", "authors": ["Nobody"]}, [HOBBIT], HOURS)
check(match is None and near and near["asin"] == HOBBIT["asin"], f"a rejected near miss is kept as a suggestion ({reason})")

check(not _decided(None), "a new book is not decided")
check(_decided({"status": "imported"}), "an imported book stays imported")
check(not _decided({"status": "review", "reason": "old"}), "a review from the previous matcher is tried again")
check(_decided({"status": "review", "matcher": MATCHER_VERSION}), "a review from this matcher is left alone")

check(ASIN_IN_NAME.search("Book Title [B0CMYLRB39].m4b").group(1) == "B0CMYLRB39", "bracketed ASIN")
check(ASIN_IN_NAME.search("Title_B0CMYLRB39.m4b").group(1) == "B0CMYLRB39", "ASIN after an underscore")
check(ASIN_IN_NAME.search("not-an-asin-B0CMYLRB391.m4b") is None, "an 11-character token is not an ASIN")
check(clean_title("01 - The Hobbit (Unabridged)") == "The Hobbit", f"search title cleaned ({clean_title('01 - The Hobbit (Unabridged)')!r})")

old = present_outcome({"status": "review", "reason": "Destination already exists: Author/Book (possibly already in the library)"})
check(old["status"] == "skipped" and old["to"] == "Author/Book", f"an existing destination is skipped ({old})")
check(present_outcome({"status": "review", "reason": "No confident match: no title"})["status"] == "review",
      "a real review stays a review")

print()
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
sys.exit(1 if FAIL else 0)
