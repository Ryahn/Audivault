"""Open Library provider."""

from .http import get_json


def search(title: str, author: str | None = None) -> list[dict]:
    params = {"title": title, "limit": 10}
    if author:
        params["author"] = author
    data = get_json("https://openlibrary.org/search.json", params) or {}
    out = []
    for doc in data.get("docs", []):
        cover = None
        if doc.get("cover_i"):
            cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
        elif doc.get("cover_edition_key"):
            cover = f"https://covers.openlibrary.org/b/OLID/{doc['cover_edition_key']}-L.jpg"
        isbns = doc.get("isbn") or []
        out.append({
            "id": doc.get("key"), "title": doc.get("title"), "subtitle": doc.get("subtitle"),
            "author": ", ".join(doc.get("author_name") or []) or None,
            "publishedYear": str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
            "publisher": (doc.get("publisher") or [None])[0], "cover": cover,
            "isbn": isbns[0] if isbns else None, "genres": (doc.get("subject") or [])[:5] or None,
        })
    return out
