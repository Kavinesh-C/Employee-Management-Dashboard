"""
HTTPS/TLS SECURITY
=================
Secure data transfer, TLS encryption in transit, and secure key exchange
configuration. Use in conjunction with a TLS-terminating server (uvicorn,
reverse proxy, or load balancer).

FLOW:
- HTTPSRedirectMiddleware forces HTTPS.
- SecurityHeadersMiddleware adds HSTS and security headers.
- create_ssl_context() builds TLS 1.2+ context.

WHY:
- Encrypts data in transit and prevents downgrade attacks.

HOW:
- Redirects HTTP to HTTPS and sets HSTS.
"""

from __future__ import annotations

import os
import ssl
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect HTTP to HTTPS using X-Forwarded-Proto when behind a proxy."""

    def __init__(self, app, https_port: int | None = None, enabled: bool = True):
        super().__init__(app)
        self.https_port = https_port
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
            url = request.url.replace(scheme="https")
            if self.https_port:
                url = url.replace(netloc=f"{url.hostname}:{self.https_port}")
            return RedirectResponse(url=str(url), status_code=307)

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers including HSTS for HTTPS deployments."""

    def __init__(
        self,
        app,
        hsts_enabled: bool = True,
        hsts_max_age: int = 31536000,
        include_subdomains: bool = True,
        preload: bool = False,
        headers_enabled: bool = True,
    ):
        super().__init__(app)
        self.hsts_enabled = hsts_enabled
        self.hsts_max_age = hsts_max_age
        self.include_subdomains = include_subdomains
        self.preload = preload
        self.headers_enabled = headers_enabled

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        if self.hsts_enabled:
            hsts = f"max-age={self.hsts_max_age}"
            if self.include_subdomains:
                hsts += "; includeSubDomains"
            if self.preload:
                hsts += "; preload"
            response.headers["Strict-Transport-Security"] = hsts

        if self.headers_enabled:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault(
                "Permissions-Policy",
                "geolocation=(), microphone=(), camera=()",
            )
        return response


def create_ssl_context(
    certfile: str,
    keyfile: str,
    cafile: str | None = None,
) -> ssl.SSLContext:
    """
    Create a hardened SSL context for TLS 1.2+ with modern ciphers.
    Supports ECDHE/DHE key exchange and AES-GCM encryption.
    """
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM")
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    if cafile:
        context.load_verify_locations(cafile=cafile)
    return context
