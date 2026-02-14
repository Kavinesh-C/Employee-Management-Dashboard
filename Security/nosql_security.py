"""
NoSQL SECURITY
==============
Input validation helpers to prevent NoSQL injection across document/graph stores.

WHY:
- Prevents operator injection (e.g., $ne, $or) and unsafe query shapes.

HOW:
- Whitelists expected keys and strips MongoDB-style operators by default.
"""

from __future__ import annotations

from typing import Any


def strip_mongo_operators(payload: dict) -> dict:
    """Remove keys that start with '$' to prevent operator injection."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict):
            clean[key] = strip_mongo_operators(value)
        else:
            clean[key] = value
    return clean


def allowlist_keys(payload: dict, allowed: set[str]) -> dict:
    """Keep only allowlisted keys to enforce query shape."""
    return {k: payload[k] for k in payload.keys() if k in allowed}
