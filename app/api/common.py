"""Shared helpers for API routes: lookups with access checks, bulk loading,
and Audiobookshelf-style filter / sort / pagination for library items."""

import base64
import random
from urllib.parse import unquote

from fastapi import HTTPException

from ..catalog import library, serialize
from ..core import auth, db
from ..core.library_cache import get_snapshot, invalidate_library_cache, set_snapshot
from ..scanner.parse import last_first, title_ignore_prefix

# Columns needed for list/filter/sort without the heavy JSON blobs.
LIST_ITEM_COLUMNS = (
    "id, library_id, folder_id, path, rel_path, ino, is_file, mtime_ms, ctime_ms, birthtime_ms, "
    "size, added_at, updated_at, last_scan, is_missing, title, title_sort, subtitle, publisher, "
    "published_year, published_date, isbn, asin, language, explicit, abridged, narrators, "
    "genres, tags, cover_path, duration, num_tracks, num_audio_files, num_chapters, num_files, ebook_format"
)


def get_library_or_404(library_id: str, user: dict) -> dict:
    lib = library.get_library(library_id)
    if not lib or not auth.can_access_library(user, library_id):
        raise HTTPException(404, "Library not found")
    return lib


def get_item_or_404(item_id: str, user: dict) -> dict:
    item = db.q1("SELECT * FROM library_items WHERE id = ?", (item_id,))
    if not item or not auth.can_access_library(user, item["library_id"]):
        raise HTTPException(404, "Item not found")
    if not item["explicit"] or auth.permissions(user).get("accessExplicitContent"):
        return item
    raise HTTPException(404, "Item not found")


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes")


def decode_filter(raw: str | None) -> tuple[str | None, str | None]:
    if not raw or "." not in raw:
        return None, None
    group, value = raw.split(".", 1)
    value = unquote(value)
    try:
        value = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    return group, unquote(value)


def encode_b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


# ------------------------------------------------------------------ bulk loading

def _load_snapshot(library_id: str, include_missing: bool) -> dict:
    cached = get_snapshot(library_id, include_missing)
    if cached is not None:
        return cached
    where = "" if include_missing else " AND is_missing = 0"
    items = db.q(f"SELECT {LIST_ITEM_COLUMNS} FROM library_items WHERE library_id = ?{where}", (library_id,))
    authors: dict[str, list[dict]] = {}
    for r in db.q("SELECT ba.item_id, a.id, a.name, a.last_first FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
                  "WHERE a.library_id = ? ORDER BY ba.position", (library_id,)):
        authors.setdefault(r["item_id"], []).append(r)
    series: dict[str, list[dict]] = {}
    for r in db.q("SELECT bs.item_id, s.id, s.name, bs.sequence FROM book_series bs JOIN series s ON s.id = bs.series_id "
                  "WHERE s.library_id = ? ORDER BY s.name", (library_id,)):
        series.setdefault(r["item_id"], []).append(r)
    snap = {"items": items, "authors": authors, "series": series}
    set_snapshot(library_id, include_missing, snap)
    return snap


class Loaded:
    """Items of a library with their authors/series preloaded (slim columns)."""

    def __init__(self, library_id: str, user: dict | None = None, include_missing: bool = True):
        self.library_id = library_id
        snap = _load_snapshot(library_id, include_missing)
        self.items = snap["items"]
        if user and not auth.permissions(user).get("accessExplicitContent"):
            self.items = [i for i in self.items if not i["explicit"]]
        self.by_id = {i["id"]: i for i in self.items}
        self.authors = snap["authors"]
        self.series = snap["series"]
        self.progress: dict[str, dict] = {}
        if user and self.by_id:
            placeholders = ",".join("?" for _ in self.by_id)
            for p in db.q(
                f"SELECT * FROM media_progress WHERE user_id = ? AND episode_id = '' AND library_item_id IN ({placeholders})",
                (user["id"], *self.by_id.keys()),
            ):
                self.progress[p["library_item_id"]] = p

    def json(self, item: dict, expanded: bool = False, user: dict | None = None, include_progress: bool = False) -> dict:
        return serialize.item_json(
            item, expanded=expanded, user=user, include_progress=include_progress,
            authors=self.authors.get(item["id"], []), series=self.series.get(item["id"], []),
            progress=self.progress.get(item["id"]) if include_progress else None,
            progress_known=include_progress,
        )

    def author_name(self, item: dict) -> str:
        return ", ".join(a["name"] for a in self.authors.get(item["id"], []))


# Re-export for callers that invalidate after mutations outside this module.
__all__ = [
    "Loaded", "LIST_ITEM_COLUMNS", "apply_filter", "as_int", "can_sql_page", "collapse_series",
    "decode_filter", "encode_b64", "get_item_or_404", "get_library_or_404", "invalidate_library_cache",
    "loaded_for_items", "paginate", "sort_items", "sql_page_items", "text_search", "truthy",
]


# ------------------------------------------------------------------ filtering

def _decade(year: str | None) -> str | None:
    try:
        return str(int(str(year)[:4]) // 10 * 10)
    except (TypeError, ValueError):
        return None


def _num_tracks(item: dict) -> int:
    if item.get("num_tracks") is not None:
        return int(item["num_tracks"] or 0)
    return len([x for x in db.jload(item.get("audio_files"), []) if not x.get("exclude")])


def _has_ebook(item: dict) -> bool:
    if item.get("ebook_format"):
        return True
    return bool(item.get("ebook_file"))


def apply_filter(loaded: Loaded, items: list[dict], group: str | None, value: str | None) -> list[dict]:
    if not group:
        return items
    a, s, p = loaded.authors, loaded.series, loaded.progress
    # Hot filters: resolve IDs in SQL then intersect with the in-memory page set
    if group == "authors" and value:
        ids = {r["item_id"] for r in db.q("SELECT item_id FROM book_authors WHERE author_id = ?", (value,))}
        return [i for i in items if i["id"] in ids]
    if group == "series":
        if value == "no-series":
            return [i for i in items if not s.get(i["id"])]
        ids = {r["item_id"] for r in db.q("SELECT item_id FROM book_series WHERE series_id = ?", (value,))}
        return [i for i in items if i["id"] in ids]
    if group in ("genres", "tags", "narrators"):
        return [i for i in items if value in db.jload(i[group], [])]
    if group == "publishers":
        return [i for i in items if i["publisher"] == value]
    if group == "languages":
        return [i for i in items if i["language"] == value]
    if group == "publishedDecades":
        return [i for i in items if _decade(i["published_year"]) == value]
    if group == "progress":
        def state(i):
            pr = p.get(i["id"])
            if not pr:
                return "not-started"
            if pr["is_finished"]:
                return "finished"
            return "in-progress" if (pr["current_pos"] > 0 or pr["progress"] > 0) else "not-started"
        if value == "not-finished":
            return [i for i in items if state(i) != "finished"]
        return [i for i in items if state(i) == value]
    if group == "missing":
        if value == "description":
            # Description is omitted from list snapshots; resolve via SQL.
            ids = {r["id"] for r in db.q(
                "SELECT id FROM library_items WHERE library_id = ? AND (description IS NULL OR trim(description) = '')",
                (loaded.library_id,))}
            return [i for i in items if i["id"] in ids]
        checks = {
            "asin": lambda i: not i["asin"], "isbn": lambda i: not i["isbn"], "subtitle": lambda i: not i["subtitle"],
            "authors": lambda i: not a.get(i["id"]), "publishedYear": lambda i: not i["published_year"],
            "series": lambda i: not s.get(i["id"]),
            "genres": lambda i: not db.jload(i["genres"], []), "tags": lambda i: not db.jload(i["tags"], []),
            "narrators": lambda i: not db.jload(i["narrators"], []), "publisher": lambda i: not i["publisher"],
            "language": lambda i: not i["language"], "cover": lambda i: not i["cover_path"],
        }
        fn = checks.get(value)
        return [i for i in items if fn(i)] if fn else items
    if group == "tracks":
        if value == "single":
            return [i for i in items if _num_tracks(i) == 1]
        if value == "multi":
            return [i for i in items if _num_tracks(i) > 1]
        return items
    if group == "ebooks":
        if value == "ebook":
            return [i for i in items if _has_ebook(i)]
        if value == "no-ebook":
            return [i for i in items if not _has_ebook(i)]
        return items
    if group == "issues":
        return [i for i in items if i["is_missing"] or (_num_tracks(i) == 0 and not _has_ebook(i))]
    if group == "abridged":
        return [i for i in items if i["abridged"]]
    if group == "explicit":
        return [i for i in items if i["explicit"]]
    return items


def text_search(loaded: Loaded, items: list[dict], query: str) -> list[dict]:
    q = query.lower().strip()
    if not q:
        return items
    from ..catalog import fts
    fts_ids = fts.search_ids(loaded.library_id, query)
    if fts_ids is not None:
        allowed = set(fts_ids)
        # Preserve caller order among matches
        return [i for i in items if i["id"] in allowed]
    out = []
    for i in items:
        hay = " ".join([i["title"] or "", i["subtitle"] or "", loaded.author_name(i), " ".join(db.jload(i["narrators"], [])),
                        " ".join(db.jload(i["genres"], [])), " ".join(db.jload(i["tags"], [])),
                        " ".join(x["name"] for x in loaded.series.get(i["id"], [])), i["asin"] or "", i["isbn"] or ""]).lower()
        if q in hay:
            out.append(i)
    return out


# ------------------------------------------------------------------ sorting

def _seq_key(seq):
    try:
        return (0, float(seq))
    except (TypeError, ValueError):
        return (1, 0.0)


def _listen_percent(progress: dict | None) -> float:
    if not progress:
        return 0.0
    if progress.get("is_finished"):
        return 1.0
    try:
        return float(progress.get("progress") or 0)
    except (TypeError, ValueError):
        return 0.0


def sort_items(loaded: Loaded, items: list[dict], sort_by: str | None, desc: bool, ignore_prefix: bool = True,
               series_id: str | None = None) -> list[dict]:
    p = loaded.progress

    def title_key(i):
        t = i["title"] or ""
        return (title_ignore_prefix(t) if ignore_prefix else t).lower()

    keys = {
        "media.metadata.title": title_key,
        "media.metadata.authorName": lambda i: (loaded.author_name(i).lower(), title_key(i)),
        "media.metadata.authorNameLF": lambda i: (", ".join(last_first(a["name"]) for a in loaded.authors.get(i["id"], [])).lower(), title_key(i)),
        "media.metadata.author": lambda i: (loaded.author_name(i).lower(), title_key(i)),
        "media.metadata.publishedYear": lambda i: (i["published_year"] or "", title_key(i)),
        "addedAt": lambda i: i["added_at"],
        "updatedAt": lambda i: i["updated_at"],
        "size": lambda i: i["size"] or 0,
        "media.duration": lambda i: i["duration"] or 0,
        "media.numTracks": lambda i: _num_tracks(i),
        "birthtimeMs": lambda i: i["birthtime_ms"] or 0,
        "mtimeMs": lambda i: i["mtime_ms"] or 0,
        "progress": lambda i: (p[i["id"]]["last_update"] if i["id"] in p else 0),
        "progress.percent": lambda i: (_listen_percent(p.get(i["id"])), title_key(i)),
        "progress.isFinished": lambda i: (1 if (p.get(i["id"]) or {}).get("is_finished") else 0, _listen_percent(p.get(i["id"])), title_key(i)),
        "progress.finishedAt": lambda i: (p[i["id"]]["finished_at"] or 0) if i["id"] in p else 0,
        "progress.createdAt": lambda i: (p[i["id"]]["started_at"] or 0) if i["id"] in p else 0,
    }
    if sort_by == "random":
        items = list(items)
        random.shuffle(items)
        return items
    if sort_by == "sequence" or (series_id and not sort_by):
        def seq_of(i):
            return next((x["sequence"] for x in loaded.series.get(i["id"], []) if x["id"] == series_id), None)
        return sorted(items, key=lambda i: (_seq_key(seq_of(i)), title_key(i)), reverse=desc)
    key = keys.get(sort_by or "media.metadata.title", title_key)
    return sorted(items, key=key, reverse=desc)


def collapse_series(loaded: Loaded, items: list[dict], hide_single: bool = False) -> list[tuple[dict, dict | None]]:
    """Replace books that belong to a series by one entry per series (the
    lowest sequence book). Returns [(item, collapsed_series_or_None)]."""
    series_books: dict[str, list[dict]] = {}
    for i in items:
        for s in loaded.series.get(i["id"], [])[:1]:
            series_books.setdefault(s["id"], []).append(i)
    out, emitted = [], set()
    for i in items:
        sers = loaded.series.get(i["id"], [])[:1]
        if not sers:
            out.append((i, None))
            continue
        s = sers[0]
        books = series_books[s["id"]]
        if hide_single and len(books) == 1:
            out.append((i, None))
            continue
        if s["id"] in emitted:
            continue
        emitted.add(s["id"])
        ordered = sorted(books, key=lambda b: _seq_key(next((x["sequence"] for x in loaded.series.get(b["id"], []) if x["id"] == s["id"]), None)))
        out.append((ordered[0], {
            "id": s["id"], "name": s["name"], "nameIgnorePrefix": title_ignore_prefix(s["name"]),
            "numBooks": len(books), "libraryItemIds": [b["id"] for b in ordered],
            "seriesSequenceList": ", ".join(x["sequence"] for b in ordered for x in loaded.series.get(b["id"], []) if x["id"] == s["id"] and x["sequence"]),
        }))
    return out


def paginate(rows: list, limit: int, page: int) -> list:
    if limit <= 0:
        return rows
    start = page * limit
    return rows[start:start + limit]


# ------------------------------------------------------------------ SQL list pushdown

# Sorts that map to denormalized library_items columns (no join / progress).
_SQL_SORT_COLS = {
    "media.metadata.title": "title_sort",
    "media.metadata.publishedYear": "published_year",
    "addedAt": "added_at",
    "updatedAt": "updated_at",
    "size": "size",
    "media.duration": "duration",
    "media.numTracks": "num_tracks",
    "birthtimeMs": "birthtime_ms",
    "mtimeMs": "mtime_ms",
}

_SQL_FILTER_GROUPS = frozenset({"authors", "series", None})


def can_sql_page(
    collapse: bool,
    sort_by: str | None,
    group: str | None,
    ignore_prefix: bool,
    value: str | None = None,
) -> bool:
    """True when library_items can ORDER BY + LIMIT/OFFSET in SQL."""
    if collapse:
        return False
    if group not in _SQL_FILTER_GROUPS:
        return False
    if sort_by in (None, "media.metadata.title") and not ignore_prefix:
        # title_sort is always the ignore-prefix form; fall back when that setting is off.
        return False
    if sort_by == "sequence" or (group == "series" and not sort_by):
        return group == "series" and bool(value) and value != "no-series"
    return (sort_by or "media.metadata.title") in _SQL_SORT_COLS


def _fts_id_clause(library_id: str, q: str | None, *, col: str = "id") -> tuple[str, list] | None:
    """Return (AND clause, params) for FTS, empty match as AND 0, or None if FTS unavailable."""
    if not q or not q.strip():
        return "", []
    from ..catalog import fts
    fts_ids = fts.search_ids(library_id, q)
    if fts_ids is None:
        return None
    if not fts_ids:
        return " AND 0", []
    placeholders = ",".join("?" for _ in fts_ids)
    return f" AND {col} IN ({placeholders})", list(fts_ids)


def _order_expr(col: str) -> str:
    if col in ("size", "duration", "num_tracks", "birthtime_ms", "mtime_ms", "added_at", "updated_at"):
        return f"COALESCE({col}, 0)"
    if col == "published_year":
        return f"COALESCE({col}, '')"
    return col


def sql_page_items(
    library_id: str,
    user: dict,
    *,
    sort_by: str | None,
    desc: bool,
    limit: int,
    page: int,
    group: str | None = None,
    value: str | None = None,
    q: str | None = None,
) -> tuple[list[dict], int] | None:
    """Return (page_rows, total) via SQL, or None if this request cannot be pushed down."""
    allow_explicit = auth.permissions(user).get("accessExplicitContent")
    direction = "DESC" if desc else "ASC"
    use_sequence = (
        group == "series" and value and value != "no-series"
        and (sort_by == "sequence" or not sort_by)
    )

    if use_sequence:
        fts_part = _fts_id_clause(library_id, q, col="li.id")
        if fts_part is None:
            return None
        fts_sql, fts_params = fts_part
        explicit = "" if allow_explicit else " AND li.explicit = 0"
        cols = ", ".join(f"li.{c.strip()}" for c in LIST_ITEM_COLUMNS.split(","))
        where = f"li.library_id = ?{explicit} AND bs.series_id = ?{fts_sql}"
        params: list = [library_id, value, *fts_params]
        total = int(db.q1(
            f"SELECT COUNT(*) AS n FROM library_items li "
            f"JOIN book_series bs ON bs.item_id = li.id WHERE {where}",
            tuple(params),
        )["n"])
        sql = (
            f"SELECT {cols} FROM library_items li "
            f"JOIN book_series bs ON bs.item_id = li.id WHERE {where} "
            f"ORDER BY CAST(bs.sequence AS REAL) {direction}, li.title_sort {direction}, li.id {direction}"
        )
        if limit > 0:
            sql += " LIMIT ? OFFSET ?"
            params = [*params, limit, page * limit]
        return db.q(sql, tuple(params)), total

    clauses = ["library_id = ?"]
    params = [library_id]
    if not allow_explicit:
        clauses.append("explicit = 0")
    if group == "authors" and value:
        clauses.append("id IN (SELECT item_id FROM book_authors WHERE author_id = ?)")
        params.append(value)
    elif group == "series":
        if value == "no-series":
            clauses.append(
                "id NOT IN (SELECT bs.item_id FROM book_series bs "
                "JOIN series s ON s.id = bs.series_id WHERE s.library_id = ?)"
            )
            params.append(library_id)
        elif value:
            clauses.append("id IN (SELECT item_id FROM book_series WHERE series_id = ?)")
            params.append(value)
        else:
            return None
    elif group is not None:
        return None

    fts_part = _fts_id_clause(library_id, q, col="id")
    if fts_part is None:
        return None
    fts_sql, fts_params = fts_part
    where = " AND ".join(clauses) + fts_sql
    params.extend(fts_params)

    total = int(db.q1(f"SELECT COUNT(*) AS n FROM library_items WHERE {where}", tuple(params))["n"])
    col = _SQL_SORT_COLS.get(sort_by or "media.metadata.title", "title_sort")
    order_expr = _order_expr(col)
    sql = (
        f"SELECT {LIST_ITEM_COLUMNS} FROM library_items WHERE {where} "
        f"ORDER BY {order_expr} {direction}, title_sort {direction}, id {direction}"
    )
    if limit > 0:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, page * limit]
    return db.q(sql, tuple(params)), total


def loaded_for_items(library_id: str, items: list[dict], user: dict | None = None) -> Loaded:
    """Build a Loaded view for a small page of items without loading the whole library."""
    loaded = Loaded.__new__(Loaded)
    loaded.library_id = library_id
    loaded.items = items
    loaded.by_id = {i["id"]: i for i in items}
    ids = list(loaded.by_id)
    authors, series = library.bulk_authors_series(ids)
    loaded.authors = authors
    loaded.series = series
    loaded.progress = {}
    if user and ids:
        placeholders = ",".join("?" for _ in ids)
        for p in db.q(
            f"SELECT * FROM media_progress WHERE user_id = ? AND episode_id = '' AND library_item_id IN ({placeholders})",
            (user["id"], *ids),
        ):
            loaded.progress[p["library_item_id"]] = p
    return loaded
