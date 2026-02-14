"""
ROLE-BASED ACCESS CONTROL (RBAC)
================================
Enforces dashboard access rules by role.
"""

# WHY:
# - Limits access to actions based on user role.
# HOW:
# - enforce_rbac() checks request path prefixes against role rules.

# FLOW:
# - enforce_rbac() checks role against route prefix.

from __future__ import annotations

from fastapi import HTTPException, status

ROLE_PATH_RULES = [
    ("/admin", {"admin"}),
    ("/employee", {"employee", "admin"}),
]


def enforce_rbac(user, path: str) -> None:
    """Raise 403 if user role does not satisfy path-based access rules."""
    for prefix, roles in ROLE_PATH_RULES:
        if path.startswith(prefix):
            if user.role not in roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied",
                )
            return
