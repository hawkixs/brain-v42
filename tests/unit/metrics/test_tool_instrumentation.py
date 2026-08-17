"""Metrics instrumentation must not mutate ``mcp.tool`` (ticket c352eaaa).

The historical wiring replaced ``FastMCP.tool`` with a wrapper at registration
time (``brain_tools.register_tools``). That coupled metrics to declaration
order, mutated a third-party object's method, and made instrumentation depend
on ``metrics_collector`` being passed to one particular register function.

Instrumenting the registered tools after the fact removes all three, and the
gateway question the ticket raised answers itself: ``_list_tools()`` returns
only real tools, so the compact profile's ``brain_call_tool`` /
``brain_find_tool`` are never counted — no name blacklist to keep in sync.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import AuthorizationError, ToolError

from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.tool_instrumentation import instrument_registered_tools


@pytest.fixture()
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def _recorded(collector: MetricsCollector) -> dict:
    """Flatten every agent bucket into one {tool: stats} mapping."""
    merged: dict = {}
    for per_agent in collector._tool_stats.values():
        merged.update(per_agent)
    return merged


async def test_registered_tool_records_a_call_under_its_real_name(
    collector: MetricsCollector,
) -> None:
    app = FastMCP("metrics-basic", mask_error_details=True)

    @app.tool()
    async def real_tool() -> str:
        return "ok"

    await instrument_registered_tools(app, collector)

    async with Client(app) as client:
        await client.call_tool("real_tool", {})

    assert "real_tool" in _recorded(collector)


async def test_tool_failure_is_recorded_as_an_error(collector: MetricsCollector) -> None:
    app = FastMCP("metrics-error", mask_error_details=True)

    @app.tool()
    async def failing() -> str:
        raise RuntimeError("boom")

    await instrument_registered_tools(app, collector)

    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("failing", {})

    assert _recorded(collector)["failing"]["errors"] == 1


async def test_authorization_error_propagates_and_is_counted(
    collector: MetricsCollector,
) -> None:
    """Constraint 2 of the ticket: the AuthorizationError path must be preserved."""
    app = FastMCP("metrics-authz", mask_error_details=True)

    @app.tool()
    async def denied() -> str:
        raise AuthorizationError("denied")

    await instrument_registered_tools(app, collector)

    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("denied", {})

    assert _recorded(collector)["denied"]["errors"] == 1


async def test_compact_gateway_is_not_counted_as_a_tool(collector: MetricsCollector) -> None:
    """The gateway re-enters the chain; only the real tool may be recorded."""
    app = FastMCP("metrics-gateway", mask_error_details=True)

    @app.tool()
    async def real_tool() -> str:
        return "ok"

    await instrument_registered_tools(app, collector)
    apply_tool_catalog_profile(app, "compact")

    async with Client(app) as client:
        await client.call_tool("brain_call_tool", {"name": "real_tool", "arguments": {}})

    names = _recorded(collector)
    assert "brain_call_tool" not in names, f"the gateway leaked into tool metrics: {names}"
    assert "brain_find_tool" not in names, f"the gateway leaked into tool metrics: {names}"
    assert "real_tool" in names


async def test_instrumentation_is_idempotent(collector: MetricsCollector) -> None:
    """A second pass must not stack wrappers and double-count every call."""
    app = FastMCP("metrics-idempotent", mask_error_details=True)

    @app.tool()
    async def real_tool() -> str:
        return "ok"

    first = await instrument_registered_tools(app, collector)
    second = await instrument_registered_tools(app, collector)

    assert first == ("real_tool",)
    assert second == (), f"second pass re-wrapped already-instrumented tools: {second}"

    async with Client(app) as client:
        await client.call_tool("real_tool", {})

    calls = _recorded(collector)["real_tool"]["calls"]
    assert calls == 1, f"one call was counted {calls} times"


def test_register_tools_no_longer_mutates_the_fastmcp_tool_decorator() -> None:
    """The monkey-patch of a third-party method is the defect being removed."""
    import inspect

    from brain_v42.mcp.tools import brain_tools

    source = inspect.getsource(brain_tools.register_tools)
    assert "mcp.tool = " not in source, (
        "register_tools still reassigns mcp.tool — the monkey-patch was not removed"
    )
