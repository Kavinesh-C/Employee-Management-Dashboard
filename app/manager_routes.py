from fastapi import Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List
import datetime
import secrets
from urllib.parse import urlparse

from .database import get_db
from .models import (
    User, Team, TeamMember, Project, ProjectAssignment, Department,
    ProjectTask, ProjectTaskAssignee, Meeting, ProjectMeetingAssignee,
    MeetingAttendance, CalendarEvent, Task
)
from .app_context import templates, get_current_user, create_notification, hash_employee_id
from .email_service import send_bulk_meeting_invites, smtp_enabled
from . import chat_store



def register_manager_routes(app):

    @app.post("/manager/update_task")
    async def manager_update_task(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Access denied")
        data = await request.json()
        employee_id = data.get("employee_id")
        idx = int(data.get("idx", 0))
        # Try to find the task in both personal and project tasks
        # 1. Personal task
        personal_tasks = db.query(Task).filter(Task.user_id == employee_id).all()
        if idx < len(personal_tasks):
            task = personal_tasks[idx]
            task.title = data.get("title", task.title)
            task.description = data.get("description", task.description)
            task.status = data.get("status", task.status)
            if data.get("assigned_at"):
                try:
                    task.created_at = datetime.datetime.strptime(data["assigned_at"], "%Y-%m-%d")
                except Exception:
                    pass
            if data.get("completed_at"):
                try:
                    task.completed_at = datetime.datetime.strptime(data["completed_at"], "%Y-%m-%d")
                except Exception:
                    pass
            if data.get("due_date"):
                try:
                    task.due_date = datetime.datetime.strptime(data["due_date"], "%Y-%m-%d")
                except Exception:
                    pass
            db.commit()
            return {"ok": True, "type": "personal"}
        # 2. Project task (find by order after personal tasks)
        team = db.query(Team).filter(Team.id == db.query(User).filter(User.employee_id == employee_id).first().current_team_id).first()
        project_tasks = []
        if team and team.project_id:
            project_tasks = (
                db.query(ProjectTask, ProjectTaskAssignee)
                .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                .filter(ProjectTask.project_id == team.project_id, ProjectTaskAssignee.employee_id == employee_id)
                .all()
            )
        proj_idx = idx - len(personal_tasks)
        if 0 <= proj_idx < len(project_tasks):
            pt, pa = project_tasks[proj_idx]
            pt.title = data.get("title", pt.title)
            pt.description = data.get("description", pt.description)
            pa.status = data.get("status", pa.status)
            if data.get("assigned_at"):
                try:
                    pa.assigned_at = datetime.datetime.strptime(data["assigned_at"], "%Y-%m-%d")
                except Exception:
                    pass
            if data.get("completed_at"):
                try:
                    pa.completed_at = datetime.datetime.strptime(data["completed_at"], "%Y-%m-%d")
                except Exception:
                    pass
            if data.get("due_date"):
                try:
                    pt.deadline = datetime.datetime.strptime(data["due_date"], "%Y-%m-%d")
                except Exception:
                    pass
            db.commit()
            return {"ok": True, "type": "project"}
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
    @app.get("/manager/manage_teams", response_class=HTMLResponse)
    async def manager_manage_teams(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Access denied")

        teams = db.query(Team).all()
        active_employees = db.query(User).filter(User.is_active == True).all()
        assigned_current_ids = {
            row[0]
            for row in db.query(User.id).filter(User.current_team_id.isnot(None)).all()
        }
        assigned_membership_ids = {
            row[0]
            for row in db.query(TeamMember.user_id).all()
        }
        assigned_ids = assigned_current_ids | assigned_membership_ids
        employees = [emp for emp in active_employees if emp.id not in assigned_ids]
        projects = db.query(Project).filter(Project.department == user.department).all()

        departments = db.query(Department).all()

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
            members = db.query(User).filter(User.current_team_id == t.id).all()
            member_employee_ids = [m.employee_id for m in members if m.employee_id]

            member_task_status = []
            if member_employee_ids and t.project_id:

                task_rows = (
                    db.query(
                        ProjectTaskAssignee.employee_id,
                        ProjectTask.id,
                        ProjectTask.title,
                        ProjectTaskAssignee.status,
                        ProjectTask.created_at,
                        ProjectTask.deadline,
                        ProjectTaskAssignee.completed_at
                    )
                    .join(ProjectTask, ProjectTask.id == ProjectTaskAssignee.task_id)
                    .filter(
                        ProjectTask.project_id == t.project_id,
                        ProjectTaskAssignee.employee_id.in_(member_employee_ids)
                    )
                    .order_by(ProjectTask.created_at.desc())
                    .all()
                )

                tasks_by_emp = {}
                for employee_id, task_id, title, status, created_at, deadline, completed_at in task_rows:
                    entry = tasks_by_emp.setdefault(employee_id, {"tasks": [], "completed": 0})
                    entry["tasks"].append({
                        "id": task_id,
                        "title": title,
                        "status": status,
                        "created_at": created_at.isoformat() if created_at else None,
                        "deadline": deadline.isoformat() if deadline else None,
                        "completed_at": completed_at.isoformat() if completed_at else None
                    })
                    if status == "completed":
                        entry["completed"] += 1

                for member in members:
                    emp_id = member.employee_id
                    if not emp_id:
                        continue
                    data = tasks_by_emp.get(emp_id, {"tasks": [], "completed": 0})
                    total = len(data["tasks"])
                    completed = data["completed"]
                    percent = int((completed / total) * 100) if total else 0
                    member_task_status.append({
                        "employee_id": emp_id,
                        "name": member.name,
                        "tasks": data["tasks"],
                        "total": total,
                        "completed": completed,
                        "percent": percent
                    })

            total_tasks = 0
            completed_tasks = 0
            if member_employee_ids:
                if t.project_id:
                    base_project_task_query = (
                        db.query(ProjectTask.id)
                        .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                        .filter(
                            ProjectTask.project_id == t.project_id,
                            ProjectTaskAssignee.employee_id.in_(member_employee_ids)
                        )
                    )
                    base_subquery = base_project_task_query.subquery()
                    total_tasks = (
                        db.query(func.count(func.distinct(base_subquery.c.id)))
                        .scalar()
                    ) or 0
                    completed_tasks = (
                        db.query(func.count(func.distinct(ProjectTask.id)))
                        .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                        .filter(
                            ProjectTask.project_id == t.project_id,
                            ProjectTaskAssignee.employee_id.in_(member_employee_ids),
                            ProjectTask.status == "completed"
                        )
                        .scalar()
                    ) or 0
                else:
                    task_query = db.query(Task).filter(Task.user_id.in_(member_employee_ids))
                    total_tasks = task_query.count()
                    completed_tasks = task_query.filter(Task.status.in_(["done", "completed"])).count()
                if total_tasks > 0:
                    completion = int((completed_tasks / total_tasks) * 100)

            team_data.append({
                "team": t,
                "completion": completion,
                "task_total": total_tasks,
                "task_done": completed_tasks,
                "member_count": len(members),
                "members": members,
                "member_task_status": member_task_status
            })

        return templates.TemplateResponse("admin/admin_manage_teams.html", {
            "request": request,
            "user": user,
            "team_data": team_data,
            "employees": employees,
            "departments": departments,
            "projects": projects
        })

    @app.get("/manager/team/{team_id}/details", response_class=HTMLResponse)
    async def manager_team_details(team_id: int, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Access denied")
        team = db.query(Team).filter(Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        members = db.query(User).filter(User.current_team_id == team_id).all()
        # For each member, get their assigned tasks (personal and project)
        member_list = []
        for member in members:
            # Personal tasks
            personal_tasks = db.query(Task).filter(Task.user_id == member.employee_id).all()
            # Project tasks (if team has a project)
            project_tasks = []
            if team.project_id:
                project_tasks = (
                    db.query(ProjectTask, ProjectTaskAssignee)
                    .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                    .filter(ProjectTask.project_id == team.project_id, ProjectTaskAssignee.employee_id == member.employee_id)
                    .all()
                )
            # Flatten project tasks
            project_task_list = [
                type('TaskObj', (), {
                    'title': pt.title,
                    'status': pa.status
                }) for pt, pa in project_tasks
            ]
            all_tasks = list(personal_tasks) + project_task_list
            member_list.append({
                'name': member.name,
                'title': member.title,
                'email': member.email,
                'tasks': all_tasks
            })
        return templates.TemplateResponse(
            "employee/manager_team_details.html",
            {
                "request": request,
                "user": user,
                "team": team,
                "members": member_list
            }
        )

    @app.get("/manager/team/{team_id}/members", response_class=HTMLResponse)
    def view_team_members(team_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Access denied")

        team = db.query(Team).filter(Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        members = db.query(User).filter(User.current_team_id == team_id).all()
        # For each member, get their assigned tasks (personal and project)
        member_list = []
        for member in members:
            # Personal tasks
            personal_tasks = db.query(Task).filter(Task.user_id == member.employee_id).all()
            personal_task_objs = [
                {
                    'title': t.title,
                    'status': t.status,
                    'description': t.description or '',
                    'assigned_at': t.created_at.strftime('%Y-%m-%d') if t.created_at else '',
                    'completed_at': t.completed_at.strftime('%Y-%m-%d') if hasattr(t, 'completed_at') and t.completed_at else '',
                    'due_date': t.due_date.strftime('%Y-%m-%d') if hasattr(t, 'due_date') and t.due_date else ''
                } for t in personal_tasks
            ]
            # Project tasks (if team has a project)
            project_tasks = []
            if team.project_id:
                project_tasks = (
                    db.query(ProjectTask, ProjectTaskAssignee)
                    .join(ProjectTaskAssignee, ProjectTask.id == ProjectTaskAssignee.task_id)
                    .filter(ProjectTask.project_id == team.project_id, ProjectTaskAssignee.employee_id == member.employee_id)
                    .all()
                )
            project_task_objs = [
                {
                    'title': pt.title,
                    'status': pa.status,
                    'description': pt.description or '',
                    'assigned_at': pa.assigned_at.strftime('%Y-%m-%d') if hasattr(pa, 'assigned_at') and pa.assigned_at else '',
                    'completed_at': pa.completed_at.strftime('%Y-%m-%d') if hasattr(pa, 'completed_at') and pa.completed_at else '',
                    'due_date': pt.deadline.strftime('%Y-%m-%d') if hasattr(pt, 'deadline') and pt.deadline else ''
                } for pt, pa in project_tasks
            ]
            all_tasks = personal_task_objs + project_task_objs
            member_list.append({
                'name': member.name,
                'employee_id': member.employee_id,
                'email': member.email,
                'department': member.department,
                'role': member.role,
                'is_active': member.is_active,
                'tasks': all_tasks
            })
        return templates.TemplateResponse(
            "admin/team_members.html",
            {
                "request": request,
                "user": user,
                "team": team,
                "members": member_list
            }
        )

    @app.post("/manager/team_tasks/create")
    async def manager_create_team_task(
        team_id: int = Form(...),
        title: str = Form(...),
        description: str = Form(""),
        priority: str = Form("medium"),
        due_date: Optional[str] = Form(None),
        assignees: List[str] = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "manager":
            raise HTTPException(status_code=403)

        team = db.query(Team).filter(Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        if not team.project_id:
            raise HTTPException(status_code=400, detail="Team has no linked project")

        member_ids = {
            row[0]
            for row in db.query(User.employee_id)
            .filter(User.current_team_id == team.id)
            .all()
            if row[0]
        }
        if not member_ids:
            raise HTTPException(status_code=400, detail="Team has no members")

        due_dt = None
        if due_date:
            try:
                due_dt = datetime.datetime.strptime(due_date, "%Y-%m-%d")
            except Exception:
                due_dt = None

        valid_assignees = []
        for employee_id in assignees:
            emp_id = (employee_id or "").strip()
            if not emp_id or emp_id not in member_ids:
                continue
            valid_assignees.append(emp_id)

        if not valid_assignees:
            raise HTTPException(status_code=400, detail="No valid assignees selected")

        project_task = ProjectTask(
            project_id=team.project_id,
            title=title.strip(),
            description=(description or "").strip(),
            deadline=due_dt,
            status="pending"
        )
        db.add(project_task)
        db.commit()
        db.refresh(project_task)

        for emp_id in valid_assignees:
            db.add(ProjectTaskAssignee(
                task_id=project_task.id,
                employee_id=emp_id,
                employee_id_hash=hash_employee_id(emp_id)
            ))
            employee = db.query(User).filter(User.employee_id == emp_id).first()
            if employee:
                create_notification(
                    db,
                    employee.id,
                    "Task assigned",
                    f"Task '{project_task.title}' assigned.",
                    "task",
                    "/employee/team"
                )

        try:
            target_tokens = sorted(set(valid_assignees))
            target_hashes = "," + ",".join(target_tokens) + "," if target_tokens else None
            event_date = due_dt.date() if due_dt else datetime.date.today()
            cal_event = CalendarEvent(
                user_id=user.id,
                event_date=event_date,
                title=project_task.title,
                notes=project_task.description or "",
                event_type="task",
                target_employee_hashes=target_hashes
            )
            db.add(cal_event)
        except Exception:
            db.rollback()

        db.commit()
        return RedirectResponse("/manager/manage_teams", status_code=303)

    @app.post("/manager/create_team")
    async def create_team(
        name: str = Form(...),
        department: str = Form(...),
        leader_employee_id: str = Form(None),
        project_id: int = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "manager":
            raise HTTPException(status_code=403)

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

        new_team = Team(
            name=name,
            department=department,
            leader_id=leader_id,
            permanent_leader_id=leader_id,
            project_id=project.id
        )
        db.add(new_team)
        db.commit()

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
        department: Optional[str] = Form(None),
        deadline: str = Form(...),
        description: Optional[str] = Form(None),
        team_id: Optional[int] = Form(None),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "manager":
            raise HTTPException(status_code=403)

        dept_value = department or user.department
        if not dept_value:
            raise HTTPException(status_code=400, detail="Department required")

        deadline_dt = None
        try:
            deadline_dt = datetime.datetime.strptime(deadline, "%Y-%m-%d")
        except Exception:
            deadline_dt = None

        new_project = Project(
            name=name,
            description=description,
            department=dept_value,
            deadline=deadline_dt,
            start_date=datetime.datetime.utcnow(),
            status="active"
        )
        db.add(new_project)
        db.commit()

        if team_id:
            team = db.query(Team).filter(Team.id == team_id).first()
            if team:
                team.project_id = new_project.id
                db.commit()

        return RedirectResponse("/manager/manage_teams", status_code=303)

    @app.post("/manager/delete_team")
    async def delete_team(
        team_id: int = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "manager":
            raise HTTPException(status_code=403)

        team = db.query(Team).filter(Team.id == team_id).first()
        if team:
            db.delete(team)
            db.commit()

        return RedirectResponse("/manager/manage_teams", status_code=303)

    @app.post("/manager/assign_member")
    async def assign_team_member(
        employee_id: str = Form(...),
        team_id: int = Form(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ):
        if user.role != "manager":
            raise HTTPException(status_code=403)

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
                db.add(ProjectAssignment(
                    project_id=team.project_id,
                    employee_id=emp.employee_id,
                    employee_id_hash=hash_employee_id(emp.employee_id)
                ))
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
        assignees: Optional[str] = Form(None),
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

        hashes = []
        target_tokens = []
        assignee_user_ids = []
        assignee_list = []
        if assignees and assignees.strip():
            assignee_list = [emp_id.strip() for emp_id in assignees.split(',') if emp_id.strip()]

        if user.employee_id and user.employee_id not in assignee_list:
            assignee_list.append(user.employee_id)

        recipients = []
        if assignee_list:
            for emp_id in assignee_list:
                try:
                    pm = ProjectMeetingAssignee(meeting_id=new_meeting.id, employee_id=emp_id)
                    db.add(pm)
                    u = db.query(User).filter(User.employee_id == emp_id).first()
                    if u and hasattr(u, 'employee_id_hash') and u.employee_id_hash:
                        hashes.append(u.employee_id_hash)
                        target_tokens.append(u.employee_id_hash)
                    elif u and u.employee_id:
                        target_tokens.append(u.employee_id)
                        assignee_user_ids.append(u.id)
                    if u and u.email:
                        recipients.append({"email": u.email, "name": u.name, "employee_id": u.employee_id})
                except Exception:
                    continue
            db.commit()

        meeting_msg = f"Meeting Invitation: {title} | {mdt.strftime('%Y-%m-%d %H:%M')} | Host: {user.name} ({user.employee_id})"
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
                project_label = "No project"
                if project_id:
                    project_obj = db.query(Project).filter(Project.id == project_id).first()
                    if project_obj:
                        project_label = project_obj.name
                create_notification(
                    db,
                    u.id,
                    "Meeting assigned",
                    f"Meeting '{title}' scheduled (Project: {project_label}).",
                    "meeting",
                    "/employee"
                )
            db.commit()

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

        pid = None
        if project_id:
            try:
                pid = int(project_id)
            except (ValueError, TypeError):
                pass

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
                        project_id=pid,
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
                    project_obj = db.query(Project).filter(Project.id == pid).first()
                    if project_obj:
                        project_name = project_obj.name
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
        employees = db.query(User).filter(
            User.department == user.department,
            User.is_active == True
        ).order_by(User.name.asc()).all()

        return templates.TemplateResponse("employee/employee_manager_projects.html", {
            "request": request,
            "user": user,
            "projects": projects,
            "employees": employees
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

    @app.post("/manager/projects/update_description")
    async def manager_update_project_description(
        project_id: int = Form(...),
        description: str = Form(""),
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

        project.description = description.strip()
        db.commit()

        return JSONResponse({"ok": True, "description": project.description or ""})

    @app.post("/manager/projects/assign_employee")
    async def manager_assign_project_employee(
        project_id: int = Form(...),
        employee_id: str = Form(...),
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

        employee = db.query(User).filter(
            User.employee_id == employee_id,
            User.department == user.department,
            User.is_active == True
        ).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        if employee.current_team_id:
            team = db.query(Team).filter(Team.id == employee.current_team_id).first()
            if team and team.project_id == project.id:
                return JSONResponse({
                    "ok": False,
                    "message": "Employee already assigned via team project."
                })

        existing = db.query(ProjectAssignment).filter(
            ProjectAssignment.project_id == project.id,
            ProjectAssignment.employee_id == employee.employee_id
        ).first()
        if not existing:
            db.add(ProjectAssignment(
                project_id=project.id,
                employee_id=employee.employee_id,
                employee_id_hash=hash_employee_id(employee.employee_id)
            ))
            create_notification(
                db,
                employee.id,
                "Project assigned",
                f"You have been assigned to project {project.name}.",
                "project",
                "/employee/team"
            )
            db.commit()

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

        total_assigned = len(assigned_employees)

        return JSONResponse({
            "ok": True,
            "employee": employee.name,
            "employee_id": employee.employee_id,
            "employee_count": total_assigned,
            "assigned_employees": [emp.name for emp in assigned_employees],
            "direct_assignments": [
                {"name": emp.name, "employee_id": emp.employee_id}
                for emp in direct_assignments
            ]
        })

    @app.post("/manager/projects/unassign_employee")
    async def manager_unassign_project_employee(
        project_id: int = Form(...),
        employee_id: str = Form(...),
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

        assignment = db.query(ProjectAssignment).filter(
            ProjectAssignment.project_id == project.id,
            ProjectAssignment.employee_id == employee_id
        ).first()
        if assignment:
            db.delete(assignment)
            db.commit()

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

        return JSONResponse({
            "ok": True,
            "employee_count": len(assigned_employees),
            "assigned_employees": [emp.name for emp in assigned_employees],
            "direct_assignments": [
                {"name": emp.name, "employee_id": emp.employee_id}
                for emp in direct_assignments
            ]
        })


    @app.post("/manager/projects/add_task")
    async def manager_add_project_task(
        project_id: int = Form(...),
        title: str = Form(...),
        description: Optional[str] = Form(None),
        deadline: Optional[str] = Form(None),
        assignee_employee_id: Optional[str] = Form(None),
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

        deadline_dt = None
        if deadline:
            try:
                deadline_dt = datetime.datetime.strptime(deadline, "%Y-%m-%d")
            except Exception:
                deadline_dt = None

        new_task = ProjectTask(
            project_id=project.id,
            title=title.strip(),
            description=(description or "").strip(),
            deadline=deadline_dt,
            status="pending"
        )
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        # If an assignee is specified, create a ProjectTaskAssignee entry
        if assignee_employee_id:
            employee = db.query(User).filter(User.employee_id == assignee_employee_id, User.is_active == True).first()
            if employee:
                from .app_context import hash_employee_id
                db.add(ProjectTaskAssignee(
                    task_id=new_task.id,
                    employee_id=employee.employee_id,
                    employee_id_hash=hash_employee_id(employee.employee_id),
                    status="pending"
                ))
                db.commit()

        return JSONResponse({
            "ok": True,
            "task": {
                "id": new_task.id,
                "title": new_task.title,
                "description": new_task.description or "",
                "status": new_task.status,
                "deadline": new_task.deadline.isoformat() if new_task.deadline else None
            }
        })

    # ...existing code...
