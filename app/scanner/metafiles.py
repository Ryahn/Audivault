"""Sidecar metadata files: metadata.json (Audiobookshelf format), .opf, desc.txt, reader.txt."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .parse import clean_sequence, parse_names


def split_series_string(value: str) -> tuple[str, str | None]:
    """'Mistborn #1' -> ('Mistborn', '1')"""
    m = re.match(r"^(.*?)\s+#([\w.]+)$", value.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return value.strip(), None


def read_abs_metadata_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, target in (("title", "title"), ("subtitle", "subtitle"), ("publishedYear", "published_year"),
                        ("publishedDate", "published_date"), ("publisher", "publisher"), ("description", "description"),
                        ("isbn", "isbn"), ("asin", "asin"), ("language", "language")):
        if data.get(key):
            out[target] = data[key]
    for key in ("explicit", "abridged"):
        if key in data:
            out[key] = bool(data[key])
    if data.get("authors"):
        out["authors"] = [a if isinstance(a, str) else a.get("name") for a in data["authors"]]
    if data.get("narrators"):
        out["narrators"] = list(data["narrators"])
    if data.get("genres"):
        out["genres"] = list(data["genres"])
    if data.get("tags"):
        out["tags"] = list(data["tags"])
    if data.get("series"):
        series = []
        for s in data["series"]:
            if isinstance(s, str):
                series.append(split_series_string(s))
            elif isinstance(s, dict) and s.get("name"):
                series.append((s["name"], s.get("sequence")))
        out["series_list"] = series
    if data.get("chapters"):
        out["chapters"] = [{"id": i, "start": float(c["start"]), "end": float(c["end"]), "title": c.get("title") or f"Chapter {i + 1}"}
                           for i, c in enumerate(data["chapters"])]
    return out


def read_opf(path: Path) -> dict:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return {}
    root = tree.getroot()

    def local(tag):
        return tag.rsplit("}", 1)[-1].lower()

    meta = next((el for el in root.iter() if local(el.tag) == "metadata"), root)
    out: dict = {}
    authors, narrators, genres = [], [], []
    series_name = series_index = None
    for el in meta.iter():
        tag = local(el.tag)
        text = (el.text or "").strip()
        attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in el.attrib.items()}
        if tag == "title" and text and "title" not in out:
            out["title"] = text
        elif tag == "creator" and text:
            role = attrs.get("role", "aut").lower()
            (narrators if role == "nrt" else authors).append(text)
        elif tag == "description" and text:
            out["description"] = text
        elif tag == "publisher" and text:
            out["publisher"] = text
        elif tag == "date" and text:
            out["published_year"] = text[:4]
        elif tag == "language" and text:
            out["language"] = text
        elif tag == "subject" and text:
            genres.append(text)
        elif tag == "identifier" and text:
            scheme = (attrs.get("scheme") or "").lower()
            if scheme == "isbn" or re.fullmatch(r"(97[89])?\d{9}[\dX]", text.replace("-", "")):
                out["isbn"] = text
            elif scheme in ("asin", "amazon", "audible") or re.fullmatch(r"B0[A-Z0-9]{8}", text):
                out["asin"] = text
        elif tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = attrs.get("content") or text
            if name in ("calibre:series", "belongs-to-collection"):
                series_name = content
            elif name in ("calibre:series_index", "group-position"):
                series_index = clean_sequence(content)
    if authors:
        out["authors"] = [n for a in authors for n in parse_names(a)]
    if narrators:
        out["narrators"] = [n for a in narrators for n in parse_names(a)]
    if genres:
        out["genres"] = genres
    if series_name:
        out["series_list"] = [(series_name, series_index)]
    return out


def read_text_files(item_dir: Path) -> dict:
    out = {}
    for name, key in (("desc.txt", "description"), ("reader.txt", "narrators")):
        p = item_dir / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                out[key] = parse_names(text) if key == "narrators" else text
    return out


def write_abs_metadata_json(item: dict, authors: list[str], series: list[tuple[str, str | None]], path: Path) -> None:
    from ..core.db import jload
    data = {
        "tags": jload(item.get("tags"), []),
        "chapters": [{"id": i, "start": c["start"], "end": c["end"], "title": c["title"]} for i, c in enumerate(jload(item.get("chapters"), []))],
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "authors": authors,
        "narrators": jload(item.get("narrators"), []),
        "series": [f"{name} #{seq}" if seq else name for name, seq in series],
        "genres": jload(item.get("genres"), []),
        "publishedYear": item.get("published_year"),
        "publishedDate": item.get("published_date"),
        "publisher": item.get("publisher"),
        "description": item.get("description"),
        "isbn": item.get("isbn"),
        "asin": item.get("asin"),
        "language": item.get("language"),
        "explicit": bool(item.get("explicit")),
        "abridged": bool(item.get("abridged")),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
