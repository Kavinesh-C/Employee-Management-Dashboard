import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .app_context import get_current_user, get_db, hash_employee_id, templates
from .models import Project, ProjectTask, ProjectTaskAssignee, Team, User

router = APIRouter(prefix="/leader")


@router.post("/delete_task")
async def delete_task(
    task_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    my_team = db.query(Team).filter(Team.leader_id == user.id).first()
    task = db.query(ProjectTask).filter(ProjectTask.id == task_id).first()
    if not my_team or not task:
        raise HTTPException(status_code=403)

    db.query(ProjectTaskAssignee).filter(ProjectTaskAssignee.task_id == task_id).delete()
    db.delete(task)
    db.commit()
    return RedirectResponse("/leader/dashboard", status_code=303)


@router.post("/edit_task")
async def edit_task(
    task_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    deadline: str = Form(...),
    assign_to_employee_id: list = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    my_team = db.query(Team).filter(Team.leader_id == user.id).first()
    task = db.query(ProjectTask).filter(ProjectTask.id == task_id).first()
    if not my_team or not task:
        raise HTTPException(status_code=403)

    existing_task = db.query(ProjectTask).filter(
        ProjectTask.project_id == task.project_id,
        ProjectTask.title == title,
        ProjectTask.id != task_id,
    ).first()
    if existing_task:
        return RedirectResponse(f"/leader/project/{task.project_id}?error=duplicate_title", status_code=303)

    task.title = title
    task.description = description
    task.deadline = datetime.datetime.strptime(deadline, "%Y-%m-%d")
    db.commit()

    current_assignees = {
        a.employee_id
        for a in db.query(ProjectTaskAssignee).filter(ProjectTaskAssignee.task_id == task_id).all()
    }
    new_assignees = set(assign_to_employee_id)

    for emp_id in current_assignees - new_assignees:
        db.query(ProjectTaskAssignee).filter(
            ProjectTaskAssignee.task_id == task_id,
            ProjectTaskAssignee.employee_id == emp_id,
        ).delete()
    for emp_id in new_assignees - current_assignees:
        db.add(
            ProjectTaskAssignee(
                task_id=task_id,
                employee_id=emp_id,
                employee_id_hash=hash_employee_id(emp_id),
            )
        )
    db.commit()

    return RedirectResponse(f"/leader/project/{task.project_id}", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
async def leader_dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    my_team = db.query(Team).filter(Team.leader_id == user.id).first()
    if not my_team or user.role not in ["manager", "employee"]:
        raise HTTPException(status_code=403)

    projects = db.query(Project).filter(Project.department == user.department).all()

    return templates.TemplateResponse(
        "employee/employee_leader_dashboard.html",
        {
            "request": request,
            "user": user,
            "team": my_team,
            "projects": projects,
        },
    )


@router.post("/assign_task")
async def assign_task(
    project_id: int = Form(...),
    title: str = Form(...),
    deadline: str = Form(...),
    assign_to_employee_id: list = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ["team_lead", "manager", "employee"]:
        raise HTTPException(status_code=403)

    existing_task = db.query(ProjectTask).filter(
        ProjectTask.project_id == project_id,
        ProjectTask.title == title,
    ).first()
    if existing_task:
        return templates.TemplateResponse(
            "employee/employee_leader_dashboard.html",
            {
                "request": Request,
                "user": user,
                "error": "Task with this title already exists in the project.",
                "team": db.query(Team).filter(Team.leader_id == user.id).first(),
                "projects": db.query(Project).filter(Project.department == user.department).all(),
            },
        )

    new_task = ProjectTask(
        project_id=project_id,
        title=title,
        deadline=datetime.datetime.strptime(deadline, "%Y-%m-%d"),
        status="pending",
    )
    db.add(new_task)
    db.commit()

    for emp_id in assign_to_employee_id:
        already_assigned = db.query(ProjectTaskAssignee).filter(
            ProjectTaskAssignee.task_id == new_task.id,
            ProjectTaskAssignee.employee_id == emp_id,
        ).first()
        if already_assigned:
            continue
        db.add(
            ProjectTaskAssignee(
                task_id=new_task.id,
                employee_id=emp_id,
                employee_id_hash=hash_employee_id(emp_id),
            )
        )
    db.commit()

    return RedirectResponse("/leader/dashboard", status_code=303)


@router.get("/project/{project_id}", response_class=HTMLResponse)
async def leader_project_detail(
    request: Request,
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    my_team = db.query(Team).filter(Team.leader_id == user.id).first()
    project = db.query(Project).filter(Project.id == project_id).first()
    if not my_team or not project or project.department != user.department:
        raise HTTPException(status_code=403)

    tasks = db.query(ProjectTask).filter(ProjectTask.project_id == project.id).all()
    for task in tasks:
        task.assignees = db.query(ProjectTaskAssignee).filter(ProjectTaskAssignee.task_id == task.id).all()
        for assignee in task.assignees:
            assignee.employee = db.query(User).filter(User.employee_id == assignee.employee_id).first()
            # Expose per-assignee status/completed_at for template
            assignee.status = getattr(assignee, 'status', 'pending')
            assignee.completed_at = getattr(assignee, 'completed_at', None)
    project.tasks = tasks

    return templates.TemplateResponse(
        "employee/leader_project_detail.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "team": my_team,
        },
    )
