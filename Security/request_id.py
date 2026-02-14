"""
REQUEST ID
==========
Attach a unique request id for traceability.
"""

# FLOW:
# - Middleware sets/echoes x-request-id for every request.
# WHY:
# - Helps correlate logs across services.
# HOW:
# - Adds a UUID per request and returns it in response headers.

from __future__ import annotations

import uuid
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response
