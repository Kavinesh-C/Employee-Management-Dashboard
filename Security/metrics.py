"""
SECURITY METRICS
================
Prometheus-backed metrics for security features.
"""

from __future__ import annotations

import os
from typing import Dict

try:
    from prometheus_client import Counter, Gauge
except Exception:  # pragma: no cover
    Counter = None
    Gauge = None


_FEATURE_EVENTS = None
_FEATURE_ENABLED = None


def _enabled() -> bool:
    return os.getenv("PROMETHEUS_ENABLED", "true").lower() == "true"


def _init_metrics() -> None:
    global _FEATURE_EVENTS, _FEATURE_ENABLED
    if _FEATURE_EVENTS or not _enabled() or Counter is None or Gauge is None:
        return
    _FEATURE_EVENTS = Counter(
        "security_feature_events_total",
        "Count of security feature events",
        ["feature"],
    )
    _FEATURE_ENABLED = Gauge(
        "security_feature_enabled",
        "Whether a security feature is enabled (1/0)",
        ["feature"],
    )


def increment_feature_event(feature: str, amount: int = 1) -> None:
    _init_metrics()
    if not _FEATURE_EVENTS:
        return
    _FEATURE_EVENTS.labels(feature=feature).inc(amount)


def set_feature_enabled(feature: str, enabled: bool) -> None:
    _init_metrics()
    if not _FEATURE_ENABLED:
        return
    _FEATURE_ENABLED.labels(feature=feature).set(1 if enabled else 0)


def _counter_value(counter, feature: str) -> int:
    try:
        return int(counter.labels(feature=feature)._value.get())
    except Exception:
        return 0


def get_feature_metrics_snapshot(features: list[str]) -> Dict[str, Dict[str, int]]:
    _init_metrics()
    snapshot: Dict[str, Dict[str, int]] = {}
    for feature in features:
        events = _counter_value(_FEATURE_EVENTS, feature) if _FEATURE_EVENTS else 0
        snapshot[feature] = {"events": events}
    return snapshot
