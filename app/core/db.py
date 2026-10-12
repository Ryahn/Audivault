"""SQLite storage. One connection per thread, dict rows."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from .config import DB_PATH

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT,
  type TEXT NOT NULL DEFAULT 'user',
  token TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  permissions TEXT NOT NULL DEFAULT '{}',
  libraries_accessible TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  user_agent TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  last_used INTEGER
);

CREATE TABLE IF NOT EXISTS libraries (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  media_type TEXT NOT NULL DEFAULT 'book',
  folders TEXT NOT NULL DEFAULT '[]',
  settings TEXT NOT NULL DEFAULT '{}',
  display_order INTEGER NOT NULL DEFAULT 1,
  icon TEXT NOT NULL DEFAULT 'audiobookshelf',
  last_scan INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS library_items (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  folder_id TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  rel_path TEXT NOT NULL,
  ino TEXT,
  is_file INTEGER NOT NULL DEFAULT 0,
  mtime_ms INTEGER, ctime_ms INTEGER, birthtime_ms INTEGER,
  size INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_scan INTEGER,
  is_missing INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  title_sort TEXT,
  subtitle TEXT,
  publisher TEXT,
  published_year TEXT,
  published_date TEXT,
  description TEXT,
  isbn TEXT,
  asin TEXT,
  language TEXT,
  explicit INTEGER NOT NULL DEFAULT 0,
  abridged INTEGER NOT NULL DEFAULT 0,
  narrators TEXT NOT NULL DEFAULT '[]',
  genres TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  cover_path TEXT,
  duration REAL NOT NULL DEFAULT 0,
  audio_files TEXT NOT NULL DEFAULT '[]',
  chapters TEXT NOT NULL DEFAULT '[]',
  ebook_file TEXT,
  library_files TEXT NOT NULL DEFAULT '[]',
  num_tracks INTEGER NOT NULL DEFAULT 0,
  num_audio_files INTEGER NOT NULL DEFAULT 0,
  num_chapters INTEGER NOT NULL DEFAULT 0,
  num_files INTEGER NOT NULL DEFAULT 0,
  ebook_format TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_library ON library_items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_library_missing ON library_items(library_id, is_missing);

CREATE TABLE IF NOT EXISTS authors (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  name TEXT NOT NULL COLLATE NOCASE,
  last_first TEXT,
  asin TEXT,
  description TEXT,
  image_path TEXT,
  added_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(library_id, name)
);

CREATE TABLE IF NOT EXISTS series (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  name TEXT NOT NULL COLLATE NOCASE,
  description TEXT,
  added_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(library_id, name)
);

CREATE TABLE IF NOT EXISTS book_authors (
  item_id TEXT NOT NULL,
  author_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (item_id, author_id)
);
CREATE INDEX IF NOT EXISTS idx_book_authors_author ON book_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_book_authors_item ON book_authors(item_id);

CREATE TABLE IF NOT EXISTS book_series (
  item_id TEXT NOT NULL,
  series_id TEXT NOT NULL,
  sequence TEXT,
  PRIMARY KEY (item_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_book_series_series ON book_series(series_id);
CREATE INDEX IF NOT EXISTS idx_book_series_item ON book_series(item_id);

CREATE TABLE IF NOT EXISTS media_progress (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  library_item_id TEXT NOT NULL,
  episode_id TEXT NOT NULL DEFAULT '',
  duration REAL NOT NULL DEFAULT 0,
  progress REAL NOT NULL DEFAULT 0,
  current_pos REAL NOT NULL DEFAULT 0,
  is_finished INTEGER NOT NULL DEFAULT 0,
  hide_from_continue INTEGER NOT NULL DEFAULT 0,
  ebook_location TEXT,
  ebook_progress REAL,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  last_update INTEGER NOT NULL,
  UNIQUE(user_id, library_item_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_user_item ON media_progress(user_id, library_item_id);

CREATE TABLE IF NOT EXISTS bookmarks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  library_item_id TEXT NOT NULL,
  time REAL NOT NULL,
  title TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_bookmarks_item ON bookmarks(library_item_id);

CREATE TABLE IF NOT EXISTS playback_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  library_id TEXT,
  library_item_id TEXT NOT NULL,
  episode_id TEXT,
  media_type TEXT NOT NULL DEFAULT 'book',
  display_title TEXT,
  display_author TEXT,
  cover_path TEXT,
  duration REAL NOT NULL DEFAULT 0,
  play_method INTEGER NOT NULL DEFAULT 0,
  media_player TEXT,
  device_info TEXT,
  session_date TEXT,
  day_of_week TEXT,
  time_listening REAL NOT NULL DEFAULT 0,
  start_time REAL NOT NULL DEFAULT 0,
  current_pos REAL NOT NULL DEFAULT 0,
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  media_metadata TEXT,
  chapters TEXT,
  is_open INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON playback_sessions(user_id, updated_at);

CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collections_library ON collections(library_id);

CREATE TABLE IF NOT EXISTS collection_books (
  collection_id TEXT NOT NULL,
  library_item_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (collection_id, library_item_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_books_item ON collection_books(library_item_id);

CREATE TABLE IF NOT EXISTS playlists (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);
CREATE INDEX IF NOT EXISTS idx_playlists_library_user ON playlists(library_id, user_id);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id TEXT NOT NULL,
  library_item_id TEXT NOT NULL,
  episode_id TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (playlist_id, library_item_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items_item ON playlist_items(library_item_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return str(uuid.uuid4())


_JOURNAL_MODES = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}


def _journal_mode() -> str:
    """WAL is faster, but its -wal/-shm files break on Docker Desktop bind mounts.

    When those sidecars disappear while a connection still has them open, every
    new connection fails with SQLITE_CANTOPEN. AUDIVAULT_SQLITE_JOURNAL=DELETE
    uses a rollback journal instead.
    """
    mode = os.environ.get("AUDIVAULT_SQLITE_JOURNAL", "WAL").strip().upper()
    return mode if mode in _JOURNAL_MODES else "WAL"


def _connect() -> sqlite3.Connection:
    mode = _journal_mode()
    last_error = None
    for attempt in range(6):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(f"PRAGMA journal_mode={mode}")
            conn.execute("PRAGMA foreign_keys=OFF")
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            last_error = exc
            name = getattr(exc, "sqlite_errorname", "")
            if name != "SQLITE_CANTOPEN" and "unable to open database file" not in str(exc).lower():
                raise
            time.sleep(0.05 * (attempt + 1))
    raise last_error


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _connect()
        _local.conn = c
    return c


_ITEM_COUNT_COLS = (
    ("num_tracks", "INTEGER NOT NULL DEFAULT 0"),
    ("num_audio_files", "INTEGER NOT NULL DEFAULT 0"),
    ("num_chapters", "INTEGER NOT NULL DEFAULT 0"),
    ("num_files", "INTEGER NOT NULL DEFAULT 0"),
    ("ebook_format", "TEXT"),
)


def compute_item_counts(audio=None, chapters=None, library_files=None, ebook_file=None) -> dict:
    """Denormalized list fields derived from the JSON blobs stored on an item."""
    audio = audio if audio is not None else []
    chapters = chapters if chapters is not None else []
    library_files = library_files if library_files is not None else []
    if isinstance(ebook_file, str):
        ebook_file = jload(ebook_file, None)
    included = [a for a in audio if not a.get("exclude")]
    return {
        "num_tracks": len(included),
        "num_audio_files": len(audio),
        "num_chapters": len(chapters),
        "num_files": len(library_files),
        "ebook_format": (ebook_file or {}).get("ebook_format") if ebook_file else None,
    }


def counts_from_item_row(item: dict) -> dict:
    return compute_item_counts(
        jload(item.get("audio_files"), []),
        jload(item.get("chapters"), []),
        jload(item.get("library_files"), []),
        jload(item.get("ebook_file"), None) if item.get("ebook_file") else None,
    )


def _table_columns(table: str) -> set[str]:
    return {r["name"] for r in q(f"PRAGMA table_info({table})")}


def _migrate() -> None:
    cols = _table_columns("library_items")
    for name, decl in _ITEM_COUNT_COLS:
        if name not in cols:
            ex(f"ALTER TABLE library_items ADD COLUMN {name} {decl}")
    # Backfill once when any count column was missing (existing installs).
    if "num_tracks" not in cols:
        for row in q("SELECT id, audio_files, chapters, library_files, ebook_file FROM library_items"):
            update("library_items", row["id"], counts_from_item_row(row))
    ex("CREATE INDEX IF NOT EXISTS idx_items_library_missing ON library_items(library_id, is_missing)")
    ex("CREATE INDEX IF NOT EXISTS idx_items_ino ON library_items(ino)")
    ex("CREATE INDEX IF NOT EXISTS idx_book_authors_item ON book_authors(item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_book_series_item ON book_series(item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_progress_user_item ON media_progress(user_id, library_item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_progress_user ON media_progress(user_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_bookmarks_item ON bookmarks(library_item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_collections_library ON collections(library_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_collection_books_item ON collection_books(library_item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_playlist_items_item ON playlist_items(library_item_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id)")
    ex("CREATE INDEX IF NOT EXISTS idx_playlists_library_user ON playlists(library_id, user_id)")


def init_db() -> None:
    conn().executescript(SCHEMA)
    _migrate()


def q(sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def q1(sql: str, params=()) -> dict | None:
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def scalar(sql: str, params=()):
    r = conn().execute(sql, params).fetchone()
    return r[0] if r else None


def ex(sql: str, params=()) -> int:
    with _write_lock:
        cur = conn().execute(sql, params)
        return cur.rowcount


@contextmanager
def tx():
    """Serialized write transaction."""
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def insert(table: str, row: dict) -> None:
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


def update(table: str, row_id: str, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    ex(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))


def jload(value, default=None):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def get_setting(key: str, default=None):
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    return jload(row["value"], default)


def set_setting(key: str, value) -> None:
    ex("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, jdump(value)))
