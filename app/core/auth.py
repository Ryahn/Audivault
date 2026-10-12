"""Authentication: password hashing, Audiobookshelf-style JWT tokens, request auth."""

import base64
import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import HTTPException, Request

from . import db
from .config import ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL

DOWNLOAD_TOKEN_TTL = 5 * 60  # short-lived signed download links

DEFAULT_PERMISSIONS = {
    "download": True,
    "update": False,
    "delete": False,
    "upload": False,
    "createEreader": False,
    "accessAllLibraries": True,
    "accessAllTags": True,
    "accessExplicitContent": True,
    "selectedTagsNotAccessible": False,
}
ADMIN_PERMISSIONS = {k: True for k in DEFAULT_PERMISSIONS} | {"selectedTagsNotAccessible": False}
MIN_PASSWORD_LEN = 4


# ---------------------------------------------------------------- passwords

def validate_password(password: str) -> str:
    """Return stripped password or raise ValueError if too short/empty."""
    if password is None or not isinstance(password, str):
        raise ValueError("Password required")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    return password


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- tokens

def _secret() -> str:
    secret = db.get_setting("jwt_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_setting("jwt_secret", secret)
    return secret


def _sign(payload: dict) -> str:
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode(token: str) -> dict | None:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"], options={"verify_exp": True})
    except jwt.PyJWTError:
        return None


def legacy_token_for(user_id: str, username: str) -> str:
    """Non-expiring token kept for older clients (and `?token=` query auth)."""
    return _sign({"userId": user_id, "username": username, "iat": int(time.time())})


def make_access_token(user: dict) -> str:
    now = int(time.time())
    return _sign({"userId": user["id"], "username": user["username"], "type": "access", "iat": now, "exp": now + ACCESS_TOKEN_TTL})


def make_download_token(user: dict, item_id: str, ttl: int = DOWNLOAD_TOKEN_TTL) -> str:
    """HMAC/JWT scoped to one item for short-lived download URLs (prefer over ?token=)."""
    now = int(time.time())
    return _sign({
        "userId": user["id"], "username": user["username"], "type": "download",
        "itemId": item_id, "iat": now, "exp": now + ttl,
    })


def user_from_download_token(token: str, item_id: str) -> dict | None:
    payload = decode(token)
    if not payload or payload.get("type") != "download":
        return None
    if payload.get("itemId") != item_id:
        return None
    return db.q1("SELECT * FROM users WHERE id = ? AND is_active = 1", (payload.get("userId"),))


def make_refresh_token(user: dict, user_agent: str | None = None) -> str:
    now = int(time.time())
    jti = secrets.token_hex(16)
    db.insert("refresh_tokens", {
        "id": jti, "user_id": user["id"], "expires_at": now + REFRESH_TOKEN_TTL, "created_at": now, "user_agent": (user_agent or "")[:200],
    })
    return _sign({"userId": user["id"], "username": user["username"], "type": "refresh", "jti": jti, "iat": now, "exp": now + REFRESH_TOKEN_TTL})


def rotate_refresh_token(token: str, user_agent: str | None = None) -> tuple[dict, str, str] | None:
    """Validate a refresh token and issue a new access + refresh pair."""
    payload = decode(token)
    if not payload or payload.get("type") != "refresh":
        return None
    row = db.q1("SELECT * FROM refresh_tokens WHERE id = ?", (payload.get("jti"),))
    if not row or row["expires_at"] < time.time():
        return None
    user = db.q1("SELECT * FROM users WHERE id = ? AND is_active = 1", (row["user_id"],))
    if not user:
        return None
    db.ex("DELETE FROM refresh_tokens WHERE id = ?", (row["id"],))
    return user, make_access_token(user), make_refresh_token(user, user_agent)


def revoke_refresh_token(token: str) -> None:
    payload = decode(token)
    if payload and payload.get("jti"):
        db.ex("DELETE FROM refresh_tokens WHERE id = ?", (payload["jti"],))


def make_api_key(user: dict, name: str, expires_in: int | None = None) -> tuple[str, dict]:
    now = int(time.time())
    key_id = db.new_id()
    row = {"id": key_id, "user_id": user["id"], "name": name, "created_at": now * 1000,
           "expires_at": (now + expires_in) * 1000 if expires_in else None, "last_used": None}
    db.insert("api_keys", row)
    payload = {"keyId": key_id, "userId": user["id"], "username": user["username"], "type": "api", "iat": now}
    if expires_in:
        payload["exp"] = now + expires_in
    return _sign(payload), row


def user_from_token(token: str) -> dict | None:
    payload = decode(token)
    if not payload:
        return None
    kind = payload.get("type")
    if kind in ("refresh", "download"):
        return None
    user = db.q1("SELECT * FROM users WHERE id = ? AND is_active = 1", (payload.get("userId"),))
    if not user:
        return None
    if kind == "api":
        key = db.q1("SELECT * FROM api_keys WHERE id = ?", (payload.get("keyId"),))
        if not key:
            return None
        expires = key.get("expires_at")
        if expires is not None and int(expires) < db.now_ms():
            return None
        db.ex("UPDATE api_keys SET last_used = ? WHERE id = ?", (db.now_ms(), key["id"]))
    elif kind is None:
        # Legacy token: must match the token currently stored on the user.
        if not user.get("token") or not hmac.compare_digest(user["token"], token):
            return None
    return user


def auth_via_cookie(request: Request) -> bool:
    """True when the request is authenticated only via the access_token cookie (no Bearer)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return False
    if request.query_params.get("token"):
        return False
    return bool(request.cookies.get("access_token"))


def token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    token = request.query_params.get("token")
    if token:
        return token
    # First-party web UI: httpOnly access cookie
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(request: Request) -> bool:
    """Double-submit CSRF check for cookie-authenticated mutating requests."""
    cookie = request.cookies.get("csrf_token") or ""
    header = request.headers.get("x-csrf-token") or ""
    return bool(cookie and header and hmac.compare_digest(cookie, header))


def rotate_legacy_token(user_id: str, username: str) -> str:
    token = legacy_token_for(user_id, username)
    db.ex("UPDATE users SET token = ? WHERE id = ?", (token, user_id))
    return token


def revoke_user_sessions(user_id: str) -> None:
    db.ex("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
    user = db.q1("SELECT id, username FROM users WHERE id = ?", (user_id,))
    if user:
        rotate_legacy_token(user["id"], user["username"])


# ---------------------------------------------------------------- dependencies

def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    token = token_from_request(request)
    user = user_from_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    request.state.user = user
    return user


def current_user_for_download(request: Request, item_id: str) -> dict:
    """Auth for download endpoints: cookie/Bearer/API key, or short-lived ?dl= token."""
    dl = request.query_params.get("dl")
    if dl:
        user = user_from_download_token(dl, item_id)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        request.state.user = user
        return user
    return current_user(request)


def is_admin(user: dict) -> bool:
    return user["type"] in ("root", "admin")


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def permissions(user: dict) -> dict:
    if is_admin(user):
        return dict(ADMIN_PERMISSIONS)
    return DEFAULT_PERMISSIONS | (db.jload(user["permissions"], {}) or {})


def require_permission(user: dict, name: str) -> None:
    if not permissions(user).get(name):
        raise HTTPException(status_code=403, detail=f"Missing permission: {name}")


def can_access_library(user: dict, library_id: str) -> bool:
    perms = permissions(user)
    if perms.get("accessAllLibraries"):
        return True
    return library_id in (db.jload(user["libraries_accessible"], []) or [])


def create_user(username: str, password: str, user_type: str = "user", perms: dict | None = None, libraries: list | None = None) -> dict:
    user_id = db.new_id()
    row = {
        "id": user_id,
        "username": username,
        "password_hash": hash_password(password),
        "type": user_type,
        "token": legacy_token_for(user_id, username),
        "is_active": 1,
        "permissions": db.jdump(perms or (ADMIN_PERMISSIONS if user_type in ("root", "admin") else DEFAULT_PERMISSIONS)),
        "libraries_accessible": db.jdump(libraries or []),
        "created_at": db.now_ms(),
        "last_seen": None,
    }
    db.insert("users", row)
    return row
