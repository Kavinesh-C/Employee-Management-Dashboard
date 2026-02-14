from fastapi import Depends, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional
import datetime
import random
import string

# ...existing code...

# Place this endpoint after app is defined and other endpoints are declared
# ...existing code...
from fastapi import Depends, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional
import datetime
import random
import string
import pandas as pd


from .database import get_db
from .models import (
    ProjectMeetingAssignee, User, Attendance, RemovedEmployee, UnknownRFID, Room, Department,
    Task, LeaveRequest, Team, TeamMember, Payroll, ProjectTask, ProjectTaskAssignee,
    EmailSettings, InappropriateEntry
)
from .auth import hash_password
from .email_service import send_welcome_email, send_leave_status_email
from .app_context import templates, get_current_user, create_notification
from .payroll_utils import calculate_monthly_payroll
from Security.data_integrity import sha256_hex
from Security.hash_history import log_hash_history
from .security_bootstrap import encrypt_value


def _hash_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return sha256_hex(cleaned)


def _sync_user_hashes(emp: User, actor: User | None = None, details: str = "update") -> None:
    fields = {
        "employee_id": ("employee_id_hash", emp.employee_id),
        "name": ("name_hash", emp.name),
        "email": ("email_hash", emp.email),
        "rfid_tag": ("rfid_tag_hash", emp.rfid_tag),
        "role": ("role_hash", emp.role),
        "department": ("department_hash", emp.department),
    }
    for field_name, (hash_attr, source_value) in fields.items():
        old_hash = getattr(emp, hash_attr, None)
        new_hash = _hash_optional(source_value)
        if old_hash == new_hash:
            continue
        setattr(emp, hash_attr, new_hash)
        log_hash_history(
            entity_type="User",
            entity_id=emp.employee_id,
            field_name=field_name,
            old_hash=old_hash,
            new_hash=new_hash,
            actor_id=str(actor.id) if actor else None,
            actor_name=actor.name if actor else None,
            employee_name=emp.name,
            details=details,
        )


def _sync_user_secure_fields(emp: User) -> None:
    # Keep encrypted mirrors in sync so values can be safely revealed where required.
    emp.name_secure = encrypt_value(emp.name)
    emp.email_secure = encrypt_value(emp.email)
    emp.rfid_tag_secure = encrypt_value(emp.rfid_tag)
    emp.role_secure = encrypt_value(emp.role)
    emp.department_secure = encrypt_value(emp.department)


def register_admin_routes(app):
    @app.post("/admin/update_department")
    async def update_department(request: Request, id: int = Form(...), name: str = Form(...), description: str = Form(None), prefix: str = Form(None),
                               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        dept = db.query(Department).filter(Department.id == id).first()
        if not dept:
            raise HTTPException(status_code=404, detail="Department not found")
        dept.name = name
        dept.description = description
        dept.prefix = prefix
        db.commit()
        return RedirectResponse(url="/admin/settings", status_code=303)
    @app.get("/admin/select_dashboard", response_class=HTMLResponse)
    async def admin_choice(request: Request, user: User = Depends(get_current_user)):
        return templates.TemplateResponse("admin/admin_select_dashboard.html", {"request": request, "user": user})

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403)
        
        # Fetch live blocks data (occupancy by room)
        from sqlalchemy import func
        import datetime as dt

        # Identify rooms registered in Room table
        rooms = db.query(Room).all()
        valid_rooms = {(r.location_name, r.room_no) for r in rooms}

        # Detect any attendance records today that reference invalid rooms and log them
        today = dt.date.today()
        start_of_day = dt.datetime.combine(today, dt.time.min)
        invalid_attendances = db.query(Attendance).filter(Attendance.date == today).all()
        for a in invalid_attendances:
            if (a.location_name, a.room_no) not in valid_rooms:
                # avoid duplicate logs for same employee/location/room within same day
                exists = db.query(InappropriateEntry).filter(
                    InappropriateEntry.employee_id == a.employee_id,
                    InappropriateEntry.location_name == a.location_name,
                    InappropriateEntry.room_no == a.room_no,
                    InappropriateEntry.timestamp >= start_of_day
                ).first()
                if not exists:
                    db.add(InappropriateEntry(
                        employee_id=a.employee_id,
                        rfid_tag=(a.user.rfid_tag if getattr(a, 'user', None) else ''),
                        location_name=a.location_name,
                        room_no=a.room_no,
                        reason=f"Recorded in non-registered room",
                        timestamp=a.entry_time or dt.datetime.utcnow()
                    ))
        db.commit()

        # Now aggregate only rooms that exist in Room table
        blocks_data = (
            db.query(
                Attendance.location_name,
                Attendance.room_no,
                func.count(Attendance.id).label("count")
            )
            .join(Room, (Room.location_name == Attendance.location_name) & (Room.room_no == Attendance.room_no))
            .filter(
                Attendance.exit_time.is_(None),
                Attendance.date == today
            )
            .group_by(Attendance.location_name, Attendance.room_no)
            .all()
        )
        blocks = [
            {
                "location_name": b.location_name,
                "room_no": b.room_no,
                "count": b.count
            }
            for b in blocks_data
        ]

        # Fetch inappropriate entries (invalid rooms)
        inappropriate_entries = db.query(InappropriateEntry).order_by(InappropriateEntry.timestamp.desc()).limit(20).all()
        
        # Other dashboard datasets
        employees = db.query(User).filter(User.is_active == True).all()
        total_active = len(employees)
        # Number of employees currently inside campus: distinct open attendances today
        present_count = db.query(func.count(func.distinct(Attendance.employee_id))).filter(
            Attendance.exit_time.is_(None),
            Attendance.date == today
        ).scalar() or 0
        try:
            present_count = int(present_count)
        except Exception:
            present_count = 0
        absentee_count = max(0, total_active - present_count)
        unknown_rfids = db.query(UnknownRFID).order_by(UnknownRFID.timestamp.desc()).limit(5).all()
        admins = db.query(User).filter(User.role == "admin").all()
        removed_employees = db.query(RemovedEmployee).order_by(RemovedEmployee.removed_at.desc()).limit(5).all()

        return templates.TemplateResponse("admin/admin_dashboard.html",
                           {"request": request,
                            "user": user,
                            "blocks": blocks,
                            "employees": employees,
                            "unknown_rfids": unknown_rfids,
                            "admins": admins,
                            "removed_employees": removed_employees,
                            "inappropriate_entries": inappropriate_entries,
                            "present_count": present_count,
                            "absentee_count": absentee_count
                            }
                            )

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
        existing_name = db.query(User).filter(User.name == name).first()
        if existing_name:
            raise HTTPException(status_code=400, detail=f"Name '{name}' already exists in the system")

        existing_email = db.query(User).filter(User.email == email).first()
        if existing_email:
            raise HTTPException(status_code=400, detail=f"Email '{email}' already exists in the system")

        existing_rfid = db.query(User).filter(User.rfid_tag == rfid_tag).first()
        if existing_rfid:
            raise HTTPException(status_code=400, detail=f"RFID tag '{rfid_tag}' is already assigned to another employee")

        dept_obj = db.query(Department).filter(Department.name == department).first()
        prefix = dept_obj.prefix if dept_obj and dept_obj.prefix else "2260"
        max_emp = db.query(User).filter(User.employee_id.like(f"{prefix}%")).order_by(User.employee_id.desc()).first()
        if max_emp and len(max_emp.employee_id) > len(prefix):
            try:
                suffix = int(max_emp.employee_id[len(prefix):])
                next_id = suffix + 1
            except ValueError:
                next_id = 1
        else:
            next_id = 1
        employee_id = f"{prefix}{next_id:03d}"
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
        _sync_user_secure_fields(new_user)
        _sync_user_hashes(new_user, actor=user, details="create")
        db.add(new_user)
        db.commit()
        email_sent = send_welcome_email(email, name, employee_id, password)
        return {"employee_id": employee_id, "password": password, "email_sent": email_sent}

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

    @app.post("/admin/remove_employee")
    async def remove_employee(request: Request, employee_id: str = Form(...), user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        emp = db.query(User).filter(User.employee_id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        # Remove all project meeting assignees for this employee to avoid FK constraint
        db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.employee_id == emp.employee_id).delete(synchronize_session=False)
        db.query(TeamMember).filter(TeamMember.user_id == emp.id).delete(synchronize_session=False)
        db.query(Team).filter(Team.leader_id == emp.id).update({Team.leader_id: None}, synchronize_session=False)
        db.query(Team).filter(Team.permanent_leader_id == emp.id).update(
            {Team.permanent_leader_id: None},
            synchronize_session=False,
        )
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

        _sync_user_secure_fields(emp)
        _sync_user_hashes(emp, actor=user, details="admin_update")
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

    @app.post("/admin/add_room")
    async def add_room(request: Request, room_no: str = Form(...), location_name: str = Form(...),
                       description: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

        existing_room = db.query(Room).filter(Room.room_no == room_no, Room.location_name == location_name).first()
        if existing_room:
            raise HTTPException(status_code=400, detail="Room already exists")

        room_id = f"R{room_no}"
        new_room = Room(room_id=room_id, room_no=room_no, location_name=location_name, description=description)
        db.add(new_room)
        db.commit()
        return {"room_id": room_id, "message": "Room added successfully"}

    @app.post("/admin/add_department")
    async def add_department(request: Request, name: str = Form(...), description: str = Form(...), prefix: str = Form(None),
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")

        existing_dept = db.query(Department).filter(Department.name == name).first()
        if existing_dept:
            raise HTTPException(status_code=400, detail="Department already exists")

        new_dept = Department(name=name, description=description, prefix=prefix)
        db.add(new_dept)
        db.commit()
        return {"message": "Department added successfully"}

    @app.post("/admin/remove_room")
    async def remove_room(request: Request, room_id: str = Form(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        room = db.query(Room).filter(Room.room_id == room_id).first()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        active_attendance = db.query(Attendance).filter(Attendance.room_id == room_id, Attendance.exit_time.is_(None)).first()
        if active_attendance:
            raise HTTPException(status_code=400, detail="Cannot remove room with active attendance")
        db.delete(room)
        db.commit()
        return {"message": "Room removed successfully"}

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

    @app.post("/admin/resolve_rfid")
    async def resolve_rfid(request: Request, rfid_tag: str = Form(...), db: Session = Depends(get_db)):
        db.query(UnknownRFID).filter(UnknownRFID.rfid_tag == rfid_tag).delete()
        db.commit()
        return RedirectResponse("/admin/unknown_rfid", status_code=303)

    @app.get("/admin/inappropriate_entries", response_class=HTMLResponse)
    async def admin_inappropriate_entries(
        request: Request,
        search: Optional[str] = None,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        query = db.query(InappropriateEntry)
        if search:
            query = query.filter(
                (InappropriateEntry.employee_id.like(f"%{search}%")) |
                (InappropriateEntry.room_no.like(f"%{search}%")) |
                (InappropriateEntry.location_name.ilike(f"%{search}%"))
            )
        entries = query.order_by(InappropriateEntry.timestamp.desc()).all()
        return templates.TemplateResponse(
            "admin/admin_inappropriate_entries.html",
            {
                "request": request,
                "user": user,
                "search": search,
                "entries": entries,
                "current_year": datetime.datetime.utcnow().year
            }
        )

    @app.post("/admin/delete_inappropriate_entry")
    async def delete_inappropriate_entry(request: Request, entry_id: int = Form(...), db: Session = Depends(get_db)):
        db.query(InappropriateEntry).filter(InappropriateEntry.id == entry_id).delete()
        db.commit()
        return RedirectResponse("/admin/inappropriate_entries", status_code=303)

    @app.get("/admin/leave_requests", response_class=HTMLResponse)
    async def admin_leave_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        pending = db.query(LeaveRequest).order_by(LeaveRequest.id.desc()).all()
        return templates.TemplateResponse("admin/admin_leave_requests.html",
                                          {"request": request, "user": user, "pending": pending,
                                           "current_year": datetime.datetime.utcnow().year})

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
    
    @app.get("/admin/attendance-intelligence", response_class=HTMLResponse)
    async def admin_attendance_intelligence(
        request: Request,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)
    ):
        if user.role not in ("admin", "manager"):
            raise HTTPException(status_code=403)

        from app.analytics.attendance_intelligence import (
            get_attendance_dataframe,
            compute_behavior_metrics,
            detect_attendance_anomalies,
            compute_performer_lists
        )

        df = get_attendance_dataframe(db)
        metrics = compute_behavior_metrics(db, df)
        anomalies = detect_attendance_anomalies(df)
        top_performers, low_performers = compute_performer_lists(db)

        return templates.TemplateResponse(
            "admin/admin_attendance_intelligence.html",
            {
                "request": request,
                "user": user,
                "metrics": metrics,
                "anomalies": anomalies,
                "top_performers": top_performers,
                "low_performers": low_performers
            }
        )

