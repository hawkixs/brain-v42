from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brain_v42.mcp.activity_reporter import ActivityReporter


def _body(calls: int = 1) -> str:
    return json.dumps({"observations": [{"actor": "test", "calls": calls}]})


@pytest.mark.asyncio
async def test_post_failure_counts_observations_and_warns_once_per_outage() -> None:
    reporter = ActivityReporter("http://unused")
    reporter._client = AsyncMock()
    reporter._client.post.side_effect = OSError("offline")
    with patch("brain_v42.mcp.activity_reporter.logger.warning") as warning:
        await reporter._post(_body(3), 3)
        await reporter._post(_body(2), 2)
    assert reporter.lost == 5
    warning.assert_called_once()


@pytest.mark.asyncio
async def test_refused_counts_calls_inside_observations() -> None:
    reporter = ActivityReporter("http://unused")
    reporter._client = AsyncMock()
    reporter._client.post.return_value = SimpleNamespace(is_success=False, status_code=404)
    await reporter._post(_body(4), 4)
    assert reporter.refused == 4
    assert reporter.lost == 4
