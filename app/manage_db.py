"""
Run database schema creation and data migrations manually.
Usage: python -m app.manage_db
"""
from .database import engine, Base
from .main import (
    auto_sync_schema,
    backfill_project_assignment_hashes,
    backfill_project_task_completed_at,
    migrate_attendance_dates_csv
)

def main():
    print("Creating tables (if missing)...")
    Base.metadata.create_all(bind=engine)
    print("Syncing schema...")
    auto_sync_schema()
    print("Backfilling project assignment hashes...")
    backfill_project_assignment_hashes()
    print("Backfilling project task completed_at...")
    backfill_project_task_completed_at()
    print("Migrating attendance dates from CSV...")
    migrate_attendance_dates_csv()
    print("All DB management tasks complete.")

if __name__ == "__main__":
    main()