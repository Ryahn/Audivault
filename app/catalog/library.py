"""Library data helpers: libraries, authors, series and their links to items."""

from ..core import db
from ..scanner.parse import last_first

DEFAULT_LIBRARY_SETTINGS = {
    "coverAspectRatio": 1,
    "disableWatcher": False,
    "skipMatchingMediaWithAsin": False,
    "skipMatchingMediaWithIsbn": False,
    "autoScanCronExpression": None,
    "audiobooksOnly": False,
    "hideSingleBookSeries": False,
    "onlyShowLaterBooksInContinueSeries": False,
    "metadataPrecedence": ["folderStructure", "audioMetatags", "nfoFile", "txtFiles", "opfFile", "absMetadata"],
    "markAsFinishedPercentComplete": None,
    "markAsFinishedTimeRemaining": 10,
    # Audivault-specific
    "importFolder": "",
    "locked": False,              # whole library is a source of truth: files are never deleted
    "namingTemplate": "{author}/[{series}/][{sequence} - ]{title}",
    "renameAudioFiles": False,
    "writeMetadataJson": True,
    "storeCoverWithItem": True,
    "parseSubtitle": False,
    "preferFolderMetadata": False,
    "metadataProvider": "audible",
    "audibleRegion": "us",
    "scanIntervalMinutes": 0,
    "metadataRefreshHours": 0,    # 0 = off; fills empty metadata in small batches on this interval
    "useCueChapters": False,
    "organizeOnImport": True,
    "importMode": "move",         # default import operation: move | copy
    "autoImport": False,          # import new books from the import folder automatically
    "organizeOnScan": False,
    "watchMode": "auto",          # auto | native | polling  (auto = polling if ABSTREAM_WATCH_POLLING is set)
    "watchImportFolder": True,
}


def library_settings(lib: dict) -> dict:
    return DEFAULT_LIBRARY_SETTINGS | (db.jload(lib.get("settings"), {}) or {})


def get_library(library_id: str) -> dict | None:
    return db.q1("SELECT * FROM libraries WHERE id = ?", (library_id,))


def all_libraries() -> list[dict]:
    return db.q("SELECT * FROM libraries ORDER BY display_order, name")


def get_or_create_author(library_id: str, name: str) -> str:
    name = name.strip()
    row = db.q1("SELECT id FROM authors WHERE library_id = ? AND name = ?", (library_id, name))
    if row:
        return row["id"]
    author_id = db.new_id()
    now = db.now_ms()
    db.insert("authors", {"id": author_id, "library_id": library_id, "name": name, "last_first": last_first(name),
                          "asin": None, "description": None, "image_path": None, "added_at": now, "updated_at": now})
    return author_id


def get_or_create_series(library_id: str, name: str) -> str:
    name = name.strip()
    row = db.q1("SELECT id FROM series WHERE library_id = ? AND name = ?", (library_id, name))
    if row:
        return row["id"]
    series_id = db.new_id()
    now = db.now_ms()
    db.insert("series", {"id": series_id, "library_id": library_id, "name": name, "description": None, "added_at": now, "updated_at": now})
    return series_id


def set_item_authors(item_id: str, library_id: str, names: list[str]) -> None:
    db.ex("DELETE FROM book_authors WHERE item_id = ?", (item_id,))
    seen = set()
    for pos, name in enumerate(n for n in names if n and n.strip()):
        author_id = get_or_create_author(library_id, name)
        if author_id in seen:
            continue
        seen.add(author_id)
        db.ex("INSERT OR IGNORE INTO book_authors (item_id, author_id, position) VALUES (?, ?, ?)", (item_id, author_id, pos))


def set_item_series(item_id: str, library_id: str, series: list[tuple[str, str | None]]) -> None:
    db.ex("DELETE FROM book_series WHERE item_id = ?", (item_id,))
    for name, seq in series:
        if not name or not name.strip():
            continue
        series_id = get_or_create_series(library_id, name)
        db.ex("INSERT OR REPLACE INTO book_series (item_id, series_id, sequence) VALUES (?, ?, ?)",
              (item_id, series_id, (str(seq).strip() or None) if seq is not None else None))


def item_authors(item_id: str) -> list[dict]:
    return db.q("SELECT a.id, a.name, a.last_first FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
                "WHERE ba.item_id = ? ORDER BY ba.position", (item_id,))


def item_series(item_id: str) -> list[dict]:
    return db.q("SELECT s.id, s.name, bs.sequence FROM book_series bs JOIN series s ON s.id = bs.series_id "
                "WHERE bs.item_id = ? ORDER BY s.name", (item_id,))


def bulk_authors_series(item_ids: list[str]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Load authors and series for many items in two queries. Returns (authors_by_id, series_by_id)."""
    authors: dict[str, list[dict]] = {i: [] for i in item_ids}
    series: dict[str, list[dict]] = {i: [] for i in item_ids}
    if not item_ids:
        return authors, series
    placeholders = ",".join("?" for _ in item_ids)
    for r in db.q(f"SELECT ba.item_id, a.id, a.name, a.last_first FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
                  f"WHERE ba.item_id IN ({placeholders}) ORDER BY ba.position", tuple(item_ids)):
        authors.setdefault(r["item_id"], []).append({"id": r["id"], "name": r["name"], "last_first": r["last_first"]})
    for r in db.q(f"SELECT bs.item_id, s.id, s.name, bs.sequence FROM book_series bs JOIN series s ON s.id = bs.series_id "
                  f"WHERE bs.item_id IN ({placeholders}) ORDER BY s.name", tuple(item_ids)):
        series.setdefault(r["item_id"], []).append({"id": r["id"], "name": r["name"], "sequence": r["sequence"]})
    return authors, series


def bulk_progress(user_id: str, item_ids: list[str]) -> dict[str, dict]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = db.q(f"SELECT * FROM media_progress WHERE user_id = ? AND episode_id = '' AND library_item_id IN ({placeholders})",
                (user_id, *item_ids))
    return {r["library_item_id"]: r for r in rows}


def cleanup_orphans(library_id: str | None = None) -> None:
    where = "AND library_id = ?" if library_id else ""
    params = (library_id,) if library_id else ()
    db.ex(f"DELETE FROM series WHERE id NOT IN (SELECT series_id FROM book_series) {where}", params)
    db.ex(f"DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors) {where}", params)


def delete_item_records(item_id: str) -> None:
    row = db.q1("SELECT library_id FROM library_items WHERE id = ?", (item_id,))
    for sql in ("DELETE FROM book_authors WHERE item_id = ?", "DELETE FROM book_series WHERE item_id = ?",
                "DELETE FROM collection_books WHERE library_item_id = ?", "DELETE FROM playlist_items WHERE library_item_id = ?",
                "DELETE FROM media_progress WHERE library_item_id = ?", "DELETE FROM bookmarks WHERE library_item_id = ?",
                "DELETE FROM library_items WHERE id = ?"):
        db.ex(sql, (item_id,))
    from . import fts
    fts.remove_item(item_id)
    if row:
        from ..core.library_cache import invalidate_library_cache
        invalidate_library_cache(row["library_id"])
