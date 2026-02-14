"""
XSS PROTECTION
==============
Security headers and CSP for mitigating cross-site scripting.
"""

# FLOW:
# - Middleware applies CSP and XSS-related headers to responses.
# WHY:
# - Mitigates script injection and clickjacking.
# HOW:
# - Applies CSP and restrictive headers on every response.

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware


class XSSProtectionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer-when-downgrade")
        
        # Development CSP - permissive to allow all styling and scripts
        csp = (
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https:; "
            "font-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "frame-ancestors 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        return response
