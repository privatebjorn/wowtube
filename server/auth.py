"""
auth.py
Validates credentials against users.ini and manages in-memory session tokens.
Tokens are random hex strings mapped to usernames for the lifetime of the server.
"""

import secrets
import threading
import time
import logging
from typing import Optional

from .config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory session store  {token: {"username": str, "last_seen": float}}
# ---------------------------------------------------------------------------

_lock    = threading.Lock()
_sessions: dict[str, dict] = {}

SESSION_TIMEOUT_SECS = 3600   # 1 hour idle timeout


def validate_login(username: str, password: str) -> Optional[str]:
    """
    Check credentials. Returns a fresh session token on success, or None.
    """
    cfg = get_config()
    uname = username.lower().strip()
    user  = cfg.users.get(uname)

    if user is None:
        logger.warning("Login attempt for unknown user: %r", uname)
        return None

    if user.password != password:
        logger.warning("Bad password for user: %r", uname)
        return None

    token = secrets.token_hex(16)   # 32-char hex string

    with _lock:
        _sessions[token] = {
            "username":  uname,
            "last_seen": time.monotonic(),
        }

    logger.info("User %r logged in, token=%s", uname, token[:8] + "...")
    return token


def validate_token(token: str) -> Optional[str]:
    """
    Returns the username associated with a valid, non-expired token, else None.
    Also refreshes the last_seen timestamp.
    """
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return None

        age = time.monotonic() - session["last_seen"]
        if age > SESSION_TIMEOUT_SECS:
            del _sessions[token]
            logger.info("Token expired for user %r", session["username"])
            return None

        session["last_seen"] = time.monotonic()
        return session["username"]


def revoke_token(token: str) -> None:
    """Remove a session (on QUIT)."""
    with _lock:
        session = _sessions.pop(token, None)
        if session:
            logger.info("User %r logged out", session["username"])


def active_session_count() -> int:
    with _lock:
        return len(_sessions)


def purge_expired() -> None:
    """Reap old sessions. Call periodically from a maintenance thread."""
    now = time.monotonic()
    with _lock:
        expired = [
            tok for tok, s in _sessions.items()
            if now - s["last_seen"] > SESSION_TIMEOUT_SECS
        ]
        for tok in expired:
            logger.info("Purging expired session for %r", _sessions[tok]["username"])
            del _sessions[tok]