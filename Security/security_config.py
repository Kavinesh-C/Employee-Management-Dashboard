"""
SECURITY CONFIG
===============
Centralized security settings loaded from environment.
"""

# FLOW:
# - Read env vars once and expose SECURITY_SETTINGS.
# WHY:
# - Centralizes security tuning per environment.
# HOW:
# - Reads env vars and stores them in a dict.

from __future__ import annotations

import os
import secrets
import logging
import dotenv


def get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() == "true"


def get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def feature_enabled(feature_id: str, default: bool = True) -> bool:
    key = f"FEATURE_{feature_id.upper().replace('-', '_')}_ENABLED"
    return get_bool(key, default)


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

    # Auto-select based on ENV_ACTIVE flag if APP_ENV is not set
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


dotenv.load_dotenv(_env_path())

# Optional startup log
if os.getenv("APP_ENV_LOG", "false").lower() == "true":
    logger = logging.getLogger("security.env")
    logger.info("Active env file: %s", _env_path())

SECURITY_SETTINGS = {
    "FORCE_HTTPS": get_bool("FORCE_HTTPS", True),
    "HSTS_ENABLED": get_bool("HSTS_ENABLED", True),
    "SESSION_MAX_AGE": get_int("SESSION_MAX_AGE", 600),
    "SESSION_IDLE_TIMEOUT": get_int("SESSION_IDLE_TIMEOUT", 600),
    "LOGIN_MAX_ATTEMPTS": get_int("LOGIN_MAX_ATTEMPTS", 5),
    "LOGIN_WINDOW": get_int("LOGIN_WINDOW", 300),
    "LOGIN_LOCK": get_int("LOGIN_LOCK", 600),
    "MAX_BODY_BYTES": get_int("MAX_BODY_BYTES", 1024 * 1024),
    "CSRF_ENABLED": get_bool("CSRF_ENABLED", True),
    "CORS_ORIGINS": get_list("CORS_ORIGINS", ["http://localhost", "http://127.0.0.1"]),
}


def ensure_session_secret(env_name: str = "SESSION_SECRET_KEY") -> str:
    """Ensure a strong session secret exists in .env and environment."""
    dotenv.load_dotenv(_env_path())
    primary = os.getenv("SECRET_KEY") or os.getenv(env_name)
    placeholders = {"", "change-this-secret", "REPLACE_WITH_SECURE_RANDOM_SECRET", "AUTO_GENERATE"}
    if primary and primary not in placeholders:
        os.environ[env_name] = primary
        os.environ["SECRET_KEY"] = primary
        return primary

    secret = secrets.token_urlsafe(64)
    os.environ[env_name] = secret
    os.environ["SECRET_KEY"] = secret

    env_path = _env_path()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.splitlines()
        def _upsert(lines_list, key, value):
            if any(line.startswith(f"{key}=") for line in lines_list):
                return [f"{key}=\"{value}\"" if line.startswith(f"{key}=") else line for line in lines_list]
            return lines_list + [f"{key}=\"{value}\""]

        lines = _upsert(lines, "SECRET_KEY", secret)
        lines = _upsert(lines, env_name, secret)
        content = "\n".join(lines) + "\n"
    else:
        content = f"SECRET_KEY=\"{secret}\"\n{env_name}=\"{secret}\"\n"

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)

    return secret
