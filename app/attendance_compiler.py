from sqlalchemy.orm import Session
from datetime import date
from app.models import (
    User,
    Attendance,
    AttendanceDaily,
    LeaveRequest,
    OfficeHoliday
)

def compile_daily_attendance(db: Session, target_date: date | None = None):
    if not target_date:
        target_date = date.today()

    users = db.query(User).filter(User.is_active == True).all()

    for user in users:

        # Skip if already compiled
        exists = db.query(AttendanceDaily).filter(
            AttendanceDaily.employee_id == user.employee_id,
            AttendanceDaily.date == target_date
        ).first()

        if exists:
            continue

        # 1️⃣ Approved leave?
        leave = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == user.employee_id,
            LeaveRequest.status == "Approved",
            LeaveRequest.start_date <= target_date,
            LeaveRequest.end_date >= target_date
        ).first()

        if leave:
            status = "LEAVE"
            check_in = None

        # 2️⃣ Office holiday?
        elif db.query(OfficeHoliday).filter(
            OfficeHoliday.event_date == target_date
        ).first():
            status = "HOLIDAY"
            check_in = None

        # 3️⃣ Weekend?
        elif target_date.weekday() >= 5:
            status = "WEEKEND"
            check_in = None

        # 4️⃣ Attendance present?
        else:
            attendance = db.query(Attendance).filter(
                Attendance.employee_id == user.employee_id,
                Attendance.date == target_date
            ).first()

            if attendance:
                status = "PRESENT"
                check_in = attendance.entry_time.time() if attendance.entry_time else None
            else:
                status = "ABSENT"
                check_in = None

        db.add(
            AttendanceDaily(
                employee_id=user.employee_id,
                date=target_date,
                status=status,
                check_in_time=check_in
            )
        )

    db.commit()
