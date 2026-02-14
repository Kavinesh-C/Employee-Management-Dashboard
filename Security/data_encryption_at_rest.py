"""
DATA ENCRYPTION AT REST
=======================
AES-256-GCM helpers for encrypting/decrypting blobs before storage.

FLOW:
- encrypt_bytes() encrypts raw bytes -> token.
- decrypt_bytes() decrypts token -> bytes (plaintext fallback for migration).

WHY:
- Protects data if the database is compromised.

HOW:
- Uses AES-256-GCM with random nonce per value.
"""

from __future__ import annotations

import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


NONCE_SIZE = 12
TOKEN_PREFIX = "enc::"


def encrypt_bytes(plaintext: bytes, key: bytes) -> str:
    """Encrypt bytes with AES-256-GCM. Returns base64 token."""
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    nonce = os.urandom(NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    token = base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")
    return f"{TOKEN_PREFIX}{token}"


def decrypt_bytes(token: str, key: bytes) -> bytes:
    """Decrypt base64 token with AES-256-GCM. Falls back to plaintext."""
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    if not token.startswith(TOKEN_PREFIX):
        return token.encode("utf-8")
    raw = base64.urlsafe_b64decode(token[len(TOKEN_PREFIX):].encode("utf-8"))
    nonce, ciphertext = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)
