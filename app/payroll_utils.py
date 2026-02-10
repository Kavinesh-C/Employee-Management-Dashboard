from decimal import Decimal
import datetime
from sqlalchemy import func, extract, or_
from .models import Attendance, LeaveRequest, Payroll



def calculate_monthly_payroll(db, emp, month, year):
    # Always recalculate payroll for latest leave status (ignore cached Payroll table)

    # Present days
    present_days = db.query(func.count(func.distinct(Attendance.date))).filter(
        Attendance.employee_id == emp.employee_id,
        extract("month", Attendance.date) == month,
        extract("year", Attendance.date) == year
    ).scalar() or 0

    # Approved leaves
    leave_days = db.query(func.sum(
        func.datediff(LeaveRequest.end_date, LeaveRequest.start_date) + 1
    )).filter(
        LeaveRequest.employee_id == emp.employee_id,
        LeaveRequest.status == "Approved",
        or_(
            extract("month", LeaveRequest.start_date) == month,
            extract("month", LeaveRequest.end_date) == month
        ),
        extract("year", LeaveRequest.start_date) == year
    ).scalar() or 0

    WORKING_DAYS = 22
    base_salary = Decimal(emp.base_salary or 0)
    tax_percentage = Decimal(emp.tax_percentage or 0)

    per_day_salary = base_salary / Decimal(WORKING_DAYS)

    unpaid_leaves = max(0, (leave_days or 0) - (emp.paid_leaves_allowed or 0))
    leave_deduction = Decimal(unpaid_leaves) * per_day_salary
    gross_salary = base_salary - leave_deduction
    tax = gross_salary * (tax_percentage / Decimal(100))
    allowances = Decimal(emp.allowances or 0)
    deductions = Decimal(emp.deductions or 0)
    net_salary = gross_salary - tax + allowances - deductions

    base_salary_val = round(emp.base_salary or 0.0, 2)
    leave_deduction_val = round(leave_deduction, 2)
    tax_val = round(tax, 2)
    tax_percentage_val = emp.tax_percentage or 0.0

    explanation = f"""
Base Salary: ₹{base_salary_val}
Unpaid Leaves: {unpaid_leaves}
Leave Deduction: ₹{leave_deduction_val}
Tax ({tax_percentage_val}%): ₹{tax_val}
"""

    payroll_rec = Payroll(
        employee_id=emp.employee_id,
        month=month,
        year=year,
        present_days=present_days,
        leave_days=leave_days,
        unpaid_leaves=unpaid_leaves,
        base_salary=emp.base_salary or 0.0,
        leave_deduction=leave_deduction,
        tax=tax,
        allowances=allowances,
        deductions=deductions,
        net_salary=round(net_salary, 2),
        explanation=explanation,
        locked=True
    )
    try:
        db.add(payroll_rec)
        db.commit()
        db.refresh(payroll_rec)
    except Exception:
        db.rollback()

    return {
        "present_days": present_days,
        "leave_days": leave_days,
        "unpaid_leaves": unpaid_leaves,
        "base_salary": float(base_salary),
        "leave_deduction": float(leave_deduction),
        "tax": float(tax),
        "allowances": float(allowances),
        "deductions": float(deductions),
        "net_salary": float(net_salary),
        "explanation": explanation,
        "locked": True,
        "generated_at": payroll_rec.created_at if hasattr(payroll_rec, 'created_at') else None
    }