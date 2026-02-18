from __future__ import annotations

import traceback
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .app_context import templates


def _is_html_page_request(request: Request) -> bool:
    if request.url.path.startswith("/api"):
        return False
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _error_title(status_code: int) -> str:
    if status_code == 400:
        return "Bad request"
    if status_code == 401:
        return "Authentication required"
    if status_code == 403:
        return "Access denied"
    if status_code == 404:
        return "Page not found"
    if status_code == 405:
        return "Method not allowed"
    if status_code >= 500:
        return "Internal server error"
    return "Request failed"


def _error_reason(status_code: int) -> str:
    if status_code == 400:
        return "The request data was invalid or incomplete."
    if status_code == 401:
        return "Your session is missing, expired, or invalid."
    if status_code == 403:
        return "You do not have permission to access this resource."
    if status_code == 404:
        return "The URL does not match any existing route or the resource was removed."
    if status_code == 405:
        return "This endpoint exists, but it does not allow this HTTP method."
    if status_code >= 500:
        return "The server hit an unexpected condition while processing your request."
    return "The request could not be completed."


def _detail_from_exc(exc: Any, fallback: str) -> str:
    raw = getattr(exc, "detail", None)
    if isinstance(raw, str) and raw.strip():
        return raw
    if raw is not None:
        return str(raw)
    return fallback


def _detail_from_validation(exc: RequestValidationError) -> str:
    errors = exc.errors() or []
    if not errors:
        return "Request validation failed."
    first = errors[0]
    field = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    msg = first.get("msg") or "Invalid input."
    if field:
        return f"{field}: {msg}"
    return msg


def _render_error_page(request: Request, status_code: int, detail: str, reason: str):
    return templates.TemplateResponse(
        "common/error_modal.html",
        {
            "request": request,
            "status_code": status_code,
            "path": request.url.path,
            "detail": detail,
            "error_title": _error_title(status_code),
            "error_reason": reason,
        },
        status_code=status_code,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        if _is_html_page_request(request):
            status_code = 422
            reason = "The submitted form data is invalid."
            detail = _detail_from_validation(exc)
            return _render_error_page(request, status_code, detail, reason)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(HTTPException)
    async def fastapi_http_exception_handler(request: Request, exc: HTTPException):
        if _is_html_page_request(request):
            status_code = exc.status_code
            reason = _error_reason(status_code)
            detail = _detail_from_exc(exc, reason)
            return _render_error_page(request, status_code, detail, reason)
        return await http_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
        if _is_html_page_request(request):
            status_code = exc.status_code
            reason = _error_reason(status_code)
            detail = _detail_from_exc(exc, reason)
            return _render_error_page(request, status_code, detail, reason)
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        traceback.print_exc()
        if _is_html_page_request(request):
            status_code = 500
            reason = _error_reason(status_code)
            detail = f"{exc.__class__.__name__}: {str(exc) or 'Unhandled server exception'}"
            return _render_error_page(request, status_code, detail, reason)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
