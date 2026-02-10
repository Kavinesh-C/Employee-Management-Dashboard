from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import Attendance, User, LeaveRequest
import pandas as pd
import datetime


# =========================================================
# DATA INGESTION
# =========================================================
def get_attendance_dataframe(db: Session, employee_id: str | None = None):
    """
    Fetch attendance data (last 180 days).
    - If employee_id is None → ALL users (no role restriction)
    - If employee_id provided → only that user
    """

    query = db.query(Attendance)

    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)

    query = query.filter(
        Attendance.date >= datetime.date.today() - datetime.timedelta(days=180)
    )

    rows = query.all()
    data = []

    for r in rows:
        if r.entry_time:
            data.append({
                "employee_id": r.employee_id,
                "date": r.date,
                "entry_time": r.entry_time,
                "duration": float(r.duration or 0),
                "status": r.status
            })

    return pd.DataFrame(data)


# =========================================================
# METRICS / BI ENGINE
# =========================================================
def compute_behavior_metrics(df: pd.DataFrame, db: Session | None = None, employee_id: str | None = None):
    """
    Stable metrics contract – SAFE for UI rendering
    Works for:
    - Individual employee
    - Department
    - Organization-wide
    """

    metrics = {
        "average_login_hour": 0,
        "average_work_hours": 0,
        "late_arrival_days": 0,
        "absent_days": 0,
        "leave_days": 0,
        "present_days": 0,
        "min_work_hours": 0,
        "max_work_hours": 0,
        "total_days_analyzed": 0,
        "absence_trend": "stable",
        "attendance_score": 0,
        "risk_level": "high",
        "chart_breakdown": {
            "labels": [],
            "values": []
        }
    }

    if df.empty:
        return metrics

    df = df.copy()

    if "entry_time" not in df.columns:
        return metrics

    df["login_hour"] = pd.to_datetime(df["entry_time"]).dt.hour

    present = int((df["status"] == "PRESENT").sum())
    absent = int((df["status"] == "ABSENT").sum())
    late = int((df["login_hour"] > 10).sum())

    # Leave calculation (employee-specific if ID provided)
    leave_days = 0
    if employee_id and db:
        leaves = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status == "Approved"
        ).all()

        for l in leaves:
            leave_days += (l.end_date - l.start_date).days + 1

    # Work hours
    metrics["average_login_hour"] = round(df["login_hour"].mean(), 2)
    metrics["average_work_hours"] = round(df["duration"].mean(), 2)
    metrics["min_work_hours"] = round(df["duration"].min(), 2)
    metrics["max_work_hours"] = round(df["duration"].max(), 2)

    metrics["present_days"] = present
    metrics["absent_days"] = absent
    metrics["late_arrival_days"] = late
    metrics["leave_days"] = leave_days
    metrics["total_days_analyzed"] = int(len(df))

    # Attendance score
    score = 100
    score -= late * 2
    score -= absent * 6
    score -= leave_days * 1

    if present == 0:
        score = 0

    score = max(0, score)
    metrics["attendance_score"] = score

    if score < 60:
        metrics["risk_level"] = "high"
    elif score < 80:
        metrics["risk_level"] = "medium"
    else:
        metrics["risk_level"] = "low"

    metrics["chart_breakdown"] = {
        "labels": ["Present", "Absent", "Leave", "Late"],
        "values": [present, absent, leave_days, late]
    }

    return metrics


# =========================================================
# ANOMALY DETECTION
# =========================================================
def detect_attendance_anomalies(df: pd.DataFrame):
    anomalies = []

    if df.empty or "duration" not in df.columns or len(df) < 10:
        return anomalies

    if df["duration"].std() > 0:
        df["z"] = (df["duration"] - df["duration"].mean()) / df["duration"].std()
        for _, r in df[abs(df["z"]) > 2].iterrows():
            anomalies.append({
                "employee_id": r["employee_id"],
                "date": str(r["date"]),
                "reason": f"Unusual work duration ({r['duration']:.2f}h)"
            })

    return anomalies


# =========================================================
# DEPARTMENT LEAVE ABUSE DETECTION (ALL ROLES INCLUDED)
# =========================================================
def detect_department_leave_abuse(db: Session):
    leaves = (
        db.query(User.department, LeaveRequest.start_date, LeaveRequest.end_date)
        .join(User, User.employee_id == LeaveRequest.employee_id)
        .filter(
            LeaveRequest.status == "Approved",
            User.is_active == True
        )
        .all()
    )

    if not leaves:
        return []

    dept_leave = {}
    for dept, start, end in leaves:
        dept_leave[dept] = dept_leave.get(dept, 0) + ((end - start).days + 1)

    emp_counts = dict(
        db.query(User.department, func.count(User.id))
        .filter(User.is_active == True)
        .group_by(User.department)
        .all()
    )

    total_leave = sum(dept_leave.values())
    total_emp = sum(emp_counts.values())
    org_avg = total_leave / total_emp if total_emp else 0

    abused = []
    for dept, days in dept_leave.items():
        avg = days / emp_counts.get(dept, 1)
        if avg > org_avg * 1.5:
            abused.append({
                "department": dept,
                "avg_leave": round(avg, 2),
                "org_avg": round(org_avg, 2)
            })

    return abused


# =========================================================
# PREDICTIVE ABSENTEEISM RISK (ALL ROLES)
# =========================================================
def predict_absenteeism_risk(db: Session, employee_id: str):
    three_months_ago = datetime.date.today() - datetime.timedelta(days=90)

    recent_absents = db.query(func.count(Attendance.id)).filter(
        Attendance.employee_id == employee_id,
        Attendance.status == "ABSENT",
        Attendance.date >= three_months_ago
    ).scalar() or 0

    df = get_attendance_dataframe(db, employee_id)
    metrics = compute_behavior_metrics(df, db, employee_id)

    risk_score = recent_absents * 2
    if metrics["attendance_score"] < 70:
        risk_score += 10

    if risk_score >= 20:
        risk = "high"
    elif risk_score >= 10:
        risk = "medium"
    else:
        risk = "low"

    return {
        "employee_id": employee_id,
        "risk": risk,
        "risk_score": risk_score
    }


# =========================================================
# TOP / LOW PERFORMERS (ALL USERS, SAFE)
# =========================================================
def compute_performer_lists(db: Session):
    top, low = [], []

    users = db.query(User).filter(User.is_active == True).all()

    for u in users:
        df = get_attendance_dataframe(db, u.employee_id)
        metrics = compute_behavior_metrics(df, db, u.employee_id)

        if metrics["present_days"] == 0:
            continue

        record = {
            "name": u.name,
            "employee_id": u.employee_id,
            "score": metrics["attendance_score"]
        }

        if metrics["attendance_score"] >= 85:
            top.append(record)
        elif metrics["attendance_score"] < 60:
            low.append(record)

    return top, low
