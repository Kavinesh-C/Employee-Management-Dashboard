from sqlalchemy.orm import Session
from app.models import Attendance, User
import pandas as pd


def get_attendance_dataframe(db: Session, employee_id: str | None = None):
    # Get all admin employee_ids to exclude them from analytics
    admin_employee_ids = [
        row[0] for row in db.query(User.employee_id).filter(User.role == "admin").all()
    ]
    
    query = db.query(Attendance)
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    else:
        # When no employee_id filter, exclude admins from org-wide analytics
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


def compute_behavior_metrics(df: pd.DataFrame):
    if df.empty:
        return {}

    df["login_hour"] = pd.to_datetime(df["entry_time"]).dt.hour

    return {
        "average_login_hour": round(df["login_hour"].mean(), 2),
        "late_arrival_days": int((df["login_hour"] > 10).sum()),
        "absent_days": int((df["status"] == "ABSENT").sum()),
        "average_work_hours": round(df["duration"].mean(), 2),
        "total_days_analyzed": int(len(df))
    }


def detect_attendance_anomalies(df: pd.DataFrame):
    if df.empty or df["duration"].std() == 0:
        return []

    df["z_score"] = (df["duration"] - df["duration"].mean()) / df["duration"].std()
    anomalies = df[abs(df["z_score"]) > 2]

    return anomalies[["employee_id", "date", "duration"]].to_dict(orient="records")
