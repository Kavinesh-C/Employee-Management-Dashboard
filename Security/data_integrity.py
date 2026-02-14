"""
DATA INTEGRITY
==============
SHA-256 hashing helpers for integrity checks.

FLOW:
- sha256_hex() returns a stable hash for comparisons/uniqueness.

WHY:
- Detects tampering and supports safe uniqueness checks.

HOW:
- Computes SHA-256 hash of inputs.
"""

from __future__ import annotations

import hashlib


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
