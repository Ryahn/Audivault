"""iTunes Search API provider (audiobooks)."""

from .http import get_json


def _cover(data: dict) -> str | None:
    if data.get("artworkUrl600"):
        return data["artworkUrl600"]
    for key in ("artworkUrl100", "artworkUrl60", "artworkUrl30"):
        if data.get(key):
            size = key.replace("artworkUrl", "")
            return data[key].replace(f"{size}x{size}bb", "600x600bb")
    return None


def search(title: str, author: str | None = None) -> list[dict]:
    term = f"{title} {author or ''}".strip()
    data = get_json("https://itunes.apple.com/search", {"term": term, "media": "audiobook", "entity": "audiobook"}) or {}
    out = []
    for d in data.get("results", []):
        out.append({
            "id": d.get("collectionId"), "title": d.get("collectionName"),
            "author": (d.get("artistName") or "").replace(" & ", ", ") or None,
            "description": d.get("description"), "publishedYear": (d.get("releaseDate") or "").split("-")[0] or None,
            "genres": [d["primaryGenreName"]] if d.get("primaryGenreName") else None, "cover": _cover(d),
        })
    return out
