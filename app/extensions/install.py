"""Stage, verify, and publish one plugin version.

Files land in a staging directory and are published only after every checksum
matches. A failure deletes the staging directory and leaves the current version.
"""

import json
import os
import shutil
from pathlib import Path

from .catalog import (
    FILE_MAX_BYTES,
    PLUGIN_MAX_BYTES,
    catalog_source,
    file_request_url,
    http_get,
    plugins_dir,
    read_catalog,
    sha256_bytes,
    token,
    validate_catalog,
)
from .catalog import _headers
from .errors import ExtensionError

STATE_PATH_NAME = "state.json"


def safe_relative(path: str) -> str:
    """A catalog path that cannot leave the plugin directory."""
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or ":" in path:
        raise ExtensionError("plugin path is absolute or empty")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ExtensionError("plugin path escapes its directory")
    return path


def versions_root() -> Path:
    path = plugins_dir() / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_root() -> Path:
    path = plugins_dir() / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_root(plugin_id: str) -> Path:
    path = plugins_dir() / "data" / plugin_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_dir(plugin_id: str, version: str) -> Path:
    return versions_root() / plugin_id / version


def state_path() -> Path:
    return plugins_dir() / STATE_PATH_NAME


def read_state() -> dict:
    path = state_path()
    if not path.is_file():
        return {"plugins": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"plugins": {}}
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        return {"plugins": {}}
    return data


def write_state(state: dict) -> None:
    plugins_dir()
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(state_path())


def catalog_plugins() -> list[dict]:
    raw = read_catalog()
    if not raw:
        return []
    try:
        return validate_catalog(raw)
    except ExtensionError:
        return []


def find_entry(plugin_id: str) -> dict:
    for entry in catalog_plugins():
        if entry["id"] == plugin_id:
            return entry
    raise ExtensionError(f"{plugin_id} is not in the cached catalog")


def _contained(root: Path, dest: Path) -> None:
    if dest.is_symlink():
        raise ExtensionError("symlink refused")
    resolved = dest.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ExtensionError("path escapes the plugin directory")
    if resolved.is_symlink():
        raise ExtensionError("symlink refused")


def _write_file(root: Path, rel: str, data: bytes) -> None:
    rel = safe_relative(rel)
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        raise ExtensionError("refusing to overwrite a file while staging")
    dest.write_bytes(data)
    try:
        _contained(root, dest)
    except ExtensionError:
        dest.unlink(missing_ok=True)
        raise


def _replace_tree(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    aside = dest.with_name(dest.name + ".replacing")
    if aside.exists():
        shutil.rmtree(aside)
    moved = False
    if dest.exists():
        dest.rename(aside)
        moved = True
    try:
        src.rename(dest)
    except Exception:
        if moved and aside.exists() and not dest.exists():
            aside.rename(dest)
        raise
    if aside.exists():
        shutil.rmtree(aside)


def _auth_headers() -> dict:
    headers = _headers(None)
    headers.pop("If-None-Match", None)
    return headers


def stage_entry(entry: dict, *, fetcher=None) -> Path:
    """Download every declared file. Publish only when the set is complete."""
    fetcher = fetcher or http_get
    if not entry.get("files"):
        raise ExtensionError("plugin file list is empty")
    plugin_id = entry["id"]
    version = entry["version"]
    source = catalog_source()
    stage = staging_root() / plugin_id / version
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    total = 0
    try:
        paths = []
        for item in entry["files"]:
            rel = safe_relative(item["path"])
            if rel in paths:
                raise ExtensionError(f"{plugin_id} lists {rel} twice")
            paths.append(rel)
            url = file_request_url(source, entry["source"]["commit"], rel)
            if token():
                if "api.github.com" not in url:
                    raise ExtensionError("private catalog downloads must use the GitHub API")
            elif "raw.githubusercontent.com" not in url:
                raise ExtensionError("public plugin downloads must use raw.githubusercontent.com")
            status, _headers_out, body = fetcher(url, _auth_headers(), FILE_MAX_BYTES)
            if status != 200:
                raise ExtensionError(f"{rel} responded {status}")
            if len(body) > FILE_MAX_BYTES:
                raise ExtensionError(f"{rel} is larger than the allowed size")
            total += len(body)
            if total > PLUGIN_MAX_BYTES:
                raise ExtensionError(f"{plugin_id} is larger than the allowed size")
            if sha256_bytes(body) != item["sha256"]:
                raise ExtensionError(f"{rel} does not match its checksum")
            _write_file(stage, rel, body)
        expected = {item["path"] for item in entry["files"]}
        found = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
        if found != expected:
            raise ExtensionError(f"{plugin_id} file list does not match what was downloaded")
        for path in stage.rglob("*"):
            if path.is_symlink():
                raise ExtensionError("symlink refused")
        final = version_dir(plugin_id, version)
        _replace_tree(stage, final)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return version_dir(plugin_id, version)


def _manifest_slice(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "summary": entry["summary"],
        "publisher": entry["publisher"],
        "api": entry["api"],
        "min_server_version": entry["min_server_version"],
        "commit": entry["source"]["commit"],
        "capabilities": entry["capabilities"],
        "entry": entry["entry"],
        "settings": entry["settings"],
        "nav": entry["nav"],
        "version": entry["version"],
    }


def _blank(plugin_id: str) -> dict:
    return {
        "id": plugin_id,
        "enabled": False,
        "desiredVersion": None,
        "loadedVersion": None,
        "previousVersion": None,
        "removeOnRestart": False,
        "deleteDataOnRestart": False,
        "pendingRestart": False,
        "status": "installed",
        "error": None,
        "errorKind": None,
        "manifest": {},
    }


def record_installed(entry: dict) -> dict:
    state = read_state()
    current = state["plugins"].get(entry["id"]) or _blank(entry["id"])
    loaded = current.get("loadedVersion")
    if loaded and loaded != entry["version"]:
        current["previousVersion"] = loaded
        current["previousManifest"] = current.get("manifest") or {}
    elif current.get("desiredVersion") and current["desiredVersion"] != entry["version"]:
        if version_dir(entry["id"], current["desiredVersion"]).is_dir():
            current["previousVersion"] = current["desiredVersion"]
            current["previousManifest"] = current.get("manifest") or {}
    current["desiredVersion"] = entry["version"]
    current["enabled"] = True
    current["removeOnRestart"] = False
    current["deleteDataOnRestart"] = False
    current["pendingRestart"] = True
    current["manifest"] = _manifest_slice(entry)
    if current.get("status") != "loaded" or current.get("loadedVersion") != entry["version"]:
        current["status"] = "installed"
    state["plugins"][entry["id"]] = current
    write_state(state)
    return current


def set_enabled(plugin_id: str, enabled: bool) -> dict:
    state = read_state()
    current = state["plugins"].get(plugin_id)
    if not current or not current.get("desiredVersion"):
        raise ExtensionError(f"{plugin_id} is not installed")
    if current.get("removeOnRestart"):
        raise ExtensionError(f"{plugin_id} is set to be removed")
    current["enabled"] = bool(enabled)
    current["pendingRestart"] = current["enabled"] != (current.get("status") == "loaded")
    if current["enabled"] and current.get("loadedVersion") != current.get("desiredVersion"):
        current["pendingRestart"] = True
    state["plugins"][plugin_id] = current
    write_state(state)
    return current


def rollback(plugin_id: str) -> dict:
    state = read_state()
    current = state["plugins"].get(plugin_id)
    previous = current.get("previousVersion") if current else None
    if not previous or not version_dir(plugin_id, previous).is_dir():
        raise ExtensionError(f"{plugin_id} has no previous version to restore")
    current["desiredVersion"] = previous
    if current.get("previousManifest"):
        current["manifest"] = current["previousManifest"]
    current["enabled"] = True
    current["removeOnRestart"] = False
    current["pendingRestart"] = current.get("loadedVersion") != previous or current.get("status") != "loaded"
    state["plugins"][plugin_id] = current
    write_state(state)
    return current


def uninstall(plugin_id: str, *, delete_data: bool) -> dict:
    state = read_state()
    current = state["plugins"].get(plugin_id)
    if not current:
        raise ExtensionError(f"{plugin_id} is not installed")
    current["removeOnRestart"] = True
    current["deleteDataOnRestart"] = bool(delete_data)
    current["enabled"] = False
    current["pendingRestart"] = True
    state["plugins"][plugin_id] = current
    write_state(state)
    return current


def remove_settings(plugin_id: str) -> None:
    path = plugins_dir() / "data" / plugin_id
    if path.exists():
        shutil.rmtree(path)


def settings_values(plugin_id: str) -> dict:
    path = plugins_dir() / "data" / plugin_id / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def secret_set(plugin_id: str, key: str) -> bool:
    return (plugins_dir() / "data" / plugin_id / "secrets" / key).is_file()


def read_secret(plugin_id: str, key: str) -> str | None:
    path = plugins_dir() / "data" / plugin_id / "secrets" / key
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def write_settings(plugin_id: str, values: dict, schema: list) -> None:
    current = settings_values(plugin_id)
    by_key = {field["key"]: field for field in schema}
    for key, value in values.items():
        field = by_key.get(key)
        if not field:
            raise ExtensionError(f"unknown setting {key}")
        if field["type"] == "secret":
            if value is None:
                path = plugins_dir() / "data" / plugin_id / "secrets" / key
                path.unlink(missing_ok=True)
            elif isinstance(value, str):
                _write_secret(plugin_id, key, value)
            else:
                raise ExtensionError(f"{key} must be a string")
            continue
        current[key] = _coerce(field, value)
    path = data_root(plugin_id) / "settings.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current), encoding="utf-8")
    tmp.replace(path)


def _coerce(field: dict, value):
    kind = field["type"]
    if kind == "string":
        if not isinstance(value, str):
            raise ExtensionError(f"{field['key']} must be a string")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ExtensionError(f"{field['key']} must be true or false")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExtensionError(f"{field['key']} must be a number")
        return value
    if kind == "select":
        allowed = {option["value"] for option in field.get("options") or []}
        if value not in allowed:
            raise ExtensionError(f"{field['key']} is not one of the listed choices")
        return value
    raise ExtensionError(f"{field['key']} cannot be saved this way")


def _write_secret(plugin_id: str, key: str, value: str) -> None:
    directory = data_root(plugin_id) / "secrets"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / key
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)


def apply_removals() -> None:
    """Delete plugin code that an admin already chose to uninstall."""
    state = read_state()
    changed = False
    for plugin_id, entry in list(state["plugins"].items()):
        if not entry.get("removeOnRestart"):
            continue
        shutil.rmtree(versions_root() / plugin_id, ignore_errors=True)
        shutil.rmtree(staging_root() / plugin_id, ignore_errors=True)
        if entry.get("deleteDataOnRestart"):
            remove_settings(plugin_id)
        del state["plugins"][plugin_id]
        changed = True
    if changed:
        write_state(state)
