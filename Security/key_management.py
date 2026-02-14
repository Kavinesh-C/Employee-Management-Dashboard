"""
SECURE KEY MANAGEMENT
=====================
Load encryption keys from environment and validate length.
"""

# FLOW:
# - ensure_data_encryption_key() creates a strong key if missing.
# - get_aes256_key() loads and validates base64 key.
# WHY:
# - Prevents hard-coded keys and weak secrets.
# HOW:
# - Generates and persists a 32-byte key when absent.

from __future__ import annotations

import base64
import os
import secrets

import dotenv


PLACEHOLDER = "CHANGE_ME_BASE64_32_BYTES"


def _env_name() -> str:
    env = os.getenv("APP_ENV", "").strip().lower()
    if env in {"prod", "production"}:
        root = os.path.dirname(os.path.dirname(__file__))
        prod_typo = os.path.join(root, ".env.productuion")
        if os.path.exists(prod_typo):
            return ".env.productuion"
        return ".env.production"
    if env in {"local", "localhost", "dev", "development"}:
        return ".env.localhost"

    root = os.path.dirname(os.path.dirname(__file__))
    prod_typo_path = os.path.join(root, ".env.productuion")
    prod_path = os.path.join(root, ".env.production")
    local_path = os.path.join(root, ".env.localhost")

    def _is_active(path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("ENV_ACTIVE="):
                    return line.split("=", 1)[1].strip().strip('"').lower() == "true"
        return False

    if _is_active(prod_typo_path):
        return ".env.productuion"
    if _is_active(prod_path):
        return ".env.production"
    return ".env.localhost"


def _env_path() -> str:
    root = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(root, _env_name())


def ensure_data_encryption_key(env_name: str = "DATA_ENCRYPTION_KEY") -> str:
    """Ensure a strong base64 AES-256 key exists in .env and environment."""
    dotenv.load_dotenv(_env_path())
    raw = os.getenv("ENCRYPTION_KEY") or os.getenv(env_name)
    placeholders = {"", PLACEHOLDER, "REPLACE_WITH_BASE64_32_BYTE_KEY", "AUTO_GENERATE"}
    if raw and raw not in placeholders:
        os.environ[env_name] = raw
        os.environ["ENCRYPTION_KEY"] = raw
        return raw

    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode("utf-8")
    os.environ[env_name] = encoded
    os.environ["ENCRYPTION_KEY"] = encoded

    env_path = _env_path()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.splitlines()
        def _upsert(lines_list, key, value):
            if any(line.startswith(f"{key}=") for line in lines_list):
                return [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines_list]
            return lines_list + [f"{key}={value}"]

        lines = _upsert(lines, "ENCRYPTION_KEY", encoded)
        lines = _upsert(lines, env_name, encoded)
        content = "\n".join(lines) + "\n"
    else:
        content = f"ENCRYPTION_KEY={encoded}\n{env_name}={encoded}\n"

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)

    return encoded


def get_aes256_key(env_name: str = "DATA_ENCRYPTION_KEY") -> bytes:
    """Return 32-byte key from base64 env var, auto-generating if missing."""
    raw = ensure_data_encryption_key(env_name)
    key = base64.urlsafe_b64decode(raw.encode("utf-8"))
    if len(key) != 32:
        raise ValueError("DATA_ENCRYPTION_KEY must be 32 bytes after base64 decode")
    return key
