from fastapi import Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import Optional, List
import datetime
import os
from pathlib import Path
from io import BytesIO
from calendar import monthrange, month_name
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .database import get_db
from .models import (
    User, Attendance, Team, Project, Task, TeamMember, ProjectAssignment,
    ProjectTask, ProjectTaskAssignee, ProjectMeetingAssignee, Meeting, MeetingAttendance,
    Payroll, LeaveRequest
)
from .auth import verify_password, hash_password
from .email_service import send_leave_requested_email
from .app_context import templates, get_current_user, create_notification
from .payroll_utils import calculate_monthly_payroll

BASE_DIR = Path(__file__).resolve().parent



def register_employee_routes(app):
    @app.get("/employee", response_class=HTMLResponse)
    async def employee_dashboard(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        total_hours = 0
        team = None
        team_leader = None
        team_project = None
        additional_projects = []
        # Compute productive hours for current month
        try:
            now = datetime.datetime.utcnow()
            month_start = datetime.datetime(now.year, now.month, 1)
            total_hours = db.query(func.sum(Attendance.duration)).filter(
                Attendance.employee_id == user.employee_id,
                Attendance.entry_time >= month_start
            ).scalar() or 0
        except Exception:
            total_hours = 0
        if user.current_team_id:
            team = db.query(Team).filter(Team.id == user.current_team_id).first()
            if team:
                team_leader = team.leader
                if team.project_id:
                    team_project = db.query(Project).filter(Project.id == team.project_id).first()
        assigned_project_ids = [
            row[0]
            for row in db.query(ProjectAssignment.project_id)
            .filter(ProjectAssignment.employee_id == user.employee_id)
            .all()
        ]
        if team and team.project_id:
            assigned_project_ids = [pid for pid in assigned_project_ids if pid != team.project_id]
        if assigned_project_ids:
            additional_projects = db.query(Project).filter(Project.id.in_(assigned_project_ids)).all()
        # Gather all tasks for accurate pending count
        personal_tasks = db.query(Task).filter(Task.user_id == user.employee_id).all()
        # Removed redundant import of models to fix UnboundLocalError for Team
        # Project tasks assigned to this user
        project_tasks = (
            db.query(ProjectTask, ProjectTaskAssignee)
            .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
            .filter(ProjectTaskAssignee.employee_id == user.employee_id)
            .all()
        )
        # Team and additional projects
        assigned_project_ids = set(
            row[0]
            for row in db.query(ProjectAssignment.project_id)
            .filter(ProjectAssignment.employee_id == user.employee_id)
            .all()
        )
        if team and team.project_id:
            assigned_project_ids.add(team.project_id)
        projects = []
        if assigned_project_ids:
            projects = db.query(Project).filter(Project.id.in_(assigned_project_ids)).all()
        all_project_tasks = []
        if projects:
            project_ids_list = [p.id for p in projects]
            all_project_tasks = (
                db.query(ProjectTask, ProjectTaskAssignee)
                .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                .filter(ProjectTask.project_id.in_(project_ids_list), ProjectTaskAssignee.employee_id == user.employee_id)
                .all()
            )
        # Merge all tasks and count pending
        pending_tasks_count = sum(1 for t in personal_tasks if t.status in ["pending", "in-progress"])
        seen_ids = set()
        for pt, pa in project_tasks:
            if pa.status in ["pending", "in-progress"] and pt.id not in seen_ids:
                pending_tasks_count += 1
                seen_ids.add(pt.id)
        for pt, pa in all_project_tasks:
            if pa.status in ["pending", "in-progress"] and pt.id not in seen_ids:
                pending_tasks_count += 1
                seen_ids.add(pt.id)
        # Additional projects count
        additional_projects_count = len(additional_projects) if additional_projects else 0
        # For dashboard display, keep the original 5 recent tasks for preview
        tasks = db.query(Task).filter(
            Task.user_id == user.employee_id
        ).order_by(Task.created_at.desc()).limit(5).all()
        # Leave balance (simple): use paid_leaves_allowed if present
        leave_balance = getattr(user, "paid_leaves_allowed", None)
        if leave_balance is None:
            leave_balance = 0

        # Current presence/status: check for an open attendance (exit_time is NULL)
        current_status = "Offline"
        current_location = None
        current_checkin = None
        try:
            open_att = db.query(Attendance).filter(
                Attendance.employee_id == user.employee_id,
                Attendance.exit_time == None
            ).order_by(Attendance.entry_time.desc()).first()
            if open_att:
                current_status = "Online"
                current_location = open_att.location_name or (open_att.room_no or "Unknown")
                if open_att.entry_time:
                    current_checkin = open_att.entry_time.strftime("%I:%M %p")
        except Exception:
            pass

        # Friendly date label for header
        current_date_display = datetime.datetime.utcnow().strftime("%b %d, %Y")
        return templates.TemplateResponse("employee/employee_dashboard.html",
            {
                "request": request,
                "user": user,
                "total_hours": round(total_hours, 2),
                "team": team,
                "team_leader": team_leader,
                "team_project": team_project,
                "additional_projects": additional_projects,
                "additional_projects_count": additional_projects_count,
                "pending_tasks_count": pending_tasks_count,
                "tasks": tasks,
                "current_year": 2026,
                "leave_balance": leave_balance,
                "current_status": current_status,
                "current_location": current_location,
                "current_checkin": current_checkin,
                "current_date_display": current_date_display
            }
        )

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

    @app.get("/employee/team", response_class=HTMLResponse)
    async def employee_team(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        assigned_tasks = (
            db.query(Task)
            .filter(Task.user_id == user.employee_id)
            .order_by(Task.created_at.desc())
            .all()
        )

        teams = []
        if user.current_team_id:
            team = db.query(Team).filter(Team.id == user.current_team_id).first()
            if team:
                teams.append(team)

        teams_data = []
        for team in teams:
            member_ids = set(
                row[0]
                for row in db.query(User.id).filter(User.current_team_id == team.id).all()
            )
            membership_ids = {
                row[0]
                for row in db.query(TeamMember.user_id).filter(TeamMember.team_id == team.id).all()
            }
            member_ids.update(membership_ids)
            members = []
            if member_ids:
                members = db.query(User).filter(User.id.in_(member_ids)).all()

            teams_data.append({
                "team": team,
                "members": members,
                "leader": team.leader
            })

        assigned_project_ids = {
            row[0]
            for row in db.query(ProjectAssignment.project_id)
            .filter(ProjectAssignment.employee_id == user.employee_id)
            .all()
        }
        if user.current_team_id:
            official_team = db.query(Team).filter(Team.id == user.current_team_id).first()
            if official_team and official_team.project_id:
                assigned_project_ids.add(official_team.project_id)
        project_ids = set(assigned_project_ids)

        projects = []
        if project_ids:
            projects = db.query(Project).filter(Project.id.in_(project_ids)).all()

        project_task_counts = {}
        project_tasks_map = {}
        if projects:
            project_ids_list = [p.id for p in projects]
            task_rows = (
                db.query(ProjectTask.project_id, func.count(ProjectTask.id))
                .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                .filter(
                    ProjectTask.project_id.in_(project_ids_list),
                    ProjectTaskAssignee.employee_id == user.employee_id
                )
                .group_by(ProjectTask.project_id)
                .all()
            )
            project_task_counts = {pid: count for pid, count in task_rows}

            project_tasks = (
                db.query(ProjectTask)
                .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                .filter(
                    ProjectTask.project_id.in_(project_ids_list),
                    ProjectTaskAssignee.employee_id == user.employee_id
                )
                .order_by(ProjectTask.created_at.desc())
                .all()
            )
            for task in project_tasks:
                project_tasks_map.setdefault(task.project_id, []).append({
                    "id": task.id,
                    "title": task.title,
                    "description": task.description or "",
                    "status": task.status,
                    "deadline": task.deadline.strftime("%Y-%m-%d") if task.deadline else None
                })

        projects_by_team = {}
        for team in teams:
            if team.project_id and team.project_id in assigned_project_ids:
                projects_by_team.setdefault(team.id, []).extend(
                    [p for p in projects if p.id == team.project_id]
                )

        team_project_ids = {team.project_id for team in teams if team.project_id in assigned_project_ids}
        additional_projects = [p for p in projects if p.id not in team_project_ids]

        return templates.TemplateResponse(
            "employee/employee_team.html",
            {
                "request": request,
                "user": user,
                "assigned_tasks": assigned_tasks,
                "teams_data": teams_data,
                "projects": projects,
                "projects_by_team": projects_by_team,
                "additional_projects": additional_projects,
                "project_task_counts": project_task_counts,
                "project_tasks_map": project_tasks_map
            }
        )

    @app.get("/employee/attendance", response_class=HTMLResponse)
    async def employee_attendance_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        logs = db.query(Attendance).filter(
            Attendance.employee_id == user.employee_id
        ).order_by(Attendance.date.desc()).all()
        return templates.TemplateResponse("employee/employee_attendance.html",
                                          {"request": request, "user": user, "logs": logs,
                                           "current_year": datetime.datetime.utcnow().year})

    @app.post("/employee/project_tasks/complete")
    async def employee_complete_project_task(
        request: Request,
        task_id: int = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        task = db.query(ProjectTask).filter(ProjectTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        is_assigned = db.query(ProjectTaskAssignee).filter(
            ProjectTaskAssignee.task_id == task_id,
            ProjectTaskAssignee.employee_id == user.employee_id
        ).first()
        if not is_assigned:
            raise HTTPException(status_code=403, detail="Not assigned to this task")



        # Per-assignee completion
        is_assigned.status = "completed"
        is_assigned.completed_at = datetime.datetime.utcnow()
        db.commit()

        # Notify the leader (and optionally manager) that this user completed the task
        # Find the leader for this project (via team)
        team = db.query(Team).filter(Team.project_id == task.project_id).first()
        if team and team.leader_id:
            leader = db.query(User).filter(User.id == team.leader_id).first()
            if leader:
                create_notification(
                    db,
                    user_id=leader.id,
                    title="Task Completed",
                    message=f"{user.name} completed the task '{task.title}'",
                    notif_type="task_completed",
                    link=f"/leader/project/{task.project_id}"
                )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "task_id": task_id, "status": task.status}

        return RedirectResponse("/employee/team", status_code=303)

    @app.get("/employee/tasks", response_class=HTMLResponse)
    async def employee_tasks_page(request: Request,
                                  user: User = Depends(get_current_user),
                                  db: Session = Depends(get_db),
                                  filter: str = None):
        # Personal tasks
        task_query = db.query(Task).filter(Task.user_id == user.employee_id)
        if filter in ["pending", "in-progress", "done"]:
            task_query = task_query.filter(Task.status == filter)
        personal_tasks = task_query.order_by(Task.id.desc()).all()

        # Project tasks assigned to this user (ProjectTaskAssignee)
        from .models import ProjectTask, ProjectTaskAssignee, Project, ProjectAssignment, Team
        project_task_query = (
            db.query(ProjectTask, ProjectTaskAssignee)
            .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
            .filter(ProjectTaskAssignee.employee_id == user.employee_id)
        )
        if filter in ["pending", "in-progress", "done", "completed"]:
            status_map = {"pending": "pending", "in-progress": "in-progress", "done": "completed", "completed": "completed"}
            project_task_query = project_task_query.filter(ProjectTaskAssignee.status == status_map.get(filter, "pending"))
        project_tasks = project_task_query.order_by(ProjectTask.id.desc()).all()

        # Team and additional projects
        assigned_project_ids = set(
            row[0]
            for row in db.query(ProjectAssignment.project_id)
            .filter(ProjectAssignment.employee_id == user.employee_id)
            .all()
        )
        if user.current_team_id:
            team = db.query(Team).filter(Team.id == user.current_team_id).first()
            if team and team.project_id:
                assigned_project_ids.add(team.project_id)
        projects = []
        if assigned_project_ids:
            projects = db.query(Project).filter(Project.id.in_(assigned_project_ids)).all()

        # All project tasks from these projects assigned to this user
        all_project_tasks = []
        if projects:
            project_ids_list = [p.id for p in projects]
            all_project_tasks = (
                db.query(ProjectTask, ProjectTaskAssignee)
                .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                .filter(ProjectTask.project_id.in_(project_ids_list), ProjectTaskAssignee.employee_id == user.employee_id)
                .order_by(ProjectTask.id.desc())
                .all()
            )

        # Merge all: personal, project, and all project tasks from team/additional projects
        tasks = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "type": "personal"
            } for t in personal_tasks
        ]
        # Add project tasks (directly assigned)
        tasks += [
            {
                "id": pt.id,
                "title": pt.title,
                "description": pt.description,
                "status": pa.status,
                "type": "project"
            } for pt, pa in project_tasks
        ]
        # Add all project tasks from team/additional projects (avoid duplicates)
        existing_ids = {t["id"] for t in tasks}
        for pt, pa in all_project_tasks:
            if pt.id not in existing_ids:
                tasks.append({
                    "id": pt.id,
                    "title": pt.title,
                    "description": pt.description,
                    "status": pa.status,
                    "type": "project"
                })
                existing_ids.add(pt.id)

        # For stats, count both types
        pending = sum(1 for t in tasks if t["status"] in ["pending"])
        in_progress = sum(1 for t in tasks if t["status"] in ["in-progress"])
        done = sum(1 for t in tasks if t["status"] in ["done", "completed"])

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

    @app.get("/employee/meetings", response_class=HTMLResponse)
    async def employee_meetings_page(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        meetings = (
            db.query(Meeting)
            .join(ProjectMeetingAssignee, Meeting.id == ProjectMeetingAssignee.meeting_id)
            .filter(ProjectMeetingAssignee.employee_id == user.employee_id)
            .order_by(Meeting.meeting_datetime.desc())
            .all()
        )

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

        payroll = calculate_monthly_payroll(db, user, month, year)

        start_date = datetime.datetime(year, month, 1)
        end_date = datetime.datetime(year, month, monthrange(year, month)[1], 23, 59, 59)
        total_hours = db.query(func.sum(Attendance.duration)).filter(
            Attendance.employee_id == user.employee_id,
            Attendance.entry_time >= start_date,
            Attendance.entry_time <= end_date
        ).scalar() or 0

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

    @app.get("/employee/photo/{employee_id}")
    async def employee_photo(employee_id: str, db: Session = Depends(get_db)):
        emp = db.query(User).filter(User.employee_id == employee_id, User.is_active == True).first()
        if not emp or not emp.photo_blob:
            raise HTTPException(status_code=404, detail="Photo not found")
        return Response(content=emp.photo_blob, media_type=emp.photo_mime or "image/jpeg")






    @app.get("/employee/attendance-intelligence", response_class=HTMLResponse)
    async def employee_attendance_intelligence(
        request: Request,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user)
    ):
        from app.analytics.attendance_intelligence import (
            get_attendance_dataframe,
            compute_behavior_metrics,
            detect_attendance_anomalies
        )

        df = get_attendance_dataframe(db, user.employee_id)
        metrics = compute_behavior_metrics(db, df, user.employee_id)
        anomalies = detect_attendance_anomalies(df)

        return templates.TemplateResponse(
            "employee/employee_attendance_intelligence.html",
            {
                "request": request,
                "user": user,
                "metrics": metrics,
                "anomalies": anomalies
            }
        )
