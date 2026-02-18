"""
BRUTE-FORCE ATTACK PREVENTION
==============================
Wrapper for login attempt limiting.
"""

# FLOW:
# - create_login_limiter() returns configured limiter instance.
# WHY:
# - Slows brute-force attempts.
# HOW:
# - Tracks failures and locks for a time window.

from __future__ import annotations

from Security.password_cracking import LoginRateLimiter


def create_login_limiter(
    max_attempts: int = 5,
    window_seconds: int = 300,
    lock_seconds: int = 600,
) -> LoginRateLimiter:
    return LoginRateLimiter(
        max_attempts=max_attempts,
        window_seconds=window_seconds,
        lock_seconds=lock_seconds,
    )
