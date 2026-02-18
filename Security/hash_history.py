from __future__ import annotations

import json
import os
import datetime
from typing import Any


_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "hash_history.log")


def _ensure_log_dir() -> None:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)


def _write_payload_to_db(payload: dict[str, Any]) -> None:
    try:
        from app.database import SessionLocal
        from app.models import SecurityHashHistory

        db = SessionLocal()
        try:
            row = SecurityHashHistory(
                timestamp=str(payload.get("timestamp") or ""),
                entity_type=str(payload.get("entity_type") or ""),
                entity_id=(str(payload.get("entity_id")) if payload.get("entity_id") is not None else None),
                field_name=str(payload.get("field_name") or ""),
                old_hash=(str(payload.get("old_hash")) if payload.get("old_hash") is not None else None),
                new_hash=(str(payload.get("new_hash")) if payload.get("new_hash") is not None else None),
                actor_id=(str(payload.get("actor_id")) if payload.get("actor_id") is not None else None),
                actor_name=(str(payload.get("actor_name")) if payload.get("actor_name") is not None else None),
                employee_name=(str(payload.get("employee_name")) if payload.get("employee_name") is not None else None),
                details=(str(payload.get("details")) if payload.get("details") is not None else None),
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception:
        # Keep logging non-fatal; file logging remains fallback.
        pass


def _read_payloads_from_db(limit: int | None = 50) -> list[dict[str, Any]]:
    try:
        from app.database import SessionLocal
        from app.models import SecurityHashHistory

        db = SessionLocal()
        try:
            q = db.query(SecurityHashHistory).order_by(SecurityHashHistory.id.desc())
            rows = q.all() if limit is None else q.limit(limit).all()
            return [
                {
                    "timestamp": r.timestamp,
                    "entity_type": r.entity_type,
                    "entity_id": r.entity_id,
                    "field_name": r.field_name,
                    "old_hash": r.old_hash,
                    "new_hash": r.new_hash,
                    "actor_id": r.actor_id,
                    "actor_name": r.actor_name,
                    "employee_name": r.employee_name,
                    "details": r.details,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception:
        return []


def log_hash_history(
    *,
    entity_type: str,
    entity_id: str | None,
    field_name: str,
    old_hash: str | None,
    new_hash: str | None,
    actor_id: str | None,
    actor_name: str | None,
    employee_name: str | None = None,
    details: str | None = None,
) -> None:
    _ensure_log_dir()
    payload = {
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field_name": field_name,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "employee_name": employee_name,
        "details": details,
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _write_payload_to_db(payload)


def read_hash_history(limit: int | None = 50) -> list[dict[str, Any]]:
    db_entries = _read_payloads_from_db(limit=None)
    file_entries: list[dict[str, Any]] = []
    if os.path.exists(_LOG_PATH):
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        for line in lines:
            try:
                file_entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not db_entries and not file_entries:
        return []

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for entry in db_entries + file_entries:
        key = (
            str(entry.get("timestamp") or ""),
            str(entry.get("entity_type") or ""),
            str(entry.get("entity_id") or ""),
            str(entry.get("field_name") or ""),
            str(entry.get("old_hash") or ""),
            str(entry.get("new_hash") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    merged.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    return merged if limit is None else merged[:limit]
