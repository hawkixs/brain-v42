"""Cross-process aggregation of provider-reported embedding usage."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _row(agent_name: str, usage: dict[str, Any]) -> tuple[Any, ...]:
    return (agent_name, 1, None, None, {}, {"usage": usage}, 0, True)


@pytest.mark.asyncio
async def test_collect_process_metrics_uses_only_process_rows_for_usage() -> None:
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _Result:
        def all(self) -> list[tuple[Any, ...]]:
            return [
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
                _row(
                    "agent-a",
                    {"read": {"total_tokens": 999, "reported_requests": 999}},
                ),
            ]

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            return _Result()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_Session())  # type: ignore[attr-defined]

    result = await mixin.collect_process_metrics()
    assert result["embedding"]["usage"] == {
        "read": {"total_tokens": 3, "reported_requests": 1},
        "write": {"total_tokens": 12, "reported_requests": 2},
    }
