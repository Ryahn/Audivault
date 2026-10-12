"""Folder / file name parsing. Mirrors Audiobookshelf's scandir rules so that
libraries organised for Audiobookshelf are read the same way:

    Author/Series/[Vol 1 - ][1999 - ]Title[ - Subtitle][ {Narrator}][ [ASIN]]
"""

import re

ASIN_IN_NAME = re.compile(r"(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])")

SORT_PREFIXES = ("the", "a")


def metadata_from_match(result: dict, guess: dict) -> dict:
    """Book fields from a provider hit, falling back to the guess from the file name."""
    series = (result.get("series") or [None])[0]
    return {
        "title": result.get("title") or guess.get("title"),
        "subtitle": result.get("subtitle"),
        "authors": parse_names(result.get("author") or "") or guess.get("authors") or [],
        "narrators": parse_names(result.get("narrator") or "") or guess.get("narrators") or [],
        "series": series["series"] if series else (guess.get("series") or ""),
        "sequence": (series.get("sequence") if series else guess.get("sequence")) or "",
        "publishedYear": result.get("publishedYear") or guess.get("publishedYear"),
        "asin": result.get("asin") or guess.get("asin"),
        "isbn": result.get("isbn") or guess.get("isbn"),
        "description": result.get("description") or guess.get("description"),
        "publisher": result.get("publisher") or guess.get("publisher"),
        "genres": result.get("genres") or guess.get("genres") or [],
        "language": result.get("language") or guess.get("language"),
    }


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def get_asin(folder: str):
    m = re.search(r"(?: |^)\[([A-Z0-9]{10})](?= |$)", folder)
    if not m:
        return folder.strip(), None
    return folder.replace(m.group(0), "").strip(), m.group(1)


def get_narrator(folder: str):
    m = re.match(r"^(?P<title>.*) \{(?P<narrators>.*)\}$", folder)
    return (m.group("title"), m.group("narrators")) if m else (folder, None)


_SEQ = re.compile(r"^(?P<label>vol\.? |volume |book )?(?P<seq>\d{0,3}(?:\.\d{1,2})?)(?P<dot>\.?)(?: (?P<suffix>.*))?$", re.I)


def get_sequence(folder: str):
    parts = folder.split(" - ")
    sequence = None
    for i, part in enumerate(parts):
        m = _SEQ.match(part)
        # Exclude '101 Dalmatians' but include '101. Dalmatians'
        if m and m.group("seq") and not (m.group("suffix") and not (m.group("label") or m.group("dot"))):
            raw = m.group("seq")
            try:
                num = float(raw)
                sequence = str(int(num)) if num.is_integer() else str(num)
            except ValueError:
                sequence = raw
            if m.group("suffix"):
                parts[i] = m.group("suffix")
            else:
                parts.pop(i)
            break
    return " - ".join(parts), sequence


def get_published_year(folder: str):
    m = re.match(r"^ *\(?([0-9]{4})\)? * - *(.+)", folder)
    return (m.group(2), m.group(1)) if m else (folder, None)


def get_subtitle(folder: str):
    head, *rest = folder.split(" - ")
    return head, (" - ".join(rest) or None)


# Album tags from this publisher look like "Advance: Animus, Book 4" while the
# track title is just "Advance". The organizer writes ":" as " - ".
# "Advance: Animus, Book 4" is a short title plus a series. Light-novel titles
# such as "Classroom of the Elite: Year 2, Vol. 01" are not: the text after the
# colon is a subtitle, and "Vol." is the book's own number.
_EMBEDDED_SERIES = re.compile(
    r"^(?P<title>.+?)\s*(?::\s*|\s-\s)\s*(?P<series>.+?),\s*book\s+(?P<seq>\d+)\s*$",
    re.I,
)
_MIDDLE_INITIAL = re.compile(r"^[A-Za-z]\.?$")


def split_embedded_series(title: str) -> tuple[str, str | None, str | None]:
    """'Advance: Animus, Book 4' -> ('Advance', 'Animus', '4')."""
    text = (title or "").strip()
    match = _EMBEDDED_SERIES.match(text)
    if not match:
        return text, None, None
    short = match.group("title").strip()
    series = match.group("series").strip()
    if not short or not series:
        return text, None, None
    return short, series, match.group("seq")


def label_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").lower())


def series_key(name: str) -> str:
    """'Animus Series' and 'Animus' compare equal."""
    bare = re.sub(r"\s+series$", "", (name or "").strip(), flags=re.I)
    return label_key(bare)


def prefer_series_name(name: str, known: list[str] | None) -> str:
    """Use a series name the library already has when it is the same series."""
    key = series_key(name)
    if not key or not known:
        return name
    matches = [n for n in known if n and series_key(n) == key]
    if not matches:
        return name
    return max(matches, key=len)


def canonical_author_name(name: str) -> str:
    """'Michael T Anderle' and 'Michael Anderle' share one author folder.

    A single middle initial is dropped. Longer middle names stay, so
    'Mary Ann Evans' is left alone.
    """
    parts = (name or "").split()
    if len(parts) == 3 and _MIDDLE_INITIAL.match(parts[1]):
        return f"{parts[0]} {parts[2]}"
    return name or ""


def fold_embedded_series(merged: dict, album: str | None = None) -> None:
    """Pull 'Title: Series, Book N' out of a title and into series metadata.

    A series folder that is just the book title (the scanner's reading of
    Author/Advance/...) is replaced. A real series such as 'Animus Series'
    keeps its name; a book number in the title replaces a disagreeing sequence.
    """
    current = merged.get("title") or ""
    from_title = split_embedded_series(current)
    from_album = split_embedded_series(album or "")
    if from_title[1]:
        short, emb_series, emb_seq = from_title
    elif from_album[1] and label_key(current) in {label_key(from_album[0]), label_key(album or "")}:
        short, emb_series, emb_seq = from_album
    else:
        return
    current = list(merged.get("series_list") or [])
    if not current or not current[0][0]:
        merged["title"] = short
        merged["series_list"] = [(emb_series, emb_seq)]
        return
    name, seq = current[0]
    if series_key(name) == series_key(short):
        merged["title"] = short
        merged["series_list"] = [(emb_series, emb_seq)]
    elif series_key(name) == series_key(emb_series):
        merged["title"] = short
        if emb_seq and str(seq or "") != str(emb_seq):
            current[0] = (name, emb_seq)
            merged["series_list"] = current


def parse_names(value: str | None) -> list[str]:
    """Split an author/narrator string into names. Handles 'A & B', 'A; B',
    'A, B' and 'Last, First' forms."""
    if not value:
        return []
    names: list[str] = []
    for segment in re.split(r"\s*[&;]\s*", value):
        segment = segment.strip()
        if not segment:
            continue
        if "," not in segment:
            names.append(segment)
            continue
        pieces = [p.strip() for p in segment.split(",") if p.strip()]
        # "Last, First[, Last, First]" when even count and every last name is one word
        if len(pieces) % 2 == 0 and all(len(pieces[i].split()) == 1 for i in range(0, len(pieces), 2)) and \
                any(len(p.split()) == 1 for p in pieces[1::2]):
            for i in range(0, len(pieces), 2):
                names.append(f"{pieces[i + 1]} {pieces[i]}")
        else:
            names.extend(pieces)
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def book_data_from_dir(rel_path: str, parse_subtitle: bool = False) -> dict:
    """rel_path is the item's path relative to the library folder, using '/'."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    title_folder = parts.pop() if parts else ""
    series = parts.pop() if len(parts) > 1 else None
    author = parts.pop() if parts else None

    rest, asin = get_asin(title_folder)
    rest, narrators = get_narrator(rest)
    if series:
        rest, sequence = get_sequence(rest)
    else:
        sequence = None
    rest, year = get_published_year(rest)
    title, subtitle = get_subtitle(rest) if parse_subtitle else (rest, None)
    return {
        "title": title.strip() or title_folder,
        "subtitle": subtitle,
        "asin": asin,
        "authors": parse_names(author),
        "narrators": parse_names(narrators),
        "series": series,
        "sequence": sequence,
        "published_year": year,
    }


def title_ignore_prefix(title: str) -> str:
    """'The Hobbit' -> 'Hobbit'"""
    if not title:
        return ""
    low = title.lower()
    for p in SORT_PREFIXES:
        if low.startswith(p + " "):
            return title[len(p) + 1:].strip()
    return title


def title_prefix_at_end(title: str) -> str:
    """'The Hobbit' -> 'Hobbit, The'"""
    if not title:
        return ""
    low = title.lower()
    for p in SORT_PREFIXES:
        if low.startswith(p + " "):
            return f"{title[len(p) + 1:].strip()}, {title[:len(p)]}"
    return title


def last_first(name: str) -> str:
    if not name:
        return ""
    parts = name.split()
    if len(parts) < 2:
        return name
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "md"}
    if parts[-1].lower().rstrip(",") in suffixes and len(parts) > 2:
        return f"{parts[-2]}, {' '.join(parts[:-2])} {parts[-1]}"
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def clean_sequence(seq) -> str | None:
    """Audible sends sequences like 'Book 1' or '2, Dramatized Adaptation'."""
    if seq is None:
        return None
    seq = str(seq).strip()
    if not seq:
        return None
    m = re.search(r"\.\d+|\d+(?:\.\d+)?", seq)
    return m.group(0) if m else seq
