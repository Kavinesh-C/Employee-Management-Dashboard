"""
DEFAULT ENCRYPTED STRING/TEXT TYPES
===================================
Use these aliases so all new String/Text columns are encrypted by default.
"""

# FLOW:
# - Import EncryptedString/EncryptedText as defaults.
# - Use PlainString/PlainText to allowlist plaintext fields.
# WHY:
# - Makes encryption the default for new fields.
# HOW:
# - Re-exports encrypted types as String/Text aliases.

from __future__ import annotations

from sqlalchemy import String as PlainString, Text as PlainText
from Security.encrypted_type import EncryptedString, EncryptedText

__all__ = [
    "EncryptedString",
    "EncryptedText",
    "PlainString",
    "PlainText",
]
