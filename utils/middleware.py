"""
Token validation, web auth session management.
"""

import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from config import API_TOKEN

# Session store: {token_hex: {"username": str, "expires_at": float}}
_web_sessions: dict = {}

# Secret key for HMAC signing (generated on startup)
_SESSION_SECRET: str = secrets.token_hex(32)


def _generate_session_token(username: str) -> str:
    """Generate an HMAC-signed session token."""
    raw = f"{username}:{time.time()}:{secrets.token_hex(16)}"
    signature = hmac.new(
        _SESSION_SECRET.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{raw}:{signature}"


def _verify_session_token(token: str) -> Optional[str]:
    """Verify HMAC-signed token. Returns username if valid, None otherwise."""
    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return None
        raw, signature = parts
        expected = hmac.new(
            _SESSION_SECRET.encode(),
            raw.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        user_parts = raw.split(":")
        if len(user_parts) < 2:
            return None
        return user_parts[0]
    except Exception:
        return None


def create_session(username: str, expires_seconds: int = 86400) -> str:
    """Create a new session for a user. Returns the session token."""
    token = _generate_session_token(username)
    _web_sessions[token] = {
        "username": username,
        "expires_at": time.time() + expires_seconds,
    }
    # Clean expired sessions
    _clean_expired_sessions()
    return token


def validate_session(token: str) -> Optional[str]:
    """Validate a session token. Returns username if valid, None otherwise."""
    if token in _web_sessions:
        session = _web_sessions[token]
        if time.time() < session["expires_at"]:
            return session["username"]
        else:
            del _web_sessions[token]
            return None
    # Try HMAC verification (for tokens from other instances / restarts)
    username = _verify_session_token(token)
    if username:
        _web_sessions[token] = {
            "username": username,
            "expires_at": time.time() + 86400,
        }
        return username
    return None


def destroy_session(token: str) -> None:
    """Remove a session."""
    _web_sessions.pop(token, None)


def _clean_expired_sessions() -> None:
    """Remove expired sessions."""
    now = time.time()
    expired = [t for t, s in _web_sessions.items() if now >= s["expires_at"]]
    for t in expired:
        del _web_sessions[t]


async def verify_token(request: Request):
    """Dependency: validate API token. Raises 403 if invalid."""
    token = request.query_params.get("token", "")
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")


async def login_required(request: Request):
    """Dependency: check web session cookie. Redirects to /login if not authenticated.
    
    When enable_web_auth is disabled, skip all authentication and allow direct access.
    """
    from config import DYNAMIC_SETTINGS
    enable_web_auth = DYNAMIC_SETTINGS.get("enable_web_auth", "1")
    if enable_web_auth != "1":
        return "admin"  # Allow access without auth
    
    session_token = request.cookies.get("session_token", "")
    if not session_token:
        return RedirectResponse(url="/login", status_code=302)
    username = validate_session(session_token)
    if not username:
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie("session_token")
        return response
    return username  # Return username for route handlers that need it


async def verify_web_password(password: Optional[str] = None) -> bool:
    """Check web panel password against DB."""
    from db.models import verify_user as db_verify
    from config import WEB_USERNAME
    if not password:
        return False
    return await db_verify(WEB_USERNAME, password)
