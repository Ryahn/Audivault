"""Metadata providers. Each provider's `search` returns a list of normalized
results in the same shape Audiobookshelf's /api/search/books returns:

    {title, subtitle, author, narrator, publisher, publishedYear, description,
     cover, asin, isbn, genres, tags, series: [{series, sequence}], language,
     duration (minutes), explicit, abridged}
"""

import logging
import re

from . import audible, audiobookcovers, google, itunes, openlibrary

log = logging.getLogger("abstream.providers")

PROVIDERS = {
    "audible": {"name": "Audible.com", "search": lambda t, a, **kw: audible.search(t, a, region="us", **kw)},
    "audible.ca": {"name": "Audible.ca", "search": lambda t, a, **kw: audible.search(t, a, region="ca", **kw)},
    "audible.uk": {"name": "Audible.co.uk", "search": lambda t, a, **kw: audible.search(t, a, region="uk", **kw)},
    "audible.au": {"name": "Audible.com.au", "search": lambda t, a, **kw: audible.search(t, a, region="au", **kw)},
    "audible.fr": {"name": "Audible.fr", "search": lambda t, a, **kw: audible.search(t, a, region="fr", **kw)},
    "audible.de": {"name": "Audible.de", "search": lambda t, a, **kw: audible.search(t, a, region="de", **kw)},
    "audible.jp": {"name": "Audible.co.jp", "search": lambda t, a, **kw: audible.search(t, a, region="jp", **kw)},
    "audible.it": {"name": "Audible.it", "search": lambda t, a, **kw: audible.search(t, a, region="it", **kw)},
    "audible.in": {"name": "Audible.in", "search": lambda t, a, **kw: audible.search(t, a, region="in", **kw)},
    "audible.es": {"name": "Audible.es", "search": lambda t, a, **kw: audible.search(t, a, region="es", **kw)},
    "google": {"name": "Google Books", "search": lambda t, a, **kw: google.search(t, a)},
    "itunes": {"name": "iTunes", "search": lambda t, a, **kw: itunes.search(t, a)},
    "openlibrary": {"name": "Open Library", "search": lambda t, a, **kw: openlibrary.search(t, a)},
}

COVER_PROVIDERS = {"audiobookcovers": "AudiobookCovers.com", **{k: v["name"] for k, v in PROVIDERS.items()}}


def provider_list() -> list[dict]:
    return [{"value": k, "text": v["name"]} for k, v in PROVIDERS.items()]


def clean_title(title: str) -> str:
    """Strip things like '(Unabridged)' and bracketed ASINs before searching."""
    title = re.sub(r"\[[A-Z0-9]{10}\]", "", title or "")
    title = re.sub(r"\((?:un)?abridged\)", "", title or "", flags=re.I)
    title = re.sub(r"\((?:graphic\s*audio|dramatized(?:\s+adaptation)?)\)", "", title, flags=re.I)
    title = re.sub(r"^\d{1,2}\s*[-.:)]\s*", "", title.strip())
    return re.sub(r"\s+", " ", title).strip()


def search_books(provider: str, title: str, author: str | None = None, asin: str | None = None, isbn: str | None = None) -> list[dict]:
    p = PROVIDERS.get(provider) or PROVIDERS["audible"]
    try:
        results = p["search"](clean_title(title), (author or "").strip() or None, asin=asin)
    except Exception as e:
        log.warning("Provider %s failed: %s", provider, e)
        return []
    return [r for r in results if r and r.get("title")]


def search_covers(provider: str, title: str, author: str | None = None) -> list[str]:
    if provider == "audiobookcovers":
        return audiobookcovers.search(f"{title} {author or ''}".strip())
    if provider == "best":
        covers = []
        for prov in ("audible", "google", "itunes", "openlibrary"):
            covers += [r["cover"] for r in search_books(prov, title, author) if r.get("cover")]
        covers += audiobookcovers.search(f"{title} {author or ''}".strip())
        return list(dict.fromkeys(covers))
    return list(dict.fromkeys(r["cover"] for r in search_books(provider, title, author) if r.get("cover")))
