"""
ACTIVITY TRACKING
=================
Structured request logging for monitoring.

FLOW:
- Middleware logs each request with user/session context.
- Added to the FastAPI middleware stack in main.py.

WHY:
- Provides traceability for security audits and incident response.

HOW:
- Writes structured request logs to logs/security.log.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from starlette.middleware.base import BaseHTTPMiddleware
from Security.secrets_redaction import redact
from Security.metrics import increment_feature_event


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("security.activity")
    if logger.handlers:
        return logger

    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler("logs/security.log", maxBytes=2_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


class ActivityLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.logger = _get_logger()

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        increment_feature_event("activity-logging")
        session = request.scope.get("session", {})
        user_id = session.get("user_id")
        request_id = getattr(request.state, "request_id", None)
        query = request.url.query
        if query:
            query = redact(query)
        self.logger.info(
            "method=%s path=%s query=%s status=%s user_id=%s request_id=%s ip=%s",
            request.method,
            request.url.path,
            query or "",
            response.status_code,
            user_id,
            request_id or "",
            request.client.host if request.client else "unknown",
        )
        return response
