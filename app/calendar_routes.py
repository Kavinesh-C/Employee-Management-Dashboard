"""
Calendar Routes Module
Handles all calendar-related endpoints for managing calendar events, holidays, and settings.
"""

from fastapi import FastAPI, Depends, HTTPException, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import datetime
import holidays
import re

from .database import get_db
from .models import User, CalendarEvent, CalendarSettings, LeaveRequest, OfficeHoliday, Task, ProjectTask, ProjectTaskAssignee, Meeting, ProjectMeetingAssignee
import hashlib



# Security utility functions (inline implementations)
def sha256_hex(value: str) -> str:
    """Hash a value using SHA-256"""
    return hashlib.sha256(value.encode()).hexdigest()


def sanitize_db_text(value: str, max_len: int = 200) -> str:
    """Basic text sanitization for database"""
    if not value:
        return ""
    sanitized = value.strip()
    if max_len and len(sanitized) > max_len:
        sanitized = sanitized[:max_len]
    return sanitized


def validate_allowlist(value: str, pattern: str | list) -> bool:
    """Validate that value matches pattern (regex string or list membership)"""
    if isinstance(pattern, list):
        return value in pattern
    elif isinstance(pattern, str):
        try:
            return bool(re.match(pattern, value))
        except Exception:
            return False
    return True


def _hash_value(value: str | None) -> str | None:
    """Hash a value using SHA-256"""
    if value is None:
        return None
    return sha256_hex(value)


def _sanitize_required(value: str | None, field: str, max_len: int = 200, pattern: str | None = None) -> str:
    """Sanitize required field"""
    value = sanitize_db_text(value if value else "", max_len=max_len)
    if pattern:
        if not validate_allowlist(value, pattern):
            raise HTTPException(status_code=400, detail=f"Invalid {field}")
    if not value:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value


def _sanitize_optional(value: str | None, max_len: int = 200) -> str | None:
    """Sanitize optional field"""
    if not value:
        return None
    return sanitize_db_text(value, max_len=max_len)


# Countries list as requested (names only, no official-name variants)
_COUNTRY_NAME_LIST = [
    "Algeria", "Angola", "Benin", "Botswana", "Burkina Faso", "Burundi", "Cabo Verde",
    "Cameroon", "Central African Republic", "Chad", "Comoros", "Congo (Brazzaville)",
    "Congo (Kinshasa)", "Côte d'Ivoire", "Djibouti", "Egypt", "Equatorial Guinea",
    "Eritrea", "Eswatini", "Ethiopia", "Gabon", "Gambia", "Ghana", "Guinea",
    "Guinea-Bissau", "Kenya", "Lesotho", "Liberia", "Libya", "Madagascar", "Malawi",
    "Mali", "Mauritania", "Mauritius", "Morocco", "Mozambique", "Namibia", "Niger",
    "Nigeria", "Rwanda", "Sao Tome and Principe", "Senegal", "Seychelles",
    "Sierra Leone", "Somalia", "South Africa", "South Sudan", "Sudan", "Tanzania",
    "Togo", "Tunisia", "Uganda", "Zambia", "Zimbabwe",
    "Afghanistan", "Armenia", "Azerbaijan", "Bahrain", "Bangladesh", "Bhutan", "Brunei",
    "Cambodia", "China", "Cyprus", "Georgia", "India", "Indonesia", "Iran", "Iraq",
    "Israel", "Japan", "Jordan", "Kazakhstan", "Kuwait", "Kyrgyzstan", "Laos",
    "Lebanon", "Malaysia", "Maldives", "Mongolia", "Myanmar", "Nepal", "North Korea",
    "Oman", "Pakistan", "Palestine", "Philippines", "Qatar", "Russia", "Saudi Arabia",
    "Singapore", "South Korea", "Sri Lanka", "Syria", "Taiwan", "Tajikistan", "Thailand",
    "Timor-Leste", "Turkey", "Turkmenistan", "United Arab Emirates", "Uzbekistan",
    "Vietnam", "Yemen",
    "Albania", "Andorra", "Austria", "Belarus", "Belgium", "Bosnia and Herzegovina",
    "Bulgaria", "Croatia", "Czechia", "Denmark", "Estonia", "Finland", "France",
    "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy", "Kosovo", "Latvia",
    "Liechtenstein", "Lithuania", "Luxembourg", "Malta", "Moldova", "Monaco",
    "Montenegro", "Netherlands", "North Macedonia", "Norway", "Poland", "Portugal",
    "Romania", "San Marino", "Serbia", "Slovakia", "Slovenia", "Spain", "Sweden",
    "Switzerland", "Ukraine", "United Kingdom", "Vatican City",
    "Antigua and Barbuda", "Bahamas", "Barbados", "Belize", "Canada", "Costa Rica",
    "Cuba", "Dominica", "Dominican Republic", "El Salvador", "Grenada", "Guatemala",
    "Haiti", "Honduras", "Jamaica", "Mexico", "Nicaragua", "Panama",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines",
    "Trinidad and Tobago", "United States of America",
    "Argentina", "Bolivia", "Brazil", "Chile", "Colombia", "Ecuador", "Guyana",
    "Paraguay", "Peru", "Suriname", "Uruguay", "Venezuela",
    "Australia", "Fiji", "Kiribati", "Marshall Islands", "Micronesia", "Nauru",
    "New Zealand", "Palau", "Papua New Guinea", "Samoa", "Solomon Islands", "Tonga",
    "Tuvalu", "Vanuatu",
]

_COUNTRY_CODE_OVERRIDES = {
    "Bolivia": "BO",
    "Brunei": "BN",
    "Cabo Verde": "CV",
    "Congo (Brazzaville)": "CG",
    "Congo (Kinshasa)": "CD",
    "Côte d'Ivoire": "CI",
    "Czechia": "CZ",
    "Eswatini": "SZ",
    "Kosovo": "XK",
    "Laos": "LA",
    "North Macedonia": "MK",
    "Micronesia": "FM",
    "Moldova": "MD",
    "North Korea": "KP",
    "Palestine": "PS",
    "Russia": "RU",
    "South Korea": "KR",
    "Syria": "SY",
    "Taiwan": "TW",
    "Tanzania": "TZ",
    "Timor-Leste": "TL",
    "United Kingdom": "GB",
    "United States of America": "US",
    "Venezuela": "VE",
    "Vatican City": "VA",
    "Vietnam": "VN",
    "Sao Tome and Principe": "ST",
}


def _country_code_from_name(name: str) -> str | None:
    """Resolve ISO alpha-2 code from a provided country name"""
    if name in _COUNTRY_CODE_OVERRIDES:
        return _COUNTRY_CODE_OVERRIDES[name]

    pycountry = _safe_pycountry()
    if not pycountry:
        return None

    try:
        matches = pycountry.countries.search_fuzzy(name)
        if matches:
            return getattr(matches[0], "alpha_2", None)
    except Exception:
        return None
    return None


def _countries_list() -> list[dict]:
    """Get list of countries for selection (provided names)"""
    countries = []
    for name in _COUNTRY_NAME_LIST:
        code = _country_code_from_name(name)
        countries.append({"code": code or "", "name": name})
    return countries


def _subdivisions_list(country_code: str) -> list[dict]:
    """Get list of subdivisions/states for a country (full names)"""
    pycountry = _safe_pycountry()
    items = []
    if not pycountry or not country_code:
        return items

    for subdivision in pycountry.subdivisions:
        if getattr(subdivision, "country_code", None) != country_code:
            continue
        code = getattr(subdivision, "code", "")
        name = getattr(subdivision, "official_name", None) or getattr(subdivision, "name", None)
        if code and name:
            items.append({"code": code.split("-")[-1], "name": name})

    items.sort(key=lambda s: s["name"])
    return items


def _valid_country_codes() -> set[str]:
    return {c["code"] for c in _countries_list() if c["code"]}


def _safe_pycountry():
    """Safely import pycountry if available"""
    try:
        import pycountry
        return pycountry
    except Exception:
        return None


def register_calendar_routes(app: FastAPI, templates, get_current_user):
    """Register all calendar routes to the FastAPI app
    
    LEAVE REQUEST VISIBILITY & APPROVAL WORKFLOW:
    ============================================
    
    1. EMPLOYEE PERSONAL LEAVES:
       - Employee applies for leave (status = "Pending")
       - Manager OR Admin approves the leave (status = "Approved")
       - Only that EMPLOYEE can see their approved leaves in their calendar
       - Manager/Admin cannot see employee personal leaves in their own calendar
    
    2. MANAGER PERSONAL LEAVES:
       - Manager applies for leave (status = "Pending")
       - Only ADMIN can approve manager leaves (status = "Approved")
       - Only that MANAGER can see their approved leaves in their calendar
       - Admin cannot see manager personal leaves in their own calendar
    
    3. OFFICE HOLIDAYS:
       - Admin creates office holidays
       - Visible to ALL users (employees, managers, admins)
       - Cannot be deleted by regular users, only admins
    
    4. NATIONAL & STATE HOLIDAYS:
       - Based on country/state selection in calendar settings
       - Visible to all users
       - Read-only
    """
    
    # ----------------------------------------
    # CALENDAR API ENDPOINTS
    # ----------------------------------------
    
    @app.get("/api/calendar")
    async def list_calendar_events(
        date: Optional[str] = None,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """List calendar events for current user including office holidays"""
        from sqlalchemy import or_
        
        conditions = []
        if hasattr(user, 'id') and user.id:
            conditions.append(CalendarEvent.user_id == user.id)
        if hasattr(user, 'current_team_id') and user.current_team_id:
            conditions.append(CalendarEvent.target_team_id == user.current_team_id)
        if hasattr(user, 'employee_id_hash') and user.employee_id_hash:
            conditions.append(CalendarEvent.target_employee_hashes.like(f"%,{user.employee_id_hash},%"))
        if hasattr(user, 'employee_id') and user.employee_id:
            conditions.append(CalendarEvent.target_employee_hashes.like(f"%,{user.employee_id},%"))
        
        if conditions:
            query = db.query(CalendarEvent).filter(or_(*conditions))
        else:
            query = db.query(CalendarEvent)
        
        if date:
            try:
                event_date = datetime.date.fromisoformat(date)
                query = query.filter(CalendarEvent.event_date == event_date)
            except ValueError:
                pass
        
        events = query.order_by(CalendarEvent.event_date.asc(), CalendarEvent.id.desc()).all()
        event_items = [
            {
                "id": e.id,
                "date": e.event_date.isoformat(),
                "title": e.title,
                "notes": e.notes or "",
                "type": e.event_type or "general",
                "owner_id": e.user_id,
            }
            for e in events
        ]
        
        # Add office holidays (visible to all users)
        office_holidays = db.query(OfficeHoliday).all()
        for oh in office_holidays:
            event_items.append({
                "id": oh.id,
                "date": oh.event_date.isoformat(),
                "title": oh.title,
                "notes": oh.notes or "",
                "type": "office_holiday",
                "owner_id": None,
            })
        
        # Add approved personal leaves (only for the current user)
        # Employee sees their own approved leaves after manager/admin approval
        # Manager sees their own approved leaves after admin approval
        if hasattr(user, 'id') and user.id:
            leaves = db.query(LeaveRequest).filter(
                LeaveRequest.employee_id == user.employee_id,
                LeaveRequest.status == "Approved",
            ).order_by(LeaveRequest.id.desc()).all()
            
            for leave in leaves:
                start = leave.start_date
                end = leave.end_date
                try:
                    if isinstance(start, str):
                        start = datetime.date.fromisoformat(start)

                    if isinstance(end, str):
                        end = datetime.date.fromisoformat(end)
                except ValueError:
                    continue
                if not start or not end:
                    continue
                current = start
                while current <= end:
                    event_items.append({
                        "id": None,
                        "date": current.isoformat(),
                        "title": "Approved Leave",
                        "notes": leave.reason or "",
                        "type": "personal_leave",
                        "owner_id": user.id,
                    })
                    current += datetime.timedelta(days=1)
        
            # Add tasks assigned to the current user (visible only to assignees)
            if hasattr(user, 'employee_id') and user.employee_id:
                try:
                    tasks = db.query(Task).filter(Task.user_id == user.employee_id).order_by(Task.due_date.asc()).all()
                    for t in tasks:
                        due = getattr(t, 'due_date', None)
                        if not due:
                            continue
                        if isinstance(due, str):
                            try:
                                due = datetime.date.fromisoformat(due)
                            except Exception:
                                continue
                        if isinstance(due, datetime.datetime):
                            due = due.date()
                        if not due:
                            continue
                        event_items.append({
                            "id": getattr(t, 'id', None),
                            "date": due.isoformat(),
                            "title": getattr(t, 'title', 'Task'),
                            "notes": getattr(t, 'description', '') or '',
                            "type": "task",
                            "owner_id": user.id,
                        })
                except Exception:
                    # If Task model/query fails, continue without tasks
                    pass

                # Add project tasks assigned to the user via ProjectTaskAssignee
                try:
                    assignees = db.query(ProjectTaskAssignee).filter(ProjectTaskAssignee.employee_id == user.employee_id).all()
                    for a in assignees:
                        pt = None
                        try:
                            pt = a.task
                        except Exception:
                            try:
                                pt = db.query(ProjectTask).filter(ProjectTask.id == a.task_id).first()
                            except Exception:
                                pt = None
                        if not pt:
                            continue
                        deadline = getattr(pt, 'deadline', None)
                        if not deadline:
                            continue
                        if isinstance(deadline, datetime.datetime):
                            ddate = deadline.date()
                        elif isinstance(deadline, str):
                            try:
                                ddate = datetime.date.fromisoformat(deadline)
                            except Exception:
                                continue
                        elif isinstance(deadline, datetime.date):
                            ddate = deadline
                        else:
                            continue
                        event_items.append({
                            "id": getattr(pt, 'id', None),
                            "date": ddate.isoformat(),
                            "title": getattr(pt, 'title', 'Project Task'),
                            "notes": getattr(pt, 'description', '') or '',
                            "type": "task",
                            "owner_id": user.id,
                        })
                except Exception:
                    pass

                # Add meetings assigned to the user via ProjectMeetingAssignee
                try:
                    m_assignees = db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.employee_id == user.employee_id).all()
                    for ma in m_assignees:
                        meeting = None
                        try:
                            meeting = ma.meeting
                        except Exception:
                            try:
                                meeting = db.query(Meeting).filter(Meeting.id == ma.meeting_id).first()
                            except Exception:
                                meeting = None
                        if not meeting:
                            continue
                        mdt = getattr(meeting, 'meeting_datetime', None)
                        if not mdt:
                            continue
                        if isinstance(mdt, datetime.datetime):
                            mdate = mdt.date()
                        elif isinstance(mdt, str):
                            try:
                                mdate = datetime.date.fromisoformat(mdt)
                            except Exception:
                                continue
                        elif isinstance(mdt, datetime.date):
                            mdate = mdt
                        else:
                            continue
                        # collect attendees names
                        attendees = []
                        try:
                            attendees_q = db.query(ProjectMeetingAssignee).filter(ProjectMeetingAssignee.meeting_id == meeting.id).all()
                            for aa in attendees_q:
                                try:
                                    if aa.employee and getattr(aa.employee, 'name', None):
                                        attendees.append(aa.employee.name)
                                    else:
                                        # fallback to employee_id
                                        attendees.append(aa.employee_id)
                                except Exception:
                                    attendees.append(aa.employee_id)
                        except Exception:
                            attendees = []

                        event_items.append({
                            "id": getattr(meeting, 'id', None),
                            "date": mdate.isoformat(),
                            "title": getattr(meeting, 'title', 'Meeting'),
                            "notes": getattr(meeting, 'description', '') or '',
                            "type": "meeting",
                            "attendees": attendees,
                            "owner_id": user.id,
                        })
                except Exception:
                    pass

            if date:
                event_items = [item for item in event_items if item["date"] == date]
        return JSONResponse({"events": event_items})
    
    
    @app.post("/api/calendar")
    async def add_calendar_event(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Add a new calendar event"""
        payload = await request.json()
        date_raw = str(payload.get("date", "")).strip()
        title = _sanitize_required(str(payload.get("title", "")).strip(), "title", max_len=200)
        notes = _sanitize_optional(str(payload.get("notes", "")).strip(), max_len=1000)
        event_type = _sanitize_optional(str(payload.get("type", "general")).strip(), max_len=40) or "general"
        target_employee_hashes_raw = payload.get("target_employee_hashes")
        target_team_id_raw = payload.get("target_team_id")
        
        target_employee_hashes = None
        target_team_id = None
        
        if target_employee_hashes_raw or target_team_id_raw:
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="Admin only")
            hashes: list[str] = []
            if isinstance(target_employee_hashes_raw, list):
                for item in target_employee_hashes_raw:
                    val = str(item).strip().lower()
                    if val and validate_allowlist(val, r"^[a-f0-9]{64}$"):
                        hashes.append(val)
            elif target_employee_hashes_raw:
                for part in str(target_employee_hashes_raw).split(","):
                    val = part.strip().lower()
                    if val and validate_allowlist(val, r"^[a-f0-9]{64}$"):
                        hashes.append(val)
            if hashes:
                unique = sorted(set(hashes))
                target_employee_hashes = "," + ",".join(unique) + ","
            if target_team_id_raw is not None and str(target_team_id_raw).strip().isdigit():
                target_team_id = int(str(target_team_id_raw).strip())
        
        if event_type == "office_holiday" and user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        
        try:
            event_date = datetime.date.fromisoformat(date_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date")

        if event_date < datetime.date.today():
            raise HTTPException(status_code=400, detail="Cannot create events in the past")
        
        event = CalendarEvent(
            user_id=user.id,
            title=title,
            notes=notes,
            event_date=event_date,
            event_type=event_type,
            target_employee_hashes=target_employee_hashes,
            target_team_id=target_team_id,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return JSONResponse({
            "id": event.id,
            "date": event.event_date.isoformat(),
            "title": event.title,
            "notes": event.notes or "",
            "type": event.event_type or "general",
        })
    
    
    @app.delete("/api/calendar/{event_id}")
    async def delete_calendar_event(
        event_id: int,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Delete a calendar event"""
        event = db.query(CalendarEvent).filter(CalendarEvent.id == event_id, CalendarEvent.user_id == user.id).first()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        db.delete(event)
        db.commit()
        return JSONResponse({"status": "deleted"})
    
    
    @app.get("/api/calendar/targets")
    async def list_calendar_targets(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """List employees and teams for calendar targeting (admin only)"""
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        employees = db.query(User).order_by(User.id.asc()).all()
        from .models import Team
        teams = db.query(Team).order_by(Team.id.asc()).all()
        return JSONResponse({
            "employees": [
                {
                    "id": emp.id,
                    "name": emp.name,
                    "employee_id_hash": emp.employee_id_hash if hasattr(emp, 'employee_id_hash') else None,
                    "team_id": emp.current_team_id if hasattr(emp, 'current_team_id') else None,
                }
                for emp in employees
            ],
            "teams": [
                {
                    "id": team.id,
                    "name": team.name,
                }
                for team in teams
            ],
        })
    
    
    @app.get("/api/calendar/settings")
    async def get_calendar_settings(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Get user's calendar settings (country, state)"""
        settings = db.query(CalendarSettings).filter(CalendarSettings.user_id == user.id).first()
        if not settings:
            settings = CalendarSettings(user_id=user.id, country_code="IN", state_code=None)
            db.add(settings)
            db.commit()
        
        requested_country = request.query_params.get("country") or settings.country_code or "IN"
        if requested_country not in _valid_country_codes():
            mapped = _country_code_from_name(requested_country)
            requested_country = mapped if mapped in _valid_country_codes() else "IN"

        countries = _countries_list()
        states = _subdivisions_list(requested_country)
        
        return JSONResponse({
            "country": settings.country_code,
            "state": settings.state_code,
            "countries": countries,
            "states": states,
        })
    
    
    @app.post("/api/calendar/settings")
    async def update_calendar_settings(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Update user's calendar settings"""
        payload = await request.json()
        country_raw = _sanitize_required(str(payload.get("country", "IN")), "country", max_len=100)
        state = _sanitize_optional(str(payload.get("state", "")).strip(), max_len=20)

        valid_countries = _valid_country_codes()
        country = country_raw
        if country not in valid_countries:
            mapped = _country_code_from_name(country_raw)
            country = mapped if mapped in valid_countries else "IN"
        if state:
            valid_states = {s["code"] for s in _subdivisions_list(country)}
            if state not in valid_states:
                state = ""
        
        settings = db.query(CalendarSettings).filter(CalendarSettings.user_id == user.id).first()
        if not settings:
            settings = CalendarSettings(user_id=user.id, country_code=country, state_code=state)
            db.add(settings)
        else:
            settings.country_code = country
            settings.state_code = state or None
        db.commit()
        return JSONResponse({"status": "ok"})
    
    
    @app.get("/api/calendar/holidays")
    async def calendar_holidays(
        year: int,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Get national and state holidays for a given year"""
        settings = db.query(CalendarSettings).filter(CalendarSettings.user_id == user.id).first()
        country = settings.country_code if settings else "IN"
        state = settings.state_code if settings else None
        
        supported = holidays.list_supported_countries()
        if country not in supported:
            return JSONResponse({"holidays": [], "supported": False, "state_supported": False})

        national = holidays.country_holidays(country, years=[year])
        state_holidays = None
        state_supported = bool(state and state in supported.get(country, []))
        if state_supported:
            try:
                state_holidays = holidays.country_holidays(country, subdiv=state, years=[year])
            except Exception:
                state_holidays = None
        
        results = []
        for date_val, name in national.items():
            results.append({
                "date": date_val.isoformat(),
                "title": name,
                "type": "national_holiday",
            })
        
        if state_holidays:
            for date_val, name in state_holidays.items():
                if date_val in national:
                    continue
                results.append({
                    "date": date_val.isoformat(),
                    "title": name,
                    "type": "state_holiday",
                })
        
        return JSONResponse({
            "holidays": results,
            "supported": True,
            "state_supported": state_supported,
        })
    
    
    # ----------------------------------------
    # ADMIN OFFICE HOLIDAYS MANAGEMENT
    # ----------------------------------------
    
    @app.get("/admin/office_holidays", response_class=HTMLResponse)
    async def admin_office_holidays(
        request: Request,
        edit_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        """Display office holidays management page"""
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Query OfficeHoliday or CalendarEvent with type='office_holiday'
        try:
            holidays_list = db.query(OfficeHoliday).order_by(OfficeHoliday.event_date.desc()).all()
        except Exception:
            # Fallback to CalendarEvent if OfficeHoliday model doesn't exist
            holidays_list = db.query(CalendarEvent).filter(
                CalendarEvent.event_type == "office_holiday"
            ).order_by(CalendarEvent.event_date.desc()).all()
        
        edit_event = None
        if edit_id:
            try:
                edit_event = db.query(OfficeHoliday).filter(OfficeHoliday.id == edit_id).first()
            except Exception:
                edit_event = db.query(CalendarEvent).filter(
                    CalendarEvent.id == edit_id,
                    CalendarEvent.event_type == "office_holiday"
                ).first()
        
        return templates.TemplateResponse("admin/admin_office_holidays.html", {
            "request": request,
            "user": user,
            "holidays": holidays_list,
            "edit_event": edit_event,
        })
    
    
    @app.post("/admin/office_holidays")
    async def admin_office_holidays_create(
        request: Request,
        date: str = Form(...),
        title: str = Form(...),
        notes: str = Form(None),
        event_id: Optional[int] = Form(None),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Create or update an office holiday"""
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        
        title = _sanitize_required(title, "title", max_len=200)
        notes = _sanitize_optional(notes, max_len=1000)
        
        try:
            event_date = datetime.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date")
        
        if event_id:
            # Update existing holiday
            try:
                event = db.query(OfficeHoliday).filter(OfficeHoliday.id == event_id).first()
            except Exception:
                event = db.query(CalendarEvent).filter(
                    CalendarEvent.id == event_id,
                    CalendarEvent.event_type == "office_holiday"
                ).first()
            
            if not event:
                raise HTTPException(status_code=404, detail="Holiday not found")
            event.title = title
            event.notes = notes
            event.event_date = event_date
        else:
            # Create new holiday
            try:
                event = OfficeHoliday(event_date=event_date, title=title, notes=notes)
            except Exception:
                event = CalendarEvent(
                    user_id=user.id,
                    event_date=event_date,
                    title=title,
                    notes=notes,
                    event_type="office_holiday"
                )
            db.add(event)
        
        db.commit()
        return RedirectResponse("/admin/office_holidays", status_code=303)
    
    
    @app.post("/admin/office_holidays/delete")
    async def admin_office_holidays_delete(
        event_id: int = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """Delete an office holiday"""
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        
        event = None
        try:
            event = db.query(OfficeHoliday).filter(OfficeHoliday.id == event_id).first()
        except Exception:
            event = db.query(CalendarEvent).filter(
                CalendarEvent.id == event_id,
                CalendarEvent.event_type == "office_holiday"
            ).first()
        
        if not event:
            raise HTTPException(status_code=404, detail="Holiday not found")
        
        db.delete(event)
        db.commit()
        return RedirectResponse("/admin/office_holidays", status_code=303)
