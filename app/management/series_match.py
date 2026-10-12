"""Match every book in a series, including volumes filed under a longer series name.

A folder such as ``Author/Series (Light Novel)/Title, Vol. 7`` is the same series
as ``Series`` and the volume number is ``7``, even though the scanner stored
neither the link nor the sequence.
"""

import logging
import os
import re
from pathlib import Path, PurePosixPath

from ..catalog import library, media
from ..core import db, events, tasks
from ..scanner.parse import ASIN_IN_NAME, clean_sequence, get_sequence, label_key, metadata_from_match, natural_key, series_key

log = logging.getLogger("abstream.series_match")

_MIN_PREFIX = 4
_VOLUME = re.compile(
    r"(?:^|[\s,_\-(\[])(?:vol(?:ume)?|book|bk|#)\s*\.?\s*(\d{1,3}(?:\.\d{1,2})?)",
    re.I,
)


def series_prefix(name: str) -> str:
    bare = re.sub(r"\s+series$", "", (name or "").strip(), flags=re.I)
    return re.sub(r"[\W_]+", " ", bare.lower()).strip()


def same_series_name(series_name: str, other: str) -> bool:
    """True when ``other`` is this series or a longer form of it.

    ``Animus`` matches ``Animus Series``. ``Survival…!`` matches
    ``Survival…! (Light Novel)``. A title that only shares a word does not.
    """
    other = (other or "").strip()
    if not other:
        return False
    key = series_key(series_name)
    if key and key == series_key(other):
        return True
    prefix = series_prefix(series_name)
    text = series_prefix(other)
    if len(prefix) < _MIN_PREFIX or not text:
        return False
    return text == prefix or text.startswith(prefix + " ")


def normalize_sequence(seq) -> str | None:
    cleaned = clean_sequence(seq)
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return cleaned
    if number.is_integer() and abs(number) < 1000:
        return str(int(number))
    return str(number)


def sequence_from_label(text: str) -> str | None:
    """'Title, Vol. 01' -> '1'. 'Book 3' -> '3'."""
    match = _VOLUME.search(text or "")
    if not match:
        return None
    return normalize_sequence(match.group(1))


def file_sequence(*labels: str) -> str | None:
    for label in labels:
        if not label:
            continue
        found = sequence_from_label(label)
        if found:
            return found
        _, seq = get_sequence(label)
        normalized = normalize_sequence(seq)
        if normalized:
            return normalized
    return None


def chosen_sequence(result: dict, series_name: str, hint: str) -> str | None:
    """Volume number from the provider, or from the file name when it has none."""
    entries = [s for s in (result.get("series") or []) if isinstance(s, dict)]

    def name_of(entry: dict) -> str:
        return entry.get("series") or entry.get("name") or ""

    for entry in entries:
        seq = normalize_sequence(entry.get("sequence"))
        if seq and same_series_name(series_name, name_of(entry)):
            return seq
    if len(entries) == 1:
        seq = normalize_sequence(entries[0].get("sequence"))
        if seq:
            return seq
    return file_sequence(hint)


def pinned_series(current: list[tuple[str, str | None]], series_name: str, sequence: str | None) -> list[tuple[str, str | None]]:
    """Keep unrelated series. Replace every name that is this series with one row."""
    kept = [(name, seq) for name, seq in current if name and not same_series_name(series_name, name)]
    return [(series_name, sequence or None)] + kept


def title_key(title: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", title or "")
    text = re.sub(r"\b0+(\d)", r"\1", text)
    return label_key(text)


def asins_in_text(text: str) -> list[str]:
    return list(dict.fromkeys(ASIN_IN_NAME.findall((text or "").upper())))


def filename_belongs(series_name: str, label: str) -> bool:
    """The label starts with the series and the rest is a volume."""
    prefix = series_prefix(series_name)
    text = re.sub(r"[\W_]+", " ", (label or "").lower()).strip()
    if len(prefix) < _MIN_PREFIX or not text.startswith(prefix + " "):
        return False
    return file_sequence(label) is not None


def candidate_belongs(series_name: str, candidate: dict) -> bool:
    metadata = candidate.get("metadata") or {}
    if same_series_name(series_name, metadata.get("series") or ""):
        return True
    rel = (candidate.get("relPath") or "").replace("\\", "/")
    parts = PurePosixPath(rel).parts
    for part in parts[:-1]:
        if same_series_name(series_name, part):
            return True
    leaf = parts[-1] if parts else rel
    if filename_belongs(series_name, leaf):
        return True
    return filename_belongs(series_name, metadata.get("title") or "")


def _candidate_asins(candidate: dict) -> set[str]:
    metadata = candidate.get("metadata") or {}
    found = set(asins_in_text(candidate.get("relPath") or ""))
    if metadata.get("asin"):
        found.add(str(metadata["asin"]).upper())
    for path in candidate.get("files") or []:
        found.update(asins_in_text(Path(path).name))
    return found


def already_in_library(candidate: dict, library_asins: set[str], library_title_keys: set[str]) -> bool:
    outcome = (candidate.get("autoImport") or {}).get("status")
    if outcome in ("imported", "skipped"):
        return True
    if _candidate_asins(candidate) & library_asins:
        return True
    key = title_key((candidate.get("metadata") or {}).get("title") or "")
    return bool(key and key in library_title_keys)


def library_item_belongs(series_name: str, series_id: str, item: dict) -> bool:
    series = item.get("series") or []
    if any(s.get("id") == series_id for s in series):
        return True
    if any(same_series_name(series_name, s.get("name") or "") for s in series):
        return True
    return same_series_name(series_name, item.get("title") or "")


def _library_entry(series_name: str, series_id: str, item: dict) -> dict:
    series = item.get("series") or []
    on_this = any(s.get("id") == series_id for s in series)
    sequence = next((s.get("sequence") for s in series if s.get("id") == series_id), None) if on_this else None
    source_name = None
    if not on_this:
        source_name = next((s.get("name") for s in series if same_series_name(series_name, s.get("name") or "")), None)
    leaf = PurePosixPath((item.get("rel_path") or "").replace("\\", "/")).name
    file_seq = file_sequence(item.get("title") or "", leaf)
    asins: list[str] = []
    if item.get("asin"):
        asins.append(str(item["asin"]).upper())
    for asin in list(item.get("folder_asins") or []) + asins_in_text(item.get("rel_path") or ""):
        if asin not in asins:
            asins.append(asin)
    return {
        "kind": "library",
        "itemId": item["id"],
        "relPath": item.get("rel_path") or "",
        "title": item.get("title") or "",
        "author": item.get("author") or "",
        "asin": asins[0] if asins else None,
        "asins": asins,
        "sequence": sequence or None,
        "fileSequence": file_seq,
        "source": "series" if on_this else "split",
        "sourceName": source_name,
        "duration": item.get("duration") or 0,
    }


def _import_entry(candidate: dict) -> dict:
    metadata = candidate.get("metadata") or {}
    authors = metadata.get("authors") or []
    author = authors[0] if authors else ""
    rel = candidate.get("relPath") or ""
    leaf = PurePosixPath(rel.replace("\\", "/")).name
    asins = list(_candidate_asins(candidate))
    return {
        "kind": "import",
        "itemId": None,
        "relPath": rel,
        "title": metadata.get("title") or leaf,
        "author": author,
        "asin": asins[0] if asins else None,
        "asins": asins,
        "sequence": None,
        "fileSequence": file_sequence(metadata.get("title") or "", leaf),
        "source": "import",
        "sourceName": None,
        "duration": candidate.get("duration") or 0,
    }


def _sort_key(entry: dict):
    if entry["source"] == "import":
        group = 2
    elif entry.get("sequence"):
        group = 1
    else:
        group = 0
    number = entry.get("fileSequence") or entry.get("sequence") or ""
    return (group, natural_key(str(number)), natural_key(entry.get("title") or ""))


def queue_entries(series_name: str, series_id: str, items: list[dict], candidates: list[dict],
                  library_asins: set[str], library_title_keys: set[str]) -> list[dict]:
    """Library books on this series, split-off books, then new import files."""
    entries = []
    for item in items:
        if library_item_belongs(series_name, series_id, item):
            entries.append(_library_entry(series_name, series_id, item))
    for candidate in candidates:
        if not candidate_belongs(series_name, candidate):
            continue
        if already_in_library(candidate, library_asins, library_title_keys):
            continue
        entries.append(_import_entry(candidate))
    entries.sort(key=_sort_key)
    return entries


def folder_asins(path: str) -> list[str]:
    try:
        names = os.listdir(path)
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        for asin in asins_in_text(name):
            if asin not in found:
                found.append(asin)
    return found


def load_queue(series_id: str) -> dict:
    from . import organizer

    series = db.q1("SELECT * FROM series WHERE id = ?", (series_id,))
    if not series:
        raise ValueError("Series not found")
    library_id = series["library_id"]
    rows = db.q(
        "SELECT id, title, rel_path, path, asin, duration FROM library_items WHERE library_id = ? AND is_missing = 0",
        (library_id,),
    )
    authors_by_id, series_by_id = library.bulk_authors_series([row["id"] for row in rows])
    items = []
    library_asins: set[str] = set()
    library_title_keys: set[str] = set()
    for row in rows:
        if row.get("asin"):
            library_asins.add(str(row["asin"]).upper())
        library_asins.update(asins_in_text(row.get("rel_path") or ""))
        library_title_keys.add(title_key(row.get("title") or ""))
        names = authors_by_id.get(row["id"]) or []
        item = {
            "id": row["id"],
            "title": row["title"],
            "rel_path": row["rel_path"],
            "asin": row.get("asin"),
            "duration": row.get("duration") or 0,
            "author": names[0]["name"] if names else "",
            "series": series_by_id.get(row["id"]) or [],
            "folder_asins": [],
        }
        if not library_item_belongs(series["name"], series["id"], item):
            continue
        item["folder_asins"] = folder_asins(row.get("path") or "")
        items.append(item)
    lib = library.get_library(library_id)
    settings = library.library_settings(lib) if lib else {}
    listing = organizer.list_import_candidates(library_id)
    books = queue_entries(
        series["name"], series["id"], items, listing.get("candidates") or [], library_asins, library_title_keys,
    )
    return {
        "seriesId": series["id"],
        "seriesName": series["name"],
        "libraryId": library_id,
        "provider": settings.get("metadataProvider") or "audible",
        "books": books,
    }


def _hint(title: str, rel_path: str) -> str:
    leaf = PurePosixPath((rel_path or "").replace("\\", "/")).name
    return f"{title or ''} {leaf}".strip()


def _pin(item_id: str, library_id: str, series_name: str, sequence: str | None) -> None:
    current = [(s["name"], s["sequence"]) for s in library.item_series(item_id)]
    pinned = pinned_series(current, series_name, sequence)
    media.update_media(item_id, {"metadata": {"series": [{"name": name, "sequence": seq} for name, seq in pinned]}})


def run(task: dict, series_id: str, picks: list[dict]) -> dict:
    from . import organizer

    series = db.q1("SELECT * FROM series WHERE id = ?", (series_id,))
    if not series:
        raise ValueError("Series not found")
    library_id = series["library_id"]
    series_name = series["name"]
    task["total"] = len(picks)
    matched, imported, failed = [], [], []
    for index, pick in enumerate(picks):
        result = pick.get("result") or {}
        label = (result.get("title") or pick.get("relPath") or pick.get("itemId") or "").strip()
        task["message"] = label
        if not result.get("title"):
            failed.append({"title": label, "error": "No match selected"})
            task["progress"] = index + 1
            continue
        try:
            if pick.get("kind") == "import":
                rel = (pick.get("relPath") or "").replace("\\", "/")
                sequence = chosen_sequence(result, series_name, _hint(result.get("title") or "", rel))
                metadata = metadata_from_match(result, {})
                metadata["series"] = series_name
                metadata["sequence"] = sequence or ""
                saved = organizer.import_candidate(library_id, rel, metadata, cover_url=result.get("cover"))
                imported.append(saved["id"])
                matched.append(saved["id"])
            else:
                item = media.get_item(pick.get("itemId") or "")
                if not item or item["library_id"] != library_id:
                    raise ValueError("Book not found in this library")
                media.apply_match(item["id"], result, override=True, update_cover=True)
                sequence = chosen_sequence(result, series_name, _hint(item["title"], item["rel_path"]))
                _pin(item["id"], library_id, series_name, sequence)
                matched.append(item["id"])
        except Exception as e:
            log.exception("Series match failed for %s", label)
            failed.append({"title": label, "error": str(e)})
        task["progress"] = index + 1

    organized = {"moved": 0, "skipped": 0, "failed": []}
    if matched:
        task["message"] = "Organizing folders"
        organized = organizer.organize_items(library_id, set(matched), task)
    if matched or imported:
        tasks.activity(f"Matched {len(matched)} book(s) in \"{series_name}\"")
        events.publish({"type": "library_changed", "libraryId": library_id, "added": len(imported),
                        "updated": len(matched), "missing": 0})
    return {"matched": len(matched), "imported": len(imported), "failed": failed, "organized": organized}
