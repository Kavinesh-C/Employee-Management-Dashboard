from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import Attendance, User, LeaveRequest
import pandas as pd
import datetime


def get_attendance_dataframe(db: Session, employee_id: str | None = None):
    """Fetch attendance data, excluding admins if no specific employee is requested."""
    admin_employee_ids = [
        row[0] for row in db.query(User.employee_id).filter(User.role == "admin").all()
    ]
    
    
    query = db.query(Attendance)
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    else:
        query = query.filter(~Attendance.employee_id.in_(admin_employee_ids))

    records = query.all()
    data = []
    for r in records:
        if r.entry_time:
            data.append({
                "employee_id": r.employee_id,
                "date": r.date,
                "entry_time": r.entry_time,
                "exit_time": r.exit_time,
                "duration": float(r.duration or 0),
                "status": r.status
            })
    return pd.DataFrame(data)


def compute_behavior_metrics(df: pd.DataFrame, db: Session = None, employee_id: str = None):
    """Compute BI metrics: late arrivals, absences, avg hours, department comparison."""
    if df.empty:
        return {
            "average_login_hour": 0,
            "late_arrival_days": 0,
            "absent_days": 0,
            "average_work_hours": 0,
            "total_days_analyzed": 0,
            "absence_trend": "stable",
            "department_comparison": None
        }

    df = df.copy()
    df["login_hour"] = pd.to_datetime(df["entry_time"]).dt.hour
    df["day_of_week"] = pd.to_datetime(df["date"]).dt.day_name()

    metrics = {
        "average_login_hour": round(df["login_hour"].mean(), 2),
        "late_arrival_days": int((df["login_hour"] > 10).sum()),
        "absent_days": int((df["status"] == "ABSENT").sum()),
        "average_work_hours": round(df["duration"].mean(), 2),
        "total_days_analyzed": int(len(df)),
        "min_work_hours": round(df["duration"].min(), 2),
        "max_work_hours": round(df["duration"].max(), 2),
    }

    # Absence trend: compare this month vs last month
    if employee_id and db:
        now = datetime.datetime.utcnow()
        current_month_start = datetime.datetime(now.year, now.month, 1).date()
        last_month_start = (current_month_start - datetime.timedelta(days=1)).replace(day=1)
        
        this_month_absents = db.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.status == "ABSENT",
            Attendance.date >= current_month_start
        ).count()
        last_month_absents = db.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.status == "ABSENT",
            Attendance.date >= last_month_start,
            Attendance.date < current_month_start
        ).count()
        
        if last_month_absents == 0:
            metrics["absence_trend"] = "stable" if this_month_absents == 0 else "increasing"
        elif this_month_absents > last_month_absents * 1.2:
            metrics["absence_trend"] = "increasing"
        elif this_month_absents < last_month_absents * 0.8:
            metrics["absence_trend"] = "decreasing"
        else:
            metrics["absence_trend"] = "stable"

    # Department-wise comparison (if org-wide data, compute avg per department)
    if not employee_id and db:
        dept_stats = db.query(
            User.department,
            func.count(func.distinct(Attendance.employee_id)).label("emp_count"),
            func.avg(Attendance.duration).label("avg_duration")
        ).join(Attendance, User.employee_id == Attendance.employee_id).filter(
            User.is_active == True,
            User.role != "admin"
        ).group_by(User.department).all()
        
        metrics["department_comparison"] = [
            {
                "department": d[0] or "Unknown",
                "employees": d[1] or 0,
                "avg_work_hours": round(float(d[2]) if d[2] else 0, 2)
            }
            for d in dept_stats
        ]

    return metrics


def detect_attendance_anomalies(df: pd.DataFrame, db: Session = None, employee_id: str = None):
    """Detect anomalies: unusual hours, late arrivals, absence spikes."""
    if df.empty:
        return []

    df = df.copy()
    anomalies = []

    # Anomaly 1: Unusual work hours (z-score > 2 or < -2)
    if df["duration"].std() > 0:
        df["z_score_duration"] = (df["duration"] - df["duration"].mean()) / df["duration"].std()
        unusual_hours = df[abs(df["z_score_duration"]) > 2]
        for _, row in unusual_hours.iterrows():
            if row["duration"] < 1:
                reason = f"⚠ Very short work hours: {row['duration']:.2f}h (unusual)"
            else:
                reason = f"⚠ Very long work hours: {row['duration']:.2f}h (unusual)"
            anomalies.append({
                "employee_id": row["employee_id"],
                "date": row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"]),
                "type": "unusual_hours",
                "value": row["duration"],
                "reason": reason
            })

    # Anomaly 2: Sudden late arrivals (beyond typical)
    if len(df) > 5:
        df["login_hour"] = pd.to_datetime(df["entry_time"]).dt.hour
        usual_login = df["login_hour"].quantile(0.5)  # median
        late_threshold = usual_login + 2  # More than 2 hours later than usual
        very_late = df[df["login_hour"] > late_threshold]
        for _, row in very_late.iterrows():
            anomalies.append({
                "employee_id": row["employee_id"],
                "date": row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"]),
                "type": "late_arrival",
                "value": int(row["login_hour"]),
                "reason": f"⚠ Late arrival at {int(row['login_hour'])}:00 (usually ~{int(usual_login)}:00)"
            })

    # Anomaly 3: Absence spikes (if employee_id provided)
    if employee_id and db:
        now = datetime.datetime.utcnow()
        month_start = datetime.datetime(now.year, now.month, 1).date()
        this_month_absents = db.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.status == "ABSENT",
            Attendance.date >= month_start
        ).count()
        
        # Compare with average over past 3 months
        three_months_ago = month_start - datetime.timedelta(days=90)
        past_avg = db.query(func.avg(
            func.count(Attendance.id)
        )).filter(
            Attendance.employee_id == employee_id,
            Attendance.status == "ABSENT",
            Attendance.date >= three_months_ago,
            Attendance.date < month_start
        ).scalar() or 0
        
        if this_month_absents > (past_avg * 1.5):
            anomalies.append({
                "employee_id": employee_id,
                "date": str(now.date()),
                "type": "absence_spike",
                "value": this_month_absents,
                "reason": f"⚠ Sudden absence spike: {this_month_absents} absences this month (avg was {int(past_avg)})"
            })

    return anomalies


def get_employee_list(db: Session, department: str = None, exclude_admins: bool = True):
    """Get list of employees (optionally filtered by department)."""
    query = db.query(User).filter(User.is_active == True)
    if exclude_admins:
        query = query.filter(User.role != "admin")
    if department:
        query = query.filter(User.department == department)
    return query.order_by(User.name.asc()).all()
