"""
INPUT LENGTH LIMITS
===================
Middleware to reject oversized payloads.
"""

# FLOW:
# - Reject requests exceeding max_bytes before parsing.
# WHY:
# - Mitigates large payload attacks and memory abuse.
# HOW:
# - Checks Content-Length header before request processing.

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int = 1024 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_bytes:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        return await call_next(request)
