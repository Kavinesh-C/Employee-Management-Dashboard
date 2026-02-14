from __future__ import annotations

SECURITY_FILE_NAMES = [
    "activity_logging.py",
    "audit_trail.py",
    "authentication.py",
    "backfill_hashes.py",
    "cors_security.py",
    "csrf_protection.py",
    "database_security.py",
    "data_encryption_at_rest.py",
    "data_integrity.py",
    "encrypted_defaults.py",
    "encrypted_type.py",
    "error_handling.py",
    "field_level_encryption.py",
    "hash_history.py",
    "headers_hardening.py",
    "https_tls.py",
    "input_length_limits.py",
    "input_validation.py",
    "key_management.py",
    "login_attempt_limiting.py",
    "metrics.py",
    "nosql_security.py",
    "password_cracking.py",
    "rbac.py",
    "request_id.py",
    "secrets_redaction.py",
    "secure_connection.py",
    "security_config.py",
    "session_hijacking.py",
    "session_security.py",
    "sql_injection.py",
    "waf_integration.py",
    "xss_protection.py",
]


def _feature_id_from_filename(filename: str) -> str:
    return filename.replace(".py", "").replace("_", "-")


def _title_from_feature_id(feature_id: str) -> str:
    return " ".join(part.capitalize() for part in feature_id.split("-"))


def _bool(name: str, label: str, description: str) -> dict:
    return {"name": name, "label": label, "description": description, "type": "bool"}


def _text(name: str, label: str, description: str) -> dict:
    return {"name": name, "label": label, "description": description, "type": "text"}


def _url(name: str, label: str, description: str) -> dict:
    return {"name": name, "label": label, "description": description, "type": "url"}


def _int(name: str, label: str, description: str) -> dict:
    return {"name": name, "label": label, "description": description, "type": "int"}


def _list(name: str, label: str, description: str) -> dict:
    return {"name": name, "label": label, "description": description, "type": "list"}


def _upload_enabled(feature_id: str) -> bool:
    upload_features = {
        "https-tls",
        "secure-connection",
        "waf-integration",
        "key-management",
        "data-encryption-at-rest",
        "field-level-encryption",
        "encrypted-defaults",
        "encrypted-type",
        "security-bootstrap",
    }
    return feature_id in upload_features


def _inputs_for_feature(feature_id: str) -> list[dict]:
    p = feature_id.upper().replace("-", "_")

    if feature_id in {"https-tls", "secure-connection", "waf-integration"}:
        return [
            _text(f"{p}_CERT_ALIAS", "Certificate Alias", "Alias to identify uploaded cert/key."),
            _url(f"{p}_GATEWAY_URL", "Gateway URL", "Primary edge/gateway URL."),
            _list(f"{p}_TRUSTED_HOSTS", "Trusted Hosts", "Comma-separated host allowlist."),
        ]
    if feature_id in {"key-management", "data-encryption-at-rest", "field-level-encryption", "encrypted-defaults", "encrypted-type", "security-bootstrap"}:
        return [
            _text(f"{p}_KEY_ALIAS", "Key Alias", "Active key alias used for encryption."),
            _int(f"{p}_ROTATE_DAYS", "Rotate (Days)", "Key rotation interval in days."),
            _bool(f"{p}_ENCRYPTION_REQUIRED", "Require Encryption", "Reject plaintext writes."),
        ]
    if feature_id in {"csrf-protection", "authentication", "session-security", "session-hijacking", "login-attempt-limiting", "password-cracking", "rbac"}:
        return [
            _bool(f"{p}_STRICT", "Strict Policy", "Enable strict access/session policy."),
            _int(f"{p}_TIMEOUT_SECONDS", "Timeout (Sec)", "Session/token timeout in seconds."),
        ]
    if feature_id in {"cors-security", "headers-hardening", "xss-protection", "input-validation", "input-length-limits", "sql-injection", "nosql-security", "database-security", "secrets-redaction"}:
        return [
            _text(f"{p}_RULESET", "Rule Set", "Validation/header/policy profile text."),
            _list(f"{p}_ALLOWLIST", "Allowlist", "Comma-separated allowed patterns/origins."),
        ]
    if feature_id == "audit-trail":
        return [
            _text(f"{p}_LOG_LEVEL", "Log Level", "DEBUG/INFO/WARN/ERROR."),
            _int(f"{p}_RETENTION_DAYS", "Retention (Days)", "Log/history retention duration."),
            _url(f"{p}_WEBHOOK_URL", "Webhook URL", "Optional webhook endpoint for alerts."),
            _bool("AUDIT_TRAIL_HIDE_AUTH_EVENTS", "Hide Auth Events", "Hide login/logout entries from Security Events."),
        ]
    if feature_id in {"activity-logging", "request-id", "metrics", "hash-history", "data-integrity", "error-handling"}:
        return [
            _text(f"{p}_LOG_LEVEL", "Log Level", "DEBUG/INFO/WARN/ERROR."),
            _int(f"{p}_RETENTION_DAYS", "Retention (Days)", "Log/history retention duration."),
            _url(f"{p}_WEBHOOK_URL", "Webhook URL", "Optional webhook endpoint for alerts."),
        ]
    if feature_id in {"backfill-hashes"}:
        return [
            _bool(f"{p}_DRY_RUN", "Dry Run", "Preview only, no database writes."),
            _int(f"{p}_BATCH_SIZE", "Batch Size", "Rows processed per batch."),
            _text(f"{p}_TARGET_TABLE", "Target Table", "Table/model target for migration."),
        ]
    if feature_id == "security-config":
        return [
            _text("APP_ENV", "App Environment", "local/staging/production."),
            _bool("APP_ENV_LOG", "Environment Logging", "Log active env file at startup."),
            _int("SESSION_MAX_AGE", "Session Max Age (Sec)", "Absolute session lifetime in seconds."),
            _int("SESSION_IDLE_TIMEOUT", "Session Idle Timeout (Sec)", "Idle timeout in seconds."),
            _text("SECRET_KEY", "Secret Key", "Primary application secret key (DB-backed)."),
            _text("SESSION_SECRET_KEY", "Session Secret Key", "Session secret override."),
            _text("ENCRYPTION_KEY", "Encryption Key", "Primary encryption key (DB-backed)."),
            _text("DATA_ENCRYPTION_KEY", "Data Encryption Key", "Data-at-rest encryption key (DB-backed)."),
            _url("SECURITY_STATUS_URL", "Status URL", "Security status endpoint URL."),
        ]
    return [
        _text(f"{p}_VALUE", "Config Value", "Feature-specific configuration value."),
    ]


def _description_for_feature(feature_id: str) -> str:
    if feature_id.startswith("add-") or feature_id.startswith("migrate-") or feature_id.startswith("backfill-"):
        return "Migration/maintenance workflow for security data and schema."
    if "encryption" in feature_id or "encrypted" in feature_id or "key" in feature_id:
        return "Encryption and key-management controls for sensitive data."
    if "session" in feature_id or "authentication" in feature_id or "login" in feature_id or "password" in feature_id:
        return "Authentication/session hardening controls."
    if "sql" in feature_id or "nosql" in feature_id or "input" in feature_id or "xss" in feature_id:
        return "Input/query injection defense controls."
    if "https" in feature_id or "secure" in feature_id or "waf" in feature_id or "headers" in feature_id:
        return "HTTP transport and browser security hardening."
    if "log" in feature_id or "audit" in feature_id or "metrics" in feature_id or "request-id" in feature_id:
        return "Observability and security telemetry controls."
    return "Security control configured through the admin dashboard."


def build_feature_catalog() -> list[dict]:
    items: list[dict] = []
    for filename in SECURITY_FILE_NAMES:
        feature_id = _feature_id_from_filename(filename)
        items.append(
            {
                "id": feature_id,
                "name": _title_from_feature_id(feature_id),
                "description": _description_for_feature(feature_id),
                "toggle": True,
                "files": [f"Security/{filename}"],
                "inputs": _inputs_for_feature(feature_id),
                "allow_upload": _upload_enabled(feature_id),
            }
        )

    return items
