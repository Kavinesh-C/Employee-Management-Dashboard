from fastapi import Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .database import get_db
from .auth import authenticate_user
from .app_context import templates



def _redirect_for_role(role: str) -> str:
    if role == "admin":
        return "/admin/select_dashboard"
    if role == "manager":
        return "/manager/manage_teams"
    if role == "team_lead":
        return "/leader/dashboard"
    return "/employee"


def register_web_auth_routes(app):
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse("auth/login.html", {"request": request})

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: Session = Depends(get_db)
    ):
        user = authenticate_user(db, username, password)
        if not user:
            return templates.TemplateResponse(
                "auth/login.html",
                {"request": request, "error": "Invalid credentials"},
                status_code=401
            )

        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is inactive")

        request.session["user_id"] = user.id
        request.session["role"] = user.role
        return RedirectResponse(_redirect_for_role(user.role), status_code=303)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

