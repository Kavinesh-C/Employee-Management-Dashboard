from __future__ import annotations

import json
import os
import datetime
from typing import Any


_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "hash_history.log")


def _ensure_log_dir() -> None:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)


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


def read_hash_history(limit: int | None = 50) -> list[dict[str, Any]]:
    if not os.path.exists(_LOG_PATH):
        return []
    with open(_LOG_PATH, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    entries: list[dict[str, Any]] = []
    selected = lines if limit is None else lines[-limit:]
    for line in selected:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(entries))
