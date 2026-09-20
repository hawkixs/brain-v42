"""Read-only MCP access to the closed measured-facts catalogue."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastmcp import FastMCP

from brain_v42.facts.model import Measurement, measurement_to_json
from brain_v42.facts.registry import FactRegistry, UnknownFactError
from brain_v42.mcp.facts_transport import _FactsRegistry
from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS

type FactToolErrorCode = Literal["invalid_argument", "unknown_fact"]


class FactToolError(Exception):
    """Carry only safe, operator-actionable errors across the masked MCP boundary."""

    def __init__(self, code: FactToolErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _last_measurement(measurement: Measurement | None) -> dict[str, object] | None:
    """Publish just enough last-observation context to avoid exposing probe internals."""
    if measurement is None:
        return None
    payload = measurement_to_json(measurement)
    return {
        "status": payload["status"],
        "measured_at": payload["measured_at"],
        "source_kind": payload["source_kind"],
        "observation_id": payload["observation_id"],
    }


def register_fact_tools(mcp: FastMCP, registry: FactRegistry) -> None:
    """Register read-only facts without giving MCP callers direct probe access."""
    facts = _FactsRegistry(mcp)

    @facts.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_fact_list() -> list[dict[str, object]]:
        """List declared facts and cached observations; this tool measures nothing."""
        descriptors: list[dict[str, object]] = []
        for name in registry.names():
            descriptor = registry.describe(name)
            descriptors.append(
                {
                    "name": descriptor.name,
                    "definition_version": descriptor.definition_version,
                    "target": descriptor.target.value,
                    "ttl_seconds": descriptor.ttl_seconds,
                    "timeout_seconds": descriptor.timeout_seconds,
                    "deadline_seconds": descriptor.deadline_seconds,
                    "briefing": descriptor.briefing,
                    "policies": dict(descriptor.policies),
                    "last": _last_measurement(registry.cached(name)),
                }
            )
        return descriptors

    @facts.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_fact_get(name: str, max_age_seconds: int | None = None) -> dict[str, object]:
        """Read one fact through its probe budget and never bypass its timeout boundary."""
        if max_age_seconds is not None and max_age_seconds < 0:
            raise FactToolError("invalid_argument", "max_age_seconds must be >= 0")
        max_age = None if max_age_seconds is None else timedelta(seconds=max_age_seconds)
        try:
            measurement = await registry.measure(name, max_age=max_age)
        except UnknownFactError:
            catalogue = ", ".join(registry.names())
            raise FactToolError(
                "unknown_fact", f"{name!r} is not registered; available facts: {catalogue}"
            ) from None
        return measurement_to_json(measurement)
