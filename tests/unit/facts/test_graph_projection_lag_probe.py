"""Unit contracts for the graph projection lag fact probe."""

from __future__ import annotations

from brain_v42.facts import FactTarget
from brain_v42.facts.probe import check_value_schema
from brain_v42.facts.probes.graph_projection_lag import GraphProjectionLagProbe
from brain_v42.repositories.pg_graph_ledger import ProjectionState


class _FakeSource:
    """Expose the only source attribute the PostgreSQL probe may consume."""

    def __init__(self) -> None:
        self.session = object()


async def test_probe_declares_and_measures_the_complete_integer_value_shape(
    monkeypatch,
) -> None:
    """A float lag or synthetic generation would invalidate the published fact value."""
    source = _FakeSource()
    state = ProjectionState(
        pending=4,
        ready=2,
        claimed=1,
        exhausted=3,
        oldest_pending_age_seconds=12.9,
        generation=None,
        armed=False,
        lease_active=True,
        recovery_active=False,
    )

    async def read_state(session: object) -> ProjectionState:
        assert session is source.session
        return state

    monkeypatch.setattr(
        "brain_v42.facts.probes.graph_projection_lag._read_projection_state",
        read_state,
    )
    probe = GraphProjectionLagProbe()

    value = await probe.measure(source)  # type: ignore[arg-type]

    assert probe.name == "graph_projection_lag"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl.total_seconds() == 15
    assert probe.timeout.total_seconds() == 3
    assert probe.briefing is True
    assert probe.policies == {"late_after_seconds": 300}
    assert probe.value_schema == {
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
    assert value == {
        "pending": 4,
        "ready": 2,
        "claimed": 1,
        "exhausted": 3,
        "lag_seconds": 12,
        "generation": None,
        "armed": False,
        "lease_active": True,
        "recovery_active": False,
        "healthy": False,
    }
    check_value_schema(value, probe.value_schema)
