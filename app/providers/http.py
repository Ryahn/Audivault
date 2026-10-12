import email.utils
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

import httpx

from .. import __version__

HEADERS = {"User-Agent": f"Audivault/{__version__}"}
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_REDIRECTS = 5
MAX_RETRIES = 3
MIN_GAP = 0.4
_RETRY_STATUSES = frozenset({429, 503})


class _HostGate:
    """One shared gap and backoff clock per hostname."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            ready = self._next.get(host, 0.0)
            delay = max(0.0, ready - now)
            self._next[host] = max(now, ready) + MIN_GAP
        if delay:
            time.sleep(delay)

    def penalize(self, host: str, seconds: float) -> None:
        with self._lock:
            self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + max(0.0, seconds))


_gate = _HostGate()


def _retry_delay(response, attempt: int) -> float:
    raw = (response.headers.get("retry-after") or "").strip()
    if raw:
        try:
            return min(60.0, max(0.0, float(raw)))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                return min(60.0, max(0.0, when.timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(60.0, float(2 ** attempt))


def _blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _host_resolves_safe(hostname: str) -> bool:
    if not hostname:
        return False
    host = hostname.strip("[]").lower()
    if host in ("localhost", "metadata.google.internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        if _blocked_ip(info[4][0]):
            return False
    return True


def url_is_safe(url: str) -> bool:
    """Reject non-http(s) URLs and hosts that resolve to private/reserved IPs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if not host:
        return False
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return _host_resolves_safe(host)
    return not _blocked_ip(literal)


def _request(method: str, url: str, *, params: dict | None = None, as_bytes: bool = False,
             max_bytes: int = 15 * 1024 * 1024):
    current = url
    redirects = 0
    retries = 0
    while redirects <= MAX_REDIRECTS:
        if not url_is_safe(current):
            return None
        host = (urlparse(current).hostname or "").lower()
        _gate.wait(host)
        try:
            with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=False) as client:
                r = client.request(method, current, params=params if current == url else None)
        except httpx.HTTPError:
            return None
        if r.status_code in _RETRY_STATUSES and retries < MAX_RETRIES:
            delay = _retry_delay(r, retries)
            _gate.penalize(host, delay)
            time.sleep(delay)
            retries += 1
            continue
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location")
            if not loc:
                return None
            current = str(httpx.URL(current).join(loc))
            params = None
            redirects += 1
            retries = 0
            continue
        if r.status_code != 200:
            return None
        if as_bytes:
            ctype = r.headers.get("content-type", "")
            data = r.content
            if len(data) > max_bytes:
                return None
            return data, ctype
        try:
            return r.json()
        except ValueError:
            return None
    return None


def get_json(url: str, params: dict | None = None):
    return _request("GET", url, params=params, as_bytes=False)


def get_bytes(url: str, max_bytes: int = 15 * 1024 * 1024) -> tuple[bytes, str] | None:
    return _request("GET", url, as_bytes=True, max_bytes=max_bytes)
