"""Tests for the recent-log structlog processor."""

from __future__ import annotations

from unittest.mock import MagicMock

from brain_v42.metrics.recent_log import RecentLogProcessor


def test_processor_pushes_entry_to_collector() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "tool.invoked", "tool": "brain_search", "level": "info"}
    proc(None, "info", event_dict)
    collector.push_recent_log.assert_called_once_with("info", "tool.invoked tool=brain_search")


def test_processor_formats_key_value_pairs_deterministically() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "thing", "b": 2, "a": 1, "level": "warn"}
    proc(None, "warn", event_dict)
    msg = collector.push_recent_log.call_args[0][1]
    assert msg == "thing a=1 b=2"


def test_processor_returns_event_dict_unchanged() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "thing", "level": "info"}
    result = proc(None, "info", event_dict)
    assert result is event_dict


def test_processor_skips_below_threshold() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector, min_level="warn")
    proc(None, "info", {"event": "quiet", "level": "info"})
    collector.push_recent_log.assert_not_called()
    proc(None, "warn", {"event": "loud", "level": "warn"})
    collector.push_recent_log.assert_called_once()


def test_processor_truncates_long_messages() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector, max_len=40)
    proc(None, "info", {"event": "x" * 100, "level": "info"})
    msg = collector.push_recent_log.call_args[0][1]
    assert len(msg) <= 40
    assert msg.endswith("…")
