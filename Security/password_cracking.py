"""
PASSWORD CRACKING PROTECTION
============================
Simple in-memory rate limiting for login attempts.
"""

# FLOW:
# - Track attempts per key and lock after threshold.
# WHY:
# - Prevents credential stuffing and brute-force attacks.
# HOW:
# - In-memory attempt counter with lockout window.

from __future__ import annotations

import time
from collections import defaultdict


class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300, lock_seconds: int = 600):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._attempts = defaultdict(list)
        self._locked_until = {}

    def _cleanup(self, key: str) -> None:
        now = time.time()
        self._attempts[key] = [t for t in self._attempts[key] if now - t <= self.window_seconds]
        if key in self._locked_until and now >= self._locked_until[key]:
            del self._locked_until[key]

    def is_locked(self, key: str) -> bool:
        self._cleanup(key)
        until = self._locked_until.get(key)
        return until is not None and until > time.time()

    def record_failure(self, key: str) -> None:
        now = time.time()
        self._attempts[key].append(now)
        self._cleanup(key)
        if len(self._attempts[key]) >= self.max_attempts:
            self._locked_until[key] = now + self.lock_seconds

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)
        self._locked_until.pop(key, None)
