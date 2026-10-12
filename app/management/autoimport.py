"""Automatic import from a library's import folder.

When a library has `autoImport` on, new books in its import folder are:
1. left alone until their files have stopped changing (newest file at least
   ABSTREAM_IMPORT_SETTLE seconds old, sizes stable), so half-copied books are
   never moved;
2. matched against the library's metadata provider. A confident match is
   imported: same ASIN; a closely matching title and author (a last name is
   enough); or a near-exact title whose length agrees, which is how a book
   with no author — or with the narrator tagged as the author — still gets
   its author from the provider. When the provider knows the length, it has
   to be within 5%;
3. imported with the library's default operation (`importMode`: move or copy),
   using the matched metadata and cover.

Books without a confident match stay on the Import page marked "needs review",
with the closest provider match filled into the empty fields. A confident
match whose library folder already exists is skipped: the library copy is
left alone and the import copy stays where it is. Outcomes are remembered
per book (by a signature of its files) so a copied book isn't imported twice
and a book needing review isn't searched again until its files change or the
matcher itself changes.
"""

import hashlib
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from ..core import db, events, tasks
from ..catalog import library
from .. import providers
from ..scanner.parse import ASIN_IN_NAME, metadata_from_match, parse_names
from . import organizer

log = logging.getLogger("abstream.autoimport")

SETTLE_SECONDS = float(os.environ.get("ABSTREAM_IMPORT_SETTLE", "60"))
MAX_WAIT_SECONDS = 15 * 60
# Review outcomes from an older matcher are searched once more.
MATCHER_VERSION = 2
_PLACEHOLDER_AUTHORS = {
    "unknown", "unknownartist", "unknownauthor", "various", "variousartists", "none", "na", "soundtrack",
}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


# ------------------------------------------------------------------ remembered outcomes

def _state_key(library_id: str) -> str:
    return f"auto_import_state:{library_id}"


def load_state(library_id: str) -> dict:
    return db.get_setting(_state_key(library_id), {}) or {}


def _probe_cache_key(library_id: str) -> str:
    return f"import_probe_cache:{library_id}"


def load_probe_cache(library_id: str) -> dict:
    return db.get_setting(_probe_cache_key(library_id), {}) or {}


def save_probe_cache(library_id: str, cache: dict) -> None:
    # Cap cache size; drop oldest-looking keys arbitrarily by insertion order
    if len(cache) > 2000:
        for k in list(cache.keys())[: len(cache) - 2000]:
            cache.pop(k, None)
    db.set_setting(_probe_cache_key(library_id), cache)


def _save_outcome(library_id: str, signature: str, outcome: dict) -> None:
    state = load_state(library_id)
    state[signature] = outcome | {"at": int(time.time() * 1000), "matcher": MATCHER_VERSION}
    # keep the most recent 500 outcomes
    if len(state) > 500:
        for k, _ in sorted(state.items(), key=lambda kv: kv[1].get("at", 0))[:len(state) - 500]:
            state.pop(k, None)
    db.set_setting(_state_key(library_id), state)


def present_outcome(outcome: dict | None) -> dict | None:
    """Older runs stored a destination collision as a review. It is a skip."""
    if not outcome or outcome.get("status") != "review":
        return outcome
    reason = outcome.get("reason") or ""
    if not reason.startswith("Destination already exists"):
        return outcome
    target = reason.split(": ", 1)[-1].removesuffix(" (possibly already in the library)")
    return outcome | {
        "status": "skipped", "to": target,
        "reason": f"Already in the library at {target}. This copy was left in the import folder.",
    }


def _decided(outcome: dict | None) -> bool:
    """Imported books stay decided. A review is final only for this matcher."""
    if not outcome:
        return False
    if outcome.get("status") == "review" and outcome.get("matcher") != MATCHER_VERSION:
        return False
    return True


def signature(rel: str, files: list[str]) -> str:
    parts = [rel]
    for f in sorted(files):
        try:
            st = os.stat(f)
            parts.append(f"{os.path.basename(f)}:{st.st_size}")
        except OSError:
            parts.append(os.path.basename(f))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ matching

def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"\((?:un)?abridged\)|\[[^\]]*\]", " ", t)
    t = re.sub(r"\((?:graphic\s*audio|dramatized(?:\s+adaptation)?)\)", " ", t)
    t = re.sub(r"^\d{1,2}\s*[-.:)]\s*", "", t.strip())
    t = re.sub(r"^(?:the|a|an)\s+", "", t.strip())
    return re.sub(r"[\W_]+", " ", t).strip()


def _norm_name(name: str) -> str:
    return re.sub(r"[\W_]+", "", (name or "").lower())


def _usable_authors(names) -> list[str]:
    """Drop tags like 'Unknown' that would hide the real author from a search."""
    return [n for n in (names or []) if n and _norm_name(n) not in _PLACEHOLDER_AUTHORS]


def _name_tokens(name: str) -> list[str]:
    return [p for p in re.sub(r"[^\w\s]", " ", name or "").lower().split() if p and p not in _NAME_SUFFIXES]


def _last_tokens(names: list[str]) -> set[str]:
    last = set()
    for name in names:
        tokens = _name_tokens(name)
        if tokens:
            last.add(tokens[-1])
    return last


def _names_match(guess_names: list[str], other: str | None) -> bool:
    """Full-name match, or a shared last name of at least 3 letters."""
    others = parse_names(other or "")
    if {_norm_name(a) for a in guess_names} & {_norm_name(a) for a in others}:
        return True
    other_last = _last_tokens(others)
    return any(len(token) >= 3 and token in other_last for token in _last_tokens(guess_names))


def _title_similarity(title: str | None, result: dict) -> float:
    left = _norm(title)
    if not left:
        return 0
    candidates = [_norm(result.get("title"))]
    if result.get("subtitle"):
        candidates.append(_norm(f"{result.get('title')} {result.get('subtitle')}"))
    scores = [SequenceMatcher(None, left, c).ratio() for c in candidates if c]
    return max(scores) if scores else 0


def _length_note(result: dict, duration_s: float) -> str | None:
    """A reason to reject, when both sides have a length and they disagree."""
    minutes = result.get("duration") or 0
    if not minutes or not duration_s:
        return None
    if abs(duration_s / (minutes * 60) - 1) > 0.05:
        return f"length differs from the match ({duration_s / 3600:.1f}h vs {minutes / 60:.1f}h)"
    return None


def _length_known(result: dict, duration_s: float) -> bool:
    return bool((result.get("duration") or 0) and duration_s)


def confident(guess: dict, result: dict, duration_s: float) -> tuple[bool, str]:
    asin = (guess.get("asin") or "").upper()
    if asin and (result.get("asin") or "").upper() == asin:
        return True, "ASIN matches"
    if not _norm(guess.get("title")):
        return False, "no title to compare"
    similarity = _title_similarity(guess.get("title"), result)
    if similarity < 0.85:
        return False, f"closest title found was \"{result.get('title')}\""
    authors = _usable_authors(guess.get("authors"))
    if authors and _names_match(authors, result.get("author")):
        note = _length_note(result, duration_s)
        if note:
            return False, note
        return True, "title and author match"
    # No usable author, or the artist tag is the narrator. An almost exact title
    # plus an agreeing length is enough to take the author from the provider.
    length_ok = _length_known(result, duration_s) and _length_note(result, duration_s) is None
    if not authors and similarity >= 0.95 and length_ok:
        return True, "title and length match; author filled from the provider"
    if authors and _names_match(authors, result.get("narrator")) and similarity >= 0.92 and length_ok:
        return True, "file tagged the narrator as the author; title and length match"
    if not authors:
        return False, _length_note(result, duration_s) or "the author is unknown, so the match can't be confirmed"
    return False, f"author doesn't match (found {result.get('author') or 'none'})"


# ------------------------------------------------------------------ settling

def _settled(files: list[str]) -> bool:
    """Newest file old enough and no size change over a short interval."""
    try:
        newest = max(os.stat(f).st_mtime for f in files)
    except (OSError, ValueError):
        return False
    if time.time() - newest < SETTLE_SECONDS:
        return False
    sizes = [os.path.getsize(f) for f in files]
    time.sleep(1)
    try:
        return sizes == [os.path.getsize(f) for f in files]
    except OSError:
        return False


# ------------------------------------------------------------------ run

def run(task: dict | None, library_id: str) -> dict:
    lib = library.get_library(library_id)
    if not lib:
        raise ValueError("Library not found")
    settings = library.library_settings(lib)
    if not settings.get("autoImport"):
        return {"imported": 0, "review": [], "failed": [], "skipped": "auto-import is off"}
    provider = settings.get("metadataProvider") or "audible"
    mode = "copy" if settings.get("importMode") == "copy" else "move"
    import_dir = settings.get("importFolder")
    imported, review, failed, skipped = [], [], [], []
    started = time.time()

    while True:
        listing = organizer.list_import_candidates(library_id)
        state = load_state(library_id)
        todo = [c for c in listing["candidates"] if not _decided(state.get(c["signature"]))]
        waiting = []
        if task is not None:
            task["total"] = len(imported) + len(review) + len(failed) + len(skipped) + len(todo)
        for cand in todo:
            files = cand["files"]
            if not _settled(files):
                waiting.append(cand)
                continue
            if task is not None:
                task["message"] = cand["relPath"]
            guess = cand["metadata"]
            outcome = _process(library_id, cand, guess, provider, mode, import_dir)
            if outcome["status"] == "imported":
                imported.append({"relPath": cand["relPath"], "to": outcome["to"], "match": outcome["reason"]})
            elif outcome["status"] == "review":
                review.append({"relPath": cand["relPath"], "reason": outcome["reason"]})
            elif outcome["status"] == "skipped":
                skipped.append({"relPath": cand["relPath"], "to": outcome.get("to"), "reason": outcome["reason"]})
            else:
                failed.append({"relPath": cand["relPath"], "error": outcome["reason"]})
            if task is not None:
                task["progress"] = len(imported) + len(review) + len(failed) + len(skipped)
        if not waiting or time.time() - started > MAX_WAIT_SECONDS:
            break
        if task is not None:
            task["message"] = f"Waiting for {len(waiting)} book(s) to finish copying"
        time.sleep(min(15, max(2, SETTLE_SECONDS / 4)))

    if imported:
        tasks.activity(f"Auto-imported {len(imported)} book(s) into \"{lib['name']}\"")
        events.publish({"type": "library_changed", "libraryId": library_id, "added": len(imported), "updated": 0, "missing": 0})
    if review:
        tasks.activity(f"{len(review)} book(s) in the import folder need review before importing", "warning")
    if skipped:
        tasks.activity(f"{len(skipped)} book(s) already in \"{lib['name']}\" were left in the import folder")
    if imported or review or failed or skipped:
        remaining = len(organizer.list_import_candidates(library_id)["candidates"])
        events.publish({"type": "import_changed", "libraryId": library_id, "count": remaining, "new": 0})
    return {"imported": imported, "review": review, "failed": failed, "skipped": skipped, "mode": mode, "libraryId": library_id}


def _result_key(result: dict) -> str:
    asin = (result.get("asin") or "").upper()
    if asin:
        return asin
    return _norm(result.get("title") or "") + "|" + _norm_name(result.get("author") or "")


def _search(provider: str, title: str, author: str | None, asin: str | None, rel: str) -> list[dict]:
    try:
        return providers.search_books(provider, title, author, asin=asin)
    except Exception as e:
        log.warning("Provider search failed for %s: %s", rel, e)
        return []


def _choose(guess: dict, results: list[dict], duration_s: float) -> tuple[dict | None, str, dict | None]:
    """Best confident match, plus the closest result to show when nothing qualifies."""
    match, match_sim, reason = None, -1.0, "no results from the metadata provider"
    best, best_sim = None, -1.0
    for result in results:
        if not result:
            continue
        ok, why = confident(guess, result, duration_s)
        sim = _title_similarity(guess.get("title"), result)
        asin_hit = ok and why == "ASIN matches"
        if ok and (match is None or asin_hit or (reason != "ASIN matches" and sim > match_sim)):
            match, match_sim, reason = result, sim, why
        if sim > best_sim:
            best, best_sim = result, sim
            if match is None:
                reason = why
    if match is not None:
        return match, reason, None
    if best is not None and best_sim >= 0.5:
        return None, reason, best
    return None, reason, None


def _suggestion(result: dict) -> dict:
    description = result.get("description") or None
    if description and len(description) > 1500:
        description = description[:1500].rsplit(" ", 1)[0]
    return {
        "title": result.get("title"), "subtitle": result.get("subtitle"), "author": result.get("author"),
        "narrator": result.get("narrator"), "series": result.get("series"), "publishedYear": result.get("publishedYear"),
        "asin": result.get("asin"), "isbn": result.get("isbn"), "description": description,
        "publisher": result.get("publisher"), "genres": result.get("genres"), "language": result.get("language"),
        "cover": result.get("cover"),
    }


def _process(library_id: str, cand: dict, guess: dict, provider: str, mode: str, import_dir: str) -> dict:
    sig = cand["signature"]
    title = guess.get("title") or Path(cand["relPath"]).stem
    authors = _usable_authors(guess.get("authors"))
    author = authors[0] if authors else None
    asin = guess.get("asin")
    duration = cand.get("duration") or 0
    results = _search(provider, title, author, asin, cand["relPath"])
    match, reason, near = _choose(guess, results, duration)
    # A wrong artist tag makes the provider filter out the real book. Search the title on its own.
    if not match and (author or asin):
        more = _search(provider, title, None, None, cand["relPath"])
        seen = {_result_key(r) for r in results}
        extra = [r for r in more if _result_key(r) not in seen]
        match, reason, near = _choose(guess, results + extra, duration)
    if not match:
        outcome = {"status": "review", "reason": f"No confident match: {reason}"}
        if near:
            outcome["suggestion"] = _suggestion(near)
        _save_outcome(library_id, sig, outcome)
        return outcome
    try:
        res = organizer.import_candidate(library_id, cand["relPath"], metadata_from_match(match, guess), mode=mode,
                                         cover_url=match.get("cover"), source_signature=cand.get("signature"))
    except FileExistsError as e:
        target = str(e).split(": ", 1)[-1]
        outcome = {"status": "skipped", "to": target,
                   "reason": f"Already in the library at {target}. This copy was left in the import folder."}
        _save_outcome(library_id, sig, outcome)
        return outcome
    except Exception as e:
        log.exception("Auto-import failed for %s", cand["relPath"])
        return {"status": "failed", "reason": str(e)}  # not remembered: retried next time
    outcome = {"status": "imported", "reason": reason, "to": res["relPath"], "itemId": res["id"]}
    _save_outcome(library_id, sig, outcome)
    return outcome


def start(library_id: str, reason: str = "") -> dict | None:
    lib = library.get_library(library_id)
    if not lib or not library.library_settings(lib).get("autoImport"):
        return None
    return tasks.start("import", f"Auto-import into {lib['name']}", run, library_id, key=f"autoimport:{library_id}",
                       library_id=library_id, quiet=True)
