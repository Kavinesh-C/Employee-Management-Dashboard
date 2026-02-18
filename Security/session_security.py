"""
SESSION SECURITY
================
Encrypted, HttpOnly sessions with expiration and regeneration support.

FLOW:
- Middleware decrypts cookie into request.session.
- On response, session is encrypted back into cookie.
- Helpers manage login/logout and timing.

WHY:
- Protects session data from client-side tampering.

HOW:
- Encrypts session payload with Fernet and sets HttpOnly/Secure flags.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Dict
import os

from cryptography.fernet import Fernet, InvalidToken
from starlette.middleware.base import BaseHTTPMiddleware


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fingerprint(user_agent: str | None, ip: str | None) -> str:
    raw = f"{user_agent or ''}|{ip or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EncryptedSessionMiddleware(BaseHTTPMiddleware):
    """
    Encrypted session cookie middleware.

    - Encrypts session data with Fernet (AES in CBC + HMAC)
    - Sets HttpOnly and Secure flags
    - Supports absolute and idle session expiration
    - Optional session fingerprint validation
    """

    def __init__(
        self,
        app,
        secret_key: str,
        cookie_name: str = "session",
        max_age_seconds: int = 60 * 60 * 8,
        idle_timeout_seconds: int = 60 * 30,
        https_only: bool = True,
        same_site: str = "lax",
        domain: str | None = None,
        path: str = "/",
        enforce_fingerprint: bool = True,
    ):
        super().__init__(app)
        self.cookie_name = cookie_name
        self.max_age_seconds = max_age_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.https_only = https_only
        self.same_site = same_site
        self.domain = domain
        self.path = path
        self.enforce_fingerprint = enforce_fingerprint
        self.allow_insecure_localhost = os.getenv("ALLOW_INSECURE_LOCALHOST", "true").lower() == "true"
        self.fernet = Fernet(_derive_fernet_key(secret_key))

    async def dispatch(self, request, call_next):
        now = int(time.time())
        session: Dict[str, Any] = {}
        created = now
        last_seen = now
        expired = False

        cookie = request.cookies.get(self.cookie_name)
        if cookie:
            try:
                payload = self.fernet.decrypt(cookie.encode("utf-8"))
                data = json.loads(payload.decode("utf-8"))
                session = data.get("data", {})
                created = int(data.get("iat", now))
                last_seen = int(data.get("last", now))
                exp = int(data.get("exp", created + self.max_age_seconds))

                if self.max_age_seconds and now > exp:
                    expired = True
                if self.idle_timeout_seconds and (now - last_seen) > self.idle_timeout_seconds:
                    expired = True

                if self.enforce_fingerprint and session:
                    expected = session.get("_fp")
                    current = _fingerprint(
                        request.headers.get("user-agent"),
                        request.client.host if request.client else None,
                    )
                    if expected and expected != current:
                        expired = True
            except (InvalidToken, ValueError, TypeError):
                expired = True

        if expired:
            session = {}

        request.scope["session"] = session

        response = await call_next(request)

        session = request.scope.get("session", {})
        if not session:
            response.delete_cookie(self.cookie_name, path=self.path, domain=self.domain)
            return response

        created = int(session.get("_created", created))
        session.setdefault("_created", created)
        session.setdefault("_sid", secrets.token_urlsafe(32))
        session["_last_seen"] = now
        if self.enforce_fingerprint and "_fp" not in session:
            session["_fp"] = _fingerprint(
                request.headers.get("user-agent"),
                request.client.host if request.client else None,
            )

        exp = created + self.max_age_seconds if self.max_age_seconds else None
        data = {
            "data": session,
            "iat": created,
            "last": now,
            "exp": exp,
        }
        token = self.fernet.encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")

        secure_flag = self.https_only
        if self.allow_insecure_localhost:
            client_host = request.client.host if request.client else ""
            if client_host in {"127.0.0.1", "::1", "localhost"}:
                secure_flag = False

        response.set_cookie(
            self.cookie_name,
            token,
            max_age=self.max_age_seconds,
            httponly=True,
            secure=secure_flag,
            samesite=self.same_site,
            domain=self.domain,
            path=self.path,
        )
        return response


def initialize_session(request, user_id: int) -> None:
    """Create a new session on login (regenerates session id)."""
    session = request.session
    session.clear()
    session["user_id"] = user_id
    session["_sid"] = secrets.token_urlsafe(32)
    session["_created"] = int(time.time())
    session["_last_seen"] = int(time.time())
    session["_fp"] = _fingerprint(
        request.headers.get("user-agent"),
        request.client.host if request.client else None,
    )


def regenerate_session(request) -> None:
    """Rotate session identifier for hijacking protection."""
    session = request.session
    session["_sid"] = secrets.token_urlsafe(32)
    session["_created"] = int(time.time())


def clear_session(request) -> None:
    request.session.clear()


def get_session_timing(
    request,
    max_age_seconds: int,
    idle_timeout_seconds: int,
) -> Dict[str, int | None]:
    """
    Return session timing details in seconds.

    Keys:
      - expires_at
      - idle_expires_at
      - remaining
      - idle_remaining
    """
    now = int(time.time())
    session = request.session
    created = int(session.get("_created", now))
    last_seen = int(session.get("_last_seen", now))

    expires_at = created + max_age_seconds if max_age_seconds else None
    idle_expires_at = last_seen + idle_timeout_seconds if idle_timeout_seconds else None

    remaining = expires_at - now if expires_at else None
    idle_remaining = idle_expires_at - now if idle_expires_at else None

    return {
        "expires_at": expires_at,
        "idle_expires_at": idle_expires_at,
        "remaining": max(0, remaining) if remaining is not None else None,
        "idle_remaining": max(0, idle_remaining) if idle_remaining is not None else None,
    }
