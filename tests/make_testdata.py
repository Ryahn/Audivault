"""Generate a test audiobook library with silent MP3s and tags."""
import shutil
import struct
import sys
import zlib
from pathlib import Path

from mutagen.id3 import APIC, CHAP, CTOC, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, CTOCFlags, MVNM, MVIN, TXXX

ROOT = Path(sys.argv[1])
FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413  # MPEG1 L3 128kbps 44.1kHz, 417 bytes, 26.12ms


def mp3(path: Path, seconds: float, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds / 0.0261224)
    path.write_bytes(FRAME * frames)
    t = ID3()
    if "title" in tags: t.add(TIT2(encoding=3, text=tags["title"]))
    if "album" in tags: t.add(TALB(encoding=3, text=tags["album"]))
    if "artist" in tags: t.add(TPE1(encoding=3, text=tags["artist"]))
    if "album_artist" in tags: t.add(TPE2(encoding=3, text=tags["album_artist"]))
    if "composer" in tags: t.add(TCOM(encoding=3, text=tags["composer"]))
    if "genre" in tags: t.add(TCON(encoding=3, text=tags["genre"]))
    if "year" in tags: t.add(TDRC(encoding=3, text=tags["year"]))
    if "track" in tags: t.add(TRCK(encoding=3, text=str(tags["track"])))
    if "series" in tags: t.add(MVNM(encoding=3, text=tags["series"]))
    if "series_part" in tags: t.add(MVIN(encoding=3, text=tags["series_part"]))
    if "asin" in tags: t.add(TXXX(encoding=3, desc="ASIN", text=tags["asin"]))
    if "chapters" in tags:
        ids = []
        for i, (start, end, title) in enumerate(tags["chapters"]):
            cid = f"ch{i}"
            ids.append(cid)
            t.add(CHAP(element_id=cid, start_time=int(start * 1000), end_time=int(end * 1000), sub_frames=[TIT2(encoding=3, text=title)]))
        t.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=ids, sub_frames=[]))
    if "cover" in tags:
        t.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=tags["cover"]))
    t.save(path, v2_version=4)


def png(w=64, h=64, rgb=(47, 216, 240)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full(kind, payload, version=0):
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def m4b_with_qt_chapters(path: Path, titles_durations):
    """Minimal MP4: an (empty) audio trak with tref/chap -> text trak holding chapter titles."""
    samples = [struct.pack(">H", len(t.encode())) + t.encode() for t, _ in titles_durations]
    ftyp = box(b"ftyp", b"M4B \x00\x00\x02\x00M4B isom")
    # mdat first so offsets are known
    mdat_header_len = 8
    mdat_payload = b"".join(samples)
    mdat_start = len(ftyp) + mdat_header_len
    offsets, off = [], mdat_start
    for s in samples:
        offsets.append(off)
        off += len(s)
    tkhd = lambda tid: full(b"tkhd", b"\x00" * 8 + struct.pack(">I", tid) + b"\x00" * 68)
    mdhd = lambda ts: full(b"mdhd", b"\x00" * 8 + struct.pack(">II", ts, 0) + b"\x00" * 4)
    hdlr = lambda h: full(b"hdlr", b"\x00" * 4 + h + b"\x00" * 13)
    stts = full(b"stts", struct.pack(">I", len(titles_durations)) + b"".join(struct.pack(">II", 1, int(d * 1000)) for _, d in titles_durations))
    stsz = full(b"stsz", struct.pack(">II", 0, len(samples)) + b"".join(struct.pack(">I", len(s)) for s in samples))
    stsc = full(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
    stco = full(b"stco", struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets))
    text_trak = box(b"trak", tkhd(2) + box(b"mdia", mdhd(1000) + hdlr(b"text") + box(b"minf", box(b"stbl", stts + stsz + stsc + stco))))
    audio_trak = box(b"trak", tkhd(1) + box(b"tref", box(b"chap", struct.pack(">I", 2))) + box(b"mdia", mdhd(44100) + hdlr(b"soun")))
    moov = box(b"moov", full(b"mvhd", b"\x00" * 96) + audio_trak + text_trak)
    path.write_bytes(ftyp + box(b"mdat", mdat_payload) + moov)


if __name__ == "__main__":
    shutil.rmtree(ROOT, ignore_errors=True)
    lib = ROOT / "library"
    imp = ROOT / "import"
    cover = png()

    for i in range(1, 4):
        mp3(lib / "Brandon Sanderson/Mistborn/Book 1 - The Final Empire" / f"Part {i:02d}.mp3", 20,
            title=f"Part {i}", album="The Final Empire", artist="Brandon Sanderson", track=i, composer="Michael Kramer", genre="Fantasy")
    (lib / "Brandon Sanderson/Mistborn/Book 1 - The Final Empire/cover.png").write_bytes(png(rgb=(200, 80, 60)))
    mp3(lib / "Brandon Sanderson/Mistborn/Book 2 - The Well of Ascension/Well.mp3", 15, album="The Well of Ascension", artist="Brandon Sanderson")
    # Audiobookshelf-style metadata.json whose chapters have no "id" (as some tools write them)
    (lib / "Brandon Sanderson/Mistborn/Book 2 - The Well of Ascension/metadata.json").write_text(
        '{"title": "The Well of Ascension", "chapters": [{"start": 0, "end": 7, "title": "Prologue"}, {"start": 7, "end": 15, "title": "Chapter 1"}]}')
    mp3(lib / "Andy Weir/Project Hail Mary/Project Hail Mary.mp3", 30, title="Project Hail Mary", album="Project Hail Mary",
        artist="Andy Weir", composer="Ray Porter", year="2021", genre="Science Fiction", asin="B08G9PRS1K", cover=cover,
        chapters=[(0, 10, "Opening Credits"), (10, 20, "Chapter 1"), (20, 30, "Chapter 2")])
    # A cue sheet whose chapters differ from the embedded ones (used when the library enables .cue chapters)
    (lib / "Andy Weir/Project Hail Mary/Project Hail Mary.cue").write_text('PERFORMER "Andy Weir"\nTITLE "Project Hail Mary"\nFILE "Project Hail Mary.wav" WAVE\n  TRACK 01 AUDIO\n    TITLE "Cue Intro"\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    TITLE Cue Part Two\n    INDEX 01 00:15:37\n', encoding="utf-8")
    mp3(lib / "Loose Book.mp3", 12, title="A Loose Single", album="A Loose Single", artist="Jane Doe")
    for d in (1, 2):
        for t in (1, 2):
            mp3(lib / f"Terry Pratchett/Discworld/12. Witches Abroad/CD {d}/Track {t}.mp3", 8, title=f"Disc {d} Track {t}", track=t)
    mp3(imp / "random_rip_folder/01.mp3", 10, album="Dune", artist="Frank Herbert", track=1, series="Dune", series_part="1")
    mp3(imp / "random_rip_folder/02.mp3", 10, album="Dune", artist="Frank Herbert", track=2)
    mp3(imp / "Some Loose Import.mp3", 6, album="The Hobbit", artist="J.R.R. Tolkien")
    m4b_with_qt_chapters(ROOT / "qt_chapters.m4b", [("Intro", 5.5), ("Chapter One", 60), ("Chapter Two", 42.25)])
    print("ok", ROOT)
