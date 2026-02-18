"""
CSRF PROTECTION
===============
CSRF token middleware with session storage and cookie for form injection.

FLOW:
- Generate token on session.
- Validate token on state-changing requests.
- Set csrf_token cookie for form injection.

WHY:
- Prevents forged cross-site form submissions.

HOW:
- Verifies header/form token matches session token.
"""

from __future__ import annotations

import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from Security.metrics import increment_feature_event


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        cookie_name: str = "csrf_token",
        enabled: bool = True,
        exempt_paths: list[str] | None = None,
    ):
        super().__init__(app)
        self.cookie_name = cookie_name
        self.enabled = enabled
        self.exempt_paths = exempt_paths or []

    async def dispatch(self, request, call_next):
        if not self.enabled:
            return await call_next(request)

        session = request.scope.get("session", {})
        token = session.get("_csrf")
        if not token:
            cookie_token = request.cookies.get(self.cookie_name)
            if cookie_token:
                token = cookie_token
                session["_csrf"] = token
                request.scope["session"] = session
            else:
                token = secrets.token_urlsafe(32)
                session["_csrf"] = token
                request.scope["session"] = session

        if request.method not in SAFE_METHODS:
            path = request.url.path
            if any(path == p or path.startswith(p) for p in self.exempt_paths):
                return await call_next(request)
            form = None
            try:
                if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                    form = await request.form()
            except Exception:
                form = None

            header_token = request.headers.get("x-csrf-token")
            form_token = form.get("csrf_token") if form else None
            if not header_token and not form_token:
                increment_feature_event("csrf")
                return JSONResponse({"detail": "CSRF token missing"}, status_code=403)
            if (header_token or form_token) != token:
                increment_feature_event("csrf")
                return JSONResponse({"detail": "CSRF token invalid"}, status_code=403)

        response = await call_next(request)
        response.set_cookie(self.cookie_name, token, httponly=False, samesite="lax")
        return response
