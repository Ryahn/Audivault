"""Runtime configuration, read from environment variables."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("ABSTREAM_DATA_DIR", BASE_DIR / "data")).resolve()
CONFIG_DIR = Path(os.environ.get("ABSTREAM_CONFIG_DIR", DATA_DIR / "config")).resolve()
METADATA_DIR = Path(os.environ.get("ABSTREAM_METADATA_DIR", DATA_DIR / "metadata")).resolve()
DB_PATH = CONFIG_DIR / "abstream.db"

HOST = os.environ.get("ABSTREAM_HOST", "0.0.0.0")
PORT = int(os.environ.get("ABSTREAM_PORT", "13378"))

ACCESS_TOKEN_TTL = int(os.environ.get("ABSTREAM_ACCESS_TTL", str(60 * 60 * 12)))  # 12 hours
REFRESH_TOKEN_TTL = int(os.environ.get("ABSTREAM_REFRESH_TTL", str(60 * 60 * 24 * 30)))  # 30 days

# Comma-separated origins for CORS. Empty = allow all (Bearer/API clients). When set,
# credentials are allowed for those origins (first-party web UI + cookie auth).
_cors = os.environ.get("ABSTREAM_CORS_ORIGINS", "").strip()
CORS_ORIGINS = [o.strip() for o in _cors.split(",") if o.strip()] if _cors else ["*"]

AUDIO_EXTS = {
    "m4b", "m4a", "mp3", "mp4", "aac", "ogg", "oga", "opus", "flac", "wav", "wma", "aif", "aiff", "webm", "webma", "mka", "awb", "caf",
}
EBOOK_EXTS = {"epub", "pdf", "mobi", "azw3", "cbr", "cbz"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
METADATA_EXTS = {"opf", "nfo", "json", "txt", "abs", "xml", "cue"}

for d in (DATA_DIR, CONFIG_DIR, METADATA_DIR, METADATA_DIR / "items", METADATA_DIR / "authors", METADATA_DIR / "cache"):
    d.mkdir(parents=True, exist_ok=True)
