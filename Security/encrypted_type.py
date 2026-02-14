"""
ENCRYPTED SQLALCHEMY TYPES
==========================
Field-level encryption for SQLAlchemy String/Text columns using AES-256-GCM.
"""

# FLOW:
# - Encrypt on bind (write) and decrypt on result (read).
# - Uses DATA_ENCRYPTION_KEY from environment.
# WHY:
# - Ensures sensitive fields are encrypted at rest transparently.
# HOW:
# - SQLAlchemy TypeDecorator wraps String/Text columns.

from __future__ import annotations

from sqlalchemy.types import TypeDecorator, String, Text
from Security.key_management import get_aes256_key
from Security.data_encryption_at_rest import encrypt_bytes, decrypt_bytes


class EncryptedString(TypeDecorator):
    impl = String
    cache_ok = True

    def __init__(self, length=None, **kwargs):
        super().__init__(**kwargs)
        self.length = length

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(String(self.length))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        key = get_aes256_key()
        token = encrypt_bytes(value.encode("utf-8"), key)
        return token

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        key = get_aes256_key()
        return decrypt_bytes(value, key).decode("utf-8")


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        key = get_aes256_key()
        token = encrypt_bytes(value.encode("utf-8"), key)
        return token

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        key = get_aes256_key()
        return decrypt_bytes(value, key).decode("utf-8")
