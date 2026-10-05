"""The server's own net for tracers whose connection never terminated (09d2b56e).

The transport hook closes a tracer when its connection ends; a crashed or
killed connection never reaches it. This loop runs the 4 h observation rule in
the server that opened the tracers, so their closing no longer depends on a
Dream night.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.models.brain_session import AGENT_INACTIVE_AFTER
from brain_v42.services.agent_trace_net import AgentTraceNet, agent_trace_net_is_armed

_PG = "postgresql+asyncpg://u:p@localhost:5433/db"


class _Closer:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[timedelta] = []
        self._fail_first = fail_first

    async def __call__(self, *, inactive_after: timedelta) -> list[UUID]:
        self.calls.append(inactive_after)
        if self._fail_first and len(self.calls) == 1:
            raise RuntimeError("database down")
        return [uuid4()]


@pytest.mark.asyncio
async def test_a_sweep_applies_the_documented_threshold() -> None:
    closer = _Closer()
    net = AgentTraceNet(close_inactive=closer, interval_seconds=3600)

    assert await net.sweep_once() == 1
    assert closer.calls == [AGENT_INACTIVE_AFTER]


@pytest.mark.asyncio
async def test_a_failed_sweep_does_not_end_the_loop() -> None:
    closer = _Closer(fail_first=True)
    net = AgentTraceNet(close_inactive=closer, interval_seconds=0)

    await net.start()
    for _ in range(50):
        if len(closer.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(net.stop(), timeout=2)

    assert len(closer.calls) >= 2


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels() -> None:
    net = AgentTraceNet(close_inactive=_Closer(), interval_seconds=3600)

    await net.start()
    first = net._task
    await net.start()

    assert net._task is first
    await asyncio.wait_for(net.stop(), timeout=2)
    assert first is not None and first.done()


@pytest.mark.parametrize(
    ("overrides", "armed"),
    [
        ({}, True),
        ({"brain_session_auto_open_enabled": False}, False),
        ({"mcp_http_stateless": True}, False),
        ({"brain_mcp_transport": "stdio"}, False),
    ],
)
def test_the_net_runs_exactly_where_tracers_are_opened(
    overrides: dict[str, Any], armed: bool
) -> None:
    """Tracers exist only over stateful HTTP with auto-open on; so does their net."""
    from brain_v42.config import Settings

    values: dict[str, Any] = {
        "brain_mcp_transport": "http",
        "mcp_http_stateless": False,
        "brain_session_auto_open_enabled": True,
        **overrides,
    }
    settings = Settings(POSTGRES_URL=_PG, **values)

    assert agent_trace_net_is_armed(settings) is armed


def test_the_lifecycle_starts_the_net_where_armed_and_stops_it_with_the_stack() -> None:
    import inspect

    import brain_v42.mcp.server as server_mod

    source = inspect.getsource(server_mod.app_lifecycle)
    assert "agent_trace_net_is_armed(settings)" in source
    assert "cleanup.push_async_callback(agent_trace_net.stop)" in source
