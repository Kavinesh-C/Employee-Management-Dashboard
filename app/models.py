from sqlalchemy import Column, Integer, String, DateTime, Boolean, Float, Text, Date, ForeignKey, Time, Enum, UniqueConstraint, LargeBinary
from sqlalchemy.orm import relationship
from .database import Base
import datetime

# --- CORE USER & AUTH ---


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), unique=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    rfid_tag = Column(String(100), unique=True, nullable=False)
    title = Column(String(100), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    photo_path = Column(String(255), nullable=True)
    photo_blob = Column(LargeBinary, nullable=True)
    photo_mime = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    phone = Column(String(40), nullable=True)
    address = Column(Text, nullable=True)
    # employee_id_hash = Column(String(64), nullable=True, index=True)  # SHA256 hash of employee_id for security
    
    # Roles: 'admin', 'manager', 'team_lead', 'employee'
    role = Column(String(50), nullable=False) 
    department = Column(String(100), nullable=True)
    password_hash = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)

    # Payroll (industry-grade)
    hourly_rate = Column(Float, default=200.0)
    base_salary = Column(Float, default=30000.0)
    paid_leaves_allowed = Column(Integer, default=2)
    allowances = Column(Float, default=0.0)
    deductions = Column(Float, default=0.0)
    tax_percentage = Column(Float, default=10.0)

    # Leadership Flags
    can_manage = Column(Boolean, default=False)
    current_team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    active_leader = Column(Boolean, default=False)

    # Relationships
    team = relationship("Team", back_populates="members", foreign_keys=[current_team_id])
    attendance_logs = relationship("Attendance", back_populates="user", cascade="all, delete-orphan")
    # Personal Tasks relationship
    personal_tasks = relationship(
        "Task",
        back_populates="user",
        foreign_keys="Task.user_id"
    )


# --- ORGANIZATION & TEAMS ---

class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(String(255), nullable=True)

class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(String(50), unique=True, nullable=False)
    room_no = Column(String(50), nullable=False)
    location_name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    user = relationship("User", backref="team_memberships")
    team = relationship("Team", back_populates="memberships")

class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    department = Column(String(100), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    
    # Who is acting as leader RIGHT NOW (swaps daily)
    leader_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Who is the ACTUAL leader (to swap back to)
    permanent_leader_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    leader = relationship("User", foreign_keys=[leader_id])
    permanent_leader = relationship("User", foreign_keys=[permanent_leader_id])
    members = relationship("User", back_populates="team", foreign_keys="User.current_team_id")
    memberships = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    project = relationship("Project")


# --- ATTENDANCE ---

class Attendance(Base):
    # This is the simplified view used in the dashboard
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False, index=True)
    date = Column(Date, nullable=False)
    entry_time = Column(DateTime, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    duration = Column(Float, default=0.0)
    status = Column(String(20), default="PRESENT")
    location_name = Column(String(100), nullable=True)
    room_no = Column(String(50), nullable=True)

    user = relationship("User", back_populates="attendance_logs")

class AttendanceDaily(Base):
    # Detailed daily summary
    __tablename__ = "attendance_daily"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(Date, nullable=False)
    status = Column(String(20)) # PRESENT, ABSENT, LEAVE, LATE
    check_in_time = Column(Time, nullable=True)

class AttendanceDate(Base):
    __tablename__ = "attendance_dates"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(Date, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uix_attendance_dates_user_date"),
    )

    user = relationship("User")

class AttendanceLog(Base):
    # Raw logs for every movement
    __tablename__ = "attendance_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    location_name = Column(String(255))
    room_no = Column(String(50))

class UnknownRFID(Base):
    __tablename__ = "unknown_rfids"
    id = Column(Integer, primary_key=True, index=True)
    rfid_tag = Column(String(100), nullable=False)
    location = Column(String(100), nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

class InappropriateEntry(Base):
    __tablename__ = "inappropriate_entries"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=True)
    rfid_tag = Column(String(100), nullable=False)
    location_name = Column(String(100), nullable=False)
    room_no = Column(String(50), nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    reason = Column(String(255), default="Invalid room - not in Room table")

class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), ForeignKey("users.employee_id"))
    start_date = Column(Date)
    end_date = Column(Date)
    reason = Column(String(255))
    status = Column(String(20), default="Pending")
    
    # Relationship to access user department for Managers
    user = relationship("User", foreign_keys=[employee_id], primaryjoin="User.employee_id == LeaveRequest.employee_id")

class RemovedEmployee(Base):
    __tablename__ = "removed_employees"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), nullable=False)
    name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    rfid_tag = Column(String(100), nullable=False)
    role = Column(String(50), nullable=False)
    department = Column(String(100), nullable=True)
    removed_at = Column(DateTime, default=datetime.datetime.utcnow)


# --- PROJECT MANAGEMENT ---

class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    department = Column(String(100)) # To filter for Managers
    start_date = Column(DateTime)
    deadline = Column(DateTime)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    tasks = relationship("ProjectTask", back_populates="project")
    assignments = relationship("ProjectAssignment", back_populates="project")
    personal_tasks = relationship("Task", back_populates="project")

class ProjectTask(Base):
    __tablename__ = "project_tasks"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    status = Column(String(20), default="pending")
    deadline = Column(DateTime, nullable=True) # New: Deadline for Team Lead
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="tasks")
    assignees = relationship("ProjectTaskAssignee", back_populates="task")

class ProjectAssignment(Base):
    # Links a User to a Project
    __tablename__ = "project_assignments"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False)
    employee_id_hash = Column(String(64), nullable=True, index=True)

    project = relationship("Project", back_populates="assignments")
    employee = relationship("User", primaryjoin="User.employee_id == ProjectAssignment.employee_id")

class ProjectTaskAssignee(Base):
    # Links a User to a specific Task
    __tablename__ = "project_task_assignees"
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("project_tasks.id"), nullable=False)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False)
    employee_id_hash = Column(String(64), nullable=True, index=True)
    # Per-assignee completion
    status = Column(String(20), default="pending")  # pending, in-progress, completed
    completed_at = Column(DateTime, nullable=True)

    task = relationship("ProjectTask", back_populates="assignees")
    employee = relationship("User", primaryjoin="User.employee_id == ProjectTaskAssignee.employee_id")


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    meeting_datetime = Column(DateTime, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # Jitsi Integration
    meeting_link = Column(String(500), nullable=True)  # Full Jitsi meeting URL
    room_name = Column(String(200), nullable=True)     # Unique room identifier

    project = relationship("Project")
    creator = relationship("User", primaryjoin="User.id == Meeting.created_by")
    assignees = relationship("ProjectMeetingAssignee", back_populates="meeting")


class ProjectMeetingAssignee(Base):
    __tablename__ = "project_meeting_assignees"
    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False)

    meeting = relationship("Meeting", back_populates="assignees")
    employee = relationship("User", primaryjoin="User.employee_id == ProjectMeetingAssignee.employee_id")


class MeetingAttendance(Base):
    __tablename__ = "meeting_attendance"
    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False)
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)

    meeting = relationship("Meeting")
    employee = relationship("User", primaryjoin="User.employee_id == MeetingAttendance.employee_id")


# --- GENERAL TASKS (Personal To-Do) ---

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    # Maps to the employee_id string in the User table
    user_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False, index=True)

    # Manager/creator who assigned this task
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # CRITICAL: Link task to project for context and authority
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending / in-progress / done
    priority = Column(String(20), default="medium") # low / medium / high
    
    due_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships to access the user and project objects
    user = relationship(
        "User",
        back_populates="personal_tasks",
        foreign_keys=[user_id]
    )
    project = relationship("Project", back_populates="personal_tasks")


class Payroll(Base):
    __tablename__ = "payrolls"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(60), ForeignKey("users.employee_id"), nullable=False, index=True)
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    present_days = Column(Integer, default=0)
    leave_days = Column(Integer, default=0)
    unpaid_leaves = Column(Integer, default=0)
    base_salary = Column(Float, default=0.0)
    leave_deduction = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    allowances = Column(Float, default=0.0)
    deductions = Column(Float, default=0.0)
    net_salary = Column(Float, default=0.0)
    explanation = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    locked = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint('employee_id', 'month', 'year', name='uix_employee_month_year'),
    )


# --- OFFICE HOLIDAYS ---
class OfficeHoliday(Base):
    __tablename__ = "office_holidays"
    id = Column(Integer, primary_key=True, index=True)
    event_date = Column(Date, nullable=False)
    title = Column(String(200), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class EmailSettings(Base):
    __tablename__ = "email_settings"
    id = Column(Integer, primary_key=True, index=True)
    smtp_user = Column(String(255), nullable=True)
    smtp_from = Column(String(255), nullable=True)
    smtp_pass = Column(String(255), nullable=True)
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(String(20), nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=True)
    notif_type = Column(String(50), nullable=True)
    link = Column(String(255), nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User")


# --- CALENDAR EVENTS ---
class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_date = Column(Date, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    notes = Column(Text, nullable=True)
    event_type = Column(String(50), default="general")  # general, meeting, task, personal_leave, office_holiday
    
    # For admin: can target specific teams or employees
    target_team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    target_employee_hashes = Column(Text, nullable=True)  # comma-separated list with leading/trailing comma
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # Relationships
    user = relationship("User")
    team = relationship("Team")


class CalendarSettings(Base):
    __tablename__ = "calendar_settings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    country_code = Column(String(2), default="IN")
    state_code = Column(String(10), nullable=True)
    
    # Relationships
    user = relationship("User")