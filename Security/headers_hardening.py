"""
HEADERS HARDENING
=================
Extra hardening headers.
"""

# FLOW:
# - Middleware adds strict cross-origin and permissions headers.
# WHY:
# - Reduces browser-based attack surface.
# HOW:
# - Adds COOP/COEP/CORP and Permissions-Policy headers.

from __future__ import annotations

import os
from starlette.middleware.base import BaseHTTPMiddleware


class HeadersHardeningMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if os.getenv("COOP_ENABLED", "true").lower() == "true":
            response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if os.getenv("CORP_ENABLED", "false").lower() == "true":
            response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if os.getenv("COEP_ENABLED", "false").lower() == "true":
            response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        return response
