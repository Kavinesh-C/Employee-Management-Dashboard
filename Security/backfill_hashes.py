from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    User,
    Attendance,
    RemovedEmployee,
    UnknownRFID,
    Room,
    Department,
    Task,
    LeaveRequest,
    Team,
)
from Security.data_integrity import sha256_hex
from Security.hash_history import log_hash_history


def _hash(value: str | None) -> str | None:
    if value is None:
        return None
    return sha256_hex(value)


def _set_hash(obj, hash_attr: str, source_attr: str) -> bool:
    source_value = getattr(obj, source_attr)
    new_value = _hash(source_value) if source_value else None
    if getattr(obj, hash_attr) != new_value:
        setattr(obj, hash_attr, new_value)
        return True
    return False


def _maybe_log_history(
    *,
    entity_type: str,
    entity_id: str | None,
    field_name: str,
    old_hash: str | None,
    new_hash: str | None,
    employee_name: str | None,
    details: str,
) -> None:
    if old_hash == new_hash:
        return
    log_hash_history(
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_hash=old_hash,
        new_hash=new_hash,
        actor_id=None,
        actor_name="system_backfill",
        employee_name=employee_name,
        details=details,
    )


def backfill_hashes() -> dict[str, int]:
    db = SessionLocal()
    updated = {
        "users": 0,
        "attendance": 0,
        "removed_employees": 0,
        "unknown_rfids": 0,
        "rooms": 0,
        "departments": 0,
        "tasks": 0,
        "leave_requests": 0,
        "teams": 0,
    }

    try:
        for user in db.query(User).all():
            changed = False
            old_employee_id_hash = user.employee_id_hash
            old_name_hash = user.name_hash
            old_email_hash = user.email_hash
            old_rfid_tag_hash = user.rfid_tag_hash
            old_role_hash = user.role_hash
            old_department_hash = user.department_hash
            changed |= _set_hash(user, "employee_id_hash", "employee_id")
            changed |= _set_hash(user, "name_hash", "name")
            changed |= _set_hash(user, "email_hash", "email")
            changed |= _set_hash(user, "rfid_tag_hash", "rfid_tag")
            changed |= _set_hash(user, "role_hash", "role")
            changed |= _set_hash(user, "department_hash", "department")
            if changed:
                updated["users"] += 1
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="employee_id",
                                   old_hash=old_employee_id_hash, new_hash=user.employee_id_hash,
                                   employee_name=user.name, details="backfill")
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="name",
                                   old_hash=old_name_hash, new_hash=user.name_hash,
                                   employee_name=user.name, details="backfill")
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="email",
                                   old_hash=old_email_hash, new_hash=user.email_hash,
                                   employee_name=user.name, details="backfill")
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="rfid_tag",
                                   old_hash=old_rfid_tag_hash, new_hash=user.rfid_tag_hash,
                                   employee_name=user.name, details="backfill")
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="role",
                                   old_hash=old_role_hash, new_hash=user.role_hash,
                                   employee_name=user.name, details="backfill")
                _maybe_log_history(entity_type="User", entity_id=user.employee_id, field_name="department",
                                   old_hash=old_department_hash, new_hash=user.department_hash,
                                   employee_name=user.name, details="backfill")

        for attendance in db.query(Attendance).all():
            changed = False
            old_employee_id_hash = attendance.employee_id_hash
            old_status_hash = attendance.status_hash
            old_location_hash = attendance.location_name_hash
            old_room_hash = attendance.room_no_hash
            changed |= _set_hash(attendance, "employee_id_hash", "employee_id")
            changed |= _set_hash(attendance, "status_hash", "status")
            changed |= _set_hash(attendance, "location_name_hash", "location_name")
            changed |= _set_hash(attendance, "room_no_hash", "room_no")
            if changed:
                updated["attendance"] += 1
                _maybe_log_history(entity_type="Attendance", entity_id=str(attendance.id), field_name="employee_id",
                                   old_hash=old_employee_id_hash, new_hash=attendance.employee_id_hash,
                                   employee_name=attendance.user.name if attendance.user else None, details="backfill")
                _maybe_log_history(entity_type="Attendance", entity_id=str(attendance.id), field_name="status",
                                   old_hash=old_status_hash, new_hash=attendance.status_hash,
                                   employee_name=attendance.user.name if attendance.user else None, details="backfill")
                _maybe_log_history(entity_type="Attendance", entity_id=str(attendance.id), field_name="location_name",
                                   old_hash=old_location_hash, new_hash=attendance.location_name_hash,
                                   employee_name=attendance.user.name if attendance.user else None, details="backfill")
                _maybe_log_history(entity_type="Attendance", entity_id=str(attendance.id), field_name="room_no",
                                   old_hash=old_room_hash, new_hash=attendance.room_no_hash,
                                   employee_name=attendance.user.name if attendance.user else None, details="backfill")

        for removed in db.query(RemovedEmployee).all():
            changed = False
            old_employee_id_hash = removed.employee_id_hash
            old_name_hash = removed.name_hash
            old_email_hash = removed.email_hash
            old_rfid_tag_hash = removed.rfid_tag_hash
            old_role_hash = removed.role_hash
            old_department_hash = removed.department_hash
            changed |= _set_hash(removed, "employee_id_hash", "employee_id")
            changed |= _set_hash(removed, "name_hash", "name")
            changed |= _set_hash(removed, "email_hash", "email")
            changed |= _set_hash(removed, "rfid_tag_hash", "rfid_tag")
            changed |= _set_hash(removed, "role_hash", "role")
            changed |= _set_hash(removed, "department_hash", "department")
            if changed:
                updated["removed_employees"] += 1
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="employee_id",
                                   old_hash=old_employee_id_hash, new_hash=removed.employee_id_hash,
                                   employee_name=removed.name, details="backfill")
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="name",
                                   old_hash=old_name_hash, new_hash=removed.name_hash,
                                   employee_name=removed.name, details="backfill")
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="email",
                                   old_hash=old_email_hash, new_hash=removed.email_hash,
                                   employee_name=removed.name, details="backfill")
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="rfid_tag",
                                   old_hash=old_rfid_tag_hash, new_hash=removed.rfid_tag_hash,
                                   employee_name=removed.name, details="backfill")
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="role",
                                   old_hash=old_role_hash, new_hash=removed.role_hash,
                                   employee_name=removed.name, details="backfill")
                _maybe_log_history(entity_type="RemovedEmployee", entity_id=removed.employee_id, field_name="department",
                                   old_hash=old_department_hash, new_hash=removed.department_hash,
                                   employee_name=removed.name, details="backfill")

        for unknown in db.query(UnknownRFID).all():
            changed = False
            old_rfid_tag_hash = unknown.rfid_tag_hash
            old_location_hash = unknown.location_hash
            changed |= _set_hash(unknown, "rfid_tag_hash", "rfid_tag")
            changed |= _set_hash(unknown, "location_hash", "location")
            if changed:
                updated["unknown_rfids"] += 1
                _maybe_log_history(entity_type="UnknownRFID", entity_id=unknown.rfid_tag, field_name="rfid_tag",
                                   old_hash=old_rfid_tag_hash, new_hash=unknown.rfid_tag_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="UnknownRFID", entity_id=unknown.rfid_tag, field_name="location",
                                   old_hash=old_location_hash, new_hash=unknown.location_hash,
                                   employee_name=None, details="backfill")

        for room in db.query(Room).all():
            changed = False
            old_room_id_hash = room.room_id_hash
            old_room_no_hash = room.room_no_hash
            old_location_hash = room.location_name_hash
            changed |= _set_hash(room, "room_id_hash", "room_id")
            changed |= _set_hash(room, "room_no_hash", "room_no")
            changed |= _set_hash(room, "location_name_hash", "location_name")
            if changed:
                updated["rooms"] += 1
                _maybe_log_history(entity_type="Room", entity_id=room.room_id, field_name="room_id",
                                   old_hash=old_room_id_hash, new_hash=room.room_id_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Room", entity_id=room.room_id, field_name="room_no",
                                   old_hash=old_room_no_hash, new_hash=room.room_no_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Room", entity_id=room.room_id, field_name="location_name",
                                   old_hash=old_location_hash, new_hash=room.location_name_hash,
                                   employee_name=None, details="backfill")

        for department in db.query(Department).all():
            changed = False
            old_name_hash = department.name_hash
            changed |= _set_hash(department, "name_hash", "name")
            if changed:
                updated["departments"] += 1
                _maybe_log_history(entity_type="Department", entity_id=department.name, field_name="name",
                                   old_hash=old_name_hash, new_hash=department.name_hash,
                                   employee_name=None, details="backfill")

        for task in db.query(Task).all():
            changed = False
            old_user_id_hash = task.user_id_hash
            old_title_hash = task.title_hash
            old_status_hash = task.status_hash
            old_priority_hash = task.priority_hash
            changed |= _set_hash(task, "user_id_hash", "user_id")
            changed |= _set_hash(task, "title_hash", "title")
            changed |= _set_hash(task, "status_hash", "status")
            changed |= _set_hash(task, "priority_hash", "priority")
            if changed:
                updated["tasks"] += 1
                _maybe_log_history(entity_type="Task", entity_id=str(task.id), field_name="user_id",
                                   old_hash=old_user_id_hash, new_hash=task.user_id_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Task", entity_id=str(task.id), field_name="title",
                                   old_hash=old_title_hash, new_hash=task.title_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Task", entity_id=str(task.id), field_name="status",
                                   old_hash=old_status_hash, new_hash=task.status_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Task", entity_id=str(task.id), field_name="priority",
                                   old_hash=old_priority_hash, new_hash=task.priority_hash,
                                   employee_name=None, details="backfill")

        for leave in db.query(LeaveRequest).all():
            changed = False
            old_employee_id_hash = leave.employee_id_hash
            old_reason_hash = leave.reason_hash
            old_status_hash = leave.status_hash
            changed |= _set_hash(leave, "employee_id_hash", "employee_id")
            changed |= _set_hash(leave, "reason_hash", "reason")
            changed |= _set_hash(leave, "status_hash", "status")
            if changed:
                updated["leave_requests"] += 1
                _maybe_log_history(entity_type="LeaveRequest", entity_id=str(leave.id), field_name="employee_id",
                                   old_hash=old_employee_id_hash, new_hash=leave.employee_id_hash,
                                   employee_name=leave.user.name if leave.user else None, details="backfill")
                _maybe_log_history(entity_type="LeaveRequest", entity_id=str(leave.id), field_name="reason",
                                   old_hash=old_reason_hash, new_hash=leave.reason_hash,
                                   employee_name=leave.user.name if leave.user else None, details="backfill")
                _maybe_log_history(entity_type="LeaveRequest", entity_id=str(leave.id), field_name="status",
                                   old_hash=old_status_hash, new_hash=leave.status_hash,
                                   employee_name=leave.user.name if leave.user else None, details="backfill")

        for team in db.query(Team).all():
            changed = False
            old_name_hash = team.name_hash
            old_department_hash = team.department_hash
            changed |= _set_hash(team, "name_hash", "name")
            changed |= _set_hash(team, "department_hash", "department")
            if changed:
                updated["teams"] += 1
                _maybe_log_history(entity_type="Team", entity_id=str(team.id), field_name="name",
                                   old_hash=old_name_hash, new_hash=team.name_hash,
                                   employee_name=None, details="backfill")
                _maybe_log_history(entity_type="Team", entity_id=str(team.id), field_name="department",
                                   old_hash=old_department_hash, new_hash=team.department_hash,
                                   employee_name=None, details="backfill")

        db.commit()
        return updated
    finally:
        db.close()


def main() -> None:
    updated = backfill_hashes()
    print("Hash backfill complete:")
    for key, value in updated.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
