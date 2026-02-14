from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import inspect, text
import datetime
import csv
from pathlib import Path
import os
from Security.security_config import SECURITY_SETTINGS

from .database import SessionLocal, engine, Base
from .models import AttendanceDaily, AttendanceDate, ProjectAssignment, ProjectTask, SecurityManagedSetting, User
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
from .routes_security import router as security_router
from .security_bootstrap import initialize_encryption
from Security.audit_trail import set_audit_request_context, clear_audit_request_context

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR.parent / "logs"
SCHEMA_SYNC_LOG = LOG_DIR / "schema_sync.log"
RUNTIME_SECRET_SYNC_LOG = LOG_DIR / "runtime_secret_sync.log"
scheduler = BackgroundScheduler()
DB_BACKED_SECRET_KEYS = ("SECRET_KEY", "SESSION_SECRET_KEY", "ENCRYPTION_KEY", "DATA_ENCRYPTION_KEY")

def log_schema_sync(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().isoformat()
    with SCHEMA_SYNC_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}Z] {message}\n")

def log_runtime_secret_sync(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().isoformat()
    with RUNTIME_SECRET_SYNC_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}Z] {message}\n")


def _is_severe_db_issue(message: str) -> bool:
    low = (message or "").lower()
    severe_markers = (
        "corrupt",
        "crashed",
        "tablespace",
        "disk full",
        "no space left",
        "read-only file system",
        "i/o error",
        "innodb",
        "engine",
        "can't open file",
        "cannot open file",
        "page corruption",
        "fatal",
    )
    return any(marker in low for marker in severe_markers)


def _record_severe_db_issue(severe_errors: list[str], context: str, exc: Exception) -> None:
    msg = f"{context}: {exc}"
    if _is_severe_db_issue(msg):
        severe_errors.append(msg)
        log_schema_sync(f"SEVERE_DB_ISSUE: {msg}")


def _auto_fix_table_health(preparer, severe_errors: list[str]) -> tuple[int, int]:
    """
    Best-effort table health check and repair for MariaDB/MySQL.
    - Uses CHECK TABLE and REPAIR TABLE where supported.
    - Non-fatal: logs issues and continues startup.
    """
    backend = (engine.url.get_backend_name() or "").lower()
    if "mysql" not in backend:
        log_schema_sync("Table health check skipped: backend is not MySQL/MariaDB")
        return 0, 0

    checked = 0
    repaired = 0
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    with engine.begin() as conn:
        for table_name in table_names:
            quoted_table = preparer.quote(table_name)
            try:
                check_rows = conn.execute(text(f"CHECK TABLE {quoted_table}")).mappings().all()
                checked += 1

                needs_repair = False
                for row in check_rows:
                    msg_type = str(row.get("Msg_type", "")).lower()
                    msg_text = str(row.get("Msg_text", "")).lower()
                    if msg_type == "error" or "corrupt" in msg_text or "crashed" in msg_text:
                        needs_repair = True
                        break

                if not needs_repair:
                    continue

                repair_rows = conn.execute(text(f"REPAIR TABLE {quoted_table}")).mappings().all()
                repaired += 1
                log_schema_sync(
                    f"Repaired table: {table_name} ({'; '.join(str(r.get('Msg_text', '')) for r in repair_rows)})"
                )
            except Exception as exc:
                log_schema_sync(f"Table health check/repair skipped for {table_name}: {exc}")
                _record_severe_db_issue(
                    severe_errors,
                    f"Table health check/repair failed for {table_name}",
                    exc,
                )

    return checked, repaired

def auto_sync_schema() -> None:
    """Create missing tables and add missing columns (no drops/changes)."""
    try:
        # First pass: create all ORM tables if missing.
        Base.metadata.create_all(bind=engine, checkfirst=True)

        preparer = engine.dialect.identifier_preparer
        def q(name: str) -> str:
            return preparer.quote(name)

        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        created_tables = 0
        added_columns = 0
        repaired_unique_indexes = 0
        severe_db_issues: list[str] = []

        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                table.create(bind=engine)
                created_tables += 1
                log_schema_sync(f"Created table: {table.name}")

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

                sql = f"ALTER TABLE {q(table.name)} ADD COLUMN {q(col.name)} {col_type} {nullable}{default_clause}"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    added_columns += 1
                    log_schema_sync(f"Added column: {table.name}.{col.name}")
                except Exception as exc:
                    print(f"Schema sync skipped {table.name}.{col.name}: {exc}")
                    log_schema_sync(f"Skipped column: {table.name}.{col.name} ({exc})")
                    _record_severe_db_issue(severe_db_issues, f"Add column failed {table.name}.{col.name}", exc)

            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name in existing_indexes:
                    continue
                cols = ", ".join(q(c.name) for c in idx.columns)
                unique_clause = "UNIQUE " if idx.unique else ""
                sql = f"CREATE {unique_clause}INDEX {q(idx.name)} ON {q(table.name)} ({cols})"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    log_schema_sync(f"Created index: {table.name}.{idx.name}")
                except Exception as exc:
                    print(f"Schema sync index skipped {table.name}.{idx.name}: {exc}")
                    log_schema_sync(f"Skipped index: {table.name}.{idx.name} ({exc})")
                    _record_severe_db_issue(severe_db_issues, f"Create index failed {table.name}.{idx.name}", exc)

            existing_unique = {u.get("name") for u in inspector.get_unique_constraints(table.name)}
            index_defs = inspector.get_indexes(table.name)
            unique_indexes_by_cols = {}
            for idx_def in index_defs:
                if not idx_def.get("unique"):
                    continue
                cols_key = tuple(idx_def.get("column_names") or [])
                unique_indexes_by_cols.setdefault(cols_key, []).append(idx_def.get("name"))
            for constraint in table.constraints:
                if not getattr(constraint, "columns", None):
                    continue
                if constraint.__class__.__name__ != "UniqueConstraint":
                    continue
                cname = getattr(constraint, "name", None)
                if not cname or cname in existing_unique:
                    continue
                col_names = tuple(c.name for c in constraint.columns)
                existing_idx_names = unique_indexes_by_cols.get(col_names, [])
                if existing_idx_names:
                    # One-time repair: ensure the unique index has the expected name.
                    if cname not in existing_idx_names:
                        old_name = existing_idx_names[0]
                        rename_sql = f"ALTER TABLE {q(table.name)} RENAME INDEX {q(old_name)} TO {q(cname)}"
                        try:
                            with engine.begin() as conn:
                                conn.execute(text(rename_sql))
                            existing_unique.add(cname)
                            repaired_unique_indexes += 1
                            log_schema_sync(
                                f"Repaired unique index: {table.name} {old_name} -> {cname}"
                            )
                        except Exception as exc:
                            print(f"Schema sync unique repair skipped {table.name}.{cname}: {exc}")
                            log_schema_sync(
                                f"Skipped unique repair: {table.name}.{cname} ({exc})"
                            )
                            _record_severe_db_issue(severe_db_issues, f"Unique index repair failed {table.name}.{cname}", exc)
                    continue
                cols = ", ".join(q(c.name) for c in constraint.columns)
                sql = f"ALTER TABLE {q(table.name)} ADD CONSTRAINT {q(cname)} UNIQUE ({cols})"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    log_schema_sync(f"Created unique constraint: {table.name}.{cname}")
                except Exception as exc:
                    print(f"Schema sync unique skipped {table.name}.{cname}: {exc}")
                    log_schema_sync(f"Skipped unique constraint: {table.name}.{cname} ({exc})")
                    _record_severe_db_issue(severe_db_issues, f"Create unique constraint failed {table.name}.{cname}", exc)

            existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys(table.name)}
            for constraint in table.constraints:
                if constraint.__class__.__name__ != "ForeignKeyConstraint":
                    continue
                cname = getattr(constraint, "name", None)
                if not cname or cname in existing_fks:
                    continue
                if table.name in {"users", "teams"}:
                    continue
                local_cols = ", ".join(q(c.name) for c in constraint.columns)
                remote_cols = ", ".join([f"{q(fk.column.table.name)}.{q(fk.column.name)}" for fk in constraint.elements])
                sql = f"ALTER TABLE {q(table.name)} ADD CONSTRAINT {q(cname)} FOREIGN KEY ({local_cols}) REFERENCES {remote_cols}"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                    log_schema_sync(f"Created foreign key: {table.name}.{cname}")
                except Exception as exc:
                    print(f"Schema sync fk skipped {table.name}.{cname}: {exc}")
                    log_schema_sync(f"Skipped foreign key: {table.name}.{cname} ({exc})")
                    _record_severe_db_issue(severe_db_issues, f"Create foreign key failed {table.name}.{cname}", exc)
        checked_tables, repaired_tables = _auto_fix_table_health(preparer, severe_db_issues)

        print(
            "Schema sync complete: "
            f"tables_created={created_tables}, columns_added={added_columns}, "
            f"unique_index_repairs={repaired_unique_indexes}, "
            f"tables_checked={checked_tables}, tables_repaired={repaired_tables}"
        )
        log_schema_sync(
            "Schema sync complete: "
            f"tables_created={created_tables}, columns_added={added_columns}, "
            f"unique_index_repairs={repaired_unique_indexes}, "
            f"tables_checked={checked_tables}, tables_repaired={repaired_tables}"
        )
        if severe_db_issues:
            details = " | ".join(severe_db_issues[:3])
            failure_message = (
                "Severe database engine/storage issue detected during startup. "
                "Manual DBA recovery is required. "
                f"Details: {details}"
            )
            print(failure_message)
            log_schema_sync(failure_message)
            raise RuntimeError(failure_message)
    except Exception as exc:
        print(f"Schema sync failed: {exc}")
        log_schema_sync(f"Schema sync failed: {exc}")
        raise


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


def sync_runtime_secrets_from_db() -> None:
    """
    DB-first secret loading for runtime.
    - Reads sensitive keys from security_managed_settings.
    - If missing in DB, seeds from environment once.
    - Exposes values through os.environ for runtime consumers.
    """
    db = SessionLocal()
    created = 0
    loaded = 0
    missing = 0
    try:
        for key in DB_BACKED_SECRET_KEYS:
            row = (
                db.query(SecurityManagedSetting)
                .filter(
                    SecurityManagedSetting.feature_id == "security-config",
                    SecurityManagedSetting.key == key,
                )
                .first()
            )
            if row and (row.value or "").strip():
                os.environ[key] = row.value.strip()
                loaded += 1
                continue

            env_value = (os.getenv(key) or "").strip()
            if not env_value:
                missing += 1
                continue

            db.add(SecurityManagedSetting(feature_id="security-config", key=key, value=env_value))
            os.environ[key] = env_value
            created += 1
            loaded += 1

        # Keep paired keys in sync at runtime.
        if os.getenv("SECRET_KEY") and not os.getenv("SESSION_SECRET_KEY"):
            os.environ["SESSION_SECRET_KEY"] = os.environ["SECRET_KEY"]
        if os.getenv("SESSION_SECRET_KEY") and not os.getenv("SECRET_KEY"):
            os.environ["SECRET_KEY"] = os.environ["SESSION_SECRET_KEY"]
        if os.getenv("ENCRYPTION_KEY") and not os.getenv("DATA_ENCRYPTION_KEY"):
            os.environ["DATA_ENCRYPTION_KEY"] = os.environ["ENCRYPTION_KEY"]
        if os.getenv("DATA_ENCRYPTION_KEY") and not os.getenv("ENCRYPTION_KEY"):
            os.environ["ENCRYPTION_KEY"] = os.environ["DATA_ENCRYPTION_KEY"]

        if created:
            db.commit()
        print(f"Runtime secret sync complete: loaded={loaded}, created_in_db={created}")
        log_runtime_secret_sync(
            "Runtime secret sync complete: "
            f"loaded={loaded}, created_in_db={created}, missing={missing}"
        )
    except Exception as exc:
        db.rollback()
        print(f"Runtime secret sync failed: {exc}")
        log_runtime_secret_sync(f"Runtime secret sync failed: {exc}")
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
app.include_router(security_router)
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
    def _runtime_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return int(default)

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
    if user_id:
        now_ts = int(time.time())
        session_max_age = _runtime_int("SESSION_MAX_AGE", int(SECURITY_SETTINGS.get("SESSION_MAX_AGE", 600)))
        session_idle_timeout = _runtime_int("SESSION_IDLE_TIMEOUT", int(SECURITY_SETTINGS.get("SESSION_IDLE_TIMEOUT", 600)))
        created = int(session.get("_created", now_ts))
        last_seen = int(session.get("_last_seen", created))
        absolute_expired = bool(session_max_age) and (now_ts - created) > session_max_age
        idle_expired = bool(session_idle_timeout) and (now_ts - last_seen) > session_idle_timeout
        if absolute_expired or idle_expired:
            session.clear()
            return RedirectResponse("/login", status_code=303)
        session.setdefault("_created", created)
        if is_protected:
            session["_last_seen"] = now_ts
        else:
            session.setdefault("_last_seen", last_seen)
    return await call_next(request)


@app.middleware("http")
async def bind_audit_context(request: Request, call_next):
    token = set_audit_request_context(request)
    try:
        return await call_next(request)
    finally:
        clear_audit_request_context(token)

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++


@app.get("/")
def root_redirect():
    return RedirectResponse("/login", status_code=303)



# On startup, auto-sync DB schema (create missing tables/columns)
@app.on_event("startup")
def startup_event():
    auto_sync_schema()
    sync_runtime_secrets_from_db()
    initialize_encryption()
    scheduler.add_job(auto_assign_leaders, "interval", minutes=5, id="leader_job")
    scheduler.add_job(mark_absent, "cron", hour=23, minute=59, id="mark_absent_job")
    scheduler.start()


@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()



