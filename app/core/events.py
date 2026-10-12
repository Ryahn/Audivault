"""Server-Sent Events: pushes task progress, scan results and import-folder
changes to connected web clients in real time.

Publishing is thread-safe (scans run in worker threads); each subscriber is an
asyncio queue owned by one streaming response.
"""

import asyncio
import json
import secrets
import threading
import time

_lock = threading.Lock()
_subscribers: list[dict] = []
_tickets: dict[str, tuple[str, float]] = {}

TICKET_TTL = 60


def issue_ticket(user_id: str) -> str:
    """Short-lived, single-use ticket so the EventSource URL never carries a real token."""
    now = time.time()
    ticket = secrets.token_urlsafe(24)
    with _lock:
        for t, (_, exp) in list(_tickets.items()):
            if exp < now:
                del _tickets[t]
        _tickets[ticket] = (user_id, now + TICKET_TTL)
    return ticket


def redeem_ticket(ticket: str) -> str | None:
    with _lock:
        entry = _tickets.pop(ticket, None)
    if not entry or entry[1] < time.time():
        return None
    return entry[0]


def subscribe(user: dict, loop: asyncio.AbstractEventLoop) -> dict:
    sub = {"user": user, "loop": loop, "queue": asyncio.Queue(maxsize=200)}
    with _lock:
        _subscribers.append(sub)
    return sub


def unsubscribe(sub: dict) -> None:
    with _lock:
        if sub in _subscribers:
            _subscribers.remove(sub)


def _allowed(sub: dict, event: dict) -> bool:
    from . import auth
    user = sub["user"]
    if event.get("adminOnly") and not auth.is_admin(user):
        return event.get("userId") == user["id"]
    if event.get("libraryId") and not auth.can_access_library(user, event["libraryId"]):
        return False
    return True


def _put(queue: asyncio.Queue, payload: str) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        pass  # slow client; it will resync from /api/abstream/tasks on reconnect


def publish(event: dict) -> None:
    payload = json.dumps({k: v for k, v in event.items() if k != "adminOnly"}, default=str)
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        if _allowed(sub, event):
            try:
                sub["loop"].call_soon_threadsafe(_put, sub["queue"], payload)
            except RuntimeError:
                unsubscribe(sub)  # event loop closed
