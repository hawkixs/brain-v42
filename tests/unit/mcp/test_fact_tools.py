"""MCP contracts for closed-catalogue measured facts."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.facts import (
    FactDescriptor,
    FactRegistry,
    FactTarget,
    Measured,
    SourceIdentity,
    Unreadable,
)
from brain_v42.facts.registry import UnknownFactError
from brain_v42.mcp.server import prepare_tools_for_transport
from brain_v42.mcp.tool_catalog import ToolCatalogProfile, apply_tool_catalog_profile


def test_fact_tools_module_exposes_the_fact_registration_boundary() -> None:
    """Fact access remains a separately composable MCP registration root."""
    spec = importlib.util.find_spec("brain_v42.mcp.tools.fact_tools")

    assert spec is not None

    from brain_v42.mcp.tools import fact_tools

    assert callable(getattr(fact_tools, "register_fact_tools", None))


def _measured(name: str = "alpha") -> Measured:
    return Measured.from_value(
        fact=name,
        definition_version=2,
        target=FactTarget.PRODUCTION,
        source=SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5432),
        value={"pending": 3},
        observation_id=uuid4(),
        measured_at=datetime(2026, 9, 20, 12, 30, 45, tzinfo=UTC),
        duration_ms=7,
        ttl_seconds=15,
    )


def _unreadable(name: str = "beta") -> Unreadable:
    return Unreadable(
        fact=name,
        definition_version=1,
        target=FactTarget.HOST,
        error_code="timeout",
        where="probe",
        observation_id=uuid4(),
        measured_at=datetime(2026, 9, 20, 12, 31, tzinfo=UTC),
        duration_ms=3,
        ttl_seconds=30,
        source_kind="probe",
    )


class FakeFactRegistry:
    """Keep the MCP boundary deterministic without replacing its registered tools."""

    def __init__(self, *, unreadable: bool = False) -> None:
        self._descriptors = {
            "alpha": FactDescriptor(
                name="alpha",
                definition_version=2,
                target=FactTarget.PRODUCTION,
                ttl_seconds=15,
                timeout_seconds=3,
                queue_timeout_seconds=2,
                deadline_seconds=5,
                briefing=True,
                policies={"late_after_seconds": 300},
                value_schema={"pending": "int"},
            ),
            "beta": FactDescriptor(
                name="beta",
                definition_version=1,
                target=FactTarget.HOST,
                ttl_seconds=30,
                timeout_seconds=2,
                queue_timeout_seconds=2,
                deadline_seconds=4,
                briefing=False,
                policies={"late_after_seconds": 90},
                value_schema={"pending": "int"},
            ),
        }
        self._results = {"alpha": _measured(), "beta": _unreadable()}
        if unreadable:
            self._results["alpha"] = _unreadable("alpha")
        self._cached: dict[str, Measured | Unreadable] = {}
        self.measure_calls: list[tuple[str, timedelta | None]] = []

    def names(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    def describe(self, name: str) -> FactDescriptor:
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise UnknownFactError(name) from exc

    def briefing_names(self) -> tuple[str, ...]:
        return tuple(name for name, descriptor in self._descriptors.items() if descriptor.briefing)

    def cached(self, name: str) -> Measured | Unreadable | None:
        self.describe(name)
        return self._cached.get(name)

    async def measure(
        self, name: str, *, max_age: timedelta | None = None
    ) -> Measured | Unreadable:
        self.describe(name)
        self.measure_calls.append((name, max_age))
        measurement = self._results[name]
        self._cached[name] = measurement
        return measurement


async def _app(registry: FakeFactRegistry, profile: ToolCatalogProfile) -> FastMCP:
    from brain_v42.mcp.tools.fact_tools import register_fact_tools

    app = FastMCP("facts", mask_error_details=True)
    register_fact_tools(app, cast(FactRegistry, registry))
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    return app


async def _call(app: FastMCP, profile: str, name: str, arguments: dict[str, object]) -> object:
    async with Client(app) as client:
        if profile == "compact":
            return await client.call_tool(
                "brain_call_tool", {"name": name, "arguments": arguments}, raise_on_error=False
            )
        return await client.call_tool(name, arguments, raise_on_error=False)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_list_is_ordered_and_reads_only_cached_observations(profile: str) -> None:
    """Listing exposes declared metadata and last evidence without measuring probes."""
    registry = FakeFactRegistry()
    app = await _app(registry, profile)

    first = await _call(app, profile, "brain_fact_list", {})

    assert first.data == [
        {
            "name": "alpha",
            "definition_version": 2,
            "target": "production",
            "ttl_seconds": 15,
            "timeout_seconds": 3,
            "deadline_seconds": 5,
            "briefing": True,
            "policies": {"late_after_seconds": 300},
            "last": None,
        },
        {
            "name": "beta",
            "definition_version": 1,
            "target": "host",
            "ttl_seconds": 30,
            "timeout_seconds": 2,
            "deadline_seconds": 4,
            "briefing": False,
            "policies": {"late_after_seconds": 90},
            "last": None,
        },
    ]
    assert registry.measure_calls == []

    measured = await _call(app, profile, "brain_fact_get", {"name": "alpha"})
    second = await _call(app, profile, "brain_fact_list", {})

    assert measured.data["status"] == "measured"
    assert second.data[0]["last"] == {
        "status": "measured",
        "measured_at": "2026-09-20T12:30:45Z",
        "source_kind": "probe",
        "observation_id": str(registry._results["alpha"].observation_id),
    }


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_get_returns_a_measured_json_payload(profile: str) -> None:
    """The public result retains parsed measured value and a wire-format UTC instant."""
    app = await _app(FakeFactRegistry(), profile)

    result = await _call(app, profile, "brain_fact_get", {"name": "alpha"})

    assert result.data["status"] == "measured"
    assert result.data["source"] == {
        "system_identifier": "7612696091383607335",
        "database": "brain",
        "server_addr": "127.0.0.1",
        "server_port": 5432,
    }
    assert result.data["value"] == {"pending": 3}
    assert result.data["measured_at"] == "2026-09-20T12:30:45Z"


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_get_forwards_zero_as_an_exact_forced_refresh_age(profile: str) -> None:
    """Zero must reach the registry as zero so the registry can charge its refresh budget."""
    registry = FakeFactRegistry()
    app = await _app(registry, profile)

    result = await _call(app, profile, "brain_fact_get", {"name": "alpha", "max_age_seconds": 0})

    assert result.data["status"] == "measured"
    assert registry.measure_calls == [("alpha", timedelta(0))]


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_get_rejects_negative_age_before_calling_the_registry(profile: str) -> None:
    """A malformed age cannot consume a probe, cache lookup, or refresh-budget token."""
    registry = FakeFactRegistry()
    app = await _app(registry, profile)

    result = await _call(app, profile, "brain_fact_get", {"name": "alpha", "max_age_seconds": -1})

    assert result.is_error
    assert "invalid_argument" in str(result.content)
    assert registry.measure_calls == []


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_get_names_the_closed_catalogue_for_an_unknown_fact(profile: str) -> None:
    """An absent fact is actionable without disclosing any source connection detail."""
    registry = FakeFactRegistry()
    app = await _app(registry, profile)

    result = await _call(app, profile, "brain_fact_get", {"name": "missing"})

    assert result.is_error
    rendered = str(result.content)
    assert "unknown_fact" in rendered
    assert "alpha" in rendered
    assert "beta" in rendered


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_fact_get_returns_an_unreadable_measurement_as_data(profile: str) -> None:
    """A bounded probe failure is evidence, rather than an MCP execution failure."""
    app = await _app(FakeFactRegistry(unreadable=True), profile)

    result = await _call(app, profile, "brain_fact_get", {"name": "alpha"})

    assert not result.is_error
    assert result.data["status"] == "unreadable"
    assert result.data["error_code"] == "timeout"


async def test_fact_tools_publish_read_only_annotations() -> None:
    """Clients must be able to classify both fact operations as safe reads."""
    app = await _app(FakeFactRegistry(), "native")

    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("brain_fact_list", "brain_fact_get"):
        tool = tools[name]
        assert tool.annotations.readOnlyHint is True
        assert tool.meta["fastmcp"]["version"] == "1.0"


async def test_compact_catalogue_search_finds_both_fact_tools() -> None:
    """Compact callers can discover facts before sending one through the gateway."""
    app = await _app(FakeFactRegistry(), "compact")

    async with Client(app) as client:
        found: set[str] = set()
        for name in ("brain_fact_list", "brain_fact_get"):
            result = await client.call_tool("brain_find_tool", {"query": name})
            found.update(item["name"] for item in result.data)

    assert {"brain_fact_list", "brain_fact_get"} <= found
