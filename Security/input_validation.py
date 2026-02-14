"""
INPUT VALIDATION & SANITIZATION
===============================
Basic validation helpers to reduce injection payloads.
"""

# FLOW:
# - sanitize_text() strips tags/control characters.
# - validate_allowlist() enforces regex allowlists.
# WHY:
# - Blocks common injection and formatting abuse.
# HOW:
# - Normalizes input and applies strict allowlists.

from __future__ import annotations

import re


def sanitize_text(value: str | None, max_len: int = 200) -> str | None:
    if value is None:
        return None
    value = value.strip()[:max_len]
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value or None


def validate_allowlist(value: str | None, pattern: str) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(pattern, value):
        return None
    return value
