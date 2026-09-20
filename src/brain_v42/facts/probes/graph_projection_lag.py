"""Read the PostgreSQL projection state as the graph lag fact.

The query and the health predicate are the ones the metrics collector uses
(`pg_graph_ledger.read_projection_state`, `projection_health`): one source,
two readers, so the briefing, the tool and a claim of lot B agree on what
"healthy" means. The dependency is a plain import — the layering DAG must SEE
the edge `facts -> repositories`, which is allowed; hiding it behind a
dynamic import would hide it from the fitness function that keeps the graph
acyclic.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.repositories.pg_graph_ledger import projection_health, read_projection_state


class GraphProjectionLagProbe:
    """Publish one stable projection value so readers share its health semantics."""

    name: str = "graph_projection_lag"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=15)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = True
    policies: Mapping[str, int] = {"late_after_seconds": 300}
    value_schema: Mapping[str, ValueType] = {
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
        state = await read_projection_state(cast(PostgresSourceSession, source).session)
        return {
            "pending": state.pending,
            "ready": state.ready,
            "claimed": state.claimed,
            "exhausted": state.exhausted,
            # Whole seconds, never negative: clock skew between `created_at` and
            # the server's `now` must not render as a lead.
            "lag_seconds": max(0, int(state.oldest_pending_age_seconds)),
            "generation": state.generation,
            "armed": state.armed,
            "lease_active": state.lease_active,
            "recovery_active": state.recovery_active,
            "healthy": projection_health(state),
        }
