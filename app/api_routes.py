from fastapi import Depends, HTTPException, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
import datetime
import os

from .database import get_db
from Security.session_security import get_session_timing
from Security.security_config import SECURITY_SETTINGS
from .models import (
    Attendance, UnknownRFID, User, AttendanceLog, AttendanceDaily,
    Meeting, ProjectMeetingAssignee, MeetingAttendance, Project, ProjectTask,
    ProjectTaskAssignee, ProjectAssignment, Notification, Team, Task, LeaveRequest,
    Room, InappropriateEntry
)
from .app_context import get_current_user, create_notification



def register_api_routes(app):
    def _runtime_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return int(default)

    @app.get("/api/session/timing")
    async def api_session_timing(request: Request, user=Depends(get_current_user)):
        session_max_age = _runtime_int("SESSION_MAX_AGE", int(SECURITY_SETTINGS.get("SESSION_MAX_AGE", 600)))
        session_idle_timeout = _runtime_int("SESSION_IDLE_TIMEOUT", int(SECURITY_SETTINGS.get("SESSION_IDLE_TIMEOUT", 600)))
        timing = get_session_timing(
            request,
            max_age_seconds=session_max_age,
            idle_timeout_seconds=session_idle_timeout,
        )
        return JSONResponse(
            {
                "remaining": timing.get("remaining"),
                "idle_remaining": timing.get("idle_remaining"),
                "expires_at": timing.get("expires_at"),
                "idle_expires_at": timing.get("idle_expires_at"),
                "session_max_age": session_max_age,
                "session_idle_timeout": session_idle_timeout,
            }
        )

    @app.post("/api/session/touch")
    async def api_session_touch(request: Request, user=Depends(get_current_user)):
        now_ts = int(datetime.datetime.utcnow().timestamp())
        request.session.setdefault("_created", now_ts)
        request.session["_last_seen"] = now_ts
        return JSONResponse({"status": "ok", "last_seen": now_ts})

    @app.post("/api/attendance")
    async def record_attendance(
        rfid_tag: str,
        room_no: str,
        location_name: str,
        db: Session = Depends(get_db)
    ):
        GATE_ROOM_NO = "77"

        user = db.query(User).filter(User.rfid_tag == rfid_tag, User.is_active == True).first()

        if not user:
            db.add(UnknownRFID(rfid_tag=rfid_tag, location=location_name))
            db.commit()
            return {"status": "unknown_rfid"}

        # Validate that room exists in Room table (only if not gate room)
        if room_no != GATE_ROOM_NO:
            valid_room = db.query(Room).filter(
                Room.room_no == room_no,
                Room.location_name == location_name
            ).first()
            
            if not valid_room:
                # Log inappropriate entry (invalid room)
                db.add(InappropriateEntry(
                    employee_id=user.employee_id,
                    rfid_tag=rfid_tag,
                    location_name=location_name,
                    room_no=room_no,
                    reason=f"Room '{room_no}' in '{location_name}' not found in Room table"
                ))
                db.commit()
                return {"status": "invalid_room", "error": "This room is not registered in the system"}

        today = datetime.date.today()
        now = datetime.datetime.now()

        new_log = AttendanceLog(
            user_id=user.id,
            entry_time=now,
            location_name=location_name,
            room_no=room_no
        )
        db.add(new_log)

        daily_record = db.query(AttendanceDaily).filter(
            AttendanceDaily.user_id == user.id,
            AttendanceDaily.date == today
        ).first()

        if not daily_record:
            status = "PRESENT"
            if now.time() > datetime.time(9, 30):
                status = "LATE"

            daily_record = AttendanceDaily(
                user_id=user.id,
                date=today,
                status=status,
                check_in_time=now.time()
            )
            db.add(daily_record)

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
            if not open_gate:
                db.add(Attendance(employee_id=user.employee_id, date=today, entry_time=now, status="PRESENT", location_name="Main Gate", room_no=GATE_ROOM_NO))

            if open_block and open_block.room_no == room_no:
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

    @app.get("/api/block_persons")
    async def get_block_persons(location: str, room: str, db: Session = Depends(get_db)):
        attendances = db.query(Attendance).filter(
            Attendance.location_name == location,
            Attendance.room_no == room,
            Attendance.exit_time.is_(None)
        ).all()
        persons = [{"name": a.user.name} for a in attendances]
        return {"persons": persons}

    @app.get("/api/blocks")
    async def get_blocks(db: Session = Depends(get_db)):
        # Only count open attendances (exit_time is NULL) and limit to registered rooms
        blocks = (
            db.query(
                Attendance.location_name,
                Attendance.room_no,
                func.count(Attendance.id).label("count")
            )
            .join(Room, (Room.location_name == Attendance.location_name) & (Room.room_no == Attendance.room_no))
            .filter(
                Attendance.exit_time.is_(None),
                Attendance.date == datetime.date.today()
            )
            .group_by(
                Attendance.location_name,
                Attendance.room_no
            )
            .all()
        )
        return {"blocks": [{"location": b.location_name, "room": b.room_no, "count": b.count} for b in blocks]}

    @app.get("/api/inappropriate-entries")
    async def get_inappropriate_entries(db: Session = Depends(get_db)):
        """Get list of inappropriate room entries (invalid rooms not in Room table)"""
        entries = db.query(InappropriateEntry).order_by(InappropriateEntry.timestamp.desc()).limit(50).all()
        return {
            "inappropriate_entries": [
                {
                    "id": e.id,
                    "employee_id": e.employee_id,
                    "rfid_tag": e.rfid_tag,
                    "location_name": e.location_name,
                    "room_no": e.room_no,
                    "reason": e.reason,
                    "timestamp": e.timestamp.isoformat()
                }
                for e in entries
            ]
        }

    @app.get("/api/absentees")
    async def get_absentees(department: str, db: Session = Depends(get_db)):
        all_employees = db.query(User).filter(User.department == department, User.is_active == True).all()
        present_employee_ids = db.query(Attendance.employee_id).filter(Attendance.exit_time.is_(None)).distinct().all()
        present_ids = {p[0] for p in present_employee_ids}
        absentees = [emp for emp in all_employees if emp.employee_id not in present_ids]
        return {"absentees": [{"name": emp.name, "employee_id": emp.employee_id} for emp in absentees]}

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

    @app.get("/api/leave_count")
    async def leave_count(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        pending = db.query(LeaveRequest).filter(LeaveRequest.status == "Pending").count()
        return {"count": pending}

    @app.get("/api/month-hours")
    async def month_hours(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        now = datetime.datetime.utcnow()
        first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = db.query(func.sum(Attendance.duration)).filter(
            Attendance.employee_id == user.employee_id,
            Attendance.entry_time >= first_day
        ).scalar() or 0
        return {"total_hours": round(total, 2)}

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
            func.lower(User.name).like(f"%{query.lower()}%")
        ).order_by(User.name.asc()).limit(50).all()

        return [
            {
                "id": emp.id,
                "name": emp.name,
                "employee_id": emp.employee_id
            }
            for emp in employees
        ]

    @app.get("/api/all_projects")
    async def all_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "manager":
            raise HTTPException(status_code=403)

        projects = db.query(Project).filter(
            Project.department == user.department
        ).all()

        projects_data = []
        for project in projects:
            project_tasks = (
                db.query(ProjectTask)
                .filter(ProjectTask.project_id == project.id)
                .order_by(ProjectTask.created_at.desc())
                .all()
            )

            task_ids = [t.id for t in project_tasks]
            assignee_ids = set()

            if task_ids:
                assignee_ids.update({
                    row[0]
                    for row in db.query(ProjectTaskAssignee.employee_id)
                    .filter(ProjectTaskAssignee.task_id.in_(task_ids))
                    .distinct()
                    .all()
                })

            assignment_ids = {
                row[0]
                for row in db.query(ProjectAssignment.employee_id)
                .filter(ProjectAssignment.project_id == project.id)
                .distinct()
                .all()
            }
            assignee_ids.update(assignment_ids)

            team_ids = [t.id for t in db.query(Team.id).filter(Team.project_id == project.id).all()]
            if team_ids:
                team_member_ids = {
                    row[0]
                    for row in db.query(User.employee_id)
                    .filter(User.current_team_id.in_(team_ids))
                    .distinct()
                    .all()
                }
                assignee_ids.update(team_member_ids)

            assigned_employees = []
            if assignee_ids:
                assigned_employees = db.query(User).filter(User.employee_id.in_(assignee_ids)).all()

            direct_assignments = db.query(User).join(
                ProjectAssignment,
                User.employee_id == ProjectAssignment.employee_id
            ).filter(ProjectAssignment.project_id == project.id).all()

            projects_data.append({
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "status": project.status,
                "created_at": project.created_at.isoformat() if project.created_at else None,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description or "",
                        "status": t.status,
                        "deadline": t.deadline.isoformat() if t.deadline else None
                    }
                    for t in project_tasks
                ],
                "assigned_employees": [emp.name for emp in assigned_employees],
                "direct_assignments": [
                    {"name": emp.name, "employee_id": emp.employee_id}
                    for emp in direct_assignments
                ],
                "task_count": len(project_tasks),
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

        safe_offset = max(offset, 0)
        safe_limit = min(max(limit, 1), 100)
        total_count = db.query(Notification).filter(Notification.user_id == user.id).count()
        items = (
            db.query(Notification)
            .filter(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
        unread_count = db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.is_read == False
        ).count()
        returned_count = len(items)
        next_offset = safe_offset + returned_count

        return {
            "total": total_count,
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": returned_count,
            "next_offset": next_offset,
            "has_more": next_offset < total_count,
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
        notification_id: int | None = Form(None),
        mark_all: bool | None = Form(False),
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
