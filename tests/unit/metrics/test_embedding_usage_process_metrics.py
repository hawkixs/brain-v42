"""Cross-process aggregation of provider-reported embedding usage."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _row(
    agent_name: str,
    usage: Any,
    *,
    tool_stats: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    return (agent_name, 1, None, None, tool_stats or {}, {"usage": usage}, 0, True)


def _legacy_process_row() -> tuple[Any, ...]:
    """A `_process` row written before usage existed: no `usage` key at all."""
    return ("_process", 2, None, None, {}, {"total_requests": 4}, 0, True)


async def _collect(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _Result:
        def all(self) -> list[tuple[Any, ...]]:
            return rows

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            return _Result()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_Session())  # type: ignore[attr-defined]
    result: dict[str, Any] = await mixin.collect_process_metrics()
    return result


@pytest.mark.asyncio
async def test_collect_process_metrics_uses_only_process_rows_for_usage() -> None:
    result = await _collect(
        [
            _row(
                "_process",
                {
                    "read": {"total_tokens": 3, "reported_requests": 1},
                    "write": {"total_tokens": 5, "reported_requests": 1},
                },
            ),
            _row(
                "_process",
                {"write": {"total_tokens": 7, "reported_requests": 1}},
            ),
            _legacy_process_row(),
            _row(
                "agent-a",
                {"read": {"total_tokens": 999, "reported_requests": 999}},
            ),
        ]
    )

    assert result["embedding"]["usage"] == {
        "read": {"total_tokens": 3, "reported_requests": 1},
        "write": {"total_tokens": 12, "reported_requests": 2},
    }


@pytest.mark.asyncio
async def test_a_malformed_usage_value_costs_its_own_row_not_the_whole_aggregate() -> None:
    """A JSON `null` where a mapping belongs must not wipe tools and agents.

    The aggregation runs under one `try`, and its fallback is the EMPTY aggregate:
    an `AttributeError` on one row's usage would have blanked the whole
    cross-process view, tools and agents included.
    """
    result = await _collect(
        [
            _row("_process", None),
            _row(
                "_process",
                {"read": None, "write": {"total_tokens": 2, "reported_requests": 1}},
            ),
            _row(
                "agent-a",
                {},
                tool_stats={"brain_search": {"calls": 3, "errors": 0, "total_latency": 0.3}},
            ),
        ]
    )

    assert result["embedding"]["usage"] == {
        "read": {"total_tokens": 0, "reported_requests": 0},
        "write": {"total_tokens": 2, "reported_requests": 1},
    }
    assert result["tools"]["brain_search"]["calls"] == 3
    assert result["by_agent"]["agent-a"]["calls"] == 3
