from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from Security.audit_trail import audit
from Security.hash_history import read_hash_history
from Security.metrics import get_feature_metrics_snapshot, set_feature_enabled
from Security.security_config import SECURITY_SETTINGS, _env_path, ensure_session_secret
from app.app_context import get_current_user, templates
from app.database import get_db
from app.models import SecurityCertificate, SecurityEventRecord, SecurityManagedSetting, SecurityMitreRule, User
from app.security_feature_catalog import build_feature_catalog
from app.security_bootstrap import decrypt_value

router = APIRouter()
DB_ONLY_RUNTIME_KEYS = {"SECRET_KEY", "SESSION_SECRET_KEY", "ENCRYPTION_KEY", "DATA_ENCRYPTION_KEY"}
SECURITY_WATCH_FILES = (
    "logs/security.log",
    "logs/audit.log",
    "logs/hash_history.log",
)
DASHBOARD_FEATURE_IDS = (
    "authentication",
    "session-security",
    "session-hijacking",
    "login-attempt-limiting",
    "sql-injection",
    "headers-hardening",
    "https-tls",
    "waf-integration",
    "data-encryption-at-rest",
    "key-management",
    "secrets-redaction",
    "audit-trail",
)


def _ensure_security_runtime_schema(db: Session) -> None:
    """
    Runtime self-heal for security tables.
    If a security table was dropped while the app is running, recreate it on demand.
    """
    bind = db.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    required_tables = (
        SecurityManagedSetting.__table__,
        SecurityCertificate.__table__,
        SecurityEventRecord.__table__,
        SecurityMitreRule.__table__,
    )
    for table in required_tables:
        if table.name not in existing_tables:
            table.create(bind=bind, checkfirst=True)


def _admin_guard(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")


def _sanitize_required(value: str, field: str, max_len: int = 255, pattern: str | None = None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    if len(cleaned) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} too long")
    if pattern and re.fullmatch(pattern, cleaned) is None:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return cleaned


def _set_env_flag(key: str, value: str) -> None:
    os.environ[key] = value
    if key in DB_ONLY_RUNTIME_KEYS:
        # Sensitive secrets are DB-backed and should not be persisted in .env files.
        return
    env_file = _env_path()
    lines: list[str] = []
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            break
    else:
        lines.append(f"{key}={value}\n")
    with open(env_file, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _clear_log(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8"):
        pass


def _feature_env_var(feature_id: str) -> str:
    return feature_id.upper().replace("-", "_") + "_ENABLED"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _settings_map(db: Session) -> dict[tuple[str, str], str]:
    _ensure_security_runtime_schema(db)
    rows = db.query(SecurityManagedSetting).all()
    return {(row.feature_id, row.key): row.value for row in rows}


def _upsert_setting(db: Session, feature_id: str, key: str, value: str) -> None:
    existing = (
        db.query(SecurityManagedSetting)
        .filter(SecurityManagedSetting.feature_id == feature_id, SecurityManagedSetting.key == key)
        .first()
    )
    if existing:
        existing.value = value
    else:
        db.add(SecurityManagedSetting(feature_id=feature_id, key=key, value=value))


def _ensure_session_timeout_settings(db: Session) -> None:
    defaults = {
        "SESSION_IDLE_TIMEOUT": str(os.getenv("SESSION_IDLE_TIMEOUT", SECURITY_SETTINGS.get("SESSION_IDLE_TIMEOUT", 600))),
        "SESSION_MAX_AGE": str(os.getenv("SESSION_MAX_AGE", SECURITY_SETTINGS.get("SESSION_MAX_AGE", 600))),
    }
    changed = False
    for key, default_value in defaults.items():
        row = (
            db.query(SecurityManagedSetting)
            .filter(SecurityManagedSetting.feature_id == "session-security", SecurityManagedSetting.key == key)
            .first()
        )
        if row:
            continue
        db.add(SecurityManagedSetting(feature_id="session-security", key=key, value=default_value))
        changed = True
    if changed:
        db.commit()


def _normalize_input_value(raw_value: str, input_type: str) -> str:
    value = raw_value.strip()
    if input_type == "bool":
        return "true" if value.lower() in {"1", "true", "yes", "on"} else "false"
    return value


def _feature_config_types(feature: dict) -> list[str]:
    order = ["upload", "url", "path", "text", "int", "bool", "list"]
    collected: set[str] = set()
    for item in feature.get("inputs", []):
        input_type = str(item.get("type", "")).strip().lower()
        if input_type:
            collected.add(input_type)
    if feature.get("allow_upload"):
        collected.add("upload")
    return [kind for kind in order if kind in collected]


def _security_features(db: Session) -> list[dict]:
    features = build_feature_catalog()
    settings = _settings_map(db)
    snapshot = get_feature_metrics_snapshot([f["id"] for f in features])

    for f in features:
        env_var = "PROMETHEUS_ENABLED" if f["id"] == "metrics" else _feature_env_var(f["id"])
        enabled_db = settings.get((f["id"], "__enabled__"))
        enabled = _env_bool(env_var, True) if enabled_db is None else (enabled_db.lower() == "true")
        f["env_var"] = env_var
        f["enabled"] = enabled
        f["env_value"] = settings.get((f["id"], env_var), os.getenv(env_var, ""))
        f["why"] = "Improves security posture."
        f["how"] = "Connected to the security module and dashboard controls."
        f["notes"] = "Configuration edits are saved in DB and mirrored to environment."
        f["manage"] = [f"Connected file: {path}" for path in f.get("files", [])]
        if f.get("allow_upload"):
            f["manage"].append("Upload files via dashboard for this feature.")
        f["manage"].append(f"Toggle with {env_var}=true|false")
        f["config_types"] = _feature_config_types(f)
        for item in f.get("inputs", []):
            key = item["name"]
            item["value"] = settings.get((f["id"], key), os.getenv(key, ""))
        f["metrics"] = {
            "coverage": 100 if enabled else 0,
            "score": 100 if enabled else 50,
            "events": snapshot.get(f["id"], {}).get("events", 0),
        }
        set_feature_enabled(f["id"], bool(enabled))
    return features


def _dashboard_features(features: list[dict]) -> list[dict]:
    feature_map = {f["id"]: f for f in features}
    return [feature_map[feature_id] for feature_id in DASHBOARD_FEATURE_IDS if feature_id in feature_map]


def _title_case_event(event_name: str) -> str:
    return event_name.replace("_", " ").strip().title() if event_name else "Audit Event"


def _parse_kv_payload(line: str) -> dict[str, str]:
    payload = line.strip()
    prefix = re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3}\s+\w+\s+(.*)$", payload)
    if prefix:
        payload = prefix.group(1)
    out: dict[str, str] = {}
    matches = list(re.finditer(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)=", payload))
    for idx, match in enumerate(matches):
        key = match.group(1)
        value_start = match.end()
        value_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(payload)
        out[key] = payload[value_start:value_end].strip()
    return out


def _extract_log_timestamp(line: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}", line.strip())
    if not m:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{m.group(1)} UTC"


def _severity_from_line(line: str, status: str = "") -> str:
    low = line.lower()
    if status in {"401", "403", "429", "500", "502", "503"}:
        return "High"
    if "critical" in low:
        return "Critical"
    if "error" in low or "denied" in low:
        return "High"
    if "warn" in low or "invalid" in low:
        return "Medium"
    return "Low"


def _parse_detail_pairs(detail: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (detail or "").split(";"):
        token = part.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        key = k.strip().lower()
        val = v.strip()
        if key and val:
            out[key] = val
    return out


def _source_section_from_path(path_value: str) -> str:
    path = (path_value or "").strip()
    if not path:
        return "system"
    if path == "/login" or path.startswith("/login/"):
        return "auth/login"
    if path == "/logout" or path.startswith("/logout/"):
        return "auth/logout"
    if path.startswith("/employee"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"employee/{parts[1]}"
        return "employee/dashboard"
    if path.startswith("/admin"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"admin/{parts[1]}"
        return "admin/dashboard"
    return "public"


def _is_excluded_auth_event(event: dict) -> bool:
    source = str(event.get("source_section") or "").strip().lower()
    audit_name = str(event.get("audit_event") or "").strip().lower()
    event_name = str(event.get("event") or "").strip().lower()
    path_value = str(event.get("path") or "").strip().lower()
    status_value = str(event.get("status") or "").strip().lower()
    if audit_name in {"auth_login_success", "auth_login_failed", "auth_login_inactive", "auth_logout"}:
        return True
    if source in {"auth/login", "auth/logout"}:
        return True
    if event_name in {"employee login success", "employee logout", "inactive account login blocked"}:
        return True
    if status_value == "logged_out":
        return True
    if path_value in {"/login", "/logout"}:
        return True
    if path_value.endswith("/login") or path_value.endswith("/logout"):
        return True
    return False


def _timeline_feature_id(etype: str, audit_event: str, source_section: str) -> str:
    if etype == "request":
        if source_section in {"auth/login", "auth/logout"}:
            return "authentication"
        return "request"
    ev = (audit_event or "").strip().lower()
    if ev in {"auth_login_success", "auth_login_failed", "auth_login_inactive", "auth_logout"}:
        return "authentication"
    if ev.startswith("security_env") or ev.startswith("security_toggle") or ev.startswith("security_configuration"):
        return "security-config"
    if ev.startswith("security_certificate"):
        return "key-management"
    return "audit"


def _mitre_unmapped(reason: str = "No ATT&CK rule matched this event") -> dict[str, str]:
    return {
        "mitre": "not_mapped",
        "mitre_status": "unmapped",
        "mitre_tactic_id": "-",
        "mitre_tactic": "-",
        "mitre_technique_id": "-",
        "mitre_technique": "-",
        "mitre_confidence": "low",
        "mitre_reason": reason,
        "mitre_url": "-",
    }


def _default_mitre_rule_rows() -> list[dict[str, str | int | bool]]:
    return [
        {
            "name": "Failed login brute force",
            "enabled": True,
            "priority": 10,
            "source_type": "any",
            "audit_event_pattern": r"auth_login_failed",
            "path_pattern": r"^/login",
            "method_pattern": "",
            "status_pattern": r"401|403|failed|blocked",
            "details_pattern": "",
            "raw_pattern": "",
            "tactic_id": "TA0006",
            "tactic": "Credential Access",
            "technique_id": "T1110",
            "technique": "Brute Force",
            "confidence": "high",
            "reason": "Repeated failed authentication behavior matched brute-force pattern.",
        },
        {
            "name": "Successful authentication account use",
            "enabled": True,
            "priority": 20,
            "source_type": "audit",
            "audit_event_pattern": r"auth_login_success|auth_login_inactive",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": "",
            "raw_pattern": "",
            "tactic_id": "TA0001",
            "tactic": "Initial Access",
            "technique_id": "T1078",
            "technique": "Valid Accounts",
            "confidence": "medium",
            "reason": "Authentication event indicates account credential usage.",
        },
        {
            "name": "Security controls disabled",
            "enabled": True,
            "priority": 30,
            "source_type": "audit",
            "audit_event_pattern": r"security_toggle|security_env|security_setting|security_configuration",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": r"=false|=off|disabled|disable|turned_off",
            "raw_pattern": "",
            "tactic_id": "TA0005",
            "tactic": "Defense Evasion",
            "technique_id": "T1562.001",
            "technique": "Impair Defenses: Disable or Modify Tools",
            "confidence": "high",
            "reason": "Security control/configuration was set to a disabled state.",
        },
        {
            "name": "Security configuration tamper",
            "enabled": True,
            "priority": 40,
            "source_type": "audit",
            "audit_event_pattern": r"security_toggle|security_env|security_setting|security_configuration",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": "",
            "raw_pattern": "",
            "tactic_id": "TA0005",
            "tactic": "Defense Evasion",
            "technique_id": "T1562",
            "technique": "Impair Defenses",
            "confidence": "medium",
            "reason": "Security control configuration activity can affect defensive visibility.",
        },
        {
            "name": "Certificate trust manipulation",
            "enabled": True,
            "priority": 50,
            "source_type": "audit",
            "audit_event_pattern": r"security_certificate",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": "",
            "raw_pattern": "",
            "tactic_id": "TA0005",
            "tactic": "Defense Evasion",
            "technique_id": "T1553",
            "technique": "Subvert Trust Controls",
            "confidence": "medium",
            "reason": "Certificate/trust material manipulation detected in security controls.",
        },
        {
            "name": "Web exploit indicator",
            "enabled": True,
            "priority": 60,
            "source_type": "request",
            "audit_event_pattern": "",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": r"union select| or 1=1|<script|%3cscript|\.\./|\.\.\\|%2e%2e|/etc/passwd",
            "raw_pattern": r"union select| or 1=1|<script|%3cscript|\.\./|\.\.\\|%2e%2e|/etc/passwd",
            "tactic_id": "TA0001",
            "tactic": "Initial Access",
            "technique_id": "T1190",
            "technique": "Exploit Public-Facing Application",
            "confidence": "high",
            "reason": "Request content matches exploitation indicators against a web endpoint.",
        },
        {
            "name": "Command execution indicator",
            "enabled": True,
            "priority": 70,
            "source_type": "request",
            "audit_event_pattern": "",
            "path_pattern": "",
            "method_pattern": "",
            "status_pattern": "",
            "details_pattern": r"powershell|cmd\.exe|bash -c|curl |wget |xp_cmdshell",
            "raw_pattern": r"powershell|cmd\.exe|bash -c|curl |wget |xp_cmdshell",
            "tactic_id": "TA0002",
            "tactic": "Execution",
            "technique_id": "T1059",
            "technique": "Command and Scripting Interpreter",
            "confidence": "high",
            "reason": "Request/log content includes command interpreter execution indicators.",
        },
        {
            "name": "Denied admin write attempt",
            "enabled": True,
            "priority": 80,
            "source_type": "request",
            "audit_event_pattern": "",
            "path_pattern": r"^/admin",
            "method_pattern": r"POST|PUT|PATCH|DELETE",
            "status_pattern": r"401|403|429",
            "details_pattern": "",
            "raw_pattern": "",
            "tactic_id": "TA0001",
            "tactic": "Initial Access",
            "technique_id": "T1078",
            "technique": "Valid Accounts",
            "confidence": "medium",
            "reason": "Protected admin endpoint access attempt with denial signal.",
        },
    ]


def _ensure_default_mitre_rules(db: Session) -> None:
    existing_count = db.query(SecurityMitreRule.id).count()
    if existing_count:
        return
    for item in _default_mitre_rule_rows():
        db.add(
            SecurityMitreRule(
                name=str(item["name"]),
                enabled=bool(item["enabled"]),
                priority=int(item["priority"]),
                source_type=str(item.get("source_type") or "any"),
                audit_event_pattern=str(item.get("audit_event_pattern") or ""),
                path_pattern=str(item.get("path_pattern") or ""),
                method_pattern=str(item.get("method_pattern") or ""),
                status_pattern=str(item.get("status_pattern") or ""),
                details_pattern=str(item.get("details_pattern") or ""),
                raw_pattern=str(item.get("raw_pattern") or ""),
                tactic_id=str(item["tactic_id"]),
                tactic=str(item["tactic"]),
                technique_id=str(item["technique_id"]),
                technique=str(item["technique"]),
                confidence=str(item.get("confidence") or "medium"),
                reason=str(item["reason"]),
            )
        )
    db.commit()


def _mitre_rules(db: Session) -> list[SecurityMitreRule]:
    _ensure_security_runtime_schema(db)
    _ensure_default_mitre_rules(db)
    return (
        db.query(SecurityMitreRule)
        .order_by(SecurityMitreRule.priority.asc(), SecurityMitreRule.id.asc())
        .all()
    )


def _pattern_matches(value: str, pattern: str) -> bool:
    candidate = str(value or "")
    raw_pattern = str(pattern or "").strip()
    if not raw_pattern:
        return True
    try:
        return re.search(raw_pattern, candidate, re.IGNORECASE) is not None
    except re.error:
        return raw_pattern.lower() in candidate.lower()


def _rule_matches_event(rule: SecurityMitreRule, event_ctx: dict[str, str]) -> bool:
    if not rule.enabled:
        return False
    source = (rule.source_type or "any").strip().lower()
    if source not in {"", "any"} and source != event_ctx["etype"]:
        return False
    if not _pattern_matches(event_ctx["audit_event"], rule.audit_event_pattern or ""):
        return False
    if not _pattern_matches(event_ctx["path"], rule.path_pattern or ""):
        return False
    if not _pattern_matches(event_ctx["method"], rule.method_pattern or ""):
        return False
    if not _pattern_matches(event_ctx["status"], rule.status_pattern or ""):
        return False
    if not _pattern_matches(event_ctx["details"], rule.details_pattern or ""):
        return False
    if not _pattern_matches(event_ctx["raw_line"], rule.raw_pattern or ""):
        return False
    return True


def _mitre_mapping(
    technique_id: str,
    technique: str,
    tactic_id: str,
    tactic: str,
    confidence: str,
    reason: str,
) -> dict[str, str]:
    if "." in technique_id:
        major, minor = technique_id.split(".", 1)
        mitre_url = f"https://attack.mitre.org/techniques/{major}/{minor}/"
    else:
        mitre_url = f"https://attack.mitre.org/techniques/{technique_id}/"
    return {
        "mitre": f"{technique_id} {technique}",
        "mitre_status": "mapped",
        "mitre_tactic_id": tactic_id,
        "mitre_tactic": tactic,
        "mitre_technique_id": technique_id,
        "mitre_technique": technique,
        "mitre_confidence": confidence,
        "mitre_reason": reason,
        "mitre_url": mitre_url,
    }


def _derive_mitre_mapping(
    etype: str,
    audit_event: str,
    path_value: str,
    method: str,
    status: str,
    details: str,
    raw_line: str,
    rules: list[SecurityMitreRule] | None = None,
) -> dict[str, str]:
    event_ctx = {
        "etype": (etype or "").strip().lower(),
        "audit_event": (audit_event or "").strip().lower(),
        "path": (path_value or "").strip().lower(),
        "method": (method or "").strip().upper(),
        "status": (status or "").strip().lower(),
        "details": (details or "").strip(),
        "raw_line": (raw_line or "").strip(),
    }
    if rules:
        for rule in rules:
            if not _rule_matches_event(rule, event_ctx):
                continue
            confidence = (rule.confidence or "medium").strip().lower()
            if confidence not in {"low", "medium", "high"}:
                confidence = "medium"
            return _mitre_mapping(
                technique_id=(rule.technique_id or "").strip() or "T0000",
                technique=(rule.technique or "").strip() or "Unknown Technique",
                tactic_id=(rule.tactic_id or "").strip() or "TA0000",
                tactic=(rule.tactic or "").strip() or "Unknown Tactic",
                confidence=confidence,
                reason=(rule.reason or "").strip() or f"Matched MITRE rule: {rule.name}",
            )

    event_type = (etype or "").strip().lower()
    audit_name = (audit_event or "").strip().lower()
    path = (path_value or "").strip().lower()
    method_name = (method or "").strip().upper()
    status_value = (status or "").strip().lower()
    text = " ".join([audit_name, path, (details or ""), (raw_line or "")]).lower()

    if audit_name == "auth_login_failed" or (path.startswith("/login") and status_value in {"401", "403", "failed", "blocked"}):
        return _mitre_mapping(
            technique_id="T1110",
            technique="Brute Force",
            tactic_id="TA0006",
            tactic="Credential Access",
            confidence="high",
            reason="Repeated failed authentication behavior matched brute-force pattern.",
        )

    if audit_name in {"auth_login_success", "auth_login_inactive"}:
        return _mitre_mapping(
            technique_id="T1078",
            technique="Valid Accounts",
            tactic_id="TA0001",
            tactic="Initial Access",
            confidence="medium",
            reason="Authentication event indicates account credential usage.",
        )

    if audit_name.startswith("security_events_clear"):
        return _mitre_mapping(
            technique_id="T1070",
            technique="Indicator Removal on Host",
            tactic_id="TA0005",
            tactic="Defense Evasion",
            confidence="high",
            reason="Security event/log clearing activity indicates indicator removal behavior.",
        )

    if (
        audit_name.startswith("security_toggle")
        or audit_name.startswith("security_env")
        or audit_name.startswith("security_setting")
        or audit_name.startswith("security_configuration")
    ):
        if any(token in text for token in ("=false", "=off", "disabled", "disable", "turned_off")):
            return _mitre_mapping(
                technique_id="T1562.001",
                technique="Impair Defenses: Disable or Modify Tools",
                tactic_id="TA0005",
                tactic="Defense Evasion",
                confidence="high",
                reason="Security control/configuration was set to a disabled state.",
            )
        return _mitre_mapping(
            technique_id="T1562",
            technique="Impair Defenses",
            tactic_id="TA0005",
            tactic="Defense Evasion",
            confidence="medium",
            reason="Security control configuration activity can affect defensive visibility.",
        )

    if audit_name.startswith("security_certificate"):
        return _mitre_mapping(
            technique_id="T1553",
            technique="Subvert Trust Controls",
            tactic_id="TA0005",
            tactic="Defense Evasion",
            confidence="medium",
            reason="Certificate/trust material manipulation detected in security controls.",
        )

    suspicious_web_tokens = (
        "union select",
        " or 1=1",
        "<script",
        "%3cscript",
        "../",
        "..\\",
        "%2e%2e",
        "/etc/passwd",
    )
    if any(token in text for token in suspicious_web_tokens):
        return _mitre_mapping(
            technique_id="T1190",
            technique="Exploit Public-Facing Application",
            tactic_id="TA0001",
            tactic="Initial Access",
            confidence="high",
            reason="Request content matches exploitation indicators against a web endpoint.",
        )

    command_exec_tokens = ("powershell", "cmd.exe", "bash -c", "curl ", "wget ", "xp_cmdshell")
    if any(token in text for token in command_exec_tokens):
        return _mitre_mapping(
            technique_id="T1059",
            technique="Command and Scripting Interpreter",
            tactic_id="TA0002",
            tactic="Execution",
            confidence="high",
            reason="Request/log content includes command interpreter execution indicators.",
        )

    if event_type == "request" and path.startswith("/admin") and method_name in {"POST", "PUT", "PATCH", "DELETE"} and status_value in {
        "401",
        "403",
        "429",
    }:
        return _mitre_mapping(
            technique_id="T1078",
            technique="Valid Accounts",
            tactic_id="TA0001",
            tactic="Initial Access",
            confidence="medium",
            reason="Protected admin endpoint access attempt with denial signal.",
        )

    return _mitre_unmapped()


def _apply_mitre_mapping(event: dict, rules: list[SecurityMitreRule] | None = None) -> bool:
    mapped = _derive_mitre_mapping(
        etype=str(event.get("type") or ""),
        audit_event=str(event.get("audit_event") or ""),
        path_value=str(event.get("path") or ""),
        method=str(event.get("method") or ""),
        status=str(event.get("status") or ""),
        details=str(event.get("details") or ""),
        raw_line=str(event.get("raw_log") or ""),
        rules=rules,
    )
    changed = False
    for key, value in mapped.items():
        if event.get(key) != value:
            event[key] = value
            changed = True
    sources = event.setdefault("field_sources", {})
    for key in mapped.keys():
        if sources.get(key) != "derived":
            sources[key] = "derived"
            changed = True
    return changed


def _build_event_from_log_line(etype: str, line: str, mitre_rules: list[SecurityMitreRule] | None = None) -> dict:
    parsed = _parse_kv_payload(line)
    log_timestamp = _extract_log_timestamp(line)
    status = parsed.get("status", "").strip()
    severity = _severity_from_line(line, status=status)
    user_id_raw = (parsed.get("user_id") or "").strip()
    request_id = (parsed.get("request_id") or "").strip()
    ip = (parsed.get("ip") or "").strip()
    path_value = (parsed.get("path") or "").strip()
    method = (parsed.get("method") or "").strip()
    query_value = (parsed.get("query") or "").strip()
    audit_event = (parsed.get("event") or "").strip()
    details = (parsed.get("details") or "").strip()
    detail_pairs = _parse_detail_pairs(details)
    employee_id_from_details = detail_pairs.get("employee_id", "")
    role_from_details = detail_pairs.get("role", "")

    if etype == "audit":
        if audit_event == "auth_login_success":
            event_name = "Employee Login Success"
            trigger_reason = "Authentication succeeded"
            security_control = "authentication + session-security"
            status_label = "success"
        elif audit_event == "auth_login_failed":
            event_name = "Employee Login Failed"
            trigger_reason = "Authentication failed"
            security_control = "authentication + brute-force-protection"
            status_label = "failed"
        elif audit_event == "auth_login_inactive":
            event_name = "Inactive Account Login Blocked"
            trigger_reason = "Inactive account blocked during login"
            security_control = "authentication + account-status-check"
            status_label = "blocked"
        elif audit_event == "auth_logout":
            event_name = "Employee Logout"
            trigger_reason = "Session terminated by user logout"
            security_control = "session-security"
            status_label = "logged_out"
        else:
            event_name = _title_case_event(audit_event)
            trigger_reason = f"Audit trail: {audit_event}" if audit_event else line[:180]
            security_control = "audit-trail"
            status_label = "logged"
        source_section = _source_section_from_path(path_value) if path_value else "security/audit"
    else:
        status_label = status or "-"
        source_section = _source_section_from_path(path_value)
        if path_value.startswith("/employee"):
            event_name = "Employee Dashboard Activity"
            trigger_reason = f"{method or 'REQ'} {path_value} status={status_label}"
            security_control = "activity-logging + session-security"
        elif path_value.startswith("/admin"):
            event_name = "Admin Security Activity"
            trigger_reason = f"{method or 'REQ'} {path_value} status={status_label}"
            security_control = "activity-logging + rbac"
        else:
            event_name = "Request Event"
            trigger_reason = f"{method or 'REQ'} {path_value or 'unknown'} status={status_label}"
            security_control = "activity-logging"

    fingerprint = hashlib.sha256(f"{etype}|{line}".encode("utf-8", errors="ignore")).hexdigest()
    event = {
        "type": etype,
        "event": event_name,
        "details": details or line,
        "severity": severity,
        "timestamp": log_timestamp,
        "occurred_at": log_timestamp,
        "user_id": user_id_raw,
        "request_id": request_id,
        "audit_event": audit_event,
        "trigger_reason": trigger_reason,
        "rule": security_control,
        "source_section": source_section,
        "ip": ip or "-",
        "status": status_label,
        "method": method or "-",
        "path": path_value or "-",
        "query": query_value or "-",
        "internal_user_id": user_id_raw or "-",
        "raw_log": line,
        "exact_details": (
            f"{method or '-'} {path_value or '-'}"
            + (f"?{query_value}" if query_value else "")
            + f" status={status_label} ip={ip or '-'} request_id={request_id or '-'}"
        ),
        "evidence": f"path={path_value or '-'} method={method or '-'}",
        "hits": "1",
        "threat_intel": "not_collected",
        "investigate": f"request_id={request_id or '-'} ip={ip or '-'} path={path_value or '-'}",
        "feature_id": _timeline_feature_id(etype, audit_event, source_section),
        "target_tab": "events",
        "target_anchor": "",
        "notification_id": fingerprint[:10],
        "field_sources": {
            "event": "derived",
            "type": "captured",
            "severity": "derived",
            "status": "captured" if status else "derived",
            "user": "captured" if user_id_raw else "derived",
            "user_id": "captured" if user_id_raw else "derived",
            "role": "derived",
            "department": "derived",
            "ip": "captured" if ip else "derived",
            "method": "captured" if method else "derived",
            "path": "captured" if path_value else "derived",
            "query": "captured" if query_value else "derived",
            "source_section": "derived",
            "request_id": "captured" if request_id else "derived",
            "internal_user_id": "captured" if user_id_raw else "derived",
            "exact_details": "derived",
            "rule": "derived",
            "reason": "derived",
            "evidence": "derived",
            "raw_log": "captured",
            "hits": "derived",
            "threat_intel": "derived",
            "investigate": "derived",
            "occurred": "captured",
            "timestamp": "captured",
        },
    }
    _apply_mitre_mapping(event, rules=mitre_rules)
    if employee_id_from_details:
        event["user_id"] = employee_id_from_details
        event["field_sources"]["user_id"] = "captured"
    if role_from_details:
        event["user_role"] = role_from_details
        event["field_sources"]["role"] = "captured"
    return event


def _ingest_security_events_to_db(db: Session) -> None:
    _ensure_security_runtime_schema(db)
    existing = {fp for (fp,) in db.query(SecurityEventRecord.fingerprint).all()}
    added = 0
    mitre_rules = _mitre_rules(db)
    for path, etype in (("logs/security.log", "request"), ("logs/audit.log", "audit")):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.readlines() if line.strip()]
        for line in lines:
            fingerprint = hashlib.sha256(f"{etype}|{line}".encode("utf-8", errors="ignore")).hexdigest()
            if fingerprint in existing:
                continue
            payload = _build_event_from_log_line(etype, line, mitre_rules=mitre_rules)
            db.add(
                SecurityEventRecord(
                    source_type=etype,
                    fingerprint=fingerprint,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
            )
            existing.add(fingerprint)
            added += 1
    if added:
        db.commit()


def _recent_security_events(db: Session, limit: int | None = None) -> list[dict]:
    _ensure_security_runtime_schema(db)
    settings = _settings_map(db)
    hide_auth_events = _as_bool(
        settings.get(("audit-trail", "AUDIT_TRAIL_HIDE_AUTH_EVENTS"), os.getenv("AUDIT_TRAIL_HIDE_AUTH_EVENTS")),
        True,
    )

    mitre_rules = _mitre_rules(db)
    _ingest_security_events_to_db(db)
    q = db.query(SecurityEventRecord).order_by(SecurityEventRecord.id.desc())
    rows = q.all() if limit is None else q.limit(limit).all()
    events: list[dict] = []
    updated_payloads = False
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload:
            if _apply_mitre_mapping(payload, rules=mitre_rules):
                row.payload_json = json.dumps(payload, ensure_ascii=False)
                updated_payloads = True
            events.append(payload)
    if updated_payloads:
        db.commit()

    # Backfill missing audit IPs from request log entries with the same request id.
    request_ip_by_id = {
        str(e.get("request_id")): str(e.get("ip"))
        for e in events
        if e.get("type") == "request" and e.get("request_id") and e.get("ip") and e.get("ip") != "-"
    }
    for e in events:
        if e.get("ip") and e.get("ip") != "-":
            continue
        rid = str(e.get("request_id") or "").strip()
        if rid and rid in request_ip_by_id:
            e["ip"] = request_ip_by_id[rid]
            e.setdefault("field_sources", {})["ip"] = "captured"

    # Enrich with user identity details for complete audit visibility.
    user_ids: set[int] = set()
    for e in events:
        raw = str(e.get("user_id") or "").strip()
        if raw.isdigit():
            user_ids.add(int(raw))
    if user_ids:
        users = db.query(User).filter(User.id.in_(user_ids)).all()
        users_by_id = {u.id: u for u in users}
        for e in events:
            raw = str(e.get("user_id") or "").strip()
            if not raw.isdigit():
                continue
            user = users_by_id.get(int(raw))
            if not user:
                continue
            e["user_name"] = user.name or "-"
            e["user_role"] = user.role or "-"
            e["user_department"] = user.department or "-"
            # Show employee code to admins while preserving the internal user id in details.
            e["user_id"] = user.employee_id or str(user.id)
            sources = e.setdefault("field_sources", {})
            sources["user"] = "derived"
            if sources.get("role") != "captured":
                sources["role"] = "derived"
            sources["department"] = "derived"
            if sources.get("user_id") != "captured":
                sources["user_id"] = "derived"

    # Resolve employee profile when event already carries employee_id (e.g., login failures).
    employee_ids = {
        str(e.get("user_id")).strip()
        for e in events
        if e.get("user_id") and not str(e.get("user_id")).strip().isdigit() and str(e.get("user_id")).strip() != "-"
    }
    if employee_ids:
        users = db.query(User).filter(User.employee_id.in_(list(employee_ids))).all()
        users_by_emp = {str(u.employee_id): u for u in users if u.employee_id}
        for e in events:
            emp_id = str(e.get("user_id") or "").strip()
            if not emp_id or emp_id == "-" or emp_id.isdigit():
                continue
            user = users_by_emp.get(emp_id)
            if not user:
                continue
            e["user_name"] = user.name or e.get("user_name") or "-"
            e["user_role"] = e.get("user_role") or user.role or "-"
            e["user_department"] = user.department or e.get("user_department") or "-"
            sources = e.setdefault("field_sources", {})
            sources["user"] = "derived"
            if sources.get("role") != "captured":
                sources["role"] = "derived"
            sources["department"] = "derived"

    # Optionally hide authentication login/logout events from the security events stream.
    if hide_auth_events:
        events = [e for e in events if not _is_excluded_auth_event(e)]
    return events if limit is None else events[:limit]


def _security_summary(features: list[dict], events: list[dict]) -> dict[str, int | str]:
    enabled_count = sum(1 for f in features if f["enabled"])
    return {
        "enabled": enabled_count,
        "disabled": len(features) - enabled_count,
        "total_events": len(events),
        "audit_events": sum(1 for e in events if e["type"] == "audit"),
        "request_events": sum(1 for e in events if e["type"] == "request"),
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def _group_hash_history(hash_history: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], dict] = {}
    for item in hash_history or []:
        entity_type = str(item.get("entity_type") or "unknown")
        entity_id = str(item.get("entity_id") or "-")
        employee_name = str(item.get("employee_name") or "").strip()
        key = (entity_type, entity_id, employee_name)
        group = grouped.get(key)
        if not group:
            group = {
                "label": employee_name or entity_id or entity_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "employee_name": employee_name or "-",
                "has_actor": False,
                "latest": item.get("timestamp") or "-",
                "entries": [],
            }
            grouped[key] = group
        if item.get("actor_id") or item.get("actor_name"):
            group["has_actor"] = True
        group["entries"].append(item)

    out = sorted(
        grouped.values(),
        key=lambda g: str(g.get("latest") or ""),
        reverse=True,
    )
    return out


def _enrich_hash_history(db: Session, hash_history: list[dict]) -> list[dict]:
    employee_ids = {
        str(item.get("entity_id")).strip()
        for item in hash_history
        if str(item.get("entity_type") or "").lower() == "user" and str(item.get("entity_id") or "").strip()
    }
    users_by_emp: dict[str, User] = {}
    if employee_ids:
        users = db.query(User).filter(User.employee_id.in_(list(employee_ids))).all()
        users_by_emp = {str(u.employee_id): u for u in users if u.employee_id}

    out: list[dict] = []

    def _value_from_db(u: User, plain_attr: str, secure_attr: str) -> tuple[str, str]:
        secure_val = getattr(u, secure_attr, None)
        plain_val = getattr(u, plain_attr, None)
        if secure_val is None or str(secure_val).strip() == "":
            return (plain_val or "-"), "db_plain"
        token = str(secure_val)
        if token.startswith("enc::"):
            try:
                return (decrypt_value(token) or "-"), "db_encrypted"
            except Exception:
                return token, "db_raw"
        # Legacy/plain value stored in secure mirror field; show exactly as DB has it.
        return token, "db_plain"

    for item in hash_history:
        row = dict(item)
        row["current_value"] = "-"
        row["current_value_source"] = "db_unknown"
        etype = str(row.get("entity_type") or "").lower()
        field = str(row.get("field_name") or "").lower()
        eid = str(row.get("entity_id") or "").strip()
        if etype == "user" and eid in users_by_emp:
            u = users_by_emp[eid]
            if field == "employee_id":
                row["current_value"] = u.employee_id or "-"
                row["current_value_source"] = "db_plain"
            elif field == "name":
                row["current_value"], row["current_value_source"] = _value_from_db(u, "name", "name_secure")
            elif field == "email":
                row["current_value"], row["current_value_source"] = _value_from_db(u, "email", "email_secure")
            elif field == "rfid_tag":
                row["current_value"], row["current_value_source"] = _value_from_db(u, "rfid_tag", "rfid_tag_secure")
            elif field == "role":
                row["current_value"], row["current_value_source"] = _value_from_db(u, "role", "role_secure")
            elif field == "department":
                row["current_value"], row["current_value_source"] = _value_from_db(u, "department", "department_secure")
        out.append(row)
    return out


def _configuration_rows(db: Session, features: list[dict]) -> tuple[list[dict], list[dict]]:
    _ensure_security_runtime_schema(db)
    feature_name_map = {f["id"]: f.get("name", f["id"]) for f in features}
    rows = (
        db.query(SecurityManagedSetting)
        .order_by(SecurityManagedSetting.updated_at.desc(), SecurityManagedSetting.id.desc())
        .all()
    )
    out: list[dict] = []
    for row in rows:
        updated = row.updated_at or row.created_at
        out.append(
            {
                "id": row.id,
                "feature_id": row.feature_id,
                "feature_name": feature_name_map.get(row.feature_id, row.feature_id),
                "key": row.key,
                "value": row.value or "",
                "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S UTC") if updated else "-",
                "location": "db.security_managed_settings",
            }
        )
    feature_options = [{"id": f["id"], "name": f.get("name", f["id"])} for f in features]
    return out, feature_options


def _security_live_payload(db: Session) -> dict:
    features = _dashboard_features(_security_features(db))
    events = _recent_security_events(db, limit=None)
    summary = _security_summary(features, events)
    return {
        "summary": summary,
        "metrics": {f["id"]: f["metrics"] for f in features},
        "events": events,
    }


def _security_live_signature(payload: dict) -> str:
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    events = payload.get("events", []) if isinstance(payload, dict) else []
    event_ids = [str(e.get("notification_id") or e.get("request_id") or e.get("timestamp") or "") for e in events if isinstance(e, dict)]
    signature_obj = {
        "total_events": int(summary.get("total_events", 0) or 0),
        "audit_events": int(summary.get("audit_events", 0) or 0),
        "request_events": int(summary.get("request_events", 0) or 0),
        "metrics": metrics,
        "event_ids": event_ids[:200],
    }
    return json.dumps(signature_obj, sort_keys=True, ensure_ascii=False)


def _tail_line(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return ""
            chunk = min(4096, size)
            fh.seek(-chunk, os.SEEK_END)
            data = fh.read().decode("utf-8", errors="ignore")
            lines = [line.strip() for line in data.splitlines() if line.strip()]
            return lines[-1][:220] if lines else ""
    except Exception:
        return ""


def _security_files_state() -> dict[str, dict[str, str | int | bool]]:
    out: dict[str, dict[str, str | int | bool]] = {}
    for path in SECURITY_WATCH_FILES:
        exists = os.path.exists(path)
        stat = os.stat(path) if exists else None
        out[path] = {
            "exists": exists,
            "size": int(stat.st_size) if stat else 0,
            "mtime": int(stat.st_mtime) if stat else 0,
            "tail": _tail_line(path),
        }
    return out


def _security_dashboard_payload(db: Session) -> dict:
    payload = _security_live_payload(db)
    payload["security_files"] = _security_files_state()
    payload["configuration_count"] = int(db.query(SecurityManagedSetting.id).count())
    payload["mitre_rule_count"] = int(db.query(SecurityMitreRule.id).count())
    payload["dashboard_reload_signature"] = _security_dashboard_reload_signature(payload)
    payload["stream_signature"] = _security_live_signature(payload)
    return payload


def _security_dashboard_reload_signature(payload: dict) -> str:
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    events = payload.get("events", []) if isinstance(payload, dict) else []
    files_state = payload.get("security_files", {}) if isinstance(payload, dict) else {}
    event_ids = [str(e.get("notification_id") or e.get("request_id") or e.get("timestamp") or "") for e in events if isinstance(e, dict)]
    signature_obj = {
        "total_events": int(summary.get("total_events", 0) or 0),
        "audit_events": int(summary.get("audit_events", 0) or 0),
        "request_events": int(summary.get("request_events", 0) or 0),
        "event_ids": event_ids[:200],
        "configuration_count": int(payload.get("configuration_count", 0) or 0),
        "mitre_rule_count": int(payload.get("mitre_rule_count", 0) or 0),
        "security_files": {
            path: {
                "exists": bool(data.get("exists")),
                "size": int(data.get("size", 0) or 0),
                "mtime": int(data.get("mtime", 0) or 0),
            }
            for path, data in files_state.items()
            if isinstance(data, dict)
        },
    }
    return json.dumps(signature_obj, sort_keys=True, ensure_ascii=False)


def _mitre_rule_rows(db: Session) -> list[dict]:
    rows = _mitre_rules(db)
    out: list[dict] = []
    for row in rows:
        updated = row.updated_at or row.created_at
        out.append(
            {
                "id": row.id,
                "name": row.name,
                "enabled": bool(row.enabled),
                "priority": row.priority,
                "source_type": row.source_type or "any",
                "audit_event_pattern": row.audit_event_pattern or "",
                "path_pattern": row.path_pattern or "",
                "method_pattern": row.method_pattern or "",
                "status_pattern": row.status_pattern or "",
                "details_pattern": row.details_pattern or "",
                "raw_pattern": row.raw_pattern or "",
                "tactic_id": row.tactic_id,
                "tactic": row.tactic,
                "technique_id": row.technique_id,
                "technique": row.technique,
                "confidence": row.confidence,
                "reason": row.reason,
                "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S UTC") if updated else "-",
            }
        )
    return out


def _sanitize_optional_pattern(value: str | None, field: str, max_len: int = 255) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} too long")
    return cleaned


def _sanitize_mitre_confidence(value: str | None) -> str:
    cleaned = (value or "medium").strip().lower()
    if cleaned not in {"low", "medium", "high"}:
        raise HTTPException(status_code=400, detail="Invalid confidence")
    return cleaned


def _sanitize_source_type(value: str | None) -> str:
    cleaned = (value or "any").strip().lower()
    if cleaned not in {"any", "request", "audit"}:
        raise HTTPException(status_code=400, detail="Invalid source type")
    return cleaned


@router.get("/admin/security", response_class=HTMLResponse)
async def admin_security_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    _ensure_session_timeout_settings(db)
    all_features = _security_features(db)
    features = _dashboard_features(all_features)
    events = _recent_security_events(db, limit=None)
    hash_history = read_hash_history(limit=None)
    hash_history = _enrich_hash_history(db, hash_history)
    configurations, configuration_features = _configuration_rows(db, all_features)
    mitre_rules = _mitre_rule_rows(db)
    summary = _security_summary(features, events)
    security_files = _security_files_state()
    dashboard_reload_signature = _security_dashboard_reload_signature(
        {
            "summary": summary,
            "events": events,
            "configuration_count": len(configurations),
            "mitre_rule_count": len(mitre_rules),
            "security_files": security_files,
        }
    )
    return templates.TemplateResponse(
        "admin/admin_security.html",
        {
            "request": request,
            "user": user,
            "features": features,
            "events": events,
            "hash_history": hash_history,
            "hash_groups": _group_hash_history(hash_history),
            "summary": summary,
            "environment": {
                "app_env": os.getenv("APP_ENV", "auto"),
                "metrics_enabled": _env_bool("PROMETHEUS_ENABLED", True),
                "metrics_path": os.getenv("PROMETHEUS_METRICS_PATH", "/metrics"),
                "logs": {
                    "security": "logs/security.log",
                    "audit": "logs/audit.log",
                    "hash_history": "logs/hash_history.log",
                },
            },
            "recommendations": [
                "Review feature toggles and configure required inputs.",
                "Upload cert/key files only for TLS/WAF/encryption features.",
                "Track configuration changes from the audit trail.",
            ],
            "checklist": {"total": len(features), "enabled": summary["enabled"], "disabled": summary["disabled"]},
            "checklist_items": features,
            "configurations": configurations,
            "configuration_features": configuration_features,
            "mitre_rules": mitre_rules,
            "security_files": security_files,
            "dashboard_reload_signature": dashboard_reload_signature,
            "current_year": datetime.datetime.utcnow().year,
        },
    )


@router.get("/admin/security/events/{event_id}", response_class=HTMLResponse)
async def admin_security_event_detail(
    event_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    events = _recent_security_events(db, limit=None)
    event = next((e for e in events if str(e.get("notification_id", "")) == str(event_id)), None)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return templates.TemplateResponse(
        "admin/admin_security_event_detail.html",
        {
            "request": request,
            "user": user,
            "event": event,
            "current_year": datetime.datetime.utcnow().year,
        },
    )


@router.get("/admin/security/hash/group/{group_index}", response_class=HTMLResponse)
async def admin_security_hash_group_detail(
    group_index: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    hash_history = read_hash_history(limit=None)
    hash_history = _enrich_hash_history(db, hash_history)
    hash_groups = _group_hash_history(hash_history)
    if group_index < 0 or group_index >= len(hash_groups):
        raise HTTPException(status_code=404, detail="Hash group not found")
    group = hash_groups[group_index]
    return templates.TemplateResponse(
        "admin/admin_security_hash_group_detail.html",
        {
            "request": request,
            "user": user,
            "group": group,
            "group_index": group_index,
            "current_year": datetime.datetime.utcnow().year,
        },
    )


@router.get("/admin/security/metrics")
async def admin_security_metrics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    features = _security_features(db)
    return JSONResponse({"metrics": {f["id"]: f["metrics"] for f in features}})


@router.get("/admin/security/live")
async def admin_security_live(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    return JSONResponse(_security_dashboard_payload(db))


@router.get("/admin/security/live/stream")
async def admin_security_live_stream(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)

    async def event_stream():
        last_stream_signature = ""
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = _security_dashboard_payload(db)
                stream_signature = str(payload.get("stream_signature") or "")
                if stream_signature != last_stream_signature:
                    data = json.dumps(payload, ensure_ascii=False)
                    yield f"event: security_update\ndata: {data}\n\n"
                    last_stream_signature = stream_signature
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/metrics")
async def prometheus_metrics(user: User = Depends(get_current_user)):
    _admin_guard(user)
    if not _env_bool("PROMETHEUS_ENABLED", True):
        raise HTTPException(status_code=404, detail="Metrics disabled")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post("/admin/security/events/clear")
async def admin_security_clear_events(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    _clear_log("logs/security.log")
    _clear_log("logs/audit.log")
    db.query(SecurityEventRecord).delete()
    db.commit()
    audit("security_events_clear", user_id=user.id, details="security.log,audit.log")
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/events/sample")
async def admin_security_sample_events(user: User = Depends(get_current_user)):
    _admin_guard(user)
    audit("security_toggle", user_id=user.id, details="CSRF_PROTECTION_ENABLED=true")
    audit("security_env_update", user_id=user.id, details="SECURITY_CONFIG_LOG_LEVEL=INFO")
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/toggle")
async def admin_security_toggle(
    feature: str = Form(...),
    action: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    feature_map = {f["id"]: f for f in _security_features(db)}
    selected = feature_map.get(feature)
    if not selected:
        raise HTTPException(status_code=400, detail="Unknown feature")
    env_var = selected["env_var"]
    current = selected["enabled"]
    new_value = "true" if action == "on" else "false" if action == "off" else ("false" if current else "true")
    _set_env_flag(env_var, new_value)
    _upsert_setting(db, feature, "__enabled__", new_value)
    _upsert_setting(db, feature, env_var, new_value)
    db.commit()
    audit("security_toggle", user_id=user.id, details=f"{env_var}={new_value}")
    return RedirectResponse("/admin/security", status_code=303)


@router.post("/admin/security/settings")
async def admin_security_settings_update(
    feature_id: str = Form(...),
    key: str = Form(...),
    value: str = Form(...),
    return_to: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    key = _sanitize_required(key, "setting key", 120, r"^[A-Za-z0-9_.-]{1,120}$")
    value = _sanitize_required(value, "setting value", 2000)
    _upsert_setting(db, feature_id, key, value)
    if re.fullmatch(r"[A-Z0-9_]+", key):
        _set_env_flag(key, value)
    db.commit()
    audit("security_setting_update", user_id=user.id, details=f"{feature_id}:{key}={value}")
    if return_to and return_to.startswith("/admin/security"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse(f"/admin/security/{feature_id}", status_code=303)


@router.post("/admin/security/env")
async def admin_security_env_update(
    feature_id: str = Form(...),
    env_var: str = Form(...),
    env_value: str = Form(...),
    return_to: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    cleaned = _sanitize_required(env_value, "env value", 2000)
    _set_env_flag(env_var, cleaned)
    _upsert_setting(db, feature_id, env_var, cleaned)
    db.commit()
    audit("security_env_update", user_id=user.id, details=f"{feature_id}:{env_var}={cleaned}")
    if return_to and return_to.startswith("/admin/security"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse(f"/admin/security/{feature_id}", status_code=303)


@router.post("/admin/security/env/bulk")
async def admin_security_env_bulk_update(
    request: Request,
    feature_id: str = Form(...),
    return_to: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    feature = next((f for f in _security_features(db) if f["id"] == feature_id), None)
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    allowed = {item["name"]: item for item in feature.get("inputs", [])}
    if not allowed:
        raise HTTPException(status_code=400, detail="No configurable inputs for this feature")

    form = await request.form()
    changed: list[str] = []
    for key, item in allowed.items():
        if key not in form:
            continue
        raw = str(form.get(key))
        normalized = _normalize_input_value(raw, item.get("type", "text"))
        if normalized == "":
            continue
        _upsert_setting(db, feature_id, key, normalized)
        _set_env_flag(key, normalized)
        changed.append(f"{key}={normalized}")
    db.commit()

    if changed:
        audit("security_env_bulk_update", user_id=user.id, details=f"{feature_id}:{','.join(changed)}")
    if return_to and return_to.startswith("/admin/security"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse(f"/admin/security/{feature_id}", status_code=303)


@router.post("/admin/security/certificates/upload")
async def admin_security_certificate_upload(
    feature_id: str = Form(...),
    cert_file: UploadFile = File(...),
    return_to: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    feature = next((f for f in _security_features(db) if f["id"] == feature_id), None)
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    if not feature.get("allow_upload"):
        raise HTTPException(status_code=400, detail="File upload not enabled for this feature")

    data = await cert_file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty certificate file")
    db.add(
        SecurityCertificate(
            feature_id=feature_id,
            filename=cert_file.filename or "file.bin",
            content_type=cert_file.content_type,
            data=data,
        )
    )
    db.commit()
    audit("security_certificate_upload", user_id=user.id, details=f"{feature_id}:{cert_file.filename}")
    if return_to and return_to.startswith("/admin/security"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse(f"/admin/security/{feature_id}", status_code=303)


@router.get("/admin/security/certificates/list")
async def admin_security_certificate_list(feature_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    certs = (
        db.query(SecurityCertificate)
        .filter(SecurityCertificate.feature_id == feature_id)
        .order_by(SecurityCertificate.uploaded_at.desc())
        .all()
    )
    return JSONResponse(
        {
            "items": [
                {
                    "id": c.id,
                    "filename": c.filename,
                    "content_type": c.content_type,
                    "uploaded_at": c.uploaded_at.isoformat() if c.uploaded_at else None,
                }
                for c in certs
            ]
        }
    )


@router.get("/admin/security/certificates/{cert_id}")
async def admin_security_certificate_download(cert_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    cert = db.query(SecurityCertificate).filter(SecurityCertificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    headers = {"Content-Disposition": f'attachment; filename="{cert.filename}"'}
    return Response(cert.data, media_type=cert.content_type or "application/octet-stream", headers=headers)


@router.post("/admin/security/certificates/{cert_id}/rename")
async def admin_security_certificate_rename(
    cert_id: int,
    filename: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    cert = db.query(SecurityCertificate).filter(SecurityCertificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    cert.filename = _sanitize_required(filename, "filename", 255)
    db.commit()
    audit("security_certificate_rename", user_id=user.id, details=f"{cert.feature_id}:{cert_id}:{cert.filename}")
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/certificates/{cert_id}/delete")
async def admin_security_certificate_delete(cert_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    cert = db.query(SecurityCertificate).filter(SecurityCertificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    feature_id = cert.feature_id
    db.delete(cert)
    db.commit()
    audit("security_certificate_delete", user_id=user.id, details=f"{feature_id}:{cert_id}")
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/certificates/{cert_id}/replace")
async def admin_security_certificate_replace(
    cert_id: int,
    cert_file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    cert = db.query(SecurityCertificate).filter(SecurityCertificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    data = await cert_file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    cert.filename = cert_file.filename or cert.filename
    cert.content_type = cert_file.content_type
    cert.data = data
    db.commit()
    audit("security_certificate_replace", user_id=user.id, details=f"{cert.feature_id}:{cert_id}:{cert.filename}")
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/configurations/{setting_id}/update")
async def admin_security_configuration_update(
    setting_id: int,
    key: str = Form(...),
    value: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    row = db.query(SecurityManagedSetting).filter(SecurityManagedSetting.id == setting_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Configuration not found")

    clean_key = _sanitize_required(key, "setting key", 120, r"^[A-Za-z0-9_.-]{1,120}$")
    clean_value = _sanitize_required(value, "setting value", 2000)
    old_key = row.key
    old_value = row.value or ""
    row.key = clean_key
    row.value = clean_value
    db.commit()

    if re.fullmatch(r"[A-Z0-9_]+", clean_key):
        _set_env_flag(clean_key, clean_value)

    audit(
        "security_configuration_update",
        user_id=user.id,
        details=f"{row.feature_id}:{setting_id}:{old_key}={old_value}->{clean_key}={clean_value}",
    )
    return JSONResponse({"status": "ok"})


@router.post("/admin/security/configurations/create")
async def admin_security_configuration_create(
    feature_id: str = Form(...),
    key: str = Form(...),
    value: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    clean_feature_id = _sanitize_required(feature_id, "feature id", 120, r"^[a-z0-9-]{1,120}$")
    clean_key = _sanitize_required(key, "setting key", 120, r"^[A-Za-z0-9_.-]{1,120}$")
    clean_value = _sanitize_required(value, "setting value", 2000)

    feature_map = {f["id"]: f.get("name", f["id"]) for f in _security_features(db)}
    if clean_feature_id not in feature_map:
        raise HTTPException(status_code=400, detail="Unknown feature")

    existing = (
        db.query(SecurityManagedSetting)
        .filter(SecurityManagedSetting.feature_id == clean_feature_id, SecurityManagedSetting.key == clean_key)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Configuration key already exists for this feature")

    row = SecurityManagedSetting(feature_id=clean_feature_id, key=clean_key, value=clean_value)
    db.add(row)
    db.commit()
    db.refresh(row)

    if re.fullmatch(r"[A-Z0-9_]+", clean_key):
        _set_env_flag(clean_key, clean_value)

    audit("security_configuration_create", user_id=user.id, details=f"{clean_feature_id}:{row.id}:{clean_key}")
    updated = row.updated_at or row.created_at
    return JSONResponse(
        {
            "status": "ok",
            "row": {
                "id": row.id,
                "feature_id": row.feature_id,
                "feature_name": feature_map.get(row.feature_id, row.feature_id),
                "key": row.key,
                "value": row.value or "",
                "location": "db.security_managed_settings",
                "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S UTC") if updated else "-",
            },
        }
    )


@router.post("/admin/security/configurations/{setting_id}/delete")
async def admin_security_configuration_delete(
    setting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    row = db.query(SecurityManagedSetting).filter(SecurityManagedSetting.id == setting_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Configuration not found")

    feature_id = row.feature_id
    key = row.key
    db.delete(row)
    db.commit()
    audit("security_configuration_delete", user_id=user.id, details=f"{feature_id}:{setting_id}:{key}")
    return JSONResponse({"status": "ok"})


@router.get("/admin/security/mitre-rules")
async def admin_security_mitre_rules_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    return JSONResponse({"items": _mitre_rule_rows(db)})


@router.post("/admin/security/mitre-rules/create")
async def admin_security_mitre_rule_create(
    name: str = Form(...),
    enabled: str = Form("true"),
    priority: str = Form("100"),
    source_type: str = Form("any"),
    audit_event_pattern: str = Form(""),
    path_pattern: str = Form(""),
    method_pattern: str = Form(""),
    status_pattern: str = Form(""),
    details_pattern: str = Form(""),
    raw_pattern: str = Form(""),
    tactic_id: str = Form(...),
    tactic: str = Form(...),
    technique_id: str = Form(...),
    technique: str = Form(...),
    confidence: str = Form("medium"),
    reason: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    clean_name = _sanitize_required(name, "rule name", 120)
    clean_source_type = _sanitize_source_type(source_type)
    clean_tactic_id = _sanitize_required(tactic_id, "tactic id", 20, r"^TA[0-9]{4}$")
    clean_tactic = _sanitize_required(tactic, "tactic", 120)
    clean_technique_id = _sanitize_required(technique_id, "technique id", 20, r"^T[0-9]{4}(\.[0-9]{3})?$")
    clean_technique = _sanitize_required(technique, "technique", 160)
    clean_reason = _sanitize_required(reason, "reason", 600)
    clean_confidence = _sanitize_mitre_confidence(confidence)
    clean_enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    try:
        clean_priority = int(str(priority).strip() or "100")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid priority")
    if clean_priority < 1 or clean_priority > 9999:
        raise HTTPException(status_code=400, detail="Invalid priority")

    row = SecurityMitreRule(
        name=clean_name,
        enabled=clean_enabled,
        priority=clean_priority,
        source_type=clean_source_type,
        audit_event_pattern=_sanitize_optional_pattern(audit_event_pattern, "audit_event_pattern"),
        path_pattern=_sanitize_optional_pattern(path_pattern, "path_pattern"),
        method_pattern=_sanitize_optional_pattern(method_pattern, "method_pattern", 64),
        status_pattern=_sanitize_optional_pattern(status_pattern, "status_pattern", 64),
        details_pattern=_sanitize_optional_pattern(details_pattern, "details_pattern"),
        raw_pattern=_sanitize_optional_pattern(raw_pattern, "raw_pattern"),
        tactic_id=clean_tactic_id,
        tactic=clean_tactic,
        technique_id=clean_technique_id,
        technique=clean_technique,
        confidence=clean_confidence,
        reason=clean_reason,
    )
    db.add(row)
    db.commit()
    audit("security_mitre_rule_create", user_id=user.id, details=f"{row.id}:{row.name}:{row.technique_id}")
    return JSONResponse({"status": "ok", "items": _mitre_rule_rows(db)})


@router.post("/admin/security/mitre-rules/{rule_id}/update")
async def admin_security_mitre_rule_update(
    rule_id: int,
    name: str = Form(...),
    enabled: str = Form("true"),
    priority: str = Form("100"),
    source_type: str = Form("any"),
    audit_event_pattern: str = Form(""),
    path_pattern: str = Form(""),
    method_pattern: str = Form(""),
    status_pattern: str = Form(""),
    details_pattern: str = Form(""),
    raw_pattern: str = Form(""),
    tactic_id: str = Form(...),
    tactic: str = Form(...),
    technique_id: str = Form(...),
    technique: str = Form(...),
    confidence: str = Form("medium"),
    reason: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    row = db.query(SecurityMitreRule).filter(SecurityMitreRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="MITRE rule not found")
    row.name = _sanitize_required(name, "rule name", 120)
    row.source_type = _sanitize_source_type(source_type)
    row.tactic_id = _sanitize_required(tactic_id, "tactic id", 20, r"^TA[0-9]{4}$")
    row.tactic = _sanitize_required(tactic, "tactic", 120)
    row.technique_id = _sanitize_required(technique_id, "technique id", 20, r"^T[0-9]{4}(\.[0-9]{3})?$")
    row.technique = _sanitize_required(technique, "technique", 160)
    row.reason = _sanitize_required(reason, "reason", 600)
    row.confidence = _sanitize_mitre_confidence(confidence)
    row.enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    try:
        row.priority = int(str(priority).strip() or "100")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid priority")
    if row.priority < 1 or row.priority > 9999:
        raise HTTPException(status_code=400, detail="Invalid priority")
    row.audit_event_pattern = _sanitize_optional_pattern(audit_event_pattern, "audit_event_pattern")
    row.path_pattern = _sanitize_optional_pattern(path_pattern, "path_pattern")
    row.method_pattern = _sanitize_optional_pattern(method_pattern, "method_pattern", 64)
    row.status_pattern = _sanitize_optional_pattern(status_pattern, "status_pattern", 64)
    row.details_pattern = _sanitize_optional_pattern(details_pattern, "details_pattern")
    row.raw_pattern = _sanitize_optional_pattern(raw_pattern, "raw_pattern")

    db.commit()
    audit("security_mitre_rule_update", user_id=user.id, details=f"{row.id}:{row.name}:{row.technique_id}")
    return JSONResponse({"status": "ok", "items": _mitre_rule_rows(db)})


@router.post("/admin/security/mitre-rules/{rule_id}/delete")
async def admin_security_mitre_rule_delete(
    rule_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin_guard(user)
    row = db.query(SecurityMitreRule).filter(SecurityMitreRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="MITRE rule not found")
    rule_name = row.name
    db.delete(row)
    db.commit()
    audit("security_mitre_rule_delete", user_id=user.id, details=f"{rule_id}:{rule_name}")
    return JSONResponse({"status": "ok", "items": _mitre_rule_rows(db)})


@router.get("/admin/security/{feature_id}", response_class=HTMLResponse)
async def admin_security_detail(request: Request, feature_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin_guard(user)
    features = _security_features(db)
    feature = next((f for f in features if f["id"] == feature_id), None)
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    ids = [f["id"] for f in features]
    idx = ids.index(feature_id)
    settings = (
        db.query(SecurityManagedSetting)
        .filter(SecurityManagedSetting.feature_id == feature_id)
        .order_by(SecurityManagedSetting.key.asc())
        .all()
    )
    certificates = (
        db.query(SecurityCertificate)
        .filter(SecurityCertificate.feature_id == feature_id)
        .order_by(SecurityCertificate.uploaded_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/admin_security_detail.html",
        {
            "request": request,
            "user": user,
            "feature": feature,
            "prev_id": ids[idx - 1] if idx > 0 else None,
            "next_id": ids[idx + 1] if idx < len(ids) - 1 else None,
            "settings": settings,
            "certificates": certificates,
            "hash_history": read_hash_history(limit=100),
            "current_year": datetime.datetime.utcnow().year,
        },
    )


@router.on_event("startup")
def security_startup() -> None:
    ensure_session_secret()
