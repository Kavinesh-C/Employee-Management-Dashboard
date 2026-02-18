"""
ERROR HANDLING SECURITY
=======================
Return generic error messages to avoid data leakage.
"""

# FLOW:
# - Register handlers to mask error details in responses.
# WHY:
# - Avoids leaking stack traces and internal data.
# HOW:
# - Returns generic error messages for 4xx/5xx.

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


def register_error_handlers(app, templates=None, unauthorized_template: str = "auth/401.html"):
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        accepts_html = "text/html" in (request.headers.get("accept") or "")
        if templates and accepts_html and exc.status_code == 401:
            return templates.TemplateResponse(unauthorized_template, {"request": request}, status_code=401)
        if exc.status_code >= 500:
            return JSONResponse({"detail": "An error occurred"}, status_code=exc.status_code)
        return JSONResponse({"detail": "Request failed"}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse({"detail": "An error occurred"}, status_code=500)
