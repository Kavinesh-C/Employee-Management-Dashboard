from __future__ import annotations

from sqlalchemy import inspect, text
from app.database import engine


def _column_type() -> str:
    if engine.dialect.name == "sqlite":
        return "TEXT"
    return "VARCHAR(255)"


def main() -> None:
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("users")}
    if "photo_path" in existing:
        print("users.photo_path already exists")
        return

    col_type = _column_type()
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE users ADD COLUMN photo_path {col_type}"))
    print("Added users.photo_path")


if __name__ == "__main__":
    main()
