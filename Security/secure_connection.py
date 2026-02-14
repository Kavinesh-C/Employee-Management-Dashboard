"""
SECURE CONNECTION ENFORCEMENT
=============================
Rejects insecure HTTP requests when HTTPS is required.
"""

# FLOW:
# - Middleware rejects non-HTTPS when enabled.
# WHY:
# - Prevents accidental insecure HTTP usage.
# HOW:
# - Checks scheme/x-forwarded-proto and blocks HTTP.

from __future__ import annotations

import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from Security.metrics import increment_feature_event


class BlockInsecureRequestsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.allow_insecure_localhost = os.getenv("ALLOW_INSECURE_LOCALHOST", "true").lower() == "true"

    async def dispatch(self, request, call_next):
        if not self.enabled:
            return await call_next(request)
        if self.allow_insecure_localhost:
            client_host = request.client.host if request.client else ""
            if client_host in {"127.0.0.1", "::1", "localhost"}:
                return await call_next(request)
        forwarded_proto = request.headers.get("x-forwarded-proto")
        scheme = forwarded_proto or request.url.scheme
        if scheme != "https":
            increment_feature_event("secure-connection")
            return JSONResponse({"detail": "Insecure connection"}, status_code=400)
        return await call_next(request)
