from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import Attendance, AttendanceDaily, User, LeaveRequest
import pandas as pd
import datetime

# =========================================================
# 1. DATA INGESTION
# =========================================================
def get_attendance_dataframe(db: Session, employee_id: str | None = None, days: int = 30):
    cutoff_date = datetime.date.today() - datetime.timedelta(days=days)
    
    query = db.query(
        Attendance.date,
        Attendance.entry_time,
        Attendance.duration,
        Attendance.employee_id,
        User.name.label("employee_name"),
        User.department.label("department")
    ).join(User, Attendance.employee_id == User.employee_id)\
     .filter(Attendance.date >= cutoff_date)

    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)

    rows = query.all()

    if not rows:
        return pd.DataFrame(columns=["date", "entry_time", "duration", "employee_id", "name", "department", "login_hour"])

    data = []
    for r in rows:
        data.append({
            "date": r.date,
            "entry_time": r.entry_time,
            "duration": float(r.duration) if r.duration else 0.0,
            "employee_id": r.employee_id,
            "name": r.employee_name,
            "department": r.department
        })

    df = pd.DataFrame(data)
    
    if not df.empty and "entry_time" in df.columns:
        df["login_hour"] = pd.to_datetime(df["entry_time"]).dt.hour + (pd.to_datetime(df["entry_time"]).dt.minute / 60)
    
    return df

# =========================================================
# 2. METRICS ENGINE
# =========================================================
def compute_behavior_metrics(db: Session, df: pd.DataFrame, employee_id: str | None = None):
    metrics = {
        "average_login_hour": 0,
        "average_work_hours": 0,
        "late_arrival_days": 0,
        "present_days": 0,
        "absent_days": 0,
        "leave_days": 0,
        "upcoming_leave_days": 0,
        "leaves_allowed": 0,
        "leaves_remaining": 0,
        "total_days_analyzed": 0, 
        "attendance_score": 100,
        "risk_level": "low",
        "alerts": [],
        "chart_breakdown": {"labels": ["Present", "Absent", "Leave", "Late"], "values": [0, 0, 0, 0]}
    }

    target_user_id = None
    if employee_id:
        user_obj = db.query(User).filter(User.employee_id == employee_id).first()
        if user_obj:
            target_user_id = user_obj.id
            metrics["leaves_allowed"] = int(user_obj.paid_leaves_allowed or 0)

    # --- B. Database Counts ---
    cutoff_date = datetime.date.today() - datetime.timedelta(days=30)
    query = db.query(AttendanceDaily.status, func.count(AttendanceDaily.id)).filter(AttendanceDaily.date >= cutoff_date)

    if target_user_id:
        query = query.filter(AttendanceDaily.user_id == target_user_id)
        
    daily_stats = query.group_by(AttendanceDaily.status).all()
    stat_map = {s[0]: s[1] for s in daily_stats}

    metrics["present_days"] = stat_map.get("PRESENT", 0)
    metrics["absent_days"] = stat_map.get("ABSENT", 0)
    metrics["leave_days"] = stat_map.get("LEAVE", 0)
    metrics["late_arrival_days"] = stat_map.get("LATE", 0)

    # --- C. DataFrame Analytics ---
    if not df.empty:
        metrics["average_work_hours"] = round(df["duration"].mean(), 2)
        metrics["average_login_hour"] = round(df["login_hour"].mean(), 2)
        metrics["total_days_analyzed"] = len(df)

    # --- D. Future Leaves ---
    if employee_id:
        leaves_allowed = int(metrics.get("leaves_allowed") or 0)
        leave_days = int(metrics.get("leave_days") or 0)
        metrics["leaves_remaining"] = max(0, leaves_allowed - leave_days)
        upcoming_reqs = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status == 'Approved',
            LeaveRequest.start_date > datetime.date.today()
        ).all()
        future_days = sum([
            ((req.end_date - req.start_date).days + 1)
            for req in upcoming_reqs
            if req.start_date is not None and req.end_date is not None
        ])
        metrics["upcoming_leave_days"] = future_days
        if future_days > 0:
            metrics["alerts"].append(f"ℹ️ Scheduled for {future_days} days of leave soon")
    else:
        # Organization View
        all_upcoming = db.query(LeaveRequest).filter(
            LeaveRequest.status == 'Approved',
            LeaveRequest.start_date > datetime.date.today()
        ).all()
        total_future = sum([
            ((req.end_date - req.start_date).days + 1)
            for req in all_upcoming
            if req.start_date is not None and req.end_date is not None
        ])
        metrics["upcoming_leave_days"] = total_future
        if total_future > 5:
            metrics["alerts"].append(f"ℹ️ {total_future} total man-days of leave upcoming")

    # --- E. Scoring ---
    score = 100
    if employee_id:
        score -= (metrics["absent_days"] * 5)
        score -= (metrics["late_arrival_days"] * 2)
        score -= (metrics["leave_days"] * 1)
        
        if metrics["absent_days"] >= 3: metrics["alerts"].append("⚠ Sudden absence spike")
        if metrics["late_arrival_days"] >= 5: metrics["alerts"].append("⚠ Frequent late arrivals")
        if metrics["average_work_hours"] > 0 and metrics["average_work_hours"] < 6.0: metrics["alerts"].append("⚠ Low average work hours")
    else:
        total_users = db.query(User).filter(User.is_active == True).count() or 1
        score -= ((metrics["absent_days"] / total_users) * 5)
        score -= ((metrics["late_arrival_days"] / total_users) * 2)
        if (metrics["late_arrival_days"] / total_users) > 3: metrics["alerts"].append("⚠ High organization-wide lateness")

    metrics["attendance_score"] = max(0, min(100, int(score)))

    if metrics["attendance_score"] >= 85: metrics["risk_level"] = "low"
    elif metrics["attendance_score"] >= 65: metrics["risk_level"] = "medium"
    else: metrics["risk_level"] = "high"

    metrics["chart_breakdown"]["values"] = [metrics["present_days"], metrics["absent_days"], metrics["leave_days"], metrics["late_arrival_days"]]
    return metrics

# =========================================================
# 3. ANOMALY DETECTION
# =========================================================
def detect_attendance_anomalies(df: pd.DataFrame):
    anomalies = []
    if df.empty or len(df) < 5:
        return anomalies

    std_dev = df["duration"].std()
    mean_val = df["duration"].mean()

    if std_dev > 0:
        df["z_score"] = (df["duration"] - mean_val) / std_dev
        outliers = df[abs(df["z_score"]) > 1.8]

        for _, row in outliers.iterrows():
            reason = "Shift too long (>12h)" if row["duration"] > 12 else \
                     "Shift too short (<4h)" if row["duration"] < 4 else "Unusual duration"
            
            anomalies.append({
                "date": row["date"],
                "name": row.get("name", "Unknown"), 
                "id": row.get("employee_id", ""),
                "dept": row.get("department", ""),
                "val": f"{row['duration']:.1f}h",
                "reason": reason,
                "severity": "high" if abs(row["z_score"]) > 2.5 else "medium"
            })

    if "login_hour" in df.columns:
        org_mean_entry = df["login_hour"].mean()
        late_entries = df[df["login_hour"] > (org_mean_entry + 1.5)]

        for _, row in late_entries.iterrows():
            h = int(row["login_hour"])
            m = int((row["login_hour"] % 1) * 60)
            if not any(a['date'] == row['date'] and a['id'] == row['employee_id'] for a in anomalies):
                anomalies.append({
                    "date": row["date"],
                    "name": row.get("name", "Unknown"),
                    "id": row.get("employee_id", ""),
                    "dept": row.get("department", ""),
                    "val": f"{h:02d}:{m:02d}",
                    "reason": "Late Arrival",
                    "severity": "medium"
                })

    return sorted(anomalies, key=lambda x: x["date"], reverse=True)[:20]

# =========================================================
# 4. DEPARTMENT STATS
# =========================================================
def compute_department_stats(db: Session):
    depts = db.query(User.department).filter(User.is_active==True).distinct().all()
    dept_stats = []

    for (d_name,) in depts:
        if not d_name: continue
        
        users = db.query(User).filter(User.department == d_name, User.is_active==True).all()
        ids = [u.employee_id for u in users]
        if not ids: continue

        absents = db.query(AttendanceDaily).filter(
            AttendanceDaily.user_id.in_([u.id for u in users]),
            AttendanceDaily.status == 'ABSENT',
            AttendanceDaily.date >= datetime.date.today() - datetime.timedelta(days=30)
        ).count()
        
        lates = db.query(AttendanceDaily).filter(
            AttendanceDaily.user_id.in_([u.id for u in users]),
            AttendanceDaily.status == 'LATE',
            AttendanceDaily.date >= datetime.date.today() - datetime.timedelta(days=30)
        ).count()
        
        base = 100
        penalty = (absents * 5) + (lates * 2)
        avg_penalty = penalty / len(users)
        dept_score = max(0, int(base - avg_penalty))
        
        dept_stats.append({
            "name": d_name,
            "headcount": len(users),
            "score": dept_score,
            "status": "Excellent" if dept_score > 85 else "Risk" if dept_score < 65 else "Good"
        })

    return sorted(dept_stats, key=lambda x: x["score"])

# =========================================================
# 5. LEADERBOARD (FIXED)
# =========================================================
def compute_performer_lists(db: Session):
    top, low = [], []
    users = db.query(User).filter(User.is_active == True, User.role != 'admin').all()
    
    df = get_attendance_dataframe(db, days=30)
    
    for user in users:
        u_df = df[df["employee_id"] == user.employee_id] if not df.empty else pd.DataFrame()
        metrics = compute_behavior_metrics(db, u_df, user.employee_id)
        
        # --- CRITICAL FIX ---
        # 1. Skip if 'total_days_analyzed' key is missing (KeyError fix)
        # 2. Skip if they have ZERO present days (Fixes "Ghost Employee" issue)
        #    If you have never been present, you shouldn't be on the Top/Low list.
        if metrics.get("present_days", 0) == 0:
            continue
        
        rec = {
            "name": user.name, 
            "employee_id": user.employee_id, 
            "score": metrics["attendance_score"],
            "dept": user.department
        }
        
        if metrics["attendance_score"] >= 90: top.append(rec)
        elif metrics["attendance_score"] < 70: low.append(rec)
            
    return sorted(top, key=lambda x: x["score"], reverse=True)[:5], sorted(low, key=lambda x: x["score"])[:5]
