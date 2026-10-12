"""Audible metadata via the Audible catalog API + Audnexus (same as Audiobookshelf)."""

import re

from ..scanner.parse import clean_sequence
from .http import get_json

REGION_TLD = {"us": ".com", "ca": ".ca", "uk": ".co.uk", "au": ".com.au", "fr": ".fr", "de": ".de",
              "jp": ".co.jp", "it": ".it", "in": ".in", "es": ".es"}
AUDNEXUS = "https://api.audnex.us"


def is_asin(value: str | None) -> bool:
    return bool(value) and bool(re.fullmatch(r"[A-Z0-9]{10}", value.strip().upper()))


def asin_lookup(asin: str, region: str = "us") -> dict | None:
    data = get_json(f"{AUDNEXUS}/books/{asin.upper()}", {"region": region} if region else None)
    return data if data and data.get("asin") else None


def clean_result(item: dict) -> dict:
    series = []
    for key in ("seriesPrimary", "seriesSecondary"):
        s = item.get(key)
        if s and s.get("name"):
            series.append({"series": s["name"], "sequence": clean_sequence(s.get("position") or "")})
    genres = item.get("genres") or []
    lang = item.get("language")
    runtime = item.get("runtimeLengthMin")
    return {
        "title": item.get("title"),
        "subtitle": item.get("subtitle") or None,
        "author": ", ".join(a["name"] for a in item.get("authors") or []) or None,
        "authors": [{"name": a["name"], "asin": a.get("asin")} for a in item.get("authors") or []],
        "narrator": ", ".join(n["name"] for n in item.get("narrators") or []) or None,
        "publisher": item.get("publisherName"),
        "publishedYear": (item.get("releaseDate") or "").split("-")[0] or None,
        "description": item.get("summary") or None,
        "cover": item.get("image"),
        "asin": item.get("asin"),
        "isbn": item.get("isbn"),
        "genres": list(dict.fromkeys(g["name"] for g in genres if g.get("type") == "genre")) or None,
        "tags": list(dict.fromkeys(g["name"] for g in genres if g.get("type") == "tag")) or None,
        "series": series or None,
        "language": lang.capitalize() if lang else None,
        "duration": int(runtime) if isinstance(runtime, (int, float)) else 0,
        "region": item.get("region"),
        "rating": item.get("rating"),
        "abridged": item.get("formatType") == "abridged",
        "explicit": bool(item.get("isAdult")),
    }


def search(title: str, author: str | None = None, asin: str | None = None, region: str = "us") -> list[dict]:
    region = region if region in REGION_TLD else "us"
    items = []
    if asin and is_asin(asin):
        found = asin_lookup(asin, region)
        if found:
            items.append(found)
    if not items and is_asin(title):
        found = asin_lookup(title, region)
        if found:
            items.append(found)
    if not items and title:
        params = {"num_results": "10", "products_sort_by": "Relevance", "title": title}
        if author:
            params["author"] = author
        data = get_json(f"https://api.audible{REGION_TLD[region]}/1.0/catalog/products", params) or {}
        asins = [p["asin"] for p in data.get("products", []) if p.get("asin")]
        items = []
        for asin in asins:
            found = asin_lookup(asin, region)
            if found:
                items.append(found)
    return [clean_result(i) for i in items]


# ------------------------------------------------------------- authors

def author_search(name: str, region: str = "us") -> dict | None:
    """Find an author on Audnexus. Returns {asin, name, description, image}."""
    results = get_json(f"{AUDNEXUS}/authors", {"name": name, "region": region}) or []
    if not isinstance(results, list) or not results:
        return None
    target = name.lower().replace(".", "").replace(" ", "")
    best = next((r for r in results if (r.get("name") or "").lower().replace(".", "").replace(" ", "") == target), results[0])
    return author_by_asin(best["asin"], region) if best.get("asin") else None


def author_by_asin(asin: str, region: str = "us") -> dict | None:
    data = get_json(f"{AUDNEXUS}/authors/{asin.upper()}", {"region": region})
    if not data or not data.get("asin"):
        return None
    return {"asin": data["asin"], "name": data.get("name"), "description": data.get("description"), "image": data.get("image")}
