"""Read QuickTime text-track chapters from MP4/M4B files.

mutagen only understands Nero 'chpl' chapters; most m4b files (iTunes, ffmpeg,
Audible conversions) use a QuickTime chapter text track referenced through
`tref/chap`. This module walks the box tree and decodes that track.
"""

import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"tref", b"udta", b"edts", b"dinf"}


def _boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def _find(f, start, end, path):
    for kind, body, stop in _boxes(f, start, end):
        if kind == path[0]:
            if len(path) == 1:
                return body, stop
            found = _find(f, body, stop, path[1:])
            if found:
                return found
    return None


def _read(f, start, stop):
    f.seek(start)
    return f.read(stop - start)


def _parse_trak(f, body, stop):
    info = {"id": None, "chap_refs": [], "timescale": None, "handler": None}
    tkhd = _find(f, body, stop, [b"tkhd"])
    if tkhd:
        data = _read(f, *tkhd)
        version = data[0]
        info["id"] = struct.unpack(">I", data[12:16] if version == 0 else data[20:24])[0]
    chap = _find(f, body, stop, [b"tref", b"chap"])
    if chap:
        data = _read(f, *chap)
        info["chap_refs"] = list(struct.unpack(f">{len(data) // 4}I", data[: len(data) // 4 * 4]))
    mdhd = _find(f, body, stop, [b"mdia", b"mdhd"])
    if mdhd:
        data = _read(f, *mdhd)
        version = data[0]
        info["timescale"] = struct.unpack(">I", data[12:16] if version == 0 else data[20:24])[0]
    hdlr = _find(f, body, stop, [b"mdia", b"hdlr"])
    if hdlr:
        info["handler"] = _read(f, *hdlr)[8:12]
    info["stbl"] = _find(f, body, stop, [b"mdia", b"minf", b"stbl"])
    return info


def _sample_table(f, stbl):
    start, stop = stbl
    tables = {}
    for kind, body, end in _boxes(f, start, stop):
        if kind in (b"stts", b"stsz", b"stco", b"co64", b"stsc"):
            tables[kind] = _read(f, body, end)

    durations = []
    stts = tables.get(b"stts")
    if stts:
        count = struct.unpack(">I", stts[4:8])[0]
        for i in range(count):
            n, delta = struct.unpack(">II", stts[8 + i * 8: 16 + i * 8])
            durations.extend([delta] * n)

    sizes = []
    stsz = tables.get(b"stsz")
    if stsz:
        fixed, count = struct.unpack(">II", stsz[4:12])
        sizes = [fixed] * count if fixed else list(struct.unpack(f">{count}I", stsz[12:12 + count * 4]))

    offsets_chunks = []
    if b"stco" in tables:
        data = tables[b"stco"]
        n = struct.unpack(">I", data[4:8])[0]
        offsets_chunks = list(struct.unpack(f">{n}I", data[8:8 + n * 4]))
    elif b"co64" in tables:
        data = tables[b"co64"]
        n = struct.unpack(">I", data[4:8])[0]
        offsets_chunks = list(struct.unpack(f">{n}Q", data[8:8 + n * 8]))

    stsc_entries = []
    stsc = tables.get(b"stsc")
    if stsc:
        n = struct.unpack(">I", stsc[4:8])[0]
        for i in range(n):
            stsc_entries.append(struct.unpack(">III", stsc[8 + i * 12: 20 + i * 12]))

    # Resolve each sample's file offset
    offsets = []
    sample = 0
    for ci, chunk_off in enumerate(offsets_chunks):
        chunk_no = ci + 1
        per_chunk = 1
        for first_chunk, spc, _ in stsc_entries:
            if first_chunk <= chunk_no:
                per_chunk = spc
            else:
                break
        off = chunk_off
        for _ in range(per_chunk):
            if sample >= len(sizes):
                break
            offsets.append(off)
            off += sizes[sample]
            sample += 1
    return durations, sizes, offsets


def _decode_text(raw: bytes) -> str:
    if len(raw) < 2:
        return ""
    n = struct.unpack(">H", raw[:2])[0]
    text = raw[2:2 + n]
    if text.startswith(b"\xfe\xff") or text.startswith(b"\xff\xfe"):
        return text.decode("utf-16", errors="replace").strip()
    return text.decode("utf-8", errors="replace").strip()


def read_chapters(path: str, total_duration: float) -> list[dict]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            moov = _find(f, 0, size, [b"moov"])
            if not moov:
                return []
            traks = [_parse_trak(f, body, stop) for kind, body, stop in _boxes(f, *moov) if kind == b"trak"]
            refs = {r for t in traks for r in t["chap_refs"]}
            chapter_trak = next((t for t in traks if t["id"] in refs and t["stbl"] and t["timescale"]), None)
            if not chapter_trak:
                return []
            durations, sizes, offsets = _sample_table(f, chapter_trak["stbl"])
            scale = chapter_trak["timescale"]
            chapters = []
            t = 0
            for i, off in enumerate(offsets):
                f.seek(off)
                title = _decode_text(f.read(min(sizes[i], 4096)))
                start = t / scale
                t += durations[i] if i < len(durations) else 0
                chapters.append({"start": start, "title": title or f"Chapter {i + 1}"})
            for i, ch in enumerate(chapters):
                ch["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else total_duration
            return [c for c in chapters if c["end"] > c["start"]]
    except (OSError, struct.error, ValueError):
        return []
