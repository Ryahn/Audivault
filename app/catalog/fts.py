"""SQLite FTS5 index for library item title/author/series/genre search."""

from __future__ import annotations

import re

from ..core import db
from . import library

_FTS_SPECIAL = re.compile(r'[^\w\s]', re.UNICODE)
_FTS_COLUMNS = """
            item_id UNINDEXED,
            library_id UNINDEXED,
            title,
            subtitle,
            authors,
            narrators,
            series,
            genres,
            tags,
            asin,
            isbn"""


def ensure_fts() -> None:
    existing = db.q1("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'library_items_fts'")
    if existing and "genres" not in (existing["sql"] or ""):
        db.ex("DROP TABLE library_items_fts")
    db.ex(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS library_items_fts USING fts5({_FTS_COLUMNS},
            tokenize='porter unicode61'
        )
    """)
    if db.scalar("SELECT COUNT(*) FROM library_items_fts") == 0 and db.scalar("SELECT COUNT(*) FROM library_items") > 0:
        rebuild_library()


def fts_match_query(raw: str) -> str | None:
    """Build a safe FTS5 MATCH query (AND of prefix tokens)."""
    tokens = [t for t in _FTS_SPECIAL.sub(" ", raw or "").split() if t]
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens[:12])


def sync_item(item_id: str) -> None:
    item = db.q1(
        "SELECT id, library_id, title, subtitle, narrators, genres, tags, asin, isbn FROM library_items WHERE id = ?",
        (item_id,),
    )
    db.ex("DELETE FROM library_items_fts WHERE item_id = ?", (item_id,))
    if not item:
        return
    authors = ", ".join(a["name"] for a in library.item_authors(item_id))
    series = ", ".join(s["name"] for s in library.item_series(item_id))
    narrators = ", ".join(db.jload(item["narrators"], []))
    genres = ", ".join(db.jload(item["genres"], []))
    tags = ", ".join(db.jload(item["tags"], []))
    db.ex(
        "INSERT INTO library_items_fts (item_id, library_id, title, subtitle, authors, narrators, series, genres, tags, asin, isbn) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item["id"], item["library_id"], item["title"] or "", item["subtitle"] or "", authors, narrators, series,
         genres, tags, item["asin"] or "", item["isbn"] or ""),
    )


def remove_item(item_id: str) -> None:
    db.ex("DELETE FROM library_items_fts WHERE item_id = ?", (item_id,))


def rebuild_library(library_id: str | None = None) -> None:
    if library_id:
        db.ex("DELETE FROM library_items_fts WHERE library_id = ?", (library_id,))
        items = db.q("SELECT id FROM library_items WHERE library_id = ?", (library_id,))
    else:
        db.ex("DELETE FROM library_items_fts")
        items = db.q("SELECT id FROM library_items")
    for r in items:
        sync_item(r["id"])


def search_ids(library_id: str, query: str, limit: int = 500) -> list[str] | None:
    """Return matching item ids, or None if FTS is unavailable / empty query."""
    match = fts_match_query(query)
    if not match:
        return []
    try:
        rows = db.q(
            "SELECT item_id FROM library_items_fts WHERE library_id = ? AND library_items_fts MATCH ? LIMIT ?",
            (library_id, match, limit),
        )
        return [r["item_id"] for r in rows]
    except Exception:
        return None
