"""Load installed plugins and keep each registration isolated from the others.

Plugins get this module's context. They are not given the task runner, the
database, or the Audiobookshelf serializers. In-process Python is not a sandbox.
"""

import copy
import importlib.util
import logging
import sys

from .. import PLUGIN_API_VERSION, __version__
from ..core import tasks
from ..providers import PROVIDERS
from . import install
from .errors import ExtensionError

log = logging.getLogger("abstream.extensions")

_BUILTIN_PROVIDERS = set(PROVIDERS)
_processors: list[tuple[str, object]] = []
_events: dict[str, list] = {"scan_finished": [], "import_finished": []}
_loaded: set[str] = set()

_ALLOWED = {
    "title", "subtitle", "authors", "narrators", "series", "series_list", "description",
    "asin", "isbn", "genres", "tags", "publisher", "year", "published_year", "language",
    "explicit", "abridged",
}
_ALIASES = {"series": "series_list", "year": "published_year"}
_FORBIDDEN = {
    "id", "path", "library_id", "folder_id", "ino", "user", "user_id",
    "audio_files", "library_files", "cover_path", "rel_path",
}


def _version_parts(value: str) -> tuple:
    return tuple(int(part) for part in str(value).split("."))


def compatible(manifest: dict) -> tuple[bool, str]:
    api = manifest.get("api")
    minimum = manifest.get("min_server_version") or "0.0.0"
    if api != PLUGIN_API_VERSION:
        return False, f"plugin API {api} is not {PLUGIN_API_VERSION}"
    try:
        too_new = _version_parts(minimum) > _version_parts(__version__)
    except ValueError:
        return False, "min_server_version is invalid"
    if too_new:
        return False, f"needs Audivault {minimum}"
    return True, ""


class PluginSettings:
    def __init__(self, plugin_id: str, schema: list):
        self.plugin_id = plugin_id
        self.schema = schema

    def get(self, key: str, default=None):
        field = next((item for item in self.schema if item["key"] == key), None)
        if field and field["type"] == "secret":
            value = install.read_secret(self.plugin_id, key)
            return default if value is None else value
        values = install.settings_values(self.plugin_id)
        if key in values:
            return values[key]
        if field and "default" in field:
            return field["default"]
        return default

    def set(self, key: str, value) -> None:
        install.write_settings(self.plugin_id, {key: value}, self.schema)


class PluginContext:
    """Stable host surface. `start_task` always uses the plugin kind and key prefix."""

    def __init__(self, plugin_id: str, capabilities: list, schema: list, display_name: str):
        self.plugin_id = plugin_id
        self.logger = logging.getLogger(f"abstream.plugin.{plugin_id}")
        self.settings = PluginSettings(plugin_id, schema)
        self._capabilities = set(capabilities)
        self._display_name = display_name
        self._added: list[tuple] = []

    def start_task(self, title: str, fn, *args):
        label = f"{self.plugin_id}: {title}"
        return tasks.start("plugin", label, fn, *args, key=f"plugin:{self.plugin_id}:{title}")

    def register_provider(self, search) -> None:
        if "provider" not in self._capabilities:
            raise ExtensionError("provider was not declared")
        if not callable(search):
            raise ExtensionError("provider search must be a function")
        if self.plugin_id in _BUILTIN_PROVIDERS or self.plugin_id in PROVIDERS:
            raise ExtensionError("that provider id is already registered")
        PROVIDERS[self.plugin_id] = {"name": self._display_name, "search": search}
        self._added.append(("provider", self.plugin_id))

    def register_processor(self, fn) -> None:
        if "processor" not in self._capabilities:
            raise ExtensionError("processor was not declared")
        if not callable(fn):
            raise ExtensionError("processor must be a function")
        _processors.append((self.plugin_id, fn))
        self._added.append(("processor", fn))

    def on(self, event: str, fn) -> None:
        if "events" not in self._capabilities:
            raise ExtensionError("events were not declared")
        if event not in _events or not callable(fn):
            raise ExtensionError("unknown event")
        _events[event].append((self.plugin_id, fn))
        self._added.append(("event", event, fn))

    def rollback(self) -> None:
        for kind, *rest in self._added:
            if kind == "provider":
                PROVIDERS.pop(rest[0], None)
            elif kind == "processor":
                _processors[:] = [item for item in _processors if item[1] is not rest[0]]
            elif kind == "event":
                event, fn = rest
                _events[event][:] = [item for item in _events[event] if item[1] is not fn]
        self._added.clear()


def _drop(plugin_id: str) -> None:
    if plugin_id not in _BUILTIN_PROVIDERS:
        PROVIDERS.pop(plugin_id, None)
    _processors[:] = [item for item in _processors if item[0] != plugin_id]
    for name in _events:
        _events[name][:] = [item for item in _events[name] if item[0] != plugin_id]
    _loaded.discard(plugin_id)


def _import_module(plugin_id: str, version: str, entry: str):
    path = install.version_dir(plugin_id, version) / entry
    if not path.is_file() or path.is_symlink():
        raise ExtensionError("entry file is missing")
    module_name = "audivault_plugin_" + plugin_id.replace("-", "_")
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionError("cannot load the plugin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _try_load(plugin_id: str, version: str, manifest: dict) -> tuple[str | None, str]:
    _drop(plugin_id)
    try:
        module = _import_module(plugin_id, version, manifest.get("entry") or "")
    except Exception as exc:
        log.exception("plugin %s %s failed to import", plugin_id, version)
        return "import", str(exc)
    setup = getattr(module, "setup", None)
    if not callable(setup):
        sys.modules.pop("audivault_plugin_" + plugin_id.replace("-", "_"), None)
        return "registration", "plugin has no setup function"
    context = PluginContext(
        plugin_id, manifest.get("capabilities") or [], manifest.get("settings") or [],
        manifest.get("name") or plugin_id,
    )
    try:
        setup(context)
    except Exception as exc:
        context.rollback()
        log.exception("plugin %s %s failed during registration", plugin_id, version)
        return "registration", str(exc)
    _loaded.add(plugin_id)
    return None, ""


def _manifest_for(entry: dict, version: str) -> dict:
    if version and version == entry.get("previousVersion"):
        return entry.get("previousManifest") or entry.get("manifest") or {}
    return entry.get("manifest") or {}


def load() -> None:
    """Load every enabled plugin. One failure does not stop the others or startup."""
    install.apply_removals()
    for plugin_id in list(_loaded):
        _drop(plugin_id)
    state = install.read_state()
    for plugin_id, entry in state["plugins"].items():
        _drop(plugin_id)
        entry["error"] = None
        entry["errorKind"] = None
        if not entry.get("enabled"):
            entry["status"] = "disabled"
            entry["loadedVersion"] = None
            entry["pendingRestart"] = False
            continue
        desired = entry.get("desiredVersion")
        manifest = _manifest_for(entry, desired)
        ok, reason = compatible(manifest)
        if not ok:
            entry["status"] = "failed"
            entry["error"] = reason
            entry["errorKind"] = "registration"
            entry["loadedVersion"] = None
            entry["pendingRestart"] = False
            continue
        kind, message = _try_load(plugin_id, desired, manifest)
        if kind is None:
            entry["status"] = "loaded"
            entry["loadedVersion"] = desired
            entry["pendingRestart"] = False
            continue
        previous = entry.get("previousVersion")
        previous_manifest = entry.get("previousManifest") or {}
        if previous and previous != desired and install.version_dir(plugin_id, previous).is_dir() and compatible(previous_manifest)[0]:
            fallback, _fallback_error = _try_load(plugin_id, previous, previous_manifest)
            if fallback is None:
                entry["status"] = "failed"
                entry["error"] = message
                entry["errorKind"] = kind
                entry["loadedVersion"] = previous
                entry["pendingRestart"] = True
                continue
        entry["status"] = "failed"
        entry["error"] = message
        entry["errorKind"] = kind
        entry["loadedVersion"] = None
        entry["pendingRestart"] = True
    install.write_state(state)


def apply_processors(meta: dict) -> dict:
    """Run processors in plugin-id order. A bad result is discarded."""
    current = copy.deepcopy(meta)
    for plugin_id, fn in sorted(_processors, key=lambda item: item[0]):
        snapshot = copy.deepcopy(current)
        try:
            result = fn(copy.deepcopy(current))
        except Exception:
            log.exception("plugin %s processor failed", plugin_id)
            continue
        if not _result_ok(snapshot, result):
            log.warning("plugin %s processor result rejected", plugin_id)
            continue
        current = _merge(snapshot, result)
    return current


def _result_ok(original: dict, result) -> bool:
    if not isinstance(result, dict):
        return False
    for key, value in result.items():
        if key in _FORBIDDEN:
            if original.get(key) != value:
                return False
            continue
        canon = _ALIASES.get(key, key)
        if canon in _ALLOWED:
            continue
        if original.get(key) != value:
            return False
    return True


def _merge(original: dict, result: dict) -> dict:
    out = copy.deepcopy(original)
    for key, value in result.items():
        canon = _ALIASES.get(key, key)
        if canon in _ALLOWED:
            out[canon] = copy.deepcopy(value)
    return out


def emit(event: str, **payload) -> None:
    for plugin_id, fn in list(_events.get(event) or []):
        try:
            fn(**payload)
        except Exception:
            log.exception("plugin %s %s failed", plugin_id, event)


def nav_entries() -> list[dict]:
    state = install.read_state()
    out = []
    for plugin_id in sorted(_loaded):
        entry = state["plugins"].get(plugin_id) or {}
        manifest = _manifest_for(entry, entry.get("loadedVersion"))
        for item in manifest.get("nav") or []:
            if item.get("target") == "settings":
                out.append({"id": plugin_id, "label": item.get("label") or plugin_id, "target": "settings"})
    return out
