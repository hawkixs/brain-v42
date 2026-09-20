"""Read the PostgreSQL projection state as the graph lag fact."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _PostgresSource(SourceSession, Protocol):
    """Narrow the generic source protocol only at the PostgreSQL probe boundary."""

    session: AsyncSession


class _ProjectionState(Protocol):
    """State fields the fact reads without taking ownership of repository types."""

    pending: int
    ready: int
    claimed: int
    exhausted: int
    oldest_pending_age_seconds: float
    generation: int | None
    armed: bool
    lease_active: bool
    recovery_active: bool


async def _read_projection_state(session: AsyncSession) -> _ProjectionState:
    """Resolve the repository reader only while measuring to preserve package layering."""
    reader = import_module("brain_v42.repositories.pg_graph_ledger")
    return cast(_ProjectionState, await reader.read_projection_state(session))


def _projection_health(state: _ProjectionState) -> bool:
    """Resolve the shared predicate with its reader, after the probe has started."""
    reader = import_module("brain_v42.repositories.pg_graph_ledger")
    return cast(bool, reader.projection_health(state))


class GraphProjectionLagProbe:
    """Publish one stable projection value so readers share its health semantics."""

    name = "graph_projection_lag"
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=15)
    timeout = timedelta(seconds=3)
    briefing = True
    policies: Mapping[str, int] = {"late_after_seconds": 300}
    value_schema = {
        "pending": "int",
        "ready": "int",
        "claimed": "int",
        "exhausted": "int",
        "lag_seconds": "int",
        "generation": "null|int",
        "armed": "bool",
        "lease_active": "bool",
        "recovery_active": "bool",
        "healthy": "bool",
    }

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Translate the shared snapshot without concealing a failed database read."""
        state = await _read_projection_state(cast(_PostgresSource, source).session)
        return {
            "pending": state.pending,
            "ready": state.ready,
            "claimed": state.claimed,
            "exhausted": state.exhausted,
            "lag_seconds": int(state.oldest_pending_age_seconds),
            "generation": state.generation,
            "armed": state.armed,
            "lease_active": state.lease_active,
            "recovery_active": state.recovery_active,
            "healthy": _projection_health(state),
        }
