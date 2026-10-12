"""Admin plugin catalog, install, and settings. Routes stay under /api/abstream/."""

from fastapi import APIRouter, Body, HTTPException, Request

from ..core import auth
from ..extensions import catalog, host, install
from ..extensions.errors import ExtensionError

router = APIRouter()


def _source() -> dict:
    meta = catalog.read_meta()
    try:
        parsed = catalog.catalog_source()
    except ExtensionError as exc:
        return {"owner": "", "repo": "", "ref": "", "error": str(exc)}
    return {
        "owner": meta.get("owner") or parsed["owner"],
        "repo": meta.get("repo") or parsed["repo"],
        "ref": meta.get("ref") or parsed["ref"],
        "error": "",
    }


def _settings_view(plugin_id: str, schema: list) -> list:
    values = install.settings_values(plugin_id)
    out = []
    for field in schema:
        item = {"key": field["key"], "label": field["label"], "type": field["type"]}
        if field.get("options"):
            item["options"] = field["options"]
        if field["type"] == "secret":
            item["secretSet"] = install.secret_set(plugin_id, field["key"])
        elif field["key"] in values:
            item["value"] = values[field["key"]]
        elif "default" in field:
            item["value"] = field["default"]
        else:
            item["value"] = "" if field["type"] == "string" else None
        out.append(item)
    return out


def _installed_view(entry: dict) -> dict:
    manifest = entry.get("manifest") or {}
    ok, reason = host.compatible(manifest) if manifest else (False, "not installed")
    return {
        "id": entry.get("id"),
        "name": manifest.get("name") or entry.get("id"),
        "summary": manifest.get("summary") or "",
        "publisher": manifest.get("publisher") or "",
        "desiredVersion": entry.get("desiredVersion"),
        "loadedVersion": entry.get("loadedVersion"),
        "previousVersion": entry.get("previousVersion"),
        "enabled": bool(entry.get("enabled")),
        "status": entry.get("status") or "installed",
        "error": entry.get("error"),
        "errorKind": entry.get("errorKind"),
        "restartRequired": bool(entry.get("pendingRestart")),
        "removeOnRestart": bool(entry.get("removeOnRestart")),
        "api": manifest.get("api"),
        "minServerVersion": manifest.get("min_server_version"),
        "commit": manifest.get("commit"),
        "capabilities": manifest.get("capabilities") or [],
        "compatible": ok,
        "compatibility": reason,
        "settings": _settings_view(entry.get("id"), manifest.get("settings") or []),
    }


def snapshot() -> dict:
    meta = catalog.read_meta()
    source = _source()
    state = install.read_state()
    installed_ids = set(state["plugins"])
    available = []
    for entry in install.catalog_plugins():
        ok, reason = host.compatible(entry)
        current = state["plugins"].get(entry["id"])
        desired = current.get("desiredVersion") if current else None
        available.append({
            "id": entry["id"], "name": entry["name"], "version": entry["version"],
            "summary": entry["summary"], "publisher": entry["publisher"],
            "api": entry["api"], "minServerVersion": entry["min_server_version"],
            "commit": entry["source"]["commit"], "capabilities": entry["capabilities"],
            "compatible": ok, "compatibility": reason,
            "installedVersion": desired,
            "updateAvailable": bool(desired and desired != entry["version"] and entry["id"] in installed_ids),
        })
    return {
        "catalog": source,
        "cache": {
            "lastAttempt": meta.get("lastAttempt") or 0,
            "lastSuccess": meta.get("lastSuccess") or 0,
            "lastError": meta.get("lastError") or source.get("error") or "",
        },
        "installed": [_installed_view({"id": plugin_id, **entry}) for plugin_id, entry in state["plugins"].items()],
        "available": available,
        "nav": host.nav_entries(),
    }


def _require(request: Request) -> dict:
    return auth.require_admin(request)


def _guard(fn):
    try:
        return fn()
    except ExtensionError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/abstream/plugins")
def list_plugins(request: Request):
    _require(request)
    return snapshot()


@router.post("/api/abstream/plugins/refresh")
def refresh_plugins(request: Request):
    _require(request)
    catalog.refresh(manual=True)
    return snapshot()


@router.post("/api/abstream/plugins/{plugin_id}/install")
@router.post("/api/abstream/plugins/{plugin_id}/update")
def install_plugin(plugin_id: str, request: Request, body: dict = Body(...)):
    _require(request)
    if not body.get("confirm"):
        raise HTTPException(400, "Confirm the publisher, repository, commit, and capabilities first")

    def run():
        entry = install.find_entry(plugin_id)
        ok, reason = host.compatible(entry)
        if not ok:
            raise ExtensionError(reason)
        install.stage_entry(entry)
        install.record_installed(entry)
        return snapshot()

    return _guard(run)


@router.post("/api/abstream/plugins/{plugin_id}/enable")
def enable_plugin(plugin_id: str, request: Request):
    _require(request)

    def run():
        state = install.read_state()
        current = state["plugins"].get(plugin_id) or {}
        ok, reason = host.compatible(current.get("manifest") or {})
        if not ok:
            raise ExtensionError(reason)
        install.set_enabled(plugin_id, True)
        return snapshot()

    return _guard(run)


@router.post("/api/abstream/plugins/{plugin_id}/disable")
def disable_plugin(plugin_id: str, request: Request):
    _require(request)
    return _guard(lambda: (install.set_enabled(plugin_id, False), snapshot())[1])


@router.post("/api/abstream/plugins/{plugin_id}/rollback")
def rollback_plugin(plugin_id: str, request: Request):
    _require(request)
    return _guard(lambda: (install.rollback(plugin_id), snapshot())[1])


@router.post("/api/abstream/plugins/{plugin_id}/uninstall")
def uninstall_plugin(plugin_id: str, request: Request, body: dict | None = Body(None)):
    _require(request)
    delete_data = bool((body or {}).get("deleteData"))
    return _guard(lambda: (install.uninstall(plugin_id, delete_data=delete_data), snapshot())[1])


@router.post("/api/abstream/plugins/{plugin_id}/settings")
def save_plugin_settings(plugin_id: str, request: Request, body: dict = Body(...)):
    _require(request)

    def run():
        state = install.read_state()
        current = state["plugins"].get(plugin_id)
        if not current:
            raise ExtensionError(f"{plugin_id} is not installed")
        install.write_settings(plugin_id, body.get("values") or {}, (current.get("manifest") or {}).get("settings") or [])
        return snapshot()

    return _guard(run)


@router.post("/api/abstream/plugins/{plugin_id}/settings/remove")
def remove_plugin_settings(plugin_id: str, request: Request):
    _require(request)
    _require_installed = install.read_state()["plugins"].get(plugin_id)
    if not _require_installed:
        raise HTTPException(404, "Plugin not installed")
    install.remove_settings(plugin_id)
    return snapshot()
