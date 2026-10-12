"""Server status, login, token refresh and /api/authorize."""

import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ABS_COMPAT_VERSION, __version__
from ..catalog import library, serialize
from ..core import auth, db
from ..core.config import ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL

router = APIRouter()

_login_attempts: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < 600]
    _login_attempts[ip] = attempts
    return len(attempts) >= 20


def default_library_id(user: dict) -> str | None:
    for lib in library.all_libraries():
        if auth.can_access_library(user, lib["id"]):
            return lib["id"]
    return None


def login_payload(user: dict) -> dict:
    return {
        "user": serialize.user_json(user),
        "userDefaultLibraryId": default_library_id(user) or "",
        "serverSettings": serialize.server_settings_json(),
        "ereaderDevices": [],
        "Source": "docker",
    }


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_session_cookies(resp: JSONResponse, request: Request, access: str, refresh: str | None = None) -> str:
    """Set httpOnly access (+ optional refresh) and a readable CSRF cookie. Returns csrf token."""
    secure = _secure(request)
    csrf = auth.new_csrf_token()
    resp.set_cookie("access_token", access, max_age=ACCESS_TOKEN_TTL, httponly=True, samesite="lax",
                    secure=secure, path="/")
    if refresh is not None:
        resp.set_cookie("refresh_token", refresh, max_age=REFRESH_TOKEN_TTL, httponly=True, samesite="lax",
                        secure=secure, path="/")
    resp.set_cookie("csrf_token", csrf, max_age=ACCESS_TOKEN_TTL, httponly=False, samesite="lax",
                    secure=secure, path="/")
    return csrf


def _clear_session_cookies(resp: JSONResponse) -> None:
    for name in ("access_token", "refresh_token", "csrf_token"):
        resp.delete_cookie(name, path="/")


def _set_refresh_cookie(resp: JSONResponse, request: Request, token: str) -> None:
    resp.set_cookie("refresh_token", token, max_age=REFRESH_TOKEN_TTL, httponly=True, samesite="lax",
                    secure=_secure(request), path="/")


@router.get("/status")
def status():
    has_root = db.scalar("SELECT COUNT(*) FROM users WHERE type = 'root'") > 0
    return {
        "app": "audiobookshelf",
        "appName": "Audivault",
        "appVersion": __version__,
        "serverVersion": ABS_COMPAT_VERSION,
        "isInit": has_root,
        "language": "en-us",
        "authMethods": ["local"],
        "authFormData": {"authLoginCustomMessage": None, "authOpenIDButtonText": "Login with OpenId", "authOpenIDAutoLaunch": False},
    }


@router.get("/ping")
def ping():
    return {"success": True}


@router.get("/healthcheck")
def healthcheck():
    return "OK"


@router.post("/init")
def init_server(body: dict = Body(...)):
    """First-run setup: create the root user."""
    if db.scalar("SELECT COUNT(*) FROM users WHERE type = 'root'"):
        raise HTTPException(500, "Server already initialized")
    root = body.get("newRoot") or body
    username = (root.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "Username required")
    try:
        auth.create_user(username, root.get("password") or "", "root")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True}


@router.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        raise HTTPException(429, "Too many login attempts. Try again later.")
    try:
        body = await request.json()
    except ValueError:
        form = await request.form()
        body = dict(form)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = db.q1("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    if not user or not auth.verify_password(password, user["password_hash"]):
        _login_attempts.setdefault(ip, []).append(time.time())
        raise HTTPException(401, "Invalid username or password")
    db.ex("UPDATE users SET last_seen = ? WHERE id = ?", (db.now_ms(), user["id"]))
    access = auth.make_access_token(user)
    refresh = auth.make_refresh_token(user, request.headers.get("user-agent"))
    payload = login_payload(user)
    return_tokens = request.headers.get("x-return-tokens") == "true"
    if return_tokens:
        # ABS / API clients: tokens in the body, no access cookie
        payload["user"]["accessToken"] = access
        payload["user"]["refreshToken"] = refresh
        resp = JSONResponse(payload)
        _set_refresh_cookie(resp, request, refresh)
    else:
        # First-party web UI: httpOnly cookies; do not put the access token in JS-readable storage
        payload["user"]["accessToken"] = None
        payload["user"]["refreshToken"] = None
        resp = JSONResponse(payload)
        _set_session_cookies(resp, request, access, refresh)
    return resp


@router.post("/auth/refresh")
def refresh(request: Request):
    token = request.headers.get("x-refresh-token")
    from_header = bool(token)
    token = token or request.cookies.get("refresh_token")
    if not token:
        return JSONResponse({"error": "No refresh token provided"}, status_code=401)
    result = auth.rotate_refresh_token(token, request.headers.get("user-agent"))
    if not result:
        return JSONResponse({"error": "Invalid refresh token"}, status_code=401)
    user, access, new_refresh = result
    payload = login_payload(user)
    if from_header:
        payload["user"]["accessToken"] = access
        payload["user"]["refreshToken"] = new_refresh
        resp = JSONResponse(payload)
        _set_refresh_cookie(resp, request, new_refresh)
    else:
        payload["user"]["accessToken"] = None
        payload["user"]["refreshToken"] = None
        resp = JSONResponse(payload)
        _set_session_cookies(resp, request, access, new_refresh)
    return resp


@router.post("/logout")
def logout(request: Request):
    token = request.headers.get("x-refresh-token") or request.cookies.get("refresh_token")
    if token:
        auth.revoke_refresh_token(token)
    resp = JSONResponse({"success": True})
    _clear_session_cookies(resp)
    return resp


@router.post("/api/authorize")
def authorize(request: Request):
    user = auth.current_user(request)
    db.ex("UPDATE users SET last_seen = ? WHERE id = ?", (db.now_ms(), user["id"]))
    payload = login_payload(user)
    # Cookie sessions should not re-expose a long-lived access token to JS
    if auth.auth_via_cookie(request):
        payload["user"]["accessToken"] = None
    return payload
