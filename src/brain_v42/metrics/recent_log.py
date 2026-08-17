"""Structlog processor that mirrors log events into the collector's recent-log buffer.

Wires structured log events into the in-memory deque exposed by
``MetricsCollector.get_recent_log()``. Intended to be inserted in the
structlog processor chain before the renderer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brain_v42.metrics.collector import MetricsCollector


_LEVEL_ORDER = {
    "debug": 0,
    "info": 1,
    "warn": 2,
    "warning": 2,
    "err": 3,
    "error": 3,
    "critical": 4,
}


class RecentLogProcessor:
    """Structlog processor that forwards events to the collector's buffer."""

    def __init__(
        self,
        collector: MetricsCollector,
        min_level: str = "info",
        max_len: int = 200,
    ) -> None:
        self._collector = collector
        self._min = _LEVEL_ORDER.get(min_level, 1)
        self._max_len = max_len

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        level = event_dict.get("level", method_name)
        if _LEVEL_ORDER.get(level, 1) < self._min:
            return event_dict
        event = event_dict.get("event", "")
        extras = sorted(
            (k, v)
            for k, v in event_dict.items()
            if k not in ("event", "level", "timestamp", "logger")
        )
        parts = [event, *(f"{k}={v}" for k, v in extras)]
        msg = " ".join(p for p in parts if p)
        if len(msg) > self._max_len:
            msg = msg[: self._max_len - 1] + "…"
        self._collector.push_recent_log(level if level != "warning" else "warn", msg)
        return event_dict
