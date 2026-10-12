"""Decision support for the duplicate manager.

Looks at each copy in a duplicate group and reports what matters when picking
one to keep, then recommends a copy:

- audio quality: duration-weighted average bitrate (low-bitrate copies are
  usually re-encodes of the original)
- broken copies: no playable audio
- the same audio saved more than once in one folder (e.g. "Part 01" 128k and
  "Part 02" 32k of the same book), which also doubles the length
- length compared with the official Audible runtime (via Audnexus, when an ASIN
  is known) or with the other copies
- chapters: named vs. only numbered, opening/end credits present
- metadata: ASIN, series number, cover, cue sheet, metadata.json, description
- whether the folder already matches the naming template
- identical audio across copies (then only metadata/organization differ)
"""

import logging
import re
import statistics
import threading
import time

from ..core import db

log = logging.getLogger("abstream.duplicates")

RUNTIME_CACHE_KEY = "audible_runtime_cache"
RUNTIME_TTL = 60 * 60 * 24 * 30
NUMBERED_CHAPTER = re.compile(r"^\s*(chapter|track|part|kapitel|cap[ií]tulo)?\s*[\divxlc]+\s*[.:-]?\s*(-\s*[\d:]+)?\s*$", re.I)
ASIN_IN_NAME = re.compile(r"\[([A-Z0-9]{10})\]")
CREDITS_CHAPTER = re.compile(r"(opening|intro|closing|end|outro)\s+credits", re.I)


# ------------------------------------------------------------------ Audible runtime

_fill_lock = threading.Lock()
_filling = False


def _fetch_runtime(asin: str) -> tuple[str, float | None]:
    from .providers.http import get_json
    try:
        data = get_json(f"https://api.audnex.us/books/{asin}")
        minutes = (data or {}).get("runtimeLengthMin")
        return asin, float(minutes) * 60 if minutes else None
    except Exception:
        return asin, None


def _fill_runtimes(asins: list[str]) -> None:
    global _filling
    try:
        before = db.get_setting(RUNTIME_CACHE_KEY, {}) or {}
        audible_runtimes(set(asins), wait=True)
        after = db.get_setting(RUNTIME_CACHE_KEY, {}) or {}
        if any(after.get(a) != before.get(a) for a in asins):
            from ..core import events
            events.publish({"type": "duplicates_runtimes", "adminOnly": True})
    except Exception:
        log.exception("Audible runtime lookup failed")
    finally:
        with _fill_lock:
            _filling = False


def _schedule_runtime_fill(asins: list[str]) -> None:
    global _filling
    with _fill_lock:
        if _filling or not asins:
            return
        _filling = True
    threading.Thread(target=_fill_runtimes, args=(asins,), name="audible-runtimes", daemon=True).start()


def audible_runtimes(asins: set[str], *, wait: bool = True) -> dict[str, float]:
    """ASIN -> runtime in seconds, cached for 30 days. Network failures are ignored.

    The duplicates page passes wait=False so the list can render from the cache.
    Missing lengths are filled in the background and the page refreshes once.
    """
    cache = db.get_setting(RUNTIME_CACHE_KEY, {}) or {}
    now = time.time()
    todo = [a for a in asins if a not in cache or now - cache[a].get("t", 0) > RUNTIME_TTL]

    if todo and wait:
        for asin in todo[:60]:
            _, seconds = _fetch_runtime(asin)
            cache[asin] = {"s": seconds, "t": now}
        db.set_setting(RUNTIME_CACHE_KEY, cache)
    elif todo:
        _schedule_runtime_fill(todo[:60])
    return {a: cache[a]["s"] for a in asins if a in cache and cache[a].get("s")}


# ------------------------------------------------------------------ per-copy facts

def _audio_facts(item: dict) -> dict:
    audio = [a for a in db.jload(item["audio_files"], []) if not a.get("exclude")]
    playable = [a for a in audio if (a.get("duration") or 0) > 1]
    total = sum(a["duration"] for a in playable)
    size = sum(a.get("size") or 0 for a in playable)
    if total > 0:
        weighted = sum((a.get("bit_rate") or (a.get("size", 0) * 8 / a["duration"])) * a["duration"] for a in playable) / total
    else:
        weighted = 0
    rates = [round((a.get("bit_rate") or 0) / 1000) for a in playable]

    # Same audio saved more than once: 2+ long files whose lengths are within 1% of each other
    repeated: list[list[dict]] = []
    used = set()
    for i, a in enumerate(playable):
        if i in used or a["duration"] < 600:
            continue
        cluster = [a]
        for j in range(i + 1, len(playable)):
            b = playable[j]
            if j not in used and abs(b["duration"] - a["duration"]) <= max(5.0, a["duration"] * 0.01):
                cluster.append(b)
                used.add(j)
        # Every file the same length AND different encodes (bitrate/codec): the same book saved
        # more than once. Equal-length parts of one encode could be a legitimately split book.
        rates = [a.get("bit_rate") or 0 for a in cluster]
        different_encodes = len({a.get("codec") for a in cluster}) > 1 or (min(rates) and max(rates) > min(rates) * 1.2)
        if len(cluster) > 1 and len(cluster) == len(playable) and different_encodes:
            repeated.append(cluster)

    fix = None
    if repeated:
        cluster = repeated[0]
        best = max(cluster, key=lambda a: (a.get("bit_rate") or 0, a.get("size") or 0))
        fix = {"keepFile": best["filename"], "kbps": round((best.get("bit_rate") or 0) / 1000),
               "exclude": [a["ino"] for a in cluster if a is not best],
               "excludeFiles": [a["filename"] for a in cluster if a is not best]}
    return {
        "fix": fix,
        "files": playable, "total": total, "size": size, "kbps": round(weighted / 1000) if weighted else 0,
        "rates": rates, "mixed": len(set(r // 16 for r in rates if r)) > 1, "repeated": repeated,
        "signature": tuple(sorted((round(a["duration"]), a.get("size") or 0) for a in playable)),
    }


def _chapter_facts(item: dict) -> dict:
    chapters = db.jload(item["chapters"], [])
    titles = [c.get("title") or "" for c in chapters]
    named = [t for t in titles if t and not NUMBERED_CHAPTER.match(t) and not CREDITS_CHAPTER.search(t)]
    return {"count": len(chapters), "named": len(named) >= max(1, len(titles) // 2),
            "credits": any(CREDITS_CHAPTER.search(t) for t in titles)}


def _majority_length(lengths: list[float]) -> float | None:
    """The length more than half of the copies agree on (within 2%), else None."""
    for length in lengths:
        agreeing = [x for x in lengths if abs(x / length - 1) <= 0.02]
        if len(agreeing) * 2 > len(lengths) and len(agreeing) >= 2:
            return statistics.median(agreeing)
    return None


def _tier(kbps: int) -> str:
    if kbps >= 96:
        return "high"
    if kbps >= 56:
        return "medium"
    return "low" if kbps else "none"


def _fmt_len(seconds: float) -> str:
    h, m = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ------------------------------------------------------------------ group analysis

def analyze(groups: list[dict]) -> None:
    """Adds `analysis` to every copy and `recommendation` to every group (in place)."""
    from . import organizer

    ids = [c["id"] for g in groups for c in g["items"]]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in db.q(f"SELECT * FROM library_items WHERE id IN ({marks})", tuple(ids))}
    with_progress = {r["library_item_id"] for r in db.q(
        f"SELECT DISTINCT library_item_id FROM media_progress WHERE library_item_id IN ({marks}) AND (current_pos > 0 OR is_finished = 1)",
        tuple(ids))}
    series_numbered = {r["item_id"] for r in db.q(
        f"SELECT item_id FROM book_series WHERE item_id IN ({marks}) AND sequence IS NOT NULL AND sequence != ''", tuple(ids))}

    asin_of_group = {}
    for g in groups:
        asin = next((c["asin"].strip().upper() for c in g["items"] if c.get("asin")), None)
        if not asin:  # e.g. "Book Title [B0CRBN23Q2].m4b"
            for c in g["items"]:
                for f in db.jload(rows[c["id"]]["library_files"], []):
                    m = ASIN_IN_NAME.search(f["filename"])
                    if m and m.group(1).startswith("B0"):
                        asin = m.group(1)
                        break
                if asin:
                    break
        if asin:
            asin_of_group[g["key"]] = asin
    try:
        runtimes = audible_runtimes(set(asin_of_group.values()), wait=False)
    except Exception:
        log.exception("Audible runtime lookup failed")
        runtimes = {}

    for g in groups:
        copies = g["items"]
        facts = {c["id"]: _audio_facts(rows[c["id"]]) for c in copies}
        chapters = {c["id"]: _chapter_facts(rows[c["id"]]) for c in copies}
        sigs = {facts[c["id"]]["signature"] for c in copies}
        identical = len(sigs) == 1 and next(iter(sigs)) != ()

        official = runtimes.get(asin_of_group.get(g["key"], ""))
        reference = official or _majority_length([facts[c["id"]]["total"] for c in copies
                                                  if facts[c["id"]]["total"] > 0 and not facts[c["id"]]["repeated"]])

        for c in copies:
            item, f, ch = rows[c["id"]], facts[c["id"]], chapters[c["id"]]
            score, notes = 0.0, []

            def note(kind, text, weight=0.0):
                nonlocal score
                score += weight
                notes.append({"kind": kind, "text": text})

            if f["total"] <= 0:
                note("bad", "No playable audio", -1000)
            else:
                tier = _tier(f["kbps"])
                quality = {"high": "High quality audio", "medium": "Medium quality audio", "low": "Low bitrate, likely a re-encode"}[tier]
                note("good" if tier == "high" else "bad" if tier == "low" else "info",
                     f"{quality} ({f['kbps']} kbps{', mixed between files' if f['mixed'] else ''})", min(f["kbps"], 128) / 128 * 100)
                if f["repeated"]:
                    names = ", ".join(a["filename"] for a in f["repeated"][0][:3])
                    note("bad", f"Same audio saved {len(f['repeated'][0])}× in this folder ({names})", -300)
                    note("info", f"Fixable: exclude the duplicate track(s) to keep only {f['fix']['keepFile']} ({f['fix']['kbps']} kbps)")
                if reference:
                    ratio = f["total"] / reference
                    label = "the Audible length" if official else "the other copies"
                    if abs(ratio - 1) <= 0.02:
                        note("good", f"Matches {label} ({_fmt_len(reference)})", 20 if official else 5)
                    elif not f["repeated"]:
                        note("bad", f"Length is {ratio:.1f}× {label} ({_fmt_len(f['total'])} vs {_fmt_len(reference)})", -200)

            if ch["count"]:
                if ch["named"]:
                    note("good", f"{ch['count']} named chapters", 10)
                else:
                    note("info", f"{ch['count']} chapters, numbers only")
                if ch["credits"]:
                    note("good", "Includes opening/end credits", 5)
            else:
                note("bad", "No chapters", -10)

            note(*(("good", "Has ASIN", 10) if item["asin"] else ("bad", "No ASIN", 0)))
            if c["id"] in series_numbered:
                note("good", "Series number set", 10)
            elif c.get("seriesName"):
                note("bad", "Series has no number")
            if c.get("hasCover"):
                score += 5
            else:
                note("bad", "No cover")
            if c.get("hasCue"):
                note("good", "Has .cue sheet", 5)
            if c.get("hasMetadataJson"):
                score += 5
            if not item["description"]:
                note("bad", "No description")
            else:
                score += 5
            try:
                organized = organizer.plan_item(item)["status"] == "unchanged"
            except Exception:
                organized = False
            note(*(("good", "Folder already organized", 8) if organized else ("info", "Folder doesn't match the template", 0)))
            if c["id"] in with_progress:
                note("info", "Has listening progress (moves to the kept copy)", 1)

            if c.get("locked"):
                note("good", "Locked folder (source of truth): can't be discarded", 0)
            order = {"bad": 0, "good": 1, "info": 2}
            notes.sort(key=lambda n: order[n["kind"]])
            c["analysis"] = {
                "score": round(score, 1), "kbps": f["kbps"], "tier": _tier(f["kbps"]),
                "audioLength": f["total"], "repeatedAudio": bool(f["repeated"]), "organized": organized,
                "notes": notes, "fix": f["fix"], "signature": f["signature"],
            }

        # Locked copies first (they can't be discarded), then highest score; ties go to the earliest-added copy
        ranked = sorted(copies, key=lambda c: (not c.get("locked"), -c["analysis"]["score"], c["addedAt"]))
        best = ranked[0]
        peers = [c for c in ranked[1:] if c["analysis"]["signature"] == best["analysis"]["signature"]]
        different = [c for c in ranked[1:] if c["analysis"]["signature"] != best["analysis"]["signature"]]
        g["identicalAudio"] = identical
        g["audibleLength"] = official
        locked_copies = [c for c in copies if c.get("locked")]
        g["lockedCount"] = len(locked_copies)
        top_score = max(c["analysis"]["score"] for c in copies)
        if len(locked_copies) > 1:
            reason = ("More than one copy is in a locked folder, and locked copies can't be discarded. "
                      "Unlock all but one of those folders to resolve this group.")
            confident = False
        elif locked_copies and best["analysis"]["score"] < top_score and best["analysis"]["signature"] != max(
                copies, key=lambda c: c["analysis"]["score"])["analysis"]["signature"]:
            better = max(copies, key=lambda c: c["analysis"]["score"])
            why = _reason(better, best).split(": ", 1)[-1].rstrip(".")
            reason = (f"Keep the copy in {best.get('folderPath')}: it is in a locked folder (source of truth), so it can't be discarded. "
                      f"Note that the copy in {better.get('folderPath')} scores higher: {why}.")
            confident = True
        elif different:
            reason = _reason(best, different[0])
            confident = best["analysis"]["score"] - different[0]["analysis"]["score"] >= 15
        else:
            reason, confident = _reason_identical(best, peers), True
        if peers and different and len(locked_copies) <= 1:
            where = ", ".join(p.get("folderPath") or p.get("libraryName") for p in peers)
            reason += f" The copy in {where} has identical audio, so either works."
        fixable = next((c for c in copies if c["analysis"]["fix"]), None)
        alternative = None
        if fixable and fixable["id"] != best["id"] and fixable["analysis"]["fix"]["kbps"] > best["analysis"]["kbps"] * 1.2:
            fx = fixable["analysis"]["fix"]
            alternative = (f"Better option: in {fixable.get('folderPath')}, exclude {', '.join(fx['excludeFiles'])} so only "
                           f"{fx['keepFile']} ({fx['kbps']} kbps) plays. Then that copy has the best audio.")
        g["recommendation"] = {"id": best["id"], "reason": reason, "confident": confident, "alternative": alternative,
                               "alternativeId": fixable["id"] if alternative else None}
        for c in copies:
            c["analysis"].pop("signature", None)


def _reason_identical(best: dict, peers: list[dict]) -> str:
    where = best.get("folderPath") or best.get("libraryName")
    a = best["analysis"]
    better = [p for p in peers if p["analysis"]["score"] < a["score"]]
    if not better:
        return f"All copies have identical audio and metadata, so either is fine. Suggested: the copy in {where}."
    parts = []
    for t in ("Has ASIN", "Series number set", "Folder already organized", "Has .cue sheet"):
        if any(n["text"] == t for n in a["notes"]) and not all(any(n["text"] == t for n in p["analysis"]["notes"]) for p in peers):
            parts.append(t[0].lower() + t[1:])
    detail = ", ".join(parts) if parts else "it has slightly more complete metadata"
    return f"Keep the copy in {where}: the audio is identical; {detail}."


def _reason(best: dict, runner: dict) -> str:
    a, b = best["analysis"], runner["analysis"]
    where = best.get("folderPath") or best.get("libraryName")
    parts = []
    if b["tier"] == "none" and a["tier"] != "none":
        return f"Keep the copy in {where}: the other copy has no playable audio."
    if b["repeatedAudio"] and not a["repeatedAudio"]:
        parts.append("the other copy contains the same audio more than once")
    if a["kbps"] and b["kbps"] and a["kbps"] >= b["kbps"] * 1.5:
        parts.append(f"{a['kbps']} kbps vs {b['kbps']} kbps ({a['kbps'] / b['kbps']:.1f}× the bitrate)")
    texts = {n["text"] for n in best["analysis"]["notes"] if n["kind"] == "good"}
    other = {n["text"] for n in runner["analysis"]["notes"] if n["kind"] == "good"}
    for t in ("Includes opening/end credits", "Has ASIN", "Series number set", "Folder already organized"):
        if t in texts and t not in other:
            parts.append(t[0].lower() + t[1:])
    if any(t.startswith("Matches the Audible") for t in texts) and not any(t.startswith("Matches the Audible") for t in other):
        parts.append("matches the Audible length")
    if not parts:
        return f"Keep the copy in {where}: it scores slightly better overall; check the details below."
    return f"Keep the copy in {where}: " + ", ".join(parts) + "."
