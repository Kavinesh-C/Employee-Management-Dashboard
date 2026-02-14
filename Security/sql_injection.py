"""
SQL INJECTION PREVENTION
========================
Helpers to sanitize user input for LIKE queries and limit input length.
Note: SQLAlchemy ORM already parameterizes queries.

FLOW:
- sanitize_like_input() removes wildcards used in LIKE queries.

WHY:
- Limits injection patterns in user-supplied filters.

HOW:
- Removes wildcard characters and limits length.
"""

from __future__ import annotations

import re


_MAX_INPUT_LEN = 100


def sanitize_like_input(value: str | None) -> str | None:
    """Sanitize user input used with LIKE/ILIKE by removing wildcards."""
    if value is None:
        return None
    value = value.strip()[:_MAX_INPUT_LEN]
    value = re.sub(r"[%_]", "", value)
    return value or None
