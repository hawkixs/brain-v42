"""The metrics sidecar logs JSON from its first line when BRAIN_LOG_FORMAT=json.

Measured on red-base 2026-10-12 and replayed on 2026-10-13 (ticket c206929f): the
sidecar built its runtime, whose embedding factory logs `embedding_factory.built`
and `embedding_factory.no_shim_bearer`, before configuring logging, so every start
began with two coloured console lines a JSON follower cannot parse.
"""

from __future__ import annotations

import json

import pytest
import structlog

from brain_v42.metrics import __main__ as sidecar


class _Stop(Exception):
    pass


async def test_lines_logged_while_building_the_runtime_are_json(monkeypatch, capsys) -> None:
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    sidecar.get_settings.cache_clear()
    structlog.reset_defaults()

    def build_runtime_that_logs():
        structlog.get_logger("brain_v42.services.embedding_factory").info(
            "embedding_factory.built", backend="shim"
        )
        raise _Stop

    monkeypatch.setattr(sidecar, "build_metrics_runtime", build_runtime_that_logs)
    try:
        with pytest.raises(_Stop):
            await sidecar.main()
    finally:
        sidecar.get_settings.cache_clear()
        structlog.reset_defaults()

    captured = capsys.readouterr()
    lines = [line for line in (captured.out + captured.err).splitlines() if line.strip()]
    assert lines, "the runtime's log line went nowhere"
    events = [json.loads(line) for line in lines]  # every line, the first included
    assert [event["event"] for event in events] == ["logging.configured", "embedding_factory.built"]


async def test_lines_logged_while_building_reach_the_collector_once_attached(monkeypatch) -> None:
    """The early lines are kept and handed to the real collector's recent-log buffer."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    sidecar.get_settings.cache_clear()
    structlog.reset_defaults()
    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=0)

    def build_runtime_that_logs():
        structlog.get_logger("brain_v42.services.embedding_factory").info(
            "embedding_factory.built", backend="shim"
        )
        return runtime

    monkeypatch.setattr(sidecar, "build_metrics_runtime", build_runtime_that_logs)
    try:
        assert await sidecar.main(asyncio.Event()) == 0
    finally:
        sidecar.get_settings.cache_clear()
        structlog.reset_defaults()

    pushed = [call.args for call in runtime._resources.collector.push_recent_log.call_args_list]
    assert any(
        level == "info" and "embedding_factory.built" in message for level, message in pushed
    ), pushed
