from fastapi import FastAPI, Depends, HTTPException, Request, Form, status, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from database import SessionLocal, get_db, engine, Base
from models import User, Attendance, RemovedEmployee, UnknownRFID, Room, Department, Task, LeaveRequest, TeamMember
from email_service import (
    send_welcome_email,
    send_leave_requested_email,
    send_leave_status_email,
    send_bulk_meeting_invites,
    smtp_enabled
)
from auth import authenticate_user, hash_password, verify_password
from sqlalchemy import func, extract, or_, inspect, text
from decimal import Decimal
import random
import string
import datetime
import hashlib
import secrets
import os
import csv
from urllib.parse import urlparse
from io import BytesIO
from pathlib import Path
from typing import Optional, List
from calendar import monthrange, month_name
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from team_scheduler import auto_assign_leaders
from threading import Thread
import time
from database import get_team_info
from apscheduler.schedulers.background import BackgroundScheduler
import chat_store
# Ensure these are imported from your models
from models import Team, User
from fastapi.exception_handlers import http_exception_handler
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent

scheduler = BackgroundScheduler()

# Importing all models 
from models import (
    User, Attendance, RemovedEmployee, UnknownRFID, Room, Department, 
    Task, LeaveRequest, Team, Project, ProjectTask, ProjectAssignment, 
    ProjectTaskAssignee, AttendanceLog, AttendanceDaily, AttendanceDate, Payroll, OfficeHoliday, Meeting, ProjectMeetingAssignee, MeetingAttendance, CalendarEvent, Notification
)
from models import EmailSettings
from team_scheduler import auto_assign_leaders
from calendar_routes import register_calendar_routes

# Setup
Base.metadata.create_all(bind=engine)


def create_notification(
    db: Session,
    user_id: int,
    title: str,
    message: str | None = None,
    notif_type: str | None = None,
    link: str | None = None
) -> None:
    db.add(Notification(
        user_id=user_id,
        title=title,
        message=message,
        notif_type=notif_type,
        link=link
    ))


def auto_sync_schema() -> None:
    """Create missing tables and add missing columns (no drops/changes)."""
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                table.create(bind=engine)

        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_columns:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                nullable = "NULL" if col.nullable else "NOT NULL"
                default_clause = ""
                if col.default is not None and hasattr(col.default, "arg"):
                    default_val = col.default.arg
                    if not callable(default_val):
                        if isinstance(default_val, str):
                            default_clause = f" DEFAULT '{default_val}'"
                        elif isinstance(default_val, bool):
                            default_clause = f" DEFAULT {1 if default_val else 0}"
                        elif default_val is None:
                            default_clause = ""
                        else:
                            default_clause = f" DEFAULT {default_val}"

                sql = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type} {nullable}{default_clause}"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                except Exception as exc:
                    print(f"Schema sync skipped {table.name}.{col.name}: {exc}")

            # Add missing indexes
            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name in existing_indexes:
                    continue
                cols = ", ".join(c.name for c in idx.columns)
                unique_clause = "UNIQUE " if idx.unique else ""
                sql = f"CREATE {unique_clause}INDEX {idx.name} ON {table.name} ({cols})"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                except Exception as exc:
                    print(f"Schema sync index skipped {table.name}.{idx.name}: {exc}")

            # Add missing unique constraints
            existing_unique = {u.get("name") for u in inspector.get_unique_constraints(table.name)}
            for constraint in table.constraints:
                if not getattr(constraint, "columns", None):
                    continue
                if constraint.__class__.__name__ != "UniqueConstraint":
                    continue
                cname = getattr(constraint, "name", None)
                if not cname or cname in existing_unique:
                    continue
                cols = ", ".join(c.name for c in constraint.columns)
                sql = f"ALTER TABLE {table.name} ADD CONSTRAINT {cname} UNIQUE ({cols})"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                except Exception as exc:
                    print(f"Schema sync unique skipped {table.name}.{cname}: {exc}")

            # Add missing foreign keys
            existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys(table.name)}
            for constraint in table.constraints:
                if constraint.__class__.__name__ != "ForeignKeyConstraint":
                    continue
                cname = getattr(constraint, "name", None)
                if not cname or cname in existing_fks:
                    continue
                if table.name in {"users", "teams"}:
                    continue
                local_cols = ", ".join(c.name for c in constraint.columns)
                remote_cols = ", ".join([f"{fk.column.table.name}.{fk.column.name}" for fk in constraint.elements])
                sql = f"ALTER TABLE {table.name} ADD CONSTRAINT {cname} FOREIGN KEY ({local_cols}) REFERENCES {remote_cols}"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                except Exception as exc:
                    print(f"Schema sync fk skipped {table.name}.{cname}: {exc}")
    except Exception as exc:
        print(f"Schema sync failed: {exc}")


def migrate_attendance_dates_csv() -> None:
    csv_path = BASE_DIR / "attendance_dates.csv"
    if not csv_path.exists():
        return
    db = SessionLocal()
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                raw_user_id = (row.get("user_id") or "").strip()
                raw_date = (row.get("date") or "").strip()
                if not raw_user_id or not raw_date:
                    continue
                try:
                    user_id = int(raw_user_id)
                    date_val = datetime.date.fromisoformat(raw_date)
                except Exception:
                    continue
                exists = db.query(AttendanceDate).filter(
                    AttendanceDate.user_id == user_id,
                    AttendanceDate.date == date_val
                ).first()
                if exists:
                    continue
                db.add(AttendanceDate(user_id=user_id, date=date_val))
            db.commit()
        try:
            csv_path.unlink()
        except Exception:
            pass
    except Exception as exc:
        db.rollback()
        print(f"Attendance CSV migration failed: {exc}")
    finally:
        db.close()


auto_sync_schema()
migrate_attendance_dates_csv()
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.add_middleware(SessionMiddleware, secret_key="super-secret-key")
scheduler = BackgroundScheduler()

# FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Session middleware (simple in-memory for demo; use proper sessions in prod)

app.add_middleware(SessionMiddleware, secret_key="your-secret-key")

# ----------------------------------------
# SESSION HANDLERS
# ----------------------------------------

def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# Register calendar routes (after get_current_user is defined)
register_calendar_routes(app, templates, get_current_user)


ENV_PATH = Path(".env")


def load_env_file(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def update_env_file(path: Path, updates: dict) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def calculate_monthly_payroll(db, emp, month, year):
    # First, check for an existing persisted payroll for this employee/month/year
    try:
        existing = db.query(Payroll).filter(
            Payroll.employee_id == emp.employee_id,
            Payroll.month == month,
            Payroll.year == year
        ).first()
    except Exception:
        existing = None

    if existing:
        return {
            "present_days": existing.present_days,
            "leave_days": existing.leave_days,
            "unpaid_leaves": existing.unpaid_leaves,
            "base_salary": round(existing.base_salary, 2),
            "leave_deduction": round(existing.leave_deduction, 2),
            "tax": round(existing.tax, 2),
            "allowances": round(existing.allowances or 0.0, 2),
            "deductions": round(existing.deductions or 0.0, 2),
            "net_salary": round(existing.net_salary, 2),
            "explanation": existing.explanation,
            "locked": bool(existing.locked),
            "generated_at": existing.created_at
        }

    # 1️⃣ Present days (from Attendance table)
    present_days = db.query(func.count(func.distinct(Attendance.date))).filter(
        Attendance.employee_id == emp.employee_id,
        extract("month", Attendance.date) == month,
        extract("year", Attendance.date) == year
    ).scalar() or 0

    # 2️⃣ Approved leaves
    leave_days = db.query(func.sum(
        func.datediff(LeaveRequest.end_date, LeaveRequest.start_date) + 1
    )).filter(
        LeaveRequest.employee_id == emp.employee_id,
        LeaveRequest.status == "Approved",
        or_(
            extract("month", LeaveRequest.start_date) == month,
            extract("month", LeaveRequest.end_date) == month
        ),
        extract("year", LeaveRequest.start_date) == year
    ).scalar() or 0

    # 3️⃣ Salary rules
    WORKING_DAYS = 22
    base_salary = Decimal(emp.base_salary or 0)
    tax_percentage = Decimal(emp.tax_percentage or 0)

    per_day_salary = base_salary / Decimal(WORKING_DAYS)

    unpaid_leaves = max(0, (leave_days or 0) - (emp.paid_leaves_allowed or 0))
    leave_deduction = Decimal(unpaid_leaves) * per_day_salary
    gross_salary = base_salary - leave_deduction
    tax = gross_salary * (tax_percentage / Decimal(100))
    allowances = Decimal(emp.allowances or 0)
    deductions = Decimal(emp.deductions or 0)
    net_salary = gross_salary - tax + allowances - deductions

    # Explanation text for UI
    base_salary_val = round(emp.base_salary or 0.0, 2)
    leave_deduction_val = round(leave_deduction, 2)
    tax_val = round(tax, 2)
    tax_percentage = emp.tax_percentage or 0.0

    explanation = f"""
Base Salary: ₹{base_salary_val}
Unpaid Leaves: {unpaid_leaves}
Leave Deduction: ₹{leave_deduction_val}
Tax ({tax_percentage}%): ₹{tax_val}
"""

    # Persist the generated payroll record so it's locked for future reads
    payroll_rec = Payroll(
        employee_id=emp.employee_id,
        month=month,
        year=year,
        present_days=present_days,
        leave_days=leave_days,
        unpaid_leaves=unpaid_leaves,
        base_salary=emp.base_salary or 0.0,
        leave_deduction=leave_deduction,
        tax=tax,
        allowances=allowances,
        deductions=deductions,
        net_salary=round(net_salary, 2),
        explanation=explanation,
        locked=True
    )
    try:
        db.add(payroll_rec)
        db.commit()
        db.refresh(payroll_rec)
    except Exception:
        db.rollback()

    return {
        "present_days": present_days,
        "leave_days": leave_days,
        "unpaid_leaves": unpaid_leaves,
        "base_salary": float(base_salary),
        "leave_deduction": float(leave_deduction),
        "tax": float(tax),
        "allowances": float(allowances),
        "deductions": float(deductions),
        "net_salary": float(net_salary),
        "explanation": explanation,
        "locked": True,
        "generated_at": payroll_rec.created_at if hasattr(payroll_rec, 'created_at') else None
    }

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Invalid credentials"})
    
    request.session["user_id"] = user.id

    # ROLE BASED REDIRECTS
    if user.role == "admin":
        return RedirectResponse("/admin/select_dashboard", status_code=303)
    elif user.role == "manager":
        return RedirectResponse("/employee", status_code=303)
    elif user.role == "team_lead":
        return RedirectResponse("/leader/dashboard", status_code=303)
    else:
        return RedirectResponse("/employee", status_code=303)

#----------------------------------------
# LOGOUT ROUTE
#----------------------------------------

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session") 
    return response

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Avoid caching for all protected app pages
    if not (
        path == "/" or
        path.startswith("/static") or
        path.startswith("/auth")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return templates.TemplateResponse("auth/401.html", {"request": request}, status_code=401)

    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if request.url.path.startswith("/api") or not wants_html:
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)

    return templates.TemplateResponse(
        "common/error_modal.html",
        {
            "request": request,
            "status_code": exc.status_code,
            "detail": str(exc.detail),
            "path": request.url.path,
        },
        status_code=exc.status_code
    )


@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if request.url.path.startswith("/api") or not wants_html:
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return templates.TemplateResponse(
        "common/error_modal.html",
        {
            "request": request,
            "status_code": 500,
            "detail": str(exc),
            "path": request.url.path,
        },
        status_code=500
    )


from chat_routes import router as chat_router
app.include_router(chat_router)

from auth_routes import router as auth_router
app.include_router(auth_router)


@app.get("/employee/chat", response_class=HTMLResponse)
async def employee_chat(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    members = (
        db.query(User)
        .filter(User.id != user.id)
        .all()
    )

    return templates.TemplateResponse(
        "employee/employee_chat.html",
        {
            "request": request,
            "user": user,
            "members": members
        }
    )

# ----------------------------------------
# ADMIN SELECT DASHBOARD
# ----------------------------------------

@app.get("/admin/select_dashboard", response_class=HTMLResponse)
async def admin_choice(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("admin/admin_select_dashboard.html", {"request": request, "user": user})

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(status_code=403)
    # ... (Your existing admin dashboard logic) ...
    # Placeholder return to prevent error in this snippet
    return templates.TemplateResponse("admin/admin_dashboard.html",
                                       {"request": request, 
                                        "user": user, 
                                        "blocks": [],
                                        "employees": [],
                                        "unknown_rfids": [], 
                                        "admins": [], 
                                        "removed_employees": []
                                        }
                                    )

#----------------------------------------
#ADMIN - REGISTER EMPLOYEE PAGE
#----------------------------------------

@app.get("/admin/register_employee", response_class=HTMLResponse)
async def admin_register_employee(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    departments = db.query(Department).all()
    teams = db.query(Team).order_by(Team.name.asc()).all()
    return templates.TemplateResponse("admin/admin_register_employee.html", {
        "request": request,
        "user": user,
        "departments": departments,
        "teams": teams,
    })
#----------------------------------------
# ADMIN - ADD EMPLOYEE ROUTE
#----------------------------------------

@app.post("/admin/add_employee")
async def add_employee(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: Optional[str] = Form(None),
    rfid_tag: str = Form(...),
    role: str = Form(...),
    department: str = Form(...),
    title: Optional[str] = Form(None),
    date_of_birth: Optional[str] = Form(None),
    hourly_rate: Optional[float] = Form(None),
    allowances: Optional[float] = Form(None),
    deductions: Optional[float] = Form(None),
    notes: Optional[str] = Form(None),
    team_id: Optional[int] = Form(None),
    is_active: Optional[str] = Form(None),
    can_manage: Optional[str] = Form(None),
    active_leader: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Check if name already exists
    existing_name = db.query(User).filter(User.name == name).first()
    if existing_name:
        raise HTTPException(status_code=400, detail=f"Name '{name}' already exists in the system")
    
    # Check if email already exists
    existing_email = db.query(User).filter(User.email == email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail=f"Email '{email}' already exists in the system")
    
    # Check if RFID tag already exists
    existing_rfid = db.query(User).filter(User.rfid_tag == rfid_tag).first()
    if existing_rfid:
        raise HTTPException(status_code=400, detail=f"RFID tag '{rfid_tag}' is already assigned to another employee")
    
    prefix = {"IT": "2261", "HR": "2262", "Finance": "2263"}.get(department, "2260")   # to create a defined id instead of manual entry
    max_emp = db.query(User).filter(User.employee_id.like(f"{prefix}%")).order_by(User.employee_id.desc()).first()
    if max_emp and len(max_emp.employee_id) > 4:
        try:
            suffix = int(max_emp.employee_id[4:])
            next_id = suffix + 1
        except ValueError:
            next_id = 1
    else:
        next_id = 1
    employee_id = f"{prefix}{next_id:03d}"
    # Generate password
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    password_hash = hash_password(password)
    dob_val = None
    if date_of_birth:
        dob_raw = date_of_birth.strip()
        try:
            dob_val = datetime.datetime.strptime(dob_raw, "%d-%m-%Y").date()
        except Exception:
            try:
                dob_val = datetime.date.fromisoformat(dob_raw)
            except Exception:
                dob_val = None

    photo_blob = None
    photo_mime = None
    if photo and photo.filename:
        photo_blob = await photo.read()
        photo_mime = photo.content_type or "image/jpeg"

    team_id_val = int(team_id) if team_id else None
    if team_id_val:
        team_exists = db.query(Team).filter(Team.id == team_id_val).first()
        if not team_exists:
            team_id_val = None

    new_user = User(
        employee_id=employee_id,
        name=name,
        email=email,
        phone=phone,
        rfid_tag=rfid_tag,
        role=role,
        department=department,
        password_hash=password_hash,
    )
    if title:
        new_user.title = title
    if dob_val:
        new_user.date_of_birth = dob_val
    if photo_blob:
        new_user.photo_blob = photo_blob
        new_user.photo_mime = photo_mime
    if notes:
        new_user.notes = notes
    if team_id_val:
        new_user.current_team_id = team_id_val
    if hourly_rate is not None:
        new_user.hourly_rate = hourly_rate
    if allowances is not None:
        new_user.allowances = allowances
    if deductions is not None:
        new_user.deductions = deductions
    new_user.is_active = True if is_active else False
    new_user.can_manage = True if can_manage else False
    new_user.active_leader = True if active_leader else False
    db.add(new_user)
    db.commit()
    email_sent = send_welcome_email(email, name, employee_id, password)
    return {"employee_id": employee_id, "password": password, "email_sent": email_sent}

#----------------------------------------
#ADMIN - Settings
#----------------------------------------

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    
    rooms = db.query(Room).all()
    departments = db.query(Department).all()
    
    return templates.TemplateResponse("admin/admin_settings.html", {
        "request": request, 
        "user": user, 
        "rooms": rooms, 
        "departments": departments
    })


@app.get("/admin/email_settings", response_class=HTMLResponse)
async def admin_email_settings_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    settings = db.query(EmailSettings).order_by(EmailSettings.id.desc()).first()
    return templates.TemplateResponse("admin/admin_email_settings.html", {
        "request": request,
        "user": user,
        "smtp_user": settings.smtp_user if settings else "",
        "smtp_from": settings.smtp_from if settings else "",
        "smtp_host": settings.smtp_host if settings and settings.smtp_host else "smtp.gmail.com",
        "smtp_port": settings.smtp_port if settings and settings.smtp_port else "465"
    })


@app.post("/admin/email_settings")
async def admin_email_settings_save(
    request: Request,
    smtp_user: str = Form(""),
    smtp_from: str = Form(""),
    smtp_pass: str = Form(""),
    smtp_host: str = Form("smtp.gmail.com"),
    smtp_port: str = Form("465"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    settings = db.query(EmailSettings).order_by(EmailSettings.id.desc()).first()
    if not settings:
        settings = EmailSettings()
        db.add(settings)

    settings.smtp_user = smtp_user.strip()
    settings.smtp_from = smtp_from.strip()
    settings.smtp_host = smtp_host.strip() or "smtp.gmail.com"
    settings.smtp_port = smtp_port.strip() or "465"
    if smtp_pass.strip():
        settings.smtp_pass = smtp_pass.strip()
    db.commit()

    return RedirectResponse("/admin/email_settings", status_code=303)

#-----------------------------------------
#ADMIN - REMOVE EMPLOYEE ROUTE
#-----------------------------------------

@app.post("/admin/remove_employee")
async def remove_employee(request: Request, employee_id: str = Form(...), user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    emp = db.query(User).filter(User.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    # Remove team memberships and leadership references before deleting the user.
    db.query(TeamMember).filter(TeamMember.user_id == emp.id).delete(synchronize_session=False)
    db.query(Team).filter(Team.leader_id == emp.id).update({Team.leader_id: None}, synchronize_session=False)
    db.query(Team).filter(Team.permanent_leader_id == emp.id).update(
        {Team.permanent_leader_id: None},
        synchronize_session=False,
    )
    # Move to removed
    removed = RemovedEmployee(employee_id=emp.employee_id, name=emp.name, email=emp.email, rfid_tag=emp.rfid_tag,
                              role=emp.role, department=emp.department)
    db.add(removed)
    db.delete(emp)
    db.commit()
    return RedirectResponse("/admin/manage_employees?removed=1", status_code=303)


@app.post("/admin/set_base_salary")
async def set_base_salary(
    employee_id: str = Form(...),
    base_salary: float = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "admin":
        raise HTTPException(status_code=403)

    emp = db.query(User).filter(User.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    emp.base_salary = base_salary
    db.commit()

    return RedirectResponse("/admin/manage_employees", status_code=303)

#-----------------------------------------
#ADMIN - MANAGE EMPLOYEE ROUTE
#-----------------------------------------

@app.get("/admin/manage_employees", response_class=HTMLResponse)
async def admin_manage_employees(request: Request,
                                 search: Optional[str] = None,
                                 department: Optional[str] = None,
                                 page: int = 1,
                                 user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    query = db.query(User).filter(User.is_active == True)
    if search:
        query = query.filter(
            (User.employee_id.like(f"%{search}%")) |
            (User.name.ilike(f"%{search}%"))
        )
    if department:
        query = query.filter(User.department == department)
    total_count = query.count()
    page_size = 8
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    employees = query.order_by(User.name.asc()).offset((page - 1) * page_size).limit(page_size).all()
    return templates.TemplateResponse("admin/admin_manage.html",{
        "request": request,
        "user": user,
        "employees": employees,
        "search": search,
        "department": department,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "page_size": page_size,
        "current_year": datetime.datetime.utcnow().year
        })

@app.post("/admin/update_employee")
async def admin_update_employee(request: Request,
                                 employee_id: str = Form(...),
                                 name: Optional[str] = Form(None),
                                 email: Optional[str] = Form(None),
                                 rfid_tag: Optional[str] = Form(None),
                                 title: Optional[str] = Form(None),
                                 date_of_birth: Optional[str] = Form(None),
                                 department: Optional[str] = Form(None),
                                 role: Optional[str] = Form(None),
                                 hourly_rate: Optional[float] = Form(None),
                                 allowances: Optional[float] = Form(None),
                                 deductions: Optional[float] = Form(None),
                                 notes: Optional[str] = Form(None),
                                 team_id: Optional[int] = Form(None),
                                 is_active: Optional[str] = Form(None),
                                 can_manage: Optional[str] = Form(None),
                                 active_leader: Optional[str] = Form(None),
                                 photo: Optional[UploadFile] = File(None),
                                 base_salary: Optional[float] = Form(None),
                                 paid_leaves_allowed: Optional[int] = Form(None),
                                 tax_percentage: Optional[float] = Form(None),
                                 user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    emp = db.query(User).filter(User.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Update editable fields if provided
    if name is not None:
        emp.name = name
    if email is not None:
        existing_email = db.query(User).filter(User.email == email, User.id != emp.id).first()
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already in use")
        emp.email = email
    if rfid_tag is not None:
        existing_rfid = db.query(User).filter(User.rfid_tag == rfid_tag, User.id != emp.id).first()
        if existing_rfid:
            raise HTTPException(status_code=400, detail="RFID tag already in use")
        emp.rfid_tag = rfid_tag
    if title is not None:
        emp.title = title
    if date_of_birth:
        dob_raw = date_of_birth.strip()
        try:
            emp.date_of_birth = datetime.datetime.strptime(dob_raw, "%d-%m-%Y").date()
        except Exception:
            try:
                emp.date_of_birth = datetime.date.fromisoformat(dob_raw)
            except Exception:
                pass
    if department is not None:
        emp.department = department
    if role is not None:
        emp.role = role
    if notes is not None:
        emp.notes = notes
    if team_id is not None:
        team_id_val = int(team_id) if str(team_id).isdigit() else None
        if team_id_val:
            team_exists = db.query(Team).filter(Team.id == team_id_val).first()
            emp.current_team_id = team_id_val if team_exists else None
        else:
            emp.current_team_id = None
    emp.is_active = True if is_active else False
    emp.can_manage = True if can_manage else False
    emp.active_leader = True if active_leader else False

    if photo and photo.filename:
        photo_blob = await photo.read()
        if photo_blob:
            emp.photo_blob = photo_blob
            emp.photo_mime = photo.content_type or "image/jpeg"

    # Payroll related fields
    try:
        if base_salary is not None:
            emp.base_salary = float(base_salary)
    except Exception:
        pass

    try:
        if paid_leaves_allowed is not None:
            emp.paid_leaves_allowed = int(paid_leaves_allowed)
    except Exception:
        pass

    try:
        if tax_percentage is not None:
            emp.tax_percentage = float(tax_percentage)
    except Exception:
        pass

    try:
        if hourly_rate is not None:
            emp.hourly_rate = float(hourly_rate)
    except Exception:
        pass

    try:
        if allowances is not None:
            emp.allowances = float(allowances)
    except Exception:
        pass

    try:
        if deductions is not None:
            emp.deductions = float(deductions)
    except Exception:
        pass

    db.commit()
    return RedirectResponse(url="/admin/manage_employees", status_code=303)


@app.get("/admin/edit_employee", response_class=HTMLResponse)
async def admin_edit_employee(request: Request, employee_id: str,
                              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    emp = db.query(User).filter(User.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    departments = db.query(Department).all()
    teams = db.query(Team).order_by(Team.name.asc()).all()
    return templates.TemplateResponse("admin/admin_edit_employee.html", {
        "request": request,
        "user": user,
        "employee": emp,
        "departments": departments,
        "teams": teams,
    })

# ----------------------------------------
# MANAGER TEAM MANAGEMENT ROUTES
# ----------------------------------------

@app.get("/manager/manage_teams", response_class=HTMLResponse)
async def manager_manage_teams(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "manager":
        raise HTTPException(status_code=403, detail="Access denied")
    
    teams = db.query(Team).all()
    employees = db.query(User).filter(User.is_active == True).all()
    projects = db.query(Project).filter(Project.department == user.department).all()
    
    # --- FIX: FETCH DEPARTMENTS ---
    departments = db.query(Department).all()
    
    # Calculate Project Status for Teams (Optional logic for the progress bar)
    team_data = []
    for t in teams:
        completion = 0
        projs = []
        if t.project_id:
            project = db.query(Project).filter(Project.id == t.project_id).first()
            if project:
                projs = [project]
        if not projs:
            projs = db.query(Project).filter(Project.department == t.department).all()
        if projs:
            total_tasks = sum([len(p.tasks) for p in projs])
            completed_tasks = sum([len([task for task in p.tasks if task.status == 'completed']) for p in projs])
            if total_tasks > 0:
                completion = int((completed_tasks / total_tasks) * 100)
        
        # Query members using current_team_id as single source of truth
        members = db.query(User).filter(User.current_team_id == t.id).all()
        
        team_data.append({
            "team": t,
            "completion": completion,
            "member_count": len(members),
            "members": members
        })

    return templates.TemplateResponse("admin/admin_manage_teams.html", {
        "request": request, 
        "user": user, 
        "team_data": team_data, 
        "employees": employees,
        "departments": departments,
        "projects": projects
    })

@app.get("/manager/team/{team_id}/members", response_class=HTMLResponse)
def view_team_members(team_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "manager":
        raise HTTPException(status_code=403, detail="Access denied")
    
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    members = db.query(User).filter(User.current_team_id == team_id).all()
    
    return templates.TemplateResponse(
        "admin/team_members.html",
        {
            "request": request,
            "user": user,
            "team": team,
            "members": members
        }
    )

@app.post("/manager/create_team")
async def create_team(
    name: str = Form(...),
    department: str = Form(...), # Accepts any string now
    leader_employee_id: str = Form(None),
    project_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager": raise HTTPException(status_code=403)
    
    leader_id = None
    leader = None
    if leader_employee_id:
        leader = db.query(User).filter(User.employee_id == leader_employee_id).first()
        if leader:
            leader_id = leader.id
            leader.can_manage = True

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Set both leader_id (active) and permanent_leader_id (original)
    new_team = Team(
        name=name,
        department=department,
        leader_id=leader_id,
        permanent_leader_id=leader_id,
        project_id=project.id
    )
    db.add(new_team)
    db.commit()
    
    # If leader exists, set their current_team_id
    if leader:
        leader.current_team_id = new_team.id
        create_notification(
            db,
            leader.id,
            "Team assigned",
            f"You are assigned as leader of team {new_team.name}.",
            "team",
            "/employee/team"
        )
        if project:
            create_notification(
                db,
                leader.id,
                "Project assigned",
                f"Project {project.name} is linked to your team.",
                "project",
                "/employee/team"
            )
        db.commit()

    return RedirectResponse("/manager/manage_teams", status_code=303)

@app.post("/manager/create_project")
async def manager_create_project(
    name: str = Form(...),
    department: str = Form(...),
    deadline: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    deadline_dt = None
    try:
        deadline_dt = datetime.datetime.strptime(deadline, "%Y-%m-%d")
    except Exception:
        deadline_dt = None

    new_project = Project(
        name=name,
        department=department,
        deadline=deadline_dt,
        start_date=datetime.datetime.utcnow(),
        status="active"
    )
    db.add(new_project)
    db.commit()

    return RedirectResponse("/manager/manage_teams", status_code=303)

@app.post("/manager/delete_team")
async def delete_team(
    team_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager": raise HTTPException(status_code=403)
    
    team = db.query(Team).filter(Team.id == team_id).first()
    if team:
        db.delete(team) # Cascades to team_members due to model setup
        db.commit()
        
    return RedirectResponse("/manager/manage_teams", status_code=303)

@app.post("/manager/assign_member")
async def assign_team_member(
    employee_id: str = Form(...),
    team_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager": raise HTTPException(status_code=403)

    emp = db.query(User).filter(User.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    emp.current_team_id = team_id
    team = db.query(Team).filter(Team.id == team_id).first()
    if team and team.project_id:
        existing_assignment = db.query(ProjectAssignment).filter(
            ProjectAssignment.project_id == team.project_id,
            ProjectAssignment.employee_id == emp.employee_id
        ).first()
        if not existing_assignment:
            db.add(ProjectAssignment(project_id=team.project_id, employee_id=emp.employee_id))
        project = db.query(Project).filter(Project.id == team.project_id).first()
        if project:
            create_notification(
                db,
                emp.id,
                "Project assigned",
                f"You have been assigned to project {project.name}.",
                "project",
                "/employee/team"
            )
    if team:
        create_notification(
            db,
            emp.id,
            "Team assigned",
            f"You have been added to team {team.name}.",
            "team",
            "/employee/team"
        )
    db.commit()

    return RedirectResponse("/manager/manage_teams", status_code=303)
# (Payroll update route removed)

#-----------------------------------------
#ADMIN - EMPLOYEE DETAILS ROUTE
#-----------------------------------------

@app.get("/admin/employee_details", response_class=HTMLResponse)
async def employee_details(request: Request, employee_id: Optional[str] = None, name: Optional[str] = None,
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    query = db.query(User).filter(User.is_active == True)
    if employee_id:
        query = query.filter(User.employee_id == employee_id)
    if name:
        query = query.filter(User.name.ilike(f"%{name}%"))
    emp = query.first()
    if not emp:
        return templates.TemplateResponse("admin/admin_employee_details.html", {
            "request": request,
            "user": user,
            "error": "Employee not found"
        })
    # Calculate total time (sum durations)
    total_time = db.query(Attendance).filter(Attendance.employee_id == emp.employee_id).with_entities(
        Attendance.duration).all()
    total_hours = sum(d[0] for d in total_time if d[0])
    latest_payroll = db.query(Payroll).filter(
        Payroll.employee_id == emp.employee_id
    ).order_by(Payroll.year.desc(), Payroll.month.desc()).first()
    payroll_amount = latest_payroll.net_salary if latest_payroll else None

    def _format_inr(value: float | None) -> str:
        if value is None:
            value = 0.0
        try:
            num = float(value)
        except Exception:
            num = 0.0
        whole, frac = f"{num:.2f}".split(".")
        if len(whole) <= 3:
            grouped = whole
        else:
            grouped = whole[-3:]
            whole = whole[:-3]
            while len(whole) > 2:
                grouped = whole[-2:] + "," + grouped
                whole = whole[:-2]
            if whole:
                grouped = whole + "," + grouped
        return f"{grouped}.{frac}"
    # Build tasks completed chart (last 12 months)
    today = datetime.date.today()
    month_labels = []
    month_keys = []
    for i in range(11, -1, -1):
        d = today.replace(day=1) - datetime.timedelta(days=30 * i)
        key = f"{d.year}-{d.month:02d}"
        label = d.strftime("%b")
        month_labels.append(label)
        month_keys.append(key)

    counts = {k: 0 for k in month_keys}
    done_statuses = {"done", "completed", "complete"}

    personal_tasks = db.query(Task).filter(
        Task.user_id == emp.employee_id,
        Task.status.in_(done_statuses)
    ).all()
    for t in personal_tasks:
        dt = getattr(t, "due_date", None) or getattr(t, "created_at", None)
        if not dt:
            continue
        if isinstance(dt, datetime.datetime):
            key = f"{dt.year}-{dt.month:02d}"
        elif isinstance(dt, datetime.date):
            key = f"{dt.year}-{dt.month:02d}"
        else:
            continue
        if key in counts:
            counts[key] += 1

    project_tasks = db.query(ProjectTask).join(ProjectTaskAssignee, ProjectTaskAssignee.task_id == ProjectTask.id).filter(
        ProjectTaskAssignee.employee_id == emp.employee_id,
        ProjectTask.status.in_(done_statuses)
    ).all()
    for pt in project_tasks:
        dt = getattr(pt, "deadline", None) or getattr(pt, "created_at", None)
        if not dt:
            continue
        if isinstance(dt, datetime.datetime):
            key = f"{dt.year}-{dt.month:02d}"
        elif isinstance(dt, datetime.date):
            key = f"{dt.year}-{dt.month:02d}"
        else:
            continue
        if key in counts:
            counts[key] += 1

    chart_counts = [counts[k] for k in month_keys]

    return templates.TemplateResponse("admin/admin_employee_details.html",
                                      {
                                          "request": request,
                                          "user": user,
                                          "employee": emp,
                                          "total_hours": total_hours,
                                          "payroll_amount": payroll_amount,
                                          "payroll_amount_inr": _format_inr(payroll_amount if payroll_amount is not None else (emp.base_salary or 0)),
                                          "hourly_rate_inr": _format_inr(emp.hourly_rate or 0),
                                          "allowances_inr": _format_inr(emp.allowances or 0),
                                          "deductions_inr": _format_inr(emp.deductions or 0),
                                          "task_chart_labels": month_labels,
                                          "task_chart_counts": chart_counts,
                                      })


@app.get("/admin/employee_details/print", response_class=HTMLResponse)
async def employee_details_print(request: Request, employee_id: str,
                                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    emp = db.query(User).filter(User.is_active == True, User.employee_id == employee_id).first()
    if not emp:
        return templates.TemplateResponse("admin/admin_employee_details_print.html", {
            "request": request,
            "user": user,
            "error": "Employee not found",
        })

    total_time = db.query(Attendance).filter(Attendance.employee_id == emp.employee_id).with_entities(
        Attendance.duration).all()
    total_hours = sum(d[0] for d in total_time if d[0])

    latest_payroll = db.query(Payroll).filter(
        Payroll.employee_id == emp.employee_id
    ).order_by(Payroll.year.desc(), Payroll.month.desc()).first()
    payroll_amount = latest_payroll.net_salary if latest_payroll else None

    def _format_inr(value: float | None) -> str:
        if value is None:
            value = 0.0
        try:
            num = float(value)
        except Exception:
            num = 0.0
        whole, frac = f"{num:.2f}".split(".")
        if len(whole) <= 3:
            grouped = whole
        else:
            grouped = whole[-3:]
            whole = whole[:-3]
            while len(whole) > 2:
                grouped = whole[-2:] + "," + grouped
                whole = whole[:-2]
            if whole:
                grouped = whole + "," + grouped
        return f"{grouped}.{frac}"

    return templates.TemplateResponse("admin/admin_employee_details_print.html", {
        "request": request,
        "employee": emp,
        "total_hours": total_hours,
        "payroll_amount_inr": _format_inr(payroll_amount if payroll_amount is not None else (emp.base_salary or 0)),
        "hourly_rate_inr": _format_inr(emp.hourly_rate or 0),
        "allowances_inr": _format_inr(emp.allowances or 0),
        "deductions_inr": _format_inr(emp.deductions or 0),
    })

#-----------------------------------------
# PUBLIC - EMPLOYEE QR PROFILE
#-----------------------------------------

@app.get("/public/employee/{employee_id}", response_class=HTMLResponse)
async def public_employee_profile(request: Request, employee_id: str, db: Session = Depends(get_db)):
    emp = db.query(User).filter(User.employee_id == employee_id, User.is_active == True).first()
    if not emp:
        return templates.TemplateResponse("admin/admin_employee_qr.html", {
            "request": request,
            "user": {
                "role": "employee",
                "name": "Public",
                "employee_id": "",
                "photo_blob": None,
                "photo_path": None,
            },
            "error": "Employee not found",
        })

    total_time = db.query(Attendance).filter(Attendance.employee_id == emp.employee_id).with_entities(
        Attendance.duration).all()
    total_hours = sum(d[0] for d in total_time if d[0])

    return templates.TemplateResponse("admin/admin_employee_qr.html", {
        "request": request,
        "user": emp,
        "employee": emp,
        "total_hours": total_hours,
    })


@app.get("/employee/photo/{employee_id}")
async def employee_photo(employee_id: str, db: Session = Depends(get_db)):
    emp = db.query(User).filter(User.employee_id == employee_id, User.is_active == True).first()
    if not emp or not emp.photo_blob:
        raise HTTPException(status_code=404, detail="Photo not found")
    return Response(content=emp.photo_blob, media_type=emp.photo_mime or "image/jpeg")

#-----------------------------------------
#ADMIN - ADD ROOM & DEPARTMENT ROUTES
#----------------------------------------

@app.post("/admin/add_room")
async def add_room(request: Request, room_no: str = Form(...), location_name: str = Form(...),
                   description: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if room already exists
    existing_room = db.query(Room).filter(Room.room_no == room_no, Room.location_name == location_name).first()
    if existing_room:
        raise HTTPException(status_code=400, detail="Room already exists")

    room_id = f"R{room_no}"
    new_room = Room(room_id=room_id, room_no=room_no, location_name=location_name, description=description)
    db.add(new_room)
    db.commit()
    return {"room_id": room_id, "message": "Room added successfully"}

#-----------------------------------------
#ADMIN - ADD DEPARTMENT ROUTE
#-----------------------------------------

@app.post("/admin/add_department")
async def add_department(request: Request, name: str = Form(...), description: str = Form(...),
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if department already exists
    existing_dept = db.query(Department).filter(Department.name == name).first()
    if existing_dept:
        raise HTTPException(status_code=400, detail="Department already exists")

    new_dept = Department(name=name, description=description)
    db.add(new_dept)
    db.commit()
    return {"message": "Department added successfully"}

#-----------------------------------------
#ADMIN - REMOVE ROOM ROUTE
#-----------------------------------------

@app.post("/admin/remove_room")
async def remove_room(request: Request, room_id: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    room = db.query(Room).filter(Room.room_id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    # Check if room has active attendance
    active_attendance = db.query(Attendance).filter(Attendance.room_id == room_id, Attendance.exit_time.is_(None)).first()
    if active_attendance:
        raise HTTPException(status_code=400, detail="Cannot remove room with active attendance")
    db.delete(room)
    db.commit()
    return {"message": "Room removed successfully"}

#-----------------------------------------
#ADMIN - PAYROLL PAGE
#----------------------------------------

@app.get("/admin/payroll", response_class=HTMLResponse)
async def admin_payroll(
    request: Request,
    month: int = datetime.date.today().month,
    year: int = datetime.date.today().year,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "admin":
        raise HTTPException(status_code=403)

    employees = db.query(User).filter(User.is_active == True).all()
    payroll = []

    for emp in employees:
        data = calculate_monthly_payroll(db, emp, month, year)
        payroll.append({
            "name": emp.name,
            "employee_id": emp.employee_id,
            **data
        })

    total_salary = sum(p["net_salary"] for p in payroll)
    avg_salary = round(total_salary / len(payroll), 2) if payroll else 0
    max_salary = max((p["net_salary"] for p in payroll), default=0)

    return templates.TemplateResponse(
        "admin/admin_payroll.html",
        {
            "request": request,
            "user": user,
            "payroll": payroll,
            "total_salary": round(total_salary, 2),
            "avg_salary": avg_salary,
            "max_salary": max_salary,
            "current_year": year
        }
    )

# ----------------------------------------
# ADMIN ATTENDANCE PAGE
# ----------------------------------------

@app.get("/admin/attendance", response_class=HTMLResponse)
async def admin_attendance(
    request: Request,
    department: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    present_query = db.query(
        User.employee_id,
        User.name,
        User.department,
        Attendance.room_no,
        Attendance.entry_time
    ).join(
        Attendance,
        Attendance.employee_id == User.employee_id
    ).filter(
        Attendance.exit_time == None
    )
    if department:
        present_query = present_query.filter(User.department == department)
    present = present_query.all()
    present_count = len(present)
    total_employees = db.query(User).filter(
        User.is_active == True
    ).count()
    absent_count = total_employees - present_count
    unknown_rfids = db.query(UnknownRFID).order_by(
        UnknownRFID.id.desc()
    ).limit(20).all()
    return templates.TemplateResponse(
        "admin/admin_attendance.html",
        {
            "request": request,
            "user": user,
            "present": present,
            "present_count": present_count,
            "absent_count": absent_count,
            "unknown_rfids": unknown_rfids,
        }
    )

# ----------------------------------------
# ADMIN UNKNOWN RFID PAGE
# ----------------------------------------

@app.get("/admin/unknown_rfid", response_class=HTMLResponse)
async def admin_unknown_rfid(
    request: Request,
    search: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    query = db.query(UnknownRFID)
    if search:
        query = query.filter(
            (UnknownRFID.rfid_tag.like(f"%{search}%")) |
            (UnknownRFID.location.ilike(f"%{search}%"))
        )
    unknown_rfids = query.order_by(UnknownRFID.timestamp.desc()).all()
    return templates.TemplateResponse(
        "admin/admin_unknown.html",
        {
            "request": request,
            "user": user,
            "search": search,
            "unknown_rfids": unknown_rfids,
            "current_year": datetime.datetime.utcnow().year
        }
    )

# ----------------------------------------
# ADMIN RESOLVE RFID ROUTE
# ----------------------------------------

@app.post("/admin/resolve_rfid")
async def resolve_rfid(request: Request, rfid_tag: str = Form(...), db: Session = Depends(get_db)):
    db.query(UnknownRFID).filter(UnknownRFID.rfid_tag == rfid_tag).delete()
    db.commit()
    return RedirectResponse("/admin/unknown_rfid", status_code=303)

# ----------------------------------------
# ADMIN LEAVE REQUESTS PAGE
# ----------------------------------------

@app.get("/admin/leave_requests", response_class=HTMLResponse)
async def admin_leave_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    pending = db.query(LeaveRequest).order_by(LeaveRequest.id.desc()).all()
    return templates.TemplateResponse("admin/admin_leave_requests.html",
                                      {"request": request, "user": user, "pending": pending,
                                       "current_year": datetime.datetime.utcnow().year})


# ----------------------------------------


@app.post("/admin/leave/update")
async def update_leave_status(request: Request,
                              leave_id: int = Form(...),
                              action: str = Form(...),
                              user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    leave = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")

    leave.status = "Approved" if action == "approve" else "Rejected"
    db.commit()
    employee = db.query(User).filter(User.employee_id == leave.employee_id).first()
    if employee and employee.email:
        send_leave_status_email(
            employee.email,
            employee.name,
            str(leave.start_date),
            str(leave.end_date),
            leave.reason,
            leave.status,
            employee.employee_id
        )
    if employee:
        create_notification(
            db,
            employee.id,
            "Leave request updated",
            f"Your leave request was {leave.status}.",
            "leave",
            "/employee/leave"
        )
        db.commit()
    return RedirectResponse("/admin/leave_requests", status_code=303)

# ----------------------------------------
# MANAGER DASHBOARD
# ----------------------------------------

@app.get("/manager/dashboard", response_class=HTMLResponse)
async def manager_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    raise HTTPException(status_code=404, detail="Manager dashboard has been removed")


@app.get("/manager/schedule_meeting", response_class=HTMLResponse)
async def manager_schedule_meeting(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    projects = db.query(Project).filter(Project.department == user.department).all()

    meetings = (
        db.query(Meeting)
        .filter(Meeting.created_by == user.id)
        .order_by(Meeting.meeting_datetime.desc())
        .all()
    )

    meeting_cards = []
    for meeting in meetings:
        assignees_q = (
            db.query(User)
            .join(ProjectMeetingAssignee, User.employee_id == ProjectMeetingAssignee.employee_id)
            .filter(ProjectMeetingAssignee.meeting_id == meeting.id)
            .all()
        )
        assignee_map = {emp.employee_id: emp for emp in assignees_q}

        creator = db.query(User).filter(User.id == meeting.created_by).first()
        if creator and creator.employee_id:
            assignee_map.setdefault(creator.employee_id, creator)

        assignees = ", ".join(
            [f"{emp.name} ({emp.employee_id})" for emp in assignee_map.values()]
        )

        attended_users = (
            db.query(User)
            .join(MeetingAttendance, User.employee_id == MeetingAttendance.employee_id)
            .filter(MeetingAttendance.meeting_id == meeting.id)
            .all()
        )
        attended_ids = {u.employee_id for u in attended_users if u.employee_id}

        invited_ids = set(assignee_map.keys())
        not_attended_ids = invited_ids - attended_ids

        attended_names = [
            f"{u.name} ({u.employee_id})" for u in attended_users if u.employee_id in invited_ids
        ]
        not_attended_names = [
            f"{assignee_map[emp_id].name} ({emp_id})" for emp_id in not_attended_ids if emp_id in assignee_map
        ]

        project_name = "No project"
        if meeting.project_id:
            project = db.query(Project).filter(Project.id == meeting.project_id).first()
            if project:
                project_name = project.name

        now = datetime.datetime.now()
        meeting_time = meeting.meeting_datetime
        status = "Completed"
        if meeting_time:
            if meeting_time > now:
                status = "Upcoming"
            elif meeting_time <= now <= meeting_time + datetime.timedelta(hours=1):
                status = "Ongoing"

        organizer_label = "-"
        if creator and creator.employee_id:
            organizer_label = f"{creator.name} ({creator.employee_id})"

        attendee_only_ids = [
            emp_id for emp_id in assignee_map.keys()
            if not creator or emp_id != creator.employee_id
        ]
        attendee_only_names = [
            f"{assignee_map[emp_id].name} ({emp_id})" for emp_id in attendee_only_ids if emp_id in assignee_map
        ]

        meeting_cards.append({
            "id": meeting.id,
            "title": meeting.title,
            "description": meeting.description or "",
            "meeting_datetime": meeting.meeting_datetime.strftime("%b %d, %Y %I:%M %p") if meeting.meeting_datetime else "",
            "meeting_link": meeting.meeting_link or "",
            "project_name": project_name,
            "assignees": assignees or "No attendees",
            "organizer": organizer_label,
            "attendees": ", ".join(attendee_only_names) or "No attendees",
            "attended": ", ".join(attended_names) or "None yet",
            "not_attended": ", ".join(not_attended_names) or "All attended",
            "status": status,
        })

    return templates.TemplateResponse("employee/manager_schedule_meeting.html", {
        "request": request,
        "user": user,
        "projects": projects,
        "meetings": meeting_cards,
    })


@app.get("/manager/participant_search")
async def manager_participant_search(q: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    term = (q or "").strip()
    if not term:
        return JSONResponse([])

    employees = (
        db.query(User)
        .filter(
            User.department == user.department,
            User.role.in_(["employee", "manager"]),
            (User.name.ilike(f"%{term}%")) | (User.employee_id.ilike(f"%{term}%"))
        )
        .order_by(User.name.asc())
        .limit(25)
        .all()
    )

    return JSONResponse([
        {"employee_id": emp.employee_id, "name": emp.name}
        for emp in employees
    ])


@app.post("/manager/create_meeting")
async def create_meeting(
    title: str = Form(...),
    description: str = Form(""),
    meeting_datetime: str = Form(...),
    project_id: Optional[int] = Form(None),
    assignees: Optional[str] = Form(None),  # Changed to string to receive comma-separated values
    meeting_link: Optional[str] = Form(None),
    room_name: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    try:
        mdt = datetime.datetime.fromisoformat(meeting_datetime)
    except Exception:
        try:
            mdt = datetime.datetime.strptime(meeting_datetime, "%Y-%m-%dT%H:%M")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid datetime")

    parsed_link = None
    if meeting_link and meeting_link.strip():
        parsed_link = urlparse(meeting_link.strip())
    if parsed_link and parsed_link.scheme and parsed_link.netloc:
        meeting_link = meeting_link.strip()
        if not room_name:
            path = parsed_link.path.rstrip("/")
            room_name = path.split("/")[-1] if path else None
    if not meeting_link or not room_name:
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
        random_suffix = secrets.token_hex(4)
        room_name = f"meeting_{timestamp}_{random_suffix}"
        jitsi_server = "https://meet.jit.si/"
        meeting_link = f"{jitsi_server}{room_name}"

    new_meeting = Meeting(
        project_id=project_id,
        title=title,
        description=description,
        meeting_datetime=mdt,
        created_by=user.id,
        meeting_link=meeting_link,
        room_name=room_name
    )
    db.add(new_meeting)
    db.commit()
    db.refresh(new_meeting)

    # Parse assignees from comma-separated string
    hashes = []
    target_tokens = []
    assignee_user_ids = []
    assignee_list = []
    if assignees and assignees.strip():
        # Split comma-separated employee IDs
        assignee_list = [emp_id.strip() for emp_id in assignees.split(',') if emp_id.strip()]

    # Ensure creator is included in the assignees list
    if user.employee_id and user.employee_id not in assignee_list:
        assignee_list.append(user.employee_id)

    recipients = []
    if assignee_list:
        for emp_id in assignee_list:
            try:
                pm = ProjectMeetingAssignee(meeting_id=new_meeting.id, employee_id=emp_id)
                db.add(pm)
                # find user to collect hash
                u = db.query(User).filter(User.employee_id == emp_id).first()
                if u and hasattr(u, 'employee_id_hash') and u.employee_id_hash:
                    hashes.append(u.employee_id_hash)
                    target_tokens.append(u.employee_id_hash)
                elif u and u.employee_id:
                    target_tokens.append(u.employee_id)
                    assignee_user_ids.append(u.id)
                if u and u.email:
                    recipients.append({"email": u.email, "name": u.name, "employee_id": u.employee_id})
            except Exception as e:
                print(f"Error adding assignee {emp_id}: {e}")
                continue
        db.commit()
    
    # Send meeting notification to all assignees via chat
    meeting_msg = f"📅 Meeting Invitation: {title}\n⏰ {mdt.strftime('%Y-%m-%d %H:%M')}\n📝 {description}\n\nHost: {user.name} ({user.employee_id})"
    for assignee_id in assignee_user_ids:
        try:
            chat_store.add_message(user.id, assignee_id, meeting_msg)
        except Exception:
            pass

    email_status = None
    if recipients:
        if smtp_enabled():
            send_bulk_meeting_invites(
                recipients,
                title,
                mdt.strftime("%b %d, %Y %I:%M %p"),
                f"{user.name} ({user.employee_id})",
                meeting_link
            )
            email_status = "sent"
        else:
            email_status = "disabled"

    if assignee_list:
        for emp_id in assignee_list:
            if emp_id == user.employee_id:
                continue
            u = db.query(User).filter(User.employee_id == emp_id).first()
            if not u:
                continue
            project_label = project_name
            create_notification(
                db,
                u.id,
                "Meeting assigned",
                f"Meeting '{title}' scheduled (Project: {project_label}).",
                "meeting",
                "/employee"
            )
        db.commit()

    # Create a CalendarEvent with target_employee_hashes so assignees see the meeting
    try:
        unique_targets = sorted(set(target_tokens))
        target_hashes = "," + ",".join(unique_targets) + "," if unique_targets else None
        cal_event = CalendarEvent(
            user_id=user.id,
            event_date=mdt.date(),
            title=title,
            notes=description,
            event_type="meeting",
            target_employee_hashes=target_hashes
        )
        db.add(cal_event)
        db.commit()
    except Exception:
        db.rollback()

    redirect_url = "/manager/schedule_meeting"
    if email_status:
        redirect_url = f"{redirect_url}?email={email_status}"
    return RedirectResponse(redirect_url, status_code=303)


@app.get("/manager/meetings", response_class=HTMLResponse)
async def manager_meetings_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    raise HTTPException(status_code=404, detail="Manager meetings page has been removed")


@app.post("/manager/meeting/update")
async def update_meeting(
    meeting_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    meeting_datetime: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting or meeting.created_by != user.id:
        raise HTTPException(status_code=404, detail="Meeting not found")

    try:
        mdt = datetime.datetime.fromisoformat(meeting_datetime)
    except Exception:
        try:
            mdt = datetime.datetime.strptime(meeting_datetime, "%Y-%m-%dT%H:%M")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid datetime")

    meeting.title = title
    meeting.description = description
    meeting.meeting_datetime = mdt
    db.commit()

    return RedirectResponse("/manager/schedule_meeting", status_code=303)


@app.post("/manager/meeting/delete")
async def delete_meeting(
    meeting_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting or meeting.created_by != user.id:
        raise HTTPException(status_code=404, detail="Meeting not found")

    db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.meeting_id == meeting.id).delete(synchronize_session=False)
    db.delete(meeting)
    db.commit()

    return RedirectResponse("/manager/schedule_meeting", status_code=303)


@app.post("/manager/create_task")
async def create_task(
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    due_date: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    assignees: Optional[List[str]] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    due_dt = None
    if due_date:
        try:
            due_dt = datetime.datetime.strptime(due_date, "%Y-%m-%d")
        except Exception:
            pass

    # Convert project_id to int if provided
    pid = None
    if project_id:
        try:
            pid = int(project_id)
        except (ValueError, TypeError):
            pass

    # Create task for each selected employee
    if assignees:
        for emp_id in assignees:
            emp_id = str(emp_id).strip()
            if not emp_id:
                continue
            try:
                new_task = Task(
                    user_id=emp_id,
                    title=title,
                    description=description,
                    priority=priority,
                    due_date=due_dt,
                    project_id=pid,  # CRITICAL: Link to project
                    created_by=user.id
                )
                db.add(new_task)

                task_event_date = due_dt.date() if due_dt else datetime.date.today()
                target_hashes = f",{emp_id},"
                cal_event = CalendarEvent(
                    user_id=user.id,
                    event_date=task_event_date,
                    title=f"Task: {title}",
                    notes=description,
                    event_type="task",
                    target_employee_hashes=target_hashes
                )
                db.add(cal_event)
            except Exception:
                continue
        db.commit()
        for emp_id in assignees:
            emp_id = str(emp_id).strip()
            if not emp_id:
                continue
            emp = db.query(User).filter(User.employee_id == emp_id).first()
            if not emp:
                continue
            project_name = "No project"
            if pid:
                project = db.query(Project).filter(Project.id == pid).first()
                if project:
                    project_name = project.name
            create_notification(
                db,
                emp.id,
                "Task assigned",
                f"Task '{title}' assigned (Project: {project_name}).",
                "task",
                "/employee/tasks"
            )
        db.commit()

    return RedirectResponse("/manager/assign_task", status_code=303)


@app.get("/manager/assign_task", response_class=HTMLResponse)
async def manager_assign_task(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    projects = db.query(Project).filter(Project.department == user.department).all()

    employees = db.query(User).filter(
        User.department == user.department,
        User.role.in_(["employee", "manager"])
    ).order_by(User.name.asc()).all()

    tasks = db.query(Task).filter(
        Task.created_by == user.id
    ).order_by(Task.created_at.desc()).all()

    for task in tasks:
        task.assignee = db.query(User).filter(User.employee_id == task.user_id).first()
        task.project_info = None
        if task.project_id:
            task.project_info = db.query(Project).filter(Project.id == task.project_id).first()

    return templates.TemplateResponse("employee/manager_assign_task.html", {
        "request": request,
        "user": user,
        "projects": projects,
        "employees": employees,
        "tasks": tasks,
    })


@app.post("/manager/tasks/update")
async def manager_update_task(
    task_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    status: str = Form("pending"),
    due_date: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    task = db.query(Task).filter(Task.id == task_id, Task.created_by == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    due_dt = None
    if due_date:
        try:
            due_dt = datetime.datetime.strptime(due_date, "%Y-%m-%d")
        except Exception:
            due_dt = task.due_date

    task.title = title
    task.description = description
    task.priority = priority
    task.status = status
    task.due_date = due_dt
    db.commit()

    return RedirectResponse("/manager/assign_task", status_code=303)


@app.post("/manager/tasks/delete")
async def manager_delete_task(
    task_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    task = db.query(Task).filter(Task.id == task_id, Task.created_by == user.id).first()
    if task:
        db.delete(task)
        db.commit()

    return RedirectResponse("/manager/assign_task", status_code=303)


@app.get("/manager/team_assignments", response_class=HTMLResponse)
async def manager_team_assignments(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    raise HTTPException(status_code=404, detail="Manager team assignments page has been removed")

@app.get("/manager/projects", response_class=HTMLResponse)
async def manager_projects_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    projects = db.query(Project).filter(Project.department == user.department).all()

    return templates.TemplateResponse("employee/employee_manager_projects.html", {
        "request": request,
        "user": user,
        "projects": projects
    })


@app.post("/manager/projects/delete")
async def manager_delete_project(
    project_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    project = db.query(Project).filter(
        Project.id == project_id,
        Project.department == user.department
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db.query(Team).filter(Team.project_id == project_id).update({"project_id": None}, synchronize_session=False)

    meeting_ids = [m.id for m in db.query(Meeting.id).filter(Meeting.project_id == project_id).all()]
    if meeting_ids:
        db.query(MeetingAttendance).filter(MeetingAttendance.meeting_id.in_(meeting_ids)).delete(synchronize_session=False)
        db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.meeting_id.in_(meeting_ids)).delete(synchronize_session=False)
        db.query(Meeting).filter(Meeting.id.in_(meeting_ids)).delete(synchronize_session=False)

    task_ids = [t.id for t in db.query(ProjectTask.id).filter(ProjectTask.project_id == project_id).all()]
    if task_ids:
        db.query(ProjectTaskAssignee).filter(ProjectTaskAssignee.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(ProjectTask).filter(ProjectTask.id.in_(task_ids)).delete(synchronize_session=False)

    db.query(ProjectAssignment).filter(ProjectAssignment.project_id == project_id).delete(synchronize_session=False)
    db.query(Task).filter(Task.project_id == project_id).delete(synchronize_session=False)

    db.delete(project)
    db.commit()

    return RedirectResponse("/manager/projects", status_code=303)

@app.get("/leader/dashboard", response_class=HTMLResponse)
async def leader_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Allow Managers to access this if they are also leads, or standard Team Leads
    if user.role != "team_lead" and user.role != "manager": 
        raise HTTPException(status_code=403)
    
    # Get the team this user leads
    my_team = db.query(Team).filter(Team.leader_id == user.id).first()
    
    # Get projects involving this team (logic: find projects where team members are assigned)
    # Simplified: Get all projects in department for now
    projects = db.query(Project).filter(Project.department == user.department).all()

    return templates.TemplateResponse("employee/employee_leader_dashboard.html", {
        "request": request,
        "user": user,
        "team": my_team,
        "projects": projects
    })

@app.post("/leader/assign_task")
async def assign_task(
    project_id: int = Form(...),
    title: str = Form(...),
    deadline: str = Form(...),
    assign_to_employee_id: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Check permissions
    if user.role not in ["team_lead", "manager"]: raise HTTPException(status_code=403)

    # Create Task
    new_task = ProjectTask(
        project_id=project_id,
        title=title,
        deadline=datetime.datetime.strptime(deadline, "%Y-%m-%d"),
        status="pending"
    )
    db.add(new_task)
    db.commit() # Commit to get ID

    # Assign to User
    assignment = ProjectTaskAssignee(
        task_id=new_task.id,
        employee_id=assign_to_employee_id
    )
    db.add(assignment)
    db.commit()

    return RedirectResponse("/leader/dashboard", status_code=303)



# ----------------------------------------
# EMPLOYEE DASHBOARD 
# ----------------------------------------

@app.get("/employee", response_class=HTMLResponse)
async def employee_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # ... (Your existing logic) ...
    total_hours = 0
    team = None
    team_leader = None
    team_project = None
    if user.current_team_id:
        team = db.query(Team).filter(Team.id == user.current_team_id).first()
        if team:
            team_leader = team.leader
            if team.project_id:
                team_project = db.query(Project).filter(Project.id == team.project_id).first()
    tasks = db.query(Task).filter(
        Task.user_id == user.employee_id
    ).order_by(Task.created_at.desc()).limit(5).all()
    return templates.TemplateResponse("employee/employee_dashboard.html", 
                                      {
                                        "request": request, 
                                        "user": user, 
                                        "total_hours": total_hours, 
                                        "team": team,
                                        "team_leader": team_leader,
                                        "team_project": team_project,
                                        "tasks": tasks, 
                                        "current_year": 2026
                                        }
                                    )


from fastapi import Request, Depends
from fastapi.responses import HTMLResponse

@app.get("/employee/chat", response_class=HTMLResponse)
async def employee_chat(
    request: Request,
    user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(
        "employee/employee_chat.html",
        {
            "request": request,
            "user": user,                 # 🔥 REQUIRED
            "active_page": "chat",        # optional but safe
            "chat_title": "HR Team"
        }
    )


#-----------------------------------------
#EMPLOYEE TEAM PAGE
#----------------------------------------

@app.get("/employee/team", response_class=HTMLResponse)
async def employee_team(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = None
    members = []
    leader = None
    projects = []

    if user.current_team_id:
        team = db.query(Team).filter(Team.id == user.current_team_id).first()
        if team:
            # Query members using current_team_id as single source of truth
            members = db.query(User).filter(User.current_team_id == team.id).all()
            leader = team.leader

    projects_from_tasks = (
        db.query(Project)
        .join(Task, Task.project_id == Project.id)
        .filter(Task.user_id == user.employee_id)
        .all()
    )
    projects_from_assignments = (
        db.query(Project)
        .join(ProjectAssignment, ProjectAssignment.project_id == Project.id)
        .filter(ProjectAssignment.employee_id == user.employee_id)
        .all()
    )
    projects_from_team = []
    if team and team.project_id:
        team_project = db.query(Project).filter(Project.id == team.project_id).first()
        if team_project:
            projects_from_team.append(team_project)
    project_map = {p.id: p for p in (projects_from_tasks + projects_from_assignments + projects_from_team)}
    projects = list(project_map.values())
    project_task_counts = {}
    if projects:
        project_ids = [p.id for p in projects]
        task_rows = (
            db.query(Task.project_id, func.count(Task.id))
            .filter(
                Task.user_id == user.employee_id,
                Task.project_id.in_(project_ids)
            )
            .group_by(Task.project_id)
            .all()
        )
        project_task_counts = {pid: count for pid, count in task_rows}

    return templates.TemplateResponse(
        "employee/employee_team.html",
        {
            "request": request,
            "user": user,
            "team": team,
            "members": members,
            "leader": leader,
            "projects": projects,
            "project_task_counts": project_task_counts
        }
    )

#-----------------------------------------
#EMPLOYEE ATTENDANCE PAGE
#-----------------------------------------

@app.get("/employee/attendance", response_class=HTMLResponse)
async def employee_attendance_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    logs = db.query(Attendance).filter(
        Attendance.employee_id == user.employee_id
            ).order_by(Attendance.date.desc()).all()
    return templates.TemplateResponse("employee/employee_attendance.html",
                                      {"request": request, "user": user, "logs": logs,
                                       "current_year": datetime.datetime.utcnow().year})

#-----------------------------------------
#EMPLOYEE TASKS PAGE
#-----------------------------------------

@app.get("/employee/tasks", response_class=HTMLResponse)
async def employee_tasks_page(request: Request,
                              user: User = Depends(get_current_user),
                              db: Session = Depends(get_db),
                              filter: str = None):
    task_query = db.query(Task).filter(Task.user_id == user.employee_id)
    if filter in ["pending", "in-progress", "done"]:
        task_query = task_query.filter(Task.status == filter)
    tasks = task_query.order_by(Task.id.desc()).all()
    pending = db.query(Task).filter(Task.user_id == user.employee_id, Task.status == "pending").count()
    in_progress = db.query(Task).filter(Task.user_id == user.employee_id, Task.status == "in-progress").count()
    done = db.query(Task).filter(Task.user_id == user.employee_id, Task.status == "done").count()
    return templates.TemplateResponse("employee/employee_tasks.html",
                                      {"request": request, "user": user,
                                       "tasks": tasks,
                                       "pending": pending,
                                       "in_progress": in_progress,
                                       "done": done})

@app.post("/employee/tasks/add")
async def employee_add_task(title: str = Form(...), description: str = Form(""),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    new_task = Task(user_id=user.employee_id, title=title, description=description)
    db.add(new_task)
    db.commit()
    return RedirectResponse("/employee/tasks", status_code=303)

@app.post("/employee/tasks/update")
async def update_task(task_id: int = Form(...), status: str = Form(...),
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.employee_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = status
    db.commit()
    return RedirectResponse("/employee/tasks", status_code=303)

@app.post("/employee/tasks/delete")
async def delete_task(task_id: int = Form(...),
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id, Task.user_id == user.employee_id).first()
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse("/employee/tasks", status_code=303)

#----------------------------------------
# EMPLOYEE MEETINGS PAGE
#----------------------------------------

@app.get("/employee/meetings", response_class=HTMLResponse)
async def employee_meetings_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Display all meetings assigned to the current employee"""
    # Get meetings where user is an assignee
    meetings = (
        db.query(Meeting)
        .join(ProjectMeetingAssignee, Meeting.id == ProjectMeetingAssignee.meeting_id)
        .filter(ProjectMeetingAssignee.employee_id == user.employee_id)
        .order_by(Meeting.meeting_datetime.desc())
        .all()
    )
    
    # Enrich meetings with creator info and status
    now = datetime.datetime.now()
    for meeting in meetings:
        meeting.creator_info = db.query(User).filter(User.id == meeting.created_by).first()
        status = "Completed"
        if meeting.meeting_datetime:
            if meeting.meeting_datetime > now:
                status = "Upcoming"
            elif meeting.meeting_datetime <= now <= meeting.meeting_datetime + datetime.timedelta(hours=1):
                status = "Ongoing"
        meeting.status = status
    
    return templates.TemplateResponse(
        "employee/employee_meetings.html",
        {"request": request, "user": user, "meetings": meetings}
    )


@app.get("/employee/meeting/{meeting_id}", response_class=HTMLResponse)
async def employee_meeting_room(
    request: Request,
    meeting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Join a specific meeting room with Jitsi"""
    # Verify user has access to this meeting
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    
    # Check if user is assigned to this meeting
    is_assigned = (
        db.query(ProjectMeetingAssignee)
        .filter(
            ProjectMeetingAssignee.meeting_id == meeting_id,
            ProjectMeetingAssignee.employee_id == user.employee_id
        )
        .first()
    )
    
    # Also allow creator to join
    if not is_assigned and meeting.created_by != user.id:
        raise HTTPException(status_code=403, detail="You are not invited to this meeting")
    
    # Record attendance on join
    existing_attendance = db.query(MeetingAttendance).filter(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.employee_id == user.employee_id
    ).first()
    if not existing_attendance:
        db.add(MeetingAttendance(meeting_id=meeting_id, employee_id=user.employee_id))
        db.commit()

    # Get creator info
    creator = db.query(User).filter(User.id == meeting.created_by).first()
    is_organizer = meeting.created_by == user.id
    organizer_joined = False
    if creator and creator.employee_id:
        organizer_joined = db.query(MeetingAttendance).filter(
            MeetingAttendance.meeting_id == meeting_id,
            MeetingAttendance.employee_id == creator.employee_id
        ).first() is not None

    return templates.TemplateResponse(
        "employee/employee_meeting_room.html",
        {
            "request": request,
            "user": user,
            "meeting": meeting,
            "creator": creator,
            "is_organizer": is_organizer,
            "organizer_joined": organizer_joined
        }
    )


@app.get("/api/meetings/{meeting_id}/host-status")
async def meeting_host_status(
    meeting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    is_assigned = (
        db.query(ProjectMeetingAssignee)
        .filter(
            ProjectMeetingAssignee.meeting_id == meeting_id,
            ProjectMeetingAssignee.employee_id == user.employee_id
        )
        .first()
    )

    if not is_assigned and meeting.created_by != user.id:
        raise HTTPException(status_code=403, detail="You are not invited to this meeting")

    creator = db.query(User).filter(User.id == meeting.created_by).first()
    if not creator or not creator.employee_id:
        return {"host_joined": False}

    host_joined = db.query(MeetingAttendance).filter(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.employee_id == creator.employee_id
    ).first() is not None

    return {"host_joined": host_joined}


@app.get("/meeting/{meeting_id}", response_class=HTMLResponse)
async def meeting_room_any(
    request: Request,
    meeting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    is_assigned = (
        db.query(ProjectMeetingAssignee)
        .filter(
            ProjectMeetingAssignee.meeting_id == meeting_id,
            ProjectMeetingAssignee.employee_id == user.employee_id
        )
        .first()
    )

    if not is_assigned and meeting.created_by != user.id:
        raise HTTPException(status_code=403, detail="You are not invited to this meeting")

    existing_attendance = db.query(MeetingAttendance).filter(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.employee_id == user.employee_id
    ).first()
    if not existing_attendance:
        db.add(MeetingAttendance(meeting_id=meeting_id, employee_id=user.employee_id))
        db.commit()

    creator = db.query(User).filter(User.id == meeting.created_by).first()
    is_organizer = meeting.created_by == user.id
    organizer_joined = False
    if creator and creator.employee_id:
        organizer_joined = db.query(MeetingAttendance).filter(
            MeetingAttendance.meeting_id == meeting_id,
            MeetingAttendance.employee_id == creator.employee_id
        ).first() is not None

    return templates.TemplateResponse(
        "employee/employee_meeting_room.html",
        {
            "request": request,
            "user": user,
            "meeting": meeting,
            "creator": creator,
            "is_organizer": is_organizer,
            "organizer_joined": organizer_joined
        }
    )


#----------------------------------------
#EMPLOYEE LEAVE PAGE
# ----------------------------------------

@app.get("/employee/leave", response_class=HTMLResponse)
async def employee_leave_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    leaves = db.query(LeaveRequest).filter(
        or_(
            LeaveRequest.employee_id == user.employee_id,
            LeaveRequest.employee_id == str(user.id)
        )
    ).order_by(LeaveRequest.id.desc()).all()
    return templates.TemplateResponse("employee/employee_leave.html",
                                      {"request": request, "user": user,
                                       "leaves": leaves,
                                       "current_year": datetime.datetime.utcnow().year})

@app.post("/employee/leave/apply")
async def apply_leave(request: Request,
                      start_date: str = Form(...),
                      end_date: str = Form(...),
                      reason: str = Form(...),
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    
    leave = LeaveRequest(employee_id=user.employee_id,
                         start_date=start_date,
                         end_date=end_date,
                         reason=reason)
    db.add(leave)
    db.commit()
    send_leave_requested_email(user.email, user.name, start_date, end_date, reason, user.employee_id)
    return RedirectResponse("/employee/leave", status_code=303)

#----------------------------------------
#EMPLOYEE PROFILE PAGE
#----------------------------------------

@app.get("/employee/profile", response_class=HTMLResponse)
async def employee_profile(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("employee/employee_profile.html",
                                      {"request": request, "user": user,
                                       "current_year": datetime.datetime.utcnow().year})


@app.get("/employee/profile/details", response_class=HTMLResponse)
async def employee_profile_details(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("employee/employee_profile_details.html",
                                      {"request": request, "user": user,
                                       "current_year": datetime.datetime.utcnow().year})


@app.get("/employee/profile/print", response_class=HTMLResponse)
async def employee_profile_print(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    total_time = db.query(Attendance).filter(Attendance.employee_id == user.employee_id).with_entities(
        Attendance.duration).all()
    total_hours = sum(d[0] for d in total_time if d[0])

    latest_payroll = db.query(Payroll).filter(
        Payroll.employee_id == user.employee_id
    ).order_by(Payroll.year.desc(), Payroll.month.desc()).first()
    payroll_amount = latest_payroll.net_salary if latest_payroll else None

    def _format_inr(value: float | None) -> str:
        if value is None:
            value = 0.0
        try:
            num = float(value)
        except Exception:
            num = 0.0
        whole, frac = f"{num:.2f}".split(".")
        if len(whole) <= 3:
            grouped = whole
        else:
            grouped = whole[-3:]
            whole = whole[:-3]
            while len(whole) > 2:
                grouped = whole[-2:] + "," + grouped
                whole = whole[:-2]
            if whole:
                grouped = whole + "," + grouped
        return f"{grouped}.{frac}"

    return templates.TemplateResponse("employee/employee_profile_print.html",
                                      {"request": request,
                                       "employee": user,
                                       "total_hours": total_hours,
                                       "payroll_amount_inr": _format_inr(payroll_amount if payroll_amount is not None else (user.base_salary or 0)),
                                       "hourly_rate_inr": _format_inr(user.hourly_rate or 0),
                                       "allowances_inr": _format_inr(user.allowances or 0),
                                       "deductions_inr": _format_inr(user.deductions or 0),
                                       "current_year": datetime.datetime.utcnow().year})

@app.post("/employee/profile/update")
async def update_profile(
    request: Request,
    phone: str = Form(...),
    email: str = Form(...),
    address: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user.phone = phone
    user.email = email
    user.address = address
    db.commit()
    return RedirectResponse(url="/employee/profile/details", status_code=303)


@app.post("/employee/password/change")
async def employee_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/employee/profile/details?pw=invalid", status_code=303)
    if len(new_password.strip()) < 6:
        return RedirectResponse(url="/employee/profile/details?pw=weak", status_code=303)
    if verify_password(new_password, user.password_hash):
        return RedirectResponse(url="/employee/profile/details?pw=same", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/employee/profile/details?pw=mismatch", status_code=303)

    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url="/employee/profile/details?pw=updated", status_code=303)

#-----------------------------------------
#EMPLOYEE PAYSLIPS PAGE
#----------------------------------------

@app.get("/employee/payslips", response_class=HTMLResponse)
async def employee_payslips_page(request: Request,
                                 month: int = None, year: int = None,
                                 user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    current_year = datetime.datetime.utcnow().year
    if not month or not year:
        return templates.TemplateResponse("employee/employee_payslips.html",
                                          {"request": request, "user": user,
                                           "computed": False,
                                           "current_year": current_year,
                                           "month": current_year,
                                           "year": current_year})
    
    # Block future months
    today = datetime.date.today()
    if (year > today.year) or (year == today.year and month > today.month):
        return templates.TemplateResponse(
            "employee/employee_payslips.html",
            {
                "request": request,
                "user": user,
                "computed": False,
                "error": "Payslip for future months cannot be generated.",
                "current_year": today.year,
                "month": month,
                "year": year
            }
        )
    
    # Use the shared payroll engine
    payroll = calculate_monthly_payroll(db, user, month, year)

    # Keep total_hours for compatibility/diagnostics
    start_date = datetime.datetime(year, month, 1)
    end_date = datetime.datetime(year, month, monthrange(year, month)[1], 23, 59, 59)
    total_hours = db.query(func.sum(Attendance.duration)).filter(
        Attendance.employee_id == user.employee_id,
        Attendance.entry_time >= start_date,
        Attendance.entry_time <= end_date
    ).scalar() or 0

    # Map payroll values for the template (backwards-compatible keys)
    gross_salary = payroll.get("base_salary")
    leave_deduction = payroll.get("leave_deduction")
    tax_deduction = payroll.get("tax")
    net_salary = payroll.get("net_salary")

    return templates.TemplateResponse("employee/employee_payslips.html",
                                      {"request": request, "user": user,
                                       "computed": True,
                                       "total_hours": total_hours,
                                       "gross_salary": gross_salary,
                                       "tax_deduction": tax_deduction,
                                       "leave_deduction": leave_deduction,
                                       "net_salary": net_salary,
                                       "payroll": payroll,
                                       "current_year": current_year,
                                       "month": month,
                                       "year": year})


@app.get("/employee/payslips/download")
async def employee_payslip_download(
    month: int,
    year: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not month or not year:
        raise HTTPException(status_code=400, detail="Month and year are required")
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid month")
    
    # Block future months
    today = datetime.date.today()
    if (year > today.year) or (year == today.year and month > today.month):
        raise HTTPException(
            status_code=400,
            detail="Cannot download payslip for future months"
        )

    payroll = calculate_monthly_payroll(db, user, month, year)
    base_salary = payroll.get("base_salary") or 0.0
    leave_deduction = payroll.get("leave_deduction") or 0.0
    tax_deduction = payroll.get("tax") or 0.0
    allowances = payroll.get("allowances") or 0.0
    deductions = payroll.get("deductions") or 0.0
    net_salary = payroll.get("net_salary") or 0.0
    gross_salary = max(0.0, base_salary - leave_deduction)

    start_date = datetime.datetime(year, month, 1)
    end_date = datetime.datetime(year, month, monthrange(year, month)[1], 23, 59, 59)
    total_hours = db.query(func.sum(Attendance.duration)).filter(
        Attendance.employee_id == user.employee_id,
        Attendance.entry_time >= start_date,
        Attendance.entry_time <= end_date
    ).scalar() or 0

    def format_money(value: float) -> str:
        return f"INR {value:,.2f}"

    company_name = os.getenv("COMPANY_NAME", "TeamSync")
    period_label = f"{month_name[month]} {year}"
    period_end = datetime.date(year, month, monthrange(year, month)[1])

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 48

    logo_path = BASE_DIR / "static" / "assets" / "logo.png"
    if logo_path.exists():
        pdf.drawImage(str(logo_path), margin, height - 84, width=36, height=36, mask="auto")

    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(margin + 48, height - 58, company_name)
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#64748b"))
    pdf.drawString(margin + 48, height - 72, "Pay Statement")

    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.drawRightString(width - margin, height - 58, f"Period: {period_label}")
    pdf.setFillColor(colors.HexColor("#64748b"))
    pdf.drawRightString(width - margin, height - 72, f"Pay Date: {period_end.strftime('%d %b %Y')}")

    pdf.setStrokeColor(colors.HexColor("#e2e8f0"))
    pdf.line(margin, height - 92, width - margin, height - 92)

    photo_size = 60
    photo_x = width - margin - photo_size
    photo_y = height - 170
    photo_drawn = False
    if user.photo_blob:
        try:
            photo_reader = ImageReader(BytesIO(user.photo_blob))
            pdf.drawImage(photo_reader, photo_x, photo_y, width=photo_size, height=photo_size, mask="auto")
            photo_drawn = True
        except Exception:
            photo_drawn = False
    elif user.photo_path and Path(user.photo_path).exists():
        try:
            pdf.drawImage(user.photo_path, photo_x, photo_y, width=photo_size, height=photo_size, mask="auto")
            photo_drawn = True
        except Exception:
            photo_drawn = False

    if photo_drawn:
        pdf.setStrokeColor(colors.HexColor("#e2e8f0"))
        pdf.rect(photo_x, photo_y, photo_size, photo_size, stroke=1, fill=0)
    else:
        pdf.setFillColor(colors.HexColor("#f1f5f9"))
        pdf.rect(photo_x, photo_y, photo_size, photo_size, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#334155"))
        pdf.setFont("Helvetica-Bold", 18)
        initial = (user.name or "?")[:1].upper()
        pdf.drawCentredString(photo_x + (photo_size / 2), photo_y + (photo_size / 2) - 6, initial)
        pdf.setStrokeColor(colors.HexColor("#e2e8f0"))
        pdf.rect(photo_x, photo_y, photo_size, photo_size, stroke=1, fill=0)

    y = height - 120
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(margin, y, "Employee Details")
    y -= 16
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#334155"))
    pdf.drawString(margin, y, f"Name: {user.name}")
    y -= 14
    pdf.drawString(margin, y, f"Employee ID: {user.employee_id}")
    y -= 14
    pdf.drawString(margin, y, f"Department: {user.department or 'N/A'}")
    y -= 14
    pdf.drawString(margin, y, f"Title: {user.title or 'N/A'}")

    y -= 24
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(margin, y, "Statement Summary")
    y -= 16
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#334155"))
    pdf.drawString(margin, y, f"Present Days: {payroll.get('present_days', 0)}")
    y -= 14
    pdf.drawString(margin, y, f"Leave Days: {payroll.get('leave_days', 0)}")
    y -= 14
    pdf.drawString(margin, y, f"Productive Hours: {total_hours:.2f}")

    y -= 24

    def draw_row(label: str, value: str, y_pos: float, value_color=colors.HexColor("#0f172a")):
        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(colors.HexColor("#64748b"))
        pdf.drawString(margin, y_pos, label)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColor(value_color)
        pdf.drawRightString(width - margin, y_pos, value)

    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(margin, y, "Earnings")
    y -= 16
    draw_row("Base Salary", format_money(base_salary), y)
    y -= 14
    draw_row("Allowances", format_money(allowances), y)
    y -= 14
    draw_row("Gross After Leave", format_money(gross_salary), y)

    y -= 22
    pdf.setFillColor(colors.HexColor("#0f172a"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(margin, y, "Deductions")
    y -= 16
    draw_row("Leave Deduction", f"- {format_money(leave_deduction)}", y, colors.HexColor("#e11d48"))
    y -= 14
    draw_row("Tax", f"- {format_money(tax_deduction)}", y, colors.HexColor("#e11d48"))
    y -= 14
    draw_row("Other Deductions", f"- {format_money(deductions)}", y, colors.HexColor("#e11d48"))

    y -= 44
    pdf.setFillColor(colors.HexColor("#ecfdf5"))
    pdf.setStrokeColor(colors.HexColor("#a7f3d0"))
    pdf.rect(margin, y - 12, width - (margin * 2), 44, stroke=1, fill=1)
    pdf.setFillColor(colors.HexColor("#065f46"))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin + 10, y + 8, "Net Pay")
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawRightString(width - margin - 10, y + 8, format_money(net_salary))

    pdf.setFillColor(colors.HexColor("#94a3b8"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(margin, 48, "This is a system-generated payslip and does not require a signature.")

    pdf.showPage()
    pdf.save()

    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"payslip_{user.employee_id}_{year}_{month:02d}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

#----------------------------------------
# RFID ATTENDANCE API
#----------------------------------------

@app.post("/api/attendance")
async def record_attendance(
    rfid_tag: str,
    room_no: str,
    location_name: str,
    db: Session = Depends(get_db)
):
    GATE_ROOM_NO = "77"
    
    # 1. Find User
    user = db.query(User).filter(User.rfid_tag == rfid_tag, User.is_active == True).first()
    
    if not user:
        db.add(UnknownRFID(rfid_tag=rfid_tag, location=location_name))
        db.commit()
        return {"status": "unknown_rfid"}

    today = datetime.date.today()
    now = datetime.datetime.now()

    # --- NEW: WRITE TO DETAILED LOG ---
    new_log = AttendanceLog(
        user_id=user.id,
        entry_time=now,
        location_name=location_name,
        room_no=room_no
    )
    db.add(new_log)

    # --- NEW: UPDATE DAILY SUMMARY ---
    daily_record = db.query(AttendanceDaily).filter(
        AttendanceDaily.user_id == user.id,
        AttendanceDaily.date == today
    ).first()

    if not daily_record:
        # First swipe of the day
        status = "PRESENT"
        # Late logic (e.g., after 9:30 AM)
        if now.time() > datetime.time(9, 30):
            status = "LATE"
        
        daily_record = AttendanceDaily(
            user_id=user.id,
            date=today,
            status=status,
            check_in_time=now.time()
        )
        db.add(daily_record)
    
    # --- EXISTING LOGIC (For Dashboard Views) ---
    # (Kept for backward compatibility with your existing dashboards)
    
    open_gate = db.query(Attendance).filter(Attendance.employee_id == user.employee_id, Attendance.room_no == GATE_ROOM_NO, Attendance.exit_time == None).first()
    open_block = db.query(Attendance).filter(Attendance.employee_id == user.employee_id, Attendance.room_no != GATE_ROOM_NO, Attendance.exit_time == None).first()
    
    status_msg = "entry"

    if room_no == GATE_ROOM_NO:
        if open_block:
            open_block.exit_time = now
            open_block.duration = round((now - open_block.entry_time).total_seconds() / 3600, 2)
        if open_gate:
            open_gate.exit_time = now
            open_gate.duration = round((now - open_gate.entry_time).total_seconds() / 3600, 2)
            status_msg = "gate_exited"
        else:
            db.add(Attendance(employee_id=user.employee_id, date=today, entry_time=now, status="PRESENT", location_name=location_name, room_no=GATE_ROOM_NO))
            status_msg = "gate_entered"
    else:
        # Block logic
        if not open_gate:
             db.add(Attendance(employee_id=user.employee_id, date=today, entry_time=now, status="PRESENT", location_name="Main Gate", room_no=GATE_ROOM_NO))
        
        if open_block and open_block.room_no == room_no:
             # Re-swiping same room (exit)
             open_block.exit_time = now
             open_block.duration = round((now - open_block.entry_time).total_seconds() / 3600, 2)
             status_msg = "block_exited"
        else:
            if open_block:
                open_block.exit_time = now
                open_block.duration = round((now - open_block.entry_time).total_seconds() / 3600, 2)
            
            db.add(Attendance(employee_id=user.employee_id, date=today, entry_time=now, status="PRESENT", location_name=location_name, room_no=room_no))
            status_msg = "block_entered"

    db.commit()
    return {"status": status_msg}

#----------------------------------------
#API FOR PERSONS IN BLOCK
#----------------------------------------

@app.get("/api/block_persons")
async def get_block_persons(location: str, room: str, db: Session = Depends(get_db)):
    attendances = db.query(Attendance).filter(
        Attendance.location_name == location, 
        Attendance.room_no == room, 
        Attendance.exit_time.is_(None)
    ).all()
    persons = [{"name": a.user.name} for a in attendances]
    return {"persons": persons}

#----------------------------------------
#API FOR BLOCKS STATUS
#----------------------------------------

@app.get("/api/blocks")
async def get_blocks(db: Session = Depends(get_db)):
    blocks = (
        db.query(
            Attendance.location_name,
            Attendance.room_no,
            func.count(Attendance.id).label("count")
        )
        .filter(
            Attendance.status == "PRESENT",
            Attendance.date == datetime.date.today()
        )
        .group_by(
            Attendance.location_name,
            Attendance.room_no
        )
        .all()
    )
    return {
        "blocks": [
            {
                "location": b.location_name,
                "room": b.room_no,
                "count": b.count
            }
            for b in blocks
        ]
    }

#----------------------------------------
#API FOR ABSENTEES
#----------------------------------------

@app.get("/api/absentees")
async def get_absentees(department: str, db: Session = Depends(get_db)):
    # Get all active employees in the department
    all_employees = db.query(User).filter(User.department == department, User.is_active == True).all()
    # Get employees currently checked in (present)
    present_employee_ids = db.query(Attendance.employee_id).filter(Attendance.exit_time.is_(None)).distinct().all()
    present_ids = {p[0] for p in present_employee_ids}
    # Absentees: Employees not in present_ids
    absentees = [emp for emp in all_employees if emp.employee_id not in present_ids]
    return {"absentees": [{"name": emp.name, "employee_id": emp.employee_id} for emp in absentees]}

#----------------------------------------
#API FOR EMPLOYEE LOGS
#----------------------------------------

@app.get("/api/employee_logs")
async def employee_logs(employee_id: str, db: Session = Depends(get_db)):
    logs = db.query(Attendance).filter(
        Attendance.employee_id == employee_id
    ).order_by(Attendance.entry_time.desc()).limit(10).all()
    return {
        "logs": [
            {
                "in": a.entry_time.strftime("%H:%M"),
                "out": a.exit_time.strftime("%H:%M") if a.exit_time else "-",
                "room": a.room_no,
                "location": a.location_name
            }
            for a in logs
        ]
    }

#----------------------------------------
#API FOR LEAVE COUNT
#----------------------------------------

@app.get("/api/leave_count")
async def leave_count(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    pending = db.query(LeaveRequest).filter(LeaveRequest.status == "Pending").count()
    return {"count": pending}

#----------------------------------------
#API FOR MONTH HOURS
#----------------------------------------

@app.get("/api/month-hours")
async def month_hours(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.datetime.utcnow()
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = db.query(func.sum(Attendance.duration)).filter(
        Attendance.employee_id == user.employee_id,
        Attendance.entry_time >= first_day
    ).scalar() or 0
    return {"total_hours": round(total, 2)}


#----------------------------------------
# API FOR MEETINGS POPUP
#----------------------------------------

@app.get("/api/meetings/popup")
async def meetings_popup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meetings_map = {}

    assigned_meetings = (
        db.query(Meeting)
        .join(ProjectMeetingAssignee, Meeting.id == ProjectMeetingAssignee.meeting_id)
        .filter(ProjectMeetingAssignee.employee_id == user.employee_id)
        .all()
    )

    created_meetings = db.query(Meeting).filter(Meeting.created_by == user.id).all()

    for meeting in assigned_meetings + created_meetings:
        meetings_map[meeting.id] = meeting

    upcoming = []
    past = []
    now = datetime.datetime.now()

    for meeting in meetings_map.values():
        creator = db.query(User).filter(User.id == meeting.created_by).first()
        is_assignee = (
            db.query(ProjectMeetingAssignee)
            .filter(ProjectMeetingAssignee.meeting_id == meeting.id,
                    ProjectMeetingAssignee.employee_id == user.employee_id)
            .first()
        )

        show_link = True if (is_assignee or meeting.created_by == user.id) else False

        status = "Completed"
        if meeting.meeting_datetime:
            if meeting.meeting_datetime > now:
                status = "Upcoming"
            elif meeting.meeting_datetime <= now <= meeting.meeting_datetime + datetime.timedelta(hours=1):
                status = "Ongoing"

        attendees_q = (
            db.query(User)
            .join(ProjectMeetingAssignee, User.employee_id == ProjectMeetingAssignee.employee_id)
            .filter(ProjectMeetingAssignee.meeting_id == meeting.id)
            .all()
        )
        attendee_map = {u.employee_id: u for u in attendees_q if u.employee_id}
        if creator and creator.employee_id:
            attendee_map.setdefault(creator.employee_id, creator)

        attendee_list = ", ".join(
            [f"{u.name} ({u.employee_id})" for u in attendee_map.values()]
        )

        item = {
            "id": meeting.id,
            "title": meeting.title,
            "meeting_datetime": meeting.meeting_datetime.strftime("%b %d, %Y %I:%M %p") if meeting.meeting_datetime else "",
            "sender_name": creator.name if creator else "-",
            "sender_employee_id": creator.employee_id if creator else "-",
            "meeting_link": meeting.meeting_link if show_link else None,
            "status": status,
            "employees": attendee_list or "-",
            "join_url": f"/meeting/{meeting.id}" if show_link else None
        }

        if status == "Completed":
            past.append((meeting.meeting_datetime or datetime.datetime.min, item))
        else:
            upcoming.append((meeting.meeting_datetime or datetime.datetime.min, item))

    upcoming.sort(key=lambda m: m[0])
    past.sort(key=lambda m: m[0], reverse=True)

    return {
        "upcoming": [m[1] for m in upcoming],
        "past": [m[1] for m in past]
    }


#----------------------------------------
# API FOR MANAGER EMPLOYEE SEARCH (Meeting Management)
#----------------------------------------

@app.get("/api/manager_employees")
async def manager_employees(q: str = "", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "manager":
        raise HTTPException(status_code=403)

    query = (q or "").strip()
    if not query:
        return []

    employees = db.query(User).filter(
        User.department == user.department,
        User.role.in_(["employee", "team_lead"]),
        or_(
            User.name.ilike(f"%{query}%"),
            User.employee_id.ilike(f"%{query}%")
        )
    ).order_by(User.name.asc()).limit(50).all()

    return [
        {
            "id": emp.id,
            "name": emp.name,
            "employee_id": emp.employee_id
        }
        for emp in employees
    ]

#----------------------------------------
# API FOR ALL PROJECTS (for manager projects page)
#----------------------------------------

@app.get("/api/all_projects")
async def all_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Fetch all projects for manager's department"""
    if user.role != "manager":
        raise HTTPException(status_code=403)
    
    # Get all projects in manager's department
    projects = db.query(Project).filter(
        Project.department == user.department
    ).all()
    
    projects_data = []
    for project in projects:
        # Get project tasks
        tasks = db.query(ProjectTask).filter(ProjectTask.project_id == project.id).all()

        # Collect assignees via task assignee join
        task_ids = [t.id for t in tasks]
        assigned_employees = []
        if task_ids:
            assignee_ids = [
                a.employee_id
                for a in db.query(ProjectTaskAssignee.employee_id)
                .filter(ProjectTaskAssignee.task_id.in_(task_ids))
                .distinct()
                .all()
            ]
            if assignee_ids:
                assigned_employees = db.query(User).filter(
                    User.employee_id.in_(assignee_ids)
                ).all()
        
        projects_data.append({
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "status": project.status,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "assigned_employees": [emp.name for emp in assigned_employees],
            "task_count": len(tasks),
            "employee_count": len(assigned_employees)
        })
    
    return projects_data


@app.get("/api/notifications")
async def get_notifications(
    offset: int = 0,
    limit: int = 25,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    existing_keys = {
        (n.title, n.message or "", n.link or "")
        for n in db.query(Notification)
        .filter(Notification.user_id == user.id)
        .all()
    }

    def add_if_missing(title: str, message: str, notif_type: str, link: str) -> None:
        key = (title, message, link)
        if key in existing_keys:
            return
        create_notification(db, user.id, title, message, notif_type, link)
        existing_keys.add(key)

    if user.current_team_id:
        team = db.query(Team).filter(Team.id == user.current_team_id).first()
        if team:
            add_if_missing(
                "Team assigned",
                f"You are assigned to team {team.name}.",
                "team",
                "/employee/team"
            )
            if team.project_id:
                project = db.query(Project).filter(Project.id == team.project_id).first()
                if project:
                    add_if_missing(
                        "Project assigned",
                        f"Project {project.name} is linked to your team.",
                        "project",
                        "/employee/team"
                    )

    assignments = db.query(ProjectAssignment).filter(ProjectAssignment.employee_id == user.employee_id).all()
    for assignment in assignments:
        project = db.query(Project).filter(Project.id == assignment.project_id).first()
        if project:
            add_if_missing(
                "Project assigned",
                f"You have been assigned to project {project.name}.",
                "project",
                "/employee/team"
            )

    tasks = db.query(Task).filter(Task.user_id == user.employee_id).order_by(Task.created_at.desc()).all()
    for task in tasks:
        add_if_missing(
            "Task assigned",
            f"Task '{task.title}' assigned.",
            "task",
            "/employee/tasks"
        )

    meetings = db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.employee_id == user.employee_id).all()
    for meeting_link in meetings:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_link.meeting_id).first()
        if meeting:
            add_if_missing(
                "Meeting assigned",
                f"Meeting '{meeting.title}' scheduled.",
                "meeting",
                "/employee"
            )

    leave_items = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == user.employee_id,
        LeaveRequest.status != "Pending"
    ).all()
    for leave in leave_items:
        add_if_missing(
            "Leave request updated",
            f"Your leave request was {leave.status}.",
            "leave",
            "/employee/leave"
        )

    db.commit()

    total_count = db.query(Notification).filter(Notification.user_id == user.id).count()
    items = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 100))
        .all()
    )
    unread_count = db.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.is_read == False
    ).count()

    return {
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "unread_count": unread_count,
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "message": item.message or "",
                "notif_type": item.notif_type or "",
                "link": item.link or "",
                "created_at": item.created_at.isoformat() if item.created_at else "",
                "is_read": item.is_read
            }
            for item in items
        ]
    }


@app.post("/api/notifications/read")
async def mark_notifications_read(
    notification_id: Optional[int] = Form(None),
    mark_all: Optional[bool] = Form(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if mark_all or notification_id is None:
        db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.is_read == False
        ).update({"is_read": True}, synchronize_session=False)
    else:
        db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.id == notification_id
        ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return {"ok": True}

#----------------------------------------
# SCHEDULER SETUP
# ----------------------------------------


def mark_absent():
    """Mark users with no AttendanceDaily record today as ABSENT.

    This runs as a scheduled job (23:59 daily).
    """
    db = SessionLocal()
    try:
        today = datetime.date.today()

        all_users = db.query(User).filter(User.is_active == True).all()
        present_rows = db.query(AttendanceDaily.user_id).filter(
            AttendanceDaily.date == today
        ).all()

        present_ids = {p[0] for p in present_rows}

        for u in all_users:
            if u.id not in present_ids:
                # Ensure we don't duplicate if another process added it concurrently
                exists = db.query(AttendanceDaily).filter(
                    AttendanceDaily.user_id == u.id,
                    AttendanceDaily.date == today
                ).first()
                if not exists:
                    db.add(AttendanceDaily(
                        user_id=u.id,
                        date=today,
                        status="ABSENT"
                    ))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def scheduler_loop():
    while True:
        auto_assign_leaders()
        time.sleep(300)

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(auto_assign_leaders, 'interval', minutes=5, id="leader_job")
    # Schedule daily absent marking at 23:59
    scheduler.add_job(mark_absent, 'cron', hour=23, minute=59, id="mark_absent_job")
    scheduler.start()

@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()
