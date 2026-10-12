"""Cue sheet (.cue) parsing for chapter markers.

Supports single-FILE cues (the usual m4b/mp3 + .cue pair) and multi-FILE cues,
where each FILE's track times are relative to that file and are converted to
book-wide offsets using the audio files' durations.
"""

import re
from pathlib import Path

FRAMES_PER_SECOND = 75


def _read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "TRACK" not in text.upper():
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


def _timestamp(value: str) -> float | None:
    """'mm:ss:ff' (ff = frames, 75 per second). Minutes may exceed 99."""
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})", value.strip())
    if not m:
        return None
    minutes, seconds, frames = (int(x) for x in m.groups())
    return minutes * 60 + seconds + frames / FRAMES_PER_SECOND



def parse(path: Path) -> list[dict]:
    """Returns [{"file": str, "title": str|None, "start": float}] in file order,
    with `start` relative to the track's FILE."""
    tracks: list[dict] = []
    current_file = None
    current: dict | None = None
    for raw_line in _read(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "FILE":
            m = re.match(r'FILE\s+"(.*)"(?:\s+\S+)?\s*$', line, re.I) or re.match(r"FILE\s+(.*?)(?:\s+(?:WAVE|MP3|AIFF|BINARY|MOTOROLA|FLAC))?\s*$", line, re.I)
            current_file = m.group(1) if m else None
            current = None
        elif cmd == "TRACK":
            current = {"file": current_file, "title": None, "start": None}
            tracks.append(current)
        elif cmd == "TITLE" and current is not None:
            m = re.match(r'TITLE\s+"(.*)"\s*$', line, re.I) or re.match(r"TITLE\s+(.*)$", line, re.I)
            current["title"] = m.group(1).strip() if m else None
        elif cmd == "INDEX" and current is not None and len(parts) >= 3:
            # INDEX 01 is the track start; INDEX 00 (pregap) only if no 01 is given
            number = parts[1]
            ts = _timestamp(parts[2])
            if ts is not None and (number == "01" or (number == "00" and current["start"] is None)):
                current["start"] = ts
    return [t for t in tracks if t["start"] is not None]


def chapters_from_cue(path: Path, audio: list[dict]) -> list[dict]:
    """Map cue tracks onto the item's (ordered, included) audio files and return
    book-wide chapters [{start, end, title}]. Returns [] if the cue doesn't fit."""
    if not audio:
        return []
    try:
        tracks = parse(path)
    except OSError:
        return []
    if not tracks:
        return []

    offsets, total = {}, 0.0
    by_name = {}
    for a in audio:
        offsets[a["path"]] = total
        by_name[Path(a["filename"]).name.lower()] = a
        by_name[Path(a["filename"]).stem.lower()] = a
        total += float(a.get("duration") or 0)

    files_in_cue = list(dict.fromkeys(t["file"] for t in tracks))
    mapping: dict[str | None, dict] = {}
    for i, name in enumerate(files_in_cue):
        key = Path(name or "").name.lower()
        match = by_name.get(key) or by_name.get(Path(key).stem)
        if not match:
            # Cue sheets often name the original rip (e.g. .wav); fall back to order
            if len(files_in_cue) == 1 and len(audio) == 1:
                match = audio[0]
            elif len(files_in_cue) == len(audio):
                match = audio[i]
        if not match:
            return []
        mapping[name] = match

    chapters = []
    for t in tracks:
        a = mapping[t["file"]]
        start = offsets[a["path"]] + t["start"]
        if start >= total:
            continue
        chapters.append({"start": round(start, 3), "title": t["title"]})
    chapters.sort(key=lambda c: c["start"])
    out = []
    for i, c in enumerate(chapters):
        end = chapters[i + 1]["start"] if i + 1 < len(chapters) else total
        if end > c["start"]:
            out.append({"start": c["start"], "end": round(end, 3), "title": c["title"] or f"Chapter {len(out) + 1}"})
    return out
