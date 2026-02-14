"""
AUDIT TRAIL
===========
Lightweight audit logging helper.
"""

# FLOW:
# - Call audit() on sensitive actions to emit structured audit events.
# WHY:
# - Provides accountability for critical actions.
# HOW:
# - Emits structured log lines for review.

from __future__ import annotations

import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler
from Security.metrics import increment_feature_event
from Security.security_config import feature_enabled


logger = logging.getLogger("security.audit")
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler("logs/audit.log", maxBytes=2_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

_audit_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("audit_ctx", default=None)


def _client_ip(request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    xrip = (request.headers.get("x-real-ip") or "").strip()
    if xrip:
        return xrip
    if request.client and request.client.host:
        return request.client.host
    return "-"


def set_audit_request_context(request):
    request_id = getattr(request.state, "request_id", "") or request.headers.get("x-request-id", "")
    payload = {
        "ip": _client_ip(request),
        "request_id": str(request_id or "").strip(),
        "path": str(request.url.path or "").strip(),
        "method": str(request.method or "").strip(),
    }
    return _audit_ctx.set(payload)


def clear_audit_request_context(token) -> None:
    _audit_ctx.reset(token)


def audit(event: str, user_id: int | None = None, details: str | None = None) -> None:
    if not feature_enabled("audit-trail", True):
        return
    ctx = _audit_ctx.get() or {}
    logger.info(
        "event=%s user_id=%s ip=%s request_id=%s method=%s path=%s details=%s",
        event,
        user_id,
        ctx.get("ip", "-"),
        ctx.get("request_id", ""),
        ctx.get("method", ""),
        ctx.get("path", ""),
        details or "",
    )
    increment_feature_event("audit-trail")
