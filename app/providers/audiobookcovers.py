"""audiobookcovers.com cover search."""

from .http import get_json


def search(term: str) -> list[str]:
    data = get_json("https://api.audiobookcovers.com/cover/bytext/", {"q": term}) or []
    urls = []
    for item in data if isinstance(data, list) else []:
        versions = item.get("versions") or {}
        png = versions.get("png") or {}
        url = png.get("original") or item.get("filename")
        if url:
            urls.append(url)
    return urls
