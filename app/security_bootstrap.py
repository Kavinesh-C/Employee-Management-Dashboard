"""
Security bootstrap utilities.

Initializes encryption-at-rest key material and exposes helper functions
for optional field-level encryption without changing schema or UI.
"""

from __future__ import annotations

from Security.key_management import ensure_data_encryption_key, get_aes256_key
from Security.data_encryption_at_rest import encrypt_bytes, decrypt_bytes
from Security.field_level_encryption import encrypt_field, decrypt_field
from Security.data_integrity import sha256_hex
from Security.encrypted_type import EncryptedString, EncryptedText


def initialize_encryption() -> None:
    """Ensure data encryption key exists for at-rest utilities."""
    ensure_data_encryption_key()


def get_encryption_key() -> bytes:
    """Return active AES-256 key bytes."""
    return get_aes256_key()


def encrypt_value(value: str | None) -> str | None:
    """Encrypt a single string value using the active key."""
    return encrypt_field(value, get_aes256_key())


def decrypt_value(token: str | None) -> str | None:
    """Decrypt a single string token using the active key."""
    return decrypt_field(token, get_aes256_key())


def encrypt_blob(data: bytes) -> str:
    """Encrypt raw bytes using the active key."""
    return encrypt_bytes(data, get_aes256_key())


def decrypt_blob(token: str) -> bytes:
    """Decrypt raw bytes using the active key."""
    return decrypt_bytes(token, get_aes256_key())


def hash_value(value: str | bytes) -> str:
    """Compute SHA-256 hash for integrity checks or log redaction."""
    return sha256_hex(value)


__all__ = [
    "initialize_encryption",
    "get_encryption_key",
    "encrypt_value",
    "decrypt_value",
    "encrypt_blob",
    "decrypt_blob",
    "hash_value",
    "EncryptedString",
    "EncryptedText",
]
