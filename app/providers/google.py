"""Google Books provider."""

from .http import get_json


def _isbn(ids) -> str | None:
    for kind in ("ISBN_13", "ISBN_10"):
        for i in ids or []:
            if i.get("type") == kind:
                return i.get("identifier")
    return None


def search(title: str, author: str | None = None) -> list[dict]:
    q = f"intitle:{title}"
    if author:
        q += f"+inauthor:{author}"
    data = get_json("https://www.googleapis.com/books/v1/volumes", {"q": q}) or {}
    out = []
    for item in data.get("items", []):
        v = item.get("volumeInfo") or {}
        links = v.get("imageLinks") or {}
        cover = list(links.values())[-1].replace("http:", "https:") if links else None
        out.append({
            "id": item.get("id"), "title": v.get("title"), "subtitle": v.get("subtitle"),
            "author": ", ".join(v.get("authors") or []) or None, "publisher": v.get("publisher"),
            "publishedYear": (v.get("publishedDate") or "").split("-")[0] or None, "description": v.get("description"),
            "cover": cover, "genres": v.get("categories"), "isbn": _isbn(v.get("industryIdentifiers")),
            "language": v.get("language"),
        })
    return out
