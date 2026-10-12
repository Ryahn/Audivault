"""Plugin catalog, install, and host checks. No live GitHub calls."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_root = tempfile.mkdtemp(prefix="abstream-plugins-")
os.environ["AUDIVAULT_DATA_DIR"] = _root
os.environ.pop("AUDIVAULT_PLUGIN_CATALOG_TOKEN", None)
os.environ.pop("AUDIVAULT_PLUGIN_CATALOG", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import auth, db  # noqa: E402
from app.extensions import catalog, host, install  # noqa: E402
from app.extensions.errors import ExtensionError  # noqa: E402
from app.extensions.host import PluginContext  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import PROVIDERS  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BACKOFF = 15 * 60


def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        sys.exit(1)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def plugin_doc(plugin_id="example", version="1.0.0", files=None, entry="example/plugin.py", **overrides):
    body = b"def setup(ctx):\n    pass\n"
    if files is None:
        files = [{"path": entry, "sha256": digest(body)}]
    doc = {
        "id": plugin_id,
        "name": plugin_id.title(),
        "version": version,
        "summary": "What it does",
        "publisher": "Audivault",
        "api": 1,
        "min_server_version": "0.1.0",
        "source": {"commit": COMMIT},
        "capabilities": ["provider"],
        "entry": entry,
        "files": files,
    }
    doc.update(overrides)
    return doc


def catalog_bytes(plugins):
    return json.dumps({"plugins": plugins}).encode()


def wipe_cache():
    catalog.CATALOG_PATH.unlink(missing_ok=True)
    catalog.META_PATH.unlink(missing_ok=True)


def manifest_slice(plugin_id, version, entry, capabilities):
    return {
        "name": plugin_id.title(),
        "summary": "What it does",
        "publisher": "Audivault",
        "api": 1,
        "min_server_version": "0.1.0",
        "commit": COMMIT,
        "capabilities": capabilities,
        "entry": entry,
        "settings": [],
        "nav": [],
        "version": version,
    }


def write_source(plugin_id, version, entry, source):
    dest = install.version_dir(plugin_id, version) / entry
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source, encoding="utf-8")


def reset_host():
    for plugin_id in list(host._loaded):
        host._drop(plugin_id)
    for key in list(PROVIDERS):
        if key not in host._BUILTIN_PROVIDERS:
            PROVIDERS.pop(key, None)
    host._processors.clear()
    for name in host._events:
        host._events[name].clear()
    install.write_state({"plugins": {}})


class Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url, headers, limit):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


print("catalog schedule")
wipe_cache()
good = catalog_bytes([plugin_doc()])
fetcher = Script([(200, {"etag": '"one"'}, good)])
catalog.refresh(manual=True, fetcher=fetcher, now=1_000_000)
check(fetcher.calls == 1, "first refresh downloads the catalog")
check(catalog.CATALOG_PATH.is_file(), "catalog file is cached")
catalog.refresh(manual=False, fetcher=fetcher, now=1_000_000 + 60)
check(fetcher.calls == 1, "a second check inside 12 hours does not fetch")
check(not catalog.due(catalog.read_meta(), 1_000_000 + 60, manual=False), "due() agrees the cache is fresh")
check(catalog.due(catalog.read_meta(), 1_000_000 + 60, manual=True), "manual refresh does not wait")

print("backoff")
wipe_cache()
failed = Script([ConnectionError("down"), (200, {"etag": '"two"'}, good)])
catalog.refresh(manual=True, fetcher=failed, now=5_000)
check(failed.calls == 1, "a failed refresh is recorded")
check(catalog.read_meta()["failures"] == 1, "one failure is stored")
catalog.refresh(manual=False, fetcher=failed, now=5_000 + 60)
check(failed.calls == 1, "the next automatic check waits out the backoff")
catalog.refresh(manual=True, fetcher=failed, now=5_000 + 60)
check(failed.calls == 2, "manual refresh ignores the wait")
check(catalog.read_meta()["failures"] == 0, "a later success clears the backoff")

print("304 and invalid body")
wipe_cache()
first = Script([(200, {"etag": '"keep"'}, good), (304, {"etag": '"keep"'}, b""), (200, {}, b"not-json")])
catalog.refresh(manual=True, fetcher=first, now=10)
cached = catalog.CATALOG_PATH.read_text(encoding="utf-8")
catalog.refresh(manual=True, fetcher=first, now=20)
check(catalog.CATALOG_PATH.read_text(encoding="utf-8") == cached, "a 304 keeps the cache")
check(catalog.read_meta()["lastError"] == "", "a 304 counts as success")
catalog.refresh(manual=True, fetcher=first, now=30)
check(catalog.CATALOG_PATH.read_text(encoding="utf-8") == cached, "an invalid catalog body keeps the previous cache")
check(bool(catalog.read_meta()["lastError"]), "the invalid body is recorded as an error")
check(install.catalog_plugins(), "the previous catalog still validates")

print("paths")
for bad in ("../escape.py", "/tmp/escape.py", "C:/escape.py", "dir\\file.py", "", "foo/../bar.py"):
    try:
        install.safe_relative(bad)
    except ExtensionError:
        continue
    check(False, f"path {bad!r} should be refused")
check(True, "absolute paths, backslashes, and .. are refused")
try:
    install.stage_entry(plugin_doc(files=[]), fetcher=Script([]))
    check(False, "an empty file list should be refused")
except ExtensionError:
    check(not any(install.versions_root().rglob("*")), "an empty file list publishes nothing")

print("size, checksum, and symlink")
huge = b"x" * (catalog.FILE_MAX_BYTES + 1)
huge_entry = plugin_doc(files=[{"path": "example/plugin.py", "sha256": digest(huge)}])
try:
    install.stage_entry(huge_entry, fetcher=lambda url, headers, limit: (200, {}, huge))
    check(False, "an oversized body should be refused")
except ExtensionError:
    check(not (install.staging_root() / "example" / "1.0.0").exists(), "an oversized body leaves no staging directory")


def local_fetcher(bodies):
    def fetcher(url, headers, limit):
        name = url.rsplit("/", 1)[-1]
        return 200, {}, bodies[name]
    return fetcher


one = b"version-one"
first_files = [{"path": "example/plugin.py", "sha256": digest(one)}]
install.stage_entry(plugin_doc(version="1.0.0", files=first_files), fetcher=local_fetcher({"plugin.py": one}))
kept = (install.version_dir("example", "1.0.0") / "example" / "plugin.py").read_bytes()
parts = [b"file-zero", b"file-one", b"file-two"]
names = [f"f{i}.py" for i in range(3)]
bad_files = []
for index, body in enumerate(parts):
    payload = b"wrong" if index == 2 else body
    bad_files.append({"path": f"example/{names[index]}", "sha256": digest(payload)})
broken = plugin_doc(version="1.1.0", files=bad_files, entry="example/f0.py")
try:
    install.stage_entry(broken, fetcher=local_fetcher({names[i]: parts[i] for i in range(3)}))
    check(False, "a bad checksum should be refused")
except ExtensionError as exc:
    check("checksum" in str(exc), "the third file fails its checksum")
check((install.version_dir("example", "1.0.0") / "example" / "plugin.py").read_bytes() == kept, "the previous version directory is unchanged")
check(not install.version_dir("example", "1.1.0").exists(), "the failed version is not published")
check(not (install.staging_root() / "example" / "1.1.0").exists(), "staging is deleted after the checksum failure")

outside = Path(_root) / "outside-target"
outside.mkdir()
stage = install.staging_root() / "linkcheck"
stage.mkdir(parents=True)
link = stage / "linked"
if os.name == "nt":
    subprocess.check_call(["cmd", "/c", "mklink", "/J", str(link), str(outside)], stdout=subprocess.DEVNULL)
else:
    link.symlink_to(outside, target_is_directory=True)
try:
    install._write_file(stage, "linked/evil.txt", b"nope")
    check(False, "a link outside staging should be refused")
except ExtensionError:
    check(not (outside / "evil.txt").exists(), "a link outside staging publishes no file")
shutil.rmtree(stage, ignore_errors=True)
try:
    install._contained(install.staging_root(), outside)
    check(False, "a destination outside staging should be refused")
except ExtensionError:
    check(True, "a destination outside staging is refused")


def mock_client(status, content, location=None):
    def handler(request):
        headers = {"location": location} if location else {}
        return httpx.Response(status, content=content, headers=headers)

    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


print("download limits")
url = "https://raw.githubusercontent.com/Ryahn/Audivault-Plugins/main/catalog.json"
with patch("app.extensions.catalog.httpx.Client", mock_client(200, b"y" * 50)):
    try:
        catalog.http_get(url, {}, 20)
        check(False, "http_get should cap the body")
    except ExtensionError as exc:
        check("larger than the allowed size" in str(exc), "a streamed body over the limit is refused")
with patch("app.extensions.catalog.httpx.Client", mock_client(302, b"", "https://example.test/elsewhere")):
    try:
        catalog.http_get(url, {}, 100)
        check(False, "a redirect should be refused")
    except ExtensionError as exc:
        check("redirect" in str(exc), "redirects are refused")
os.environ["AUDIVAULT_PLUGIN_CATALOG_TOKEN"] = "secret-token"
try:
    private = catalog.file_request_url(catalog.catalog_source(), COMMIT, "example/plugin.py")
    check(private.startswith("https://api.github.com/"), "a private catalog uses the GitHub API")
    check("raw.githubusercontent.com" not in private, "a token is not used with raw.githubusercontent.com")
    headers = catalog._headers(None)
    check(headers.get("Authorization") == "Bearer secret-token", "the token is an authorization header")
    try:
        catalog.http_get(url, headers, 100)
        check(False, "a token must not be sent to raw.githubusercontent.com")
    except ExtensionError:
        check(True, "raw.githubusercontent.com is refused while a token is set")
finally:
    os.environ.pop("AUDIVAULT_PLUGIN_CATALOG_TOKEN", None)

print("import and registration")
reset_host()
entry_path = "example/plugin.py"
write_source("example", "1.0.0", entry_path, "def setup(ctx):\n    ctx.register_provider(lambda title, author, **kw: [])\n")
write_source("example", "1.1.0", entry_path, "raise RuntimeError('import broke')\n")
previous = manifest_slice("example", "1.0.0", entry_path, ["provider"])
current = manifest_slice("example", "1.1.0", entry_path, ["provider"])
install.write_state({"plugins": {"example": {
    "id": "example", "enabled": True, "desiredVersion": "1.1.0", "loadedVersion": "1.0.0",
    "previousVersion": "1.0.0", "previousManifest": previous, "manifest": current,
    "pendingRestart": True, "status": "installed", "error": None, "errorKind": None,
    "removeOnRestart": False, "deleteDataOnRestart": False,
}}})
host.load()
example = install.read_state()["plugins"]["example"]
check(example["errorKind"] == "import", "an import error is recorded separately")
check(example["status"] == "failed", "the new version is marked failed")
check(example["loadedVersion"] == "1.0.0", "the previous version remains the loaded one")
check("example" in PROVIDERS, "the previous version's provider stays registered")

reset_host()
write_source("alpha", "1.0.0", "alpha/plugin.py",
             "def setup(ctx):\n    ctx.register_provider(lambda title, author, **kw: [])\n    raise RuntimeError('after')\n")
write_source("beta", "1.0.0", "beta/plugin.py",
             "def setup(ctx):\n    ctx.register_provider(lambda title, author, **kw: [])\n")
install.write_state({"plugins": {
    "alpha": {
        "id": "alpha", "enabled": True, "desiredVersion": "1.0.0", "loadedVersion": None,
        "previousVersion": None, "manifest": manifest_slice("alpha", "1.0.0", "alpha/plugin.py", ["provider"]),
        "pendingRestart": True, "status": "installed", "error": None, "errorKind": None,
        "removeOnRestart": False, "deleteDataOnRestart": False,
    },
    "beta": {
        "id": "beta", "enabled": True, "desiredVersion": "1.0.0", "loadedVersion": None,
        "previousVersion": None, "manifest": manifest_slice("beta", "1.0.0", "beta/plugin.py", ["provider"]),
        "pendingRestart": True, "status": "installed", "error": None, "errorKind": None,
        "removeOnRestart": False, "deleteDataOnRestart": False,
    },
}})
host.load()
check("alpha" not in PROVIDERS, "a registration failure leaves no provider behind")
check(install.read_state()["plugins"]["alpha"]["errorKind"] == "registration", "registration errors are separate from import errors")
check("beta" in PROVIDERS and install.read_state()["plugins"]["beta"]["status"] == "loaded", "a second plugin still registers")

print("processors and tasks")
original = {"title": "A", "path": "/books/a", "id": "item-1"}

def reject_path(meta):
    meta = dict(meta)
    meta["path"] = "/evil"
    meta["title"] = "Changed"
    return meta

host._processors.append(("zzz", reject_path))
result = host.apply_processors(original)
check(result["title"] == "A" and result["path"] == "/books/a" and result["id"] == "item-1", "a processor that changes path is discarded")
check(original == {"title": "A", "path": "/books/a", "id": "item-1"}, "the original metadata is unchanged")
host._processors.clear()

def reject_id(meta):
    meta = dict(meta)
    meta["id"] = "other"
    return meta

host._processors.append(("zzz", reject_id))
result = host.apply_processors(original)
check(result["id"] == "item-1", "a processor that changes an id is discarded")
host._processors.clear()

def boom(meta):
    raise RuntimeError("processor blew up")

def retitle(meta):
    meta = dict(meta)
    meta["title"] = "B"
    return meta

host._processors.extend([("mmm", boom), ("aaa", retitle)])
result = host.apply_processors(original)
check(result["title"] == "B" and result["path"] == "/books/a", "a later processor still runs after an exception")
host._processors.clear()

seen = []

def fail_event(**payload):
    raise RuntimeError("listener")

def ok_event(**payload):
    seen.append(payload["library_id"])

host._events["scan_finished"].extend([("bad", fail_event), ("good", ok_event)])
host.emit("scan_finished", library_id="lib1")
check(seen == ["lib1"], "one listener failure does not stop the others")
host._events["scan_finished"].clear()

ctx = PluginContext("example", ["provider"], [], "Example")
with patch("app.extensions.host.tasks.start", return_value={"id": "t"}) as started:
    ctx.start_task("sync", lambda: None)
    check(started.call_args.args[0] == "plugin", "plugin tasks use the plugin kind")
    check(started.call_args.kwargs["key"] == "plugin:example:sync", "plugin task keys cannot collide with built-in kinds")
    check(started.call_args.args[1].startswith("example:"), "the task title is prefixed with the plugin id")

print("compatibility and settings")
reset_host()
write_source("oldapi", "1.0.0", "oldapi/plugin.py", "def setup(ctx):\n    pass\n")
bad_api = manifest_slice("oldapi", "1.0.0", "oldapi/plugin.py", [])
bad_api["api"] = 2
too_new = manifest_slice("toonew", "1.0.0", "toonew/plugin.py", [])
too_new["min_server_version"] = "99.0.0"
install.write_state({"plugins": {
    "oldapi": {
        "id": "oldapi", "enabled": False, "desiredVersion": "1.0.0", "loadedVersion": None,
        "previousVersion": None, "manifest": bad_api, "pendingRestart": False, "status": "installed",
        "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
    },
    "toonew": {
        "id": "toonew", "enabled": False, "desiredVersion": "1.0.0", "loadedVersion": None,
        "previousVersion": None, "manifest": too_new, "pendingRestart": False, "status": "installed",
        "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
    },
}})
check(not host.compatible(bad_api)[0], "an API mismatch is incompatible")
check(not host.compatible(too_new)[0], "a newer min_server_version is incompatible")
schema = [
    {"key": "token", "label": "Token", "type": "secret"},
    {"key": "region", "label": "Region", "type": "select", "options": [{"label": "United States", "value": "us"}]},
]
bad_api["settings"] = schema

print("http")
reset_host()
wipe_cache()
db.init_db()
db.ex(
    "INSERT INTO libraries (id, name, folders, display_order, icon, media_type, settings, created_at, updated_at) "
    "VALUES ('lib1', 'Library', '[]', 0, 'database', 'book', '{}', 1, 1)"
)
auth.create_user("plugroot", "secret", "root")
auth.create_user("pluguser", "secret", "user")


def login(client, username):
    body = client.post("/login", json={"username": username, "password": "secret"}, headers={"x-return-tokens": "true"}).json()
    return {"Authorization": f"Bearer {body['user']['accessToken']}"}


with patch("app.main.tasks.start"), patch("app.main.watcher.start"), patch("app.main.watcher.sync"), \
        patch("app.management.autoimport.start"), patch("app.api.items.warm_cover_thumbnails"):
    with TestClient(app) as client:
        status = client.get("/status").json()
        check(status["app"] == "audiobookshelf", "startup with no plugins still serves the library")
        admin = login(client, "plugroot")
        libraries = client.get("/api/libraries", headers=admin)
        check(libraries.status_code == 200 and libraries.json()["libraries"][0]["name"] == "Library", "the library list still responds")
        listed = client.get("/api/abstream/plugins", headers=admin).json()
        check(listed["installed"] == [] and listed["catalog"]["owner"] == "Ryahn", "the plugins page reads an empty cache")

        user = login(client, "pluguser")
        check(client.post("/api/abstream/plugins/refresh", headers=user).status_code == 403, "a non-admin cannot refresh")
        check(client.post("/api/abstream/plugins/example/install", headers=user, json={"confirm": True}).status_code == 403, "a non-admin cannot install")

        def explode(url, headers, limit):
            raise ConnectionError("github down")

        with patch("app.extensions.catalog.http_get", explode):
            refreshed = client.post("/api/abstream/plugins/refresh", headers=admin)
        check(refreshed.status_code == 200, "management routes still respond when the fetch fails")
        check("github down" in refreshed.json()["cache"]["lastError"], "the fetch error is shown from the cache")
        again = client.get("/api/abstream/plugins", headers=admin)
        check(again.status_code == 200 and "github down" in again.json()["cache"]["lastError"], "opening the page reads the cache")

        install.write_state({"plugins": {
            "oldapi": {
                "id": "oldapi", "enabled": False, "desiredVersion": "1.0.0", "loadedVersion": None,
                "previousVersion": None, "manifest": bad_api, "pendingRestart": False, "status": "installed",
                "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
            },
            "toonew": {
                "id": "toonew", "enabled": False, "desiredVersion": "1.0.0", "loadedVersion": None,
                "previousVersion": None, "manifest": too_new, "pendingRestart": False, "status": "installed",
                "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
            },
        }})
        denied = client.post("/api/abstream/plugins/oldapi/enable", headers=admin)
        check(denied.status_code == 400 and "API" in denied.text, "an API mismatch cannot be enabled")
        denied = client.post("/api/abstream/plugins/toonew/enable", headers=admin)
        check(denied.status_code == 400 and "99.0.0" in denied.text, "a too-new min_server_version cannot be enabled")
        unconfirmed = client.post("/api/abstream/plugins/example/install", headers=admin, json={})
        check(unconfirmed.status_code == 400, "install waits for confirmation")

        saved = client.post("/api/abstream/plugins/oldapi/settings", headers=admin, json={"values": {"token": "s3cret", "region": "us"}})
        check(saved.status_code == 200, "settings save")
        secret_field = next(item for item in saved.json()["installed"] if item["id"] == "oldapi")
        token_field = next(item for item in secret_field["settings"] if item["key"] == "token")
        check(token_field.get("secretSet") is True and "s3cret" not in json.dumps(token_field), "the API reports that a secret is set and not its value")
        secret_path = catalog.PLUGINS_DIR / "data" / "oldapi" / "secrets" / "token"
        check(secret_path.read_text(encoding="utf-8") == "s3cret", "a secret is stored in the plugin data directory")
        if os.name != "nt":
            check(stat_mode(secret_path) == 0o600, "a secret file is mode 0600")

print("uninstall")
reset_host()
kept = manifest_slice("keepset", "1.0.0", "keepset/plugin.py", [])
install.write_state({"plugins": {"keepset": {
    "id": "keepset", "enabled": True, "desiredVersion": "1.0.0", "loadedVersion": "1.0.0",
    "previousVersion": None, "manifest": kept, "pendingRestart": False, "status": "loaded",
    "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
}}})
install.write_settings("keepset", {"region": "us"}, [{"key": "region", "label": "Region", "type": "string"}])
settings_file = catalog.PLUGINS_DIR / "data" / "keepset" / "settings.json"
install.uninstall("keepset", delete_data=False)
install.apply_removals()
check(settings_file.is_file(), "uninstall keeps settings unless asked")
check("keepset" not in install.read_state()["plugins"], "uninstall removes the plugin on the next startup")
install.write_state({"plugins": {"keepset": {
    "id": "keepset", "enabled": True, "desiredVersion": "1.0.0", "loadedVersion": "1.0.0",
    "previousVersion": None, "manifest": kept, "pendingRestart": False, "status": "loaded",
    "error": None, "errorKind": None, "removeOnRestart": False, "deleteDataOnRestart": False,
}}})
install.uninstall("keepset", delete_data=True)
install.apply_removals()
check(not settings_file.exists(), "uninstall can also delete the plugin data directory")

print("extensions ok")
