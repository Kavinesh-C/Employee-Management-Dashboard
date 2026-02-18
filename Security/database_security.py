"""
DATABASE SECURITY
=================
Helpers for safe parameterized SQL and basic input sanitation
before storing data in the database. Works across SQLAlchemy-supported SQL DBs.

FLOW:
- safe_text() wraps SQL for parameter binding.
- sanitize_db_text() strips risky content before persistence.

WHY:
- Reduces SQL injection risk and stored XSS payloads.
- Use nosql_security.py for NoSQL payload validation.

HOW:
- Uses SQLAlchemy text() with bound parameters.
"""

from __future__ import annotations

import re
from sqlalchemy import text
from sqlalchemy.orm import Session


def safe_text(query: str):
    """Return SQLAlchemy text() for parameterized queries (DB-agnostic)."""
    return text(query)


def safe_execute(db: Session, query: str, params: dict):
    """Execute a parameterized query safely across DB backends."""
    return db.execute(text(query), params)


def sanitize_db_text(value: str | None, max_len: int = 200) -> str | None:
    """
    Basic sanitation for text stored in DB to reduce XSS payloads.
    This removes HTML tags and control characters.
    """
    if value is None:
        return None
    value = value.strip()[:max_len]
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value or None
