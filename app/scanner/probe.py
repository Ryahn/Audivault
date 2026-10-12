"""Audio file probing: duration, codec info, tags, chapters and embedded cover.

Uses mutagen (pure Python). If ffprobe is on PATH it is used as a fallback for
files mutagen cannot read and for chapters mutagen cannot find.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from . import mp4chapters
from .parse import clean_sequence

log = logging.getLogger("abstream.probe")

FFPROBE = shutil.which("ffprobe")

MIME_TYPES = {
    "mp3": "audio/mpeg", "m4b": "audio/mp4", "m4a": "audio/mp4", "mp4": "audio/mp4", "aac": "audio/aac",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav",
    "wma": "audio/x-ms-wma", "aif": "audio/x-aiff", "aiff": "audio/x-aiff", "webm": "audio/webm",
    "webma": "audio/webm", "mka": "audio/x-matroska", "awb": "audio/amr-wb", "caf": "audio/x-caf",
}

FORMAT_NAMES = {
    "mp3": "MP2/3 (MPEG audio layer 2/3)", "m4b": "QuickTime / MOV", "m4a": "QuickTime / MOV", "mp4": "QuickTime / MOV",
    "flac": "raw FLAC", "ogg": "Ogg", "oga": "Ogg", "opus": "Ogg", "wav": "WAV / WAVE",
}


def _first(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    if hasattr(value, "text"):
        return _first(value.text)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def _num_pair(value):
    """'3/12' or (3, 12) -> 3"""
    v = value
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, tuple):
        return v[0] or None
    v = _first(v)
    if not v:
        return None
    try:
        return int(str(v).split("/")[0])
    except ValueError:
        return None


def _mp4_tags(f: MP4) -> dict:
    t = f.tags or {}

    def g(key):
        return _first(t.get(key))

    def freeform(name):
        for key in t.keys():
            if key.lower() == f"----:com.apple.itunes:{name}".lower():
                return _first(t[key])
        return None

    return {
        "title": g("\xa9nam"), "album": g("\xa9alb"), "artist": g("\xa9ART"), "album_artist": g("aART"),
        "composer": g("\xa9wrt") or g("\xa9nrt"), "genre": g("\xa9gen"), "date": g("\xa9day"),
        "description": g("ldes") or g("desc") or g("\xa9cmt"), "publisher": g("\xa9pub") or freeform("PUBLISHER"),
        "grouping": g("\xa9grp"), "subtitle": freeform("SUBTITLE"), "asin": freeform("ASIN") or freeform("AUDIBLE_ASIN"),
        "isbn": freeform("ISBN"), "language": freeform("LANGUAGE"), "narrator": g("\xa9nrt") or freeform("NARRATOR"),
        "series": g("\xa9mvn") or freeform("SERIES"), "series_part": _first(t.get("\xa9mvi")) or freeform("SERIES-PART") or freeform("SERIES_PART"),
        "track": _num_pair(t.get("trkn")), "disc": _num_pair(t.get("disk")),
    }


def _id3_tags(tags: ID3) -> dict:
    def g(key):
        frames = tags.getall(key)
        return _first(frames[0]) if frames else None

    def txxx(name):
        for frame in tags.getall("TXXX"):
            if frame.desc.lower() == name.lower():
                return _first(frame.text)
        return None

    comment = None
    for frame in tags.getall("COMM"):
        comment = _first(frame.text)
        if comment:
            break
    return {
        "title": g("TIT2"), "album": g("TALB"), "artist": g("TPE1"), "album_artist": g("TPE2"),
        "composer": g("TCOM"), "genre": g("TCON"), "date": g("TDRC") or g("TYER"), "description": txxx("DESCRIPTION") or comment,
        "publisher": g("TPUB"), "grouping": g("TIT1") or g("GRP1"), "subtitle": g("TIT3") or txxx("SUBTITLE"),
        "asin": txxx("ASIN") or txxx("AUDIBLE_ASIN"), "isbn": txxx("ISBN"), "language": g("TLAN") or txxx("LANGUAGE"),
        "narrator": txxx("NARRATOR") or txxx("NARRATEDBY"), "series": g("MVNM") or txxx("SERIES"),
        "series_part": g("MVIN") or txxx("SERIES-PART") or txxx("SERIES_PART"),
        "track": _num_pair(g("TRCK")), "disc": _num_pair(g("TPOS")),
    }


def _vorbis_tags(tags) -> dict:
    def g(*keys):
        for key in keys:
            try:
                v = tags.get(key)
            except (KeyError, ValueError):
                v = None
            if v:
                return _first(v)
        return None

    return {
        "title": g("title"), "album": g("album"), "artist": g("artist"), "album_artist": g("albumartist", "album artist"),
        "composer": g("composer"), "genre": g("genre"), "date": g("date", "year"), "description": g("description", "comment"),
        "publisher": g("publisher", "label"), "grouping": g("grouping"), "subtitle": g("subtitle"), "asin": g("asin", "audible_asin"),
        "isbn": g("isbn"), "language": g("language"), "narrator": g("narrator", "performer"), "series": g("series", "movementname"),
        "series_part": g("series-part", "series_part", "movement"), "track": _num_pair(g("tracknumber")), "disc": _num_pair(g("discnumber")),
    }


def _id3_chapters(tags: ID3, duration: float) -> list[dict]:
    chapters = []
    for frame in tags.getall("CHAP"):
        title = None
        sub = getattr(frame, "sub_frames", None)
        if sub:
            tit2 = sub.getall("TIT2")
            if tit2:
                title = _first(tit2[0])
        start = frame.start_time / 1000.0
        end = frame.end_time / 1000.0 if frame.end_time and frame.end_time != 0xFFFFFFFF else None
        chapters.append({"start": start, "end": end, "title": title})
    chapters.sort(key=lambda c: c["start"])
    for i, ch in enumerate(chapters):
        if not ch["end"] or ch["end"] <= ch["start"]:
            ch["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else duration
        ch["title"] = ch["title"] or f"Chapter {i + 1}"
    return chapters


def _ffprobe(path: str) -> dict | None:
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", path],
            capture_output=True, timeout=60, check=False,
        )
        return json.loads(out.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def probe_audio(path: str) -> dict:
    """Return a dict describing the audio file. Never raises."""
    ext = Path(path).suffix.lower().lstrip(".")
    result = {
        "duration": 0.0, "bit_rate": None, "codec": None, "channels": None, "channel_layout": None,
        "sample_rate": None, "format": FORMAT_NAMES.get(ext, ext.upper()), "mime_type": MIME_TYPES.get(ext, "audio/mpeg"),
        "tags": {}, "chapters": [], "embedded_cover": False, "error": None,
    }
    try:
        f = mutagen.File(path)
    except Exception as e:  # mutagen raises many error types
        f = None
        result["error"] = str(e)

    if f is not None and getattr(f, "info", None) is not None:
        info = f.info
        result["duration"] = float(getattr(info, "length", 0) or 0)
        bitrate = getattr(info, "bitrate", None)
        result["bit_rate"] = int(bitrate) if bitrate else None
        result["channels"] = getattr(info, "channels", None)
        result["sample_rate"] = getattr(info, "sample_rate", None)
        codec = getattr(info, "codec", None)
        if isinstance(f, MP4):
            result["codec"] = "aac" if (codec or "").startswith("mp4a") else (codec or "aac")
        elif ext == "mp3":
            result["codec"] = "mp3"
        else:
            result["codec"] = (codec or type(info).__name__.replace("Info", "").replace("Stream", "")).lower() or ext
        if result["channels"]:
            result["channel_layout"] = {1: "mono", 2: "stereo"}.get(result["channels"], f"{result['channels']} channels")

        try:
            if isinstance(f, MP4):
                result["tags"] = _mp4_tags(f)
                result["embedded_cover"] = bool(f.tags and f.tags.get("covr"))
                chapters = mp4chapters.read_chapters(path, result["duration"])
                if not chapters and getattr(f, "chapters", None):
                    chs = list(f.chapters)
                    chapters = [{"start": c.start, "title": c.title or f"Chapter {i + 1}",
                                 "end": chs[i + 1].start if i + 1 < len(chs) else result["duration"]} for i, c in enumerate(chs)]
                result["chapters"] = chapters
            elif isinstance(getattr(f, "tags", None), ID3):
                result["tags"] = _id3_tags(f.tags)
                result["embedded_cover"] = bool(f.tags.getall("APIC"))
                result["chapters"] = _id3_chapters(f.tags, result["duration"])
            elif getattr(f, "tags", None) is not None:
                result["tags"] = _vorbis_tags(f.tags)
                result["embedded_cover"] = bool(getattr(f, "pictures", None)) or "metadata_block_picture" in (f.tags or {})
        except Exception as e:
            log.warning("Failed reading tags for %s: %s", path, e)

    if (result["duration"] <= 0 or (not result["chapters"] and ext in ("m4b", "m4a", "mp4", "mka"))) and FFPROBE:
        data = _ffprobe(path)
        if data:
            fmt = data.get("format", {})
            if result["duration"] <= 0:
                result["duration"] = float(fmt.get("duration") or 0)
                result["bit_rate"] = int(fmt.get("bit_rate") or 0) or None
                stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
                result["codec"] = stream.get("codec_name")
                result["channels"] = stream.get("channels")
                result["channel_layout"] = stream.get("channel_layout")
            if not result["chapters"] and data.get("chapters"):
                result["chapters"] = [{
                    "start": float(c.get("start_time", 0)), "end": float(c.get("end_time", 0)),
                    "title": (c.get("tags") or {}).get("title") or f"Chapter {i + 1}",
                } for i, c in enumerate(data["chapters"])]

    tags = result["tags"]
    if tags.get("series_part"):
        tags["series_part"] = clean_sequence(tags["series_part"])
    return result


def extract_embedded_cover(path: str) -> tuple[bytes, str] | None:
    """Returns (image bytes, extension) for the first embedded picture."""
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None or f.tags is None:
        return None
    try:
        if isinstance(f, MP4):
            covr = f.tags.get("covr")
            if covr:
                img = covr[0]
                return bytes(img), "png" if getattr(img, "imageformat", 0) == 14 else "jpg"
        elif isinstance(f.tags, ID3):
            apic = f.tags.getall("APIC")
            if apic:
                return apic[0].data, "png" if "png" in (apic[0].mime or "") else "jpg"
        else:
            pics = getattr(f, "pictures", None)
            if pics:
                return pics[0].data, "png" if "png" in (pics[0].mime or "") else "jpg"
            raw = f.tags.get("metadata_block_picture") if hasattr(f.tags, "get") else None
            if raw:
                import base64
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(raw[0]))
                return pic.data, "png" if "png" in (pic.mime or "") else "jpg"
    except Exception as e:
        log.warning("Cover extraction failed for %s: %s", path, e)
    return None
