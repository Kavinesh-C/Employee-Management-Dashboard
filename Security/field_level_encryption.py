"""
SENSITIVE DATA PROTECTION
=========================
Field-level AES-256-GCM encryption for strings.
"""

# FLOW:
# - encrypt_field()/decrypt_field() call AES helpers for single values.
# WHY:
# - Protects individual columns without encrypting whole rows.
# HOW:
# - Wraps AES helper functions for string values.

from __future__ import annotations

from Security.data_encryption_at_rest import encrypt_bytes, decrypt_bytes


def encrypt_field(value: str | None, key: bytes) -> str | None:
    if value is None:
        return None
    return encrypt_bytes(value.encode("utf-8"), key)


def decrypt_field(token: str | None, key: bytes) -> str | None:
    if token is None:
        return None
    return decrypt_bytes(token, key).decode("utf-8")
