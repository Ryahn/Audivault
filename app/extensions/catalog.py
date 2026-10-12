"""Cached plugin catalog from a GitHub repository.

Refreshing downloads catalog.json only. Plugin files are fetched later, from
the commit named in each entry, and only for the plugin an admin installs.
"""

import hashlib
import json
import logging
import re
import time
from urllib.parse import quote

import httpx

from .. import __version__
from ..core.config import CONFIG_DIR
from .errors import ExtensionError

log = logging.getLogger("abstream.extensions")

DEFAULT_CATALOG = "https://github.com/Ryahn/Audivault-Plugins"
CATALOG_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 2 * 1024 * 1024
PLUGIN_MAX_BYTES = 8 * 1024 * 1024
SUCCESS_INTERVAL = 12 * 60 * 60
BACKOFF_SECONDS = (15 * 60, 60 * 60, 12 * 60 * 60)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_SERVER_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_REPO = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/tree/([A-Za-z0-9_./-]+))?/?$"
)
_CAPS = {"provider", "processor", "events"}
_SETTING_TYPES = {"string", "boolean", "number", "select", "secret"}

PLUGINS_DIR = CONFIG_DIR / "plugins"
CATALOG_PATH = PLUGINS_DIR / "catalog.json"
META_PATH = PLUGINS_DIR / "catalog-meta.json"


def plugins_dir():
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return PLUGINS_DIR


def catalog_source() -> dict:
    """The repository the next refresh will use. Does not call GitHub."""
    import os
    raw = os.environ.get("AUDIVAULT_PLUGIN_CATALOG", "").strip() or DEFAULT_CATALOG
    match = _REPO.match(raw)
    if not match:
        raise ExtensionError("AUDIVAULT_PLUGIN_CATALOG must be an https://github.com/owner/repo URL")
    owner, repo, ref = match.group(1), match.group(2), match.group(3) or "main"
    return {"owner": owner, "repo": repo, "ref": ref, "url": raw}


def token() -> str:
    import os
    return os.environ.get("AUDIVAULT_PLUGIN_CATALOG_TOKEN", "").strip()


def _catalog_request_url(source: dict) -> str:
    owner, repo, ref = source["owner"], source["repo"], quote(source["ref"], safe="")
    if token():
        return f"https://api.github.com/repos/{owner}/{repo}/contents/catalog.json?ref={ref}"
    safe_ref = quote(source["ref"], safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{safe_ref}/catalog.json"


def file_request_url(source: dict, commit: str, path: str) -> str:
    owner, repo = source["owner"], source["repo"]
    if token():
        return f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={commit}"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{quote(path, safe='/')}"


def _headers(etag: str | None) -> dict:
    headers = {
        "User-Agent": f"Audivault/{__version__}",
        "Accept": "application/vnd.github.raw" if token() else "application/json",
    }
    if token():
        headers["Authorization"] = f"Bearer {token()}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def http_get(url: str, headers: dict, limit: int) -> tuple[int, dict, bytes]:
    """GET one URL. Redirects are refused. The body is capped at `limit` bytes."""
    host = httpx.URL(url).host or ""
    allowed = "api.github.com" if token() else "raw.githubusercontent.com"
    if host != allowed:
        raise ExtensionError(f"download host {host or url} is not {allowed}")
    timeout = httpx.Timeout(30.0, connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise ExtensionError("redirect refused")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise ExtensionError("response is larger than the allowed size")
                chunks.append(chunk)
            header_map = {k.lower(): v for k, v in response.headers.items()}
            return response.status_code, header_map, b"".join(chunks)


def read_meta() -> dict:
    if not META_PATH.is_file():
        return {"etag": None, "lastAttempt": 0, "lastSuccess": 0, "lastError": "", "failures": 0}
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"etag": None, "lastAttempt": 0, "lastSuccess": 0, "lastError": "", "failures": 0}
    return data if isinstance(data, dict) else {"etag": None, "lastAttempt": 0, "lastSuccess": 0, "lastError": "", "failures": 0}


def write_meta(meta: dict) -> None:
    plugins_dir()
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    tmp.replace(META_PATH)


def read_catalog() -> dict | None:
    if not CATALOG_PATH.is_file():
        return None
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def due(meta: dict, now: float, *, manual: bool) -> bool:
    """Whether a refresh should call GitHub. Manual refresh does not wait."""
    if manual:
        return True
    last_success = float(meta.get("lastSuccess") or 0)
    last_attempt = float(meta.get("lastAttempt") or 0)
    failures = int(meta.get("failures") or 0)
    if last_success and now - last_success < SUCCESS_INTERVAL:
        return False
    if failures and last_attempt:
        wait = BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]
        if now - last_attempt < wait:
            return False
    return True


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtensionError(f"{field} is required")
    return value.strip()


def _settings(raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ExtensionError("settings must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ExtensionError("each setting must be an object")
        key = _text(item.get("key"), "setting key")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ExtensionError(f"setting key {key!r} is not allowed")
        kind = item.get("type")
        if kind not in _SETTING_TYPES:
            raise ExtensionError(f"setting {key} has an unknown type")
        field = {"key": key, "label": _text(item.get("label"), f"setting {key} label"), "type": kind}
        if "default" in item:
            field["default"] = item["default"]
        if kind == "select":
            options = item.get("options")
            if not isinstance(options, list) or not options:
                raise ExtensionError(f"setting {key} needs options")
            cleaned = []
            for option in options:
                if not isinstance(option, dict):
                    raise ExtensionError(f"setting {key} has a bad option")
                cleaned.append({
                    "label": _text(option.get("label"), "option label"),
                    "value": _text(str(option.get("value")) if option.get("value") is not None else "", "option value"),
                })
            field["options"] = cleaned
        out.append(field)
    return out


def _nav(raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ExtensionError("nav must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict) or item.get("target") != "settings":
            raise ExtensionError("nav target must be settings")
        out.append({"label": _text(item.get("label"), "nav label"), "target": "settings"})
    return out


def validate_catalog(data: dict) -> list[dict]:
    """Return the plugin entries, or raise if the document is not a catalog."""
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ExtensionError("catalog must be an object with a plugins list")
    plugins = []
    seen = set()
    for raw in data["plugins"]:
        if not isinstance(raw, dict):
            raise ExtensionError("each plugin must be an object")
        plugin_id = raw.get("id")
        if not isinstance(plugin_id, str) or not _ID.match(plugin_id):
            raise ExtensionError("plugin id must be a short lowercase slug")
        if plugin_id in seen:
            raise ExtensionError(f"duplicate plugin id {plugin_id}")
        seen.add(plugin_id)
        commit = (raw.get("source") or {}).get("commit") if isinstance(raw.get("source"), dict) else None
        if not isinstance(commit, str) or not _COMMIT.match(commit):
            raise ExtensionError(f"{plugin_id} needs a 40-character commit")
        version = raw.get("version")
        if not isinstance(version, str) or not _VERSION.match(version):
            raise ExtensionError(f"{plugin_id} has an invalid version")
        api = raw.get("api")
        if not isinstance(api, int) or isinstance(api, bool) or api < 1:
            raise ExtensionError(f"{plugin_id} has an invalid api version")
        minimum = raw.get("min_server_version")
        if not isinstance(minimum, str) or not _SERVER_VERSION.match(minimum):
            raise ExtensionError(f"{plugin_id} has an invalid min_server_version")
        caps = raw.get("capabilities")
        if not isinstance(caps, list) or any(c not in _CAPS for c in caps) or len(caps) != len(set(caps)):
            raise ExtensionError(f"{plugin_id} has unknown capabilities")
        files = raw.get("files")
        if not isinstance(files, list) or not files:
            raise ExtensionError(f"{plugin_id} needs a file list")
        cleaned_files = []
        paths = set()
        for item in files:
            if not isinstance(item, dict):
                raise ExtensionError(f"{plugin_id} has a bad file entry")
            path = item.get("path")
            digest = item.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise ExtensionError(f"{plugin_id} file entries need path and sha256")
            from .install import safe_relative
            safe_relative(path)
            digest = digest.lower()
            if not _SHA.match(digest):
                raise ExtensionError(f"{plugin_id} has a bad sha256")
            if path in paths:
                raise ExtensionError(f"{plugin_id} lists {path} twice")
            paths.add(path)
            cleaned_files.append({"path": path, "sha256": digest})
        entry = raw.get("entry")
        if not isinstance(entry, str) or entry not in paths:
            raise ExtensionError(f"{plugin_id} entry must be one of its files")
        plugins.append({
            "id": plugin_id,
            "name": _text(raw.get("name"), "name"),
            "version": version,
            "summary": _text(raw.get("summary"), "summary"),
            "publisher": _text(raw.get("publisher"), "publisher"),
            "api": api,
            "min_server_version": minimum,
            "source": {"commit": commit},
            "capabilities": list(caps),
            "entry": entry,
            "files": cleaned_files,
            "settings": _settings(raw.get("settings")),
            "nav": _nav(raw.get("nav")),
        })
    return plugins


def _remember_failure(meta: dict, message: str, now: float) -> None:
    meta["lastAttempt"] = now
    meta["lastError"] = message
    meta["failures"] = int(meta.get("failures") or 0) + 1
    write_meta(meta)


def refresh(*, manual: bool = False, fetcher=None, now: float | None = None) -> dict:
    """Download the catalog when it is due. A bad body leaves the previous cache."""
    fetcher = fetcher or http_get
    now = time.time() if now is None else now
    meta = read_meta()
    if not due(meta, now, manual=manual):
        return meta
    try:
        source = catalog_source()
        status, headers, body = fetcher(_catalog_request_url(source), _headers(meta.get("etag")), CATALOG_MAX_BYTES)
    except Exception as exc:
        log.warning("plugin catalog refresh failed: %s", exc)
        _remember_failure(meta, str(exc), now)
        return read_meta()
    meta["lastAttempt"] = now
    if status == 304 and CATALOG_PATH.is_file():
        meta["lastSuccess"] = now
        meta["lastError"] = ""
        meta["failures"] = 0
        if headers.get("etag"):
            meta["etag"] = headers["etag"]
        write_meta(meta)
        return meta
    if status != 200:
        _remember_failure(meta, f"catalog responded {status}", now)
        return read_meta()
    try:
        parsed = json.loads(body.decode("utf-8"))
        validate_catalog(parsed)
    except (UnicodeError, json.JSONDecodeError, ExtensionError) as exc:
        log.warning("plugin catalog rejected: %s", exc)
        _remember_failure(meta, str(exc), now)
        return read_meta()
    plugins_dir()
    tmp = CATALOG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(parsed), encoding="utf-8")
    tmp.replace(CATALOG_PATH)
    meta["lastSuccess"] = now
    meta["lastError"] = ""
    meta["failures"] = 0
    meta["etag"] = headers.get("etag")
    meta["owner"] = source["owner"]
    meta["repo"] = source["repo"]
    meta["ref"] = source["ref"]
    write_meta(meta)
    return meta


def maybe_refresh() -> None:
    """Scheduler entry. Swallows errors so a catalog problem cannot stop scans."""
    try:
        refresh(manual=False)
    except Exception:
        log.exception("plugin catalog refresh failed")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
