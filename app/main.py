from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import inspect, text
import datetime
import csv
from pathlib import Path
from fastapi.exception_handlers import http_exception_handler

from .database import SessionLocal, engine, Base
from .models import AttendanceDaily, AttendanceDate, ProjectAssignment, ProjectTask, User
from .team_scheduler import auto_assign_leaders
from .auth_routes import router as auth_router
from .web_auth_routes import register_web_auth_routes
from .chat_routes import router as chat_router
from .calendar_routes import register_calendar_routes
from .admin_routes import register_admin_routes
from .manager_routes import register_manager_routes
from .employee_routes import register_employee_routes
from .api_routes import register_api_routes
from .app_context import templates, get_current_user, hash_employee_id
from .leader_dashboard_routes import router as leader_dashboard_router
from .error_handlers import register_error_handlers
from .custom_error_page import router as custom_error_router

BASE_DIR = Path(__file__).resolve().parent
scheduler = BackgroundScheduler()

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


def backfill_project_assignment_hashes() -> None:
    db = SessionLocal()
    try:
        rows = db.query(ProjectAssignment).filter(
            (ProjectAssignment.employee_id_hash == None) | (ProjectAssignment.employee_id_hash == "")
        ).all()
        for row in rows:
            if row.employee_id:
                row.employee_id_hash = hash_employee_id(row.employee_id)
        if rows:
            db.commit()
    except Exception as exc:
        db.rollback()
        print(f"Project assignment hash backfill failed: {exc}")
    finally:
        db.close()


def backfill_project_task_completed_at() -> None:
    db = SessionLocal()
    try:
        rows = db.query(ProjectTask).filter(
            ProjectTask.status == "completed",
            ProjectTask.completed_at == None
        ).all()
        for row in rows:
            row.completed_at = row.created_at or datetime.datetime.utcnow()
        if rows:
            db.commit()
    except Exception as exc:
        db.rollback()
        print(f"Project task completed_at backfill failed: {exc}")
    finally:
        db.close()


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
    except Exception as exc:
        db.rollback()
        print(f"Attendance date migration failed: {exc}")
    finally:
        db.close()


def mark_absent() -> None:
    """Mark users with no AttendanceDaily record today as ABSENT."""
    db = SessionLocal()
    try:
        today = datetime.date.today()

        all_users = db.query(User).filter(User.is_active == True).all()
        present_rows = db.query(AttendanceDaily.user_id).filter(
            AttendanceDaily.date == today
        ).all()

        present_ids = {p[0] for p in present_rows}

        for user in all_users:
            if user.id not in present_ids:
                exists = db.query(AttendanceDaily).filter(
                    AttendanceDaily.user_id == user.id,
                    AttendanceDaily.date == today
                ).first()
                if not exists:
                    db.add(AttendanceDaily(
                        user_id=user.id,
                        date=today,
                        status="ABSENT"
                    ))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()




import time

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="super-secret-key")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(leader_dashboard_router)
app.include_router(custom_error_router)
register_web_auth_routes(app)
register_calendar_routes(app, templates, get_current_user)
register_admin_routes(app)
register_manager_routes(app)
register_employee_routes(app)
register_api_routes(app)
register_error_handlers(app)

#======================================================================================================
# DO NOT TOUCH THIS PART NO MATTER WHO YOU ARE--------------------------------------------------------
#======================================================================================================

# Timing middleware to log request duration
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = (time.perf_counter() - start) * 1000  # ms
    print(f"[TIMING] {request.method} {request.url.path} took {duration:.2f} ms")
    return response

# No-cache middleware
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # Only apply to protected routes (admin and employee)
    if request.url.path.startswith("/admin") or request.url.path.startswith("/employee"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

@app.middleware("http")
async def prevent_back_after_logout(request: Request, call_next):
    # Only run this middleware if SessionMiddleware is present
    if "session" not in request.scope:
        return await call_next(request)
    session = request.session
    user_id = session.get("user_id")
    # List of protected route prefixes
    protected_prefixes = ["/admin", "/employee", "/manager", "/leader", "/api"]
    is_protected = any(request.url.path.startswith(prefix) for prefix in protected_prefixes)
    is_login_page = request.url.path == "/login"
    # If not authenticated and accessing protected or login page via back button
    if not user_id and (is_protected or is_login_page) and request.method == "GET":
        return templates.TemplateResponse("auth/401.html", {"request": request}, status_code=401)
    return await call_next(request)

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return templates.TemplateResponse("auth/401.html", {"request": request}, status_code=401)
    
    # Use the imported default handler for all other errors
    return await http_exception_handler(request, exc)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++


@app.get("/")
def root_redirect():
    return RedirectResponse("/login", status_code=303)


@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(auto_assign_leaders, "interval", minutes=5, id="leader_job")
    scheduler.add_job(mark_absent, "cron", hour=23, minute=59, id="mark_absent_job")
    scheduler.start()


@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()
