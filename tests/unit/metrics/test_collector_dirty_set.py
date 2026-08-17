"""Task 2.2 — Dirty-set tracking (Plan 2, H7b).

Tests for the _dirty_agents set and get_flush_data(dirty_only=True) contract:
  - record_tool_call adds agent to _dirty_agents (post-remap name)
  - get_flush_data(dirty_only=True) returns only dirty agents + "_process" unconditionally
  - mark_flushed(agents) removes specific agents from dirty set (difference_update)
  - process-global record_* methods do NOT touch _dirty_agents
  - dirty_only=False is unchanged (back-compat)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_dirty_only_single_agent_returns_that_agent_and_process(
    collector: MetricsCollector,
) -> None:
    """Record agent A → dirty_only=True returns {A, _process}."""
    collector.record_tool_call("brain_search", 50.0, agent="red-shrik")

    fd = collector.get_flush_data(dirty_only=True)

    assert set(fd.keys()) == {"red-shrik", "_process"}
    # Agent row still has the tools sub-dict
    assert "tools" in fd["red-shrik"]
    # _process always has its global blocks
    assert "embedding" in fd["_process"]


def test_dirty_only_after_mark_flushed_excludes_cleared_agent(
    collector: MetricsCollector,
) -> None:
    """After mark_flushed({A}), recording B → dirty_only=True returns {B, _process} not A."""
    collector.record_tool_call("brain_search", 50.0, agent="red-shrik")
    collector.mark_flushed({"red-shrik"})

    collector.record_tool_call("brain_learn", 30.0, agent="red-codex")

    fd = collector.get_flush_data(dirty_only=True)

    assert set(fd.keys()) == {"red-codex", "_process"}
    assert "red-shrik" not in fd


def test_dirty_only_empty_dirty_set_still_returns_process(
    collector: MetricsCollector,
) -> None:
    """Pure-embedding cycle: _dirty_agents empty → dirty_only=True returns {"_process"} only."""
    collector.record_embedding_request(20.0)

    fd = collector.get_flush_data(dirty_only=True)

    assert set(fd.keys()) == {"_process"}
    assert fd["_process"]["embedding"]["total_requests"] == 1


def test_dirty_only_false_unchanged_back_compat(
    collector: MetricsCollector,
) -> None:
    """dirty_only=False (default) returns ALL agents + _process — back-compat."""
    collector.record_tool_call("brain_search", 50.0, agent="red-shrik")
    collector.record_tool_call("brain_learn", 30.0, agent="red-codex")
    # Flush shrik, but with dirty_only=False we still see both
    collector.mark_flushed({"red-shrik"})

    fd = collector.get_flush_data(dirty_only=False)

    assert "red-shrik" in fd
    assert "red-codex" in fd
    assert "_process" in fd


def test_process_global_records_do_not_dirty_any_agent(
    collector: MetricsCollector,
) -> None:
    """record_embedding_request / record_reranker_call / record_graph_query / record_cost
    / record_decay_stats must NOT add anything to _dirty_agents."""
    collector.record_embedding_request(10.0)
    collector.record_reranker_call(5.0, 3)
    collector.record_graph_query(4.0)
    collector.record_cost("sonnet-4.5", 0.01)
    collector.record_decay_stats(stale_count=1, archived_count=0, access_log_size=10)

    fd = collector.get_flush_data(dirty_only=True)

    # Only _process, no real agent entries
    assert set(fd.keys()) == {"_process"}


def test_spoofed_process_agent_marks_collision_bucket_dirty(
    collector: MetricsCollector,
) -> None:
    """A spoofed agent="_process" is remapped to "_process_collision" BEFORE
    dirtying — the dirty set must contain "_process_collision", not "_process"."""
    collector.record_tool_call("brain_search", 50.0, agent="_process")

    fd = collector.get_flush_data(dirty_only=True)

    assert "_process_collision" in fd
    assert "_process" in fd  # always present as global row
    # The global _process row is NOT a tool-bearing row
    assert "tools" not in fd["_process"]
    assert "tools" in fd["_process_collision"]


def test_mark_flushed_difference_update_preserves_concurrent_dirty(
    collector: MetricsCollector,
) -> None:
    """mark_flushed uses difference_update: agents that became dirty DURING a flush
    are not lost. Simulate: A+B dirty, flush A, then B still dirty."""
    collector.record_tool_call("brain_search", 50.0, agent="agent-a")
    collector.record_tool_call("brain_learn", 30.0, agent="agent-b")

    # Simulate flusher only acks agent-a
    collector.mark_flushed({"agent-a"})

    fd = collector.get_flush_data(dirty_only=True)

    assert "agent-b" in fd
    assert "agent-a" not in fd


def test_get_flush_data_idempotent_does_not_clear_dirty_set(
    collector: MetricsCollector,
) -> None:
    """Calling get_flush_data(dirty_only=True) must NOT mutate _dirty_agents.
    Reading is idempotent — only mark_flushed clears entries."""
    collector.record_tool_call("brain_search", 50.0, agent="red-shrik")

    fd1 = collector.get_flush_data(dirty_only=True)
    fd2 = collector.get_flush_data(dirty_only=True)

    assert set(fd1.keys()) == set(fd2.keys()) == {"red-shrik", "_process"}
