from __future__ import annotations

from sqlalchemy import inspect, text
from app.database import engine


HASH_COLS: dict[str, list[str]] = {
    "users": [
        "employee_id_hash",
        "name_hash",
        "email_hash",
        "rfid_tag_hash",
        "role_hash",
        "department_hash",
    ],
    "attendance": [
        "employee_id_hash",
        "status_hash",
        "location_name_hash",
        "room_no_hash",
    ],
    "removed_employees": [
        "employee_id_hash",
        "name_hash",
        "email_hash",
        "rfid_tag_hash",
        "role_hash",
        "department_hash",
    ],
    "unknown_rfids": [
        "rfid_tag_hash",
        "location_hash",
    ],
    "rooms": [
        "room_id_hash",
        "room_no_hash",
        "location_name_hash",
    ],
    "departments": [
        "name_hash",
    ],
    "tasks": [
        "user_id_hash",
        "title_hash",
        "status_hash",
        "priority_hash",
    ],
    "leave_requests": [
        "employee_id_hash",
        "reason_hash",
        "status_hash",
    ],
    "teams": [
        "name_hash",
        "department_hash",
    ],
}


def _column_type() -> str:
    if engine.dialect.name == "sqlite":
        return "TEXT"
    return "VARCHAR(64)"


def main() -> None:
    inspector = inspect(engine)
    col_type = _column_type()

    with engine.begin() as conn:
        for table, cols in HASH_COLS.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col in cols:
                if col in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                print(f"Added {table}.{col}")


if __name__ == "__main__":
    main()
