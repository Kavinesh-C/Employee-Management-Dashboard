from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from .app_context import templates

router = APIRouter()

@router.get("/error/{status_code}", response_class=HTMLResponse)
async def custom_error_page(request: Request, status_code: int = 500, detail: str = None):
    """
    Custom error page route for testing or direct error display.
    Shows error_modal.html with explanation for the error code.
    """
    # Map status code to explanation
    explanations = {
        400: "Bad request: The request data was invalid or incomplete.",
        401: "Authentication required: Your session is missing, expired, or invalid.",
        403: "Access denied: You do not have permission to access this resource.",
        404: "Page not found: The URL does not match any existing route or the resource was removed.",
        405: "Method not allowed: This endpoint exists, but it does not allow this HTTP method.",
        500: "Internal server error: The server hit an unexpected condition while processing your request.",
    }
    reason = explanations.get(status_code, "Request failed: The request could not be completed.")
    return templates.TemplateResponse(
        "common/error_modal.html",
        {
            "request": request,
            "status_code": status_code,
            "path": request.url.path,
            "detail": detail or reason,
        },
        status_code=status_code,
    )
