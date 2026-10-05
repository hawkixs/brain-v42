"""Contracts for the shared PostgreSQL projection state reader."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.repositories.pg_graph_ledger import (
    ProjectionState,
    projection_health,
    read_projection_state,
)


class _FailingSession:
    """Make the database boundary fail without fabricating a query result."""

    async def execute(self, _statement: object) -> object:
        raise PermissionError("projection state denied")


async def test_projection_state_reader_propagates_database_errors() -> None:
    """A probe must raise so the registry can publish an unreadable measurement."""
    with pytest.raises(PermissionError, match="projection state denied"):
        await read_projection_state(_FailingSession())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("armed", "lease_active", "recovery_active", "pending", "exhausted", "age", "expected"),
    [
        (True, True, False, 0, 0, 0.0, True),
        (False, True, False, 0, 0, 0.0, False),
        (True, False, False, 0, 0, 0.0, False),
        (True, True, True, 0, 0, 0.0, False),
        (True, True, False, 1, 1, 0.0, False),
        (True, True, False, 1, 0, 301.0, False),
        (True, True, False, 1, 0, 300.0, True),
    ],
)
def test_projection_health_requires_a_live_lease_no_exhausted_event_and_a_bounded_lag(
    armed: bool,
    lease_active: bool,
    recovery_active: bool,
    pending: int,
    exhausted: int,
    age: float,
    expected: bool,
) -> None:
    """Changing any health condition must make the shared predicate fail closed."""
    state = ProjectionState(
        pending=pending,
        ready=0,
        claimed=0,
        exhausted=exhausted,
        oldest_pending_age_seconds=age,
        generation=None,
        armed=armed,
        lease_active=lease_active,
        recovery_active=recovery_active,
    )
    assert projection_health(state) is expected


async def test_collector_keeps_unavailable_projection_sentinels_on_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics remain readable when their database adapter cannot collect state."""
    settings = MagicMock(embedding_dimension=1024)
    monkeypatch.setattr("brain_v42.metrics.collector.get_settings", lambda: settings)
    engine = MagicMock()
    engine.sync_engine.pool.size.return_value = 5
    engine.sync_engine.pool.checkedout.return_value = 0
    engine.sync_engine.pool.checkedin.return_value = 1
    engine.sync_engine.pool.overflow.return_value = -4
    engine.sync_engine.pool._max_overflow = 10
    collector = MetricsCollector(
        engine=engine,
        session_factory=MagicMock(side_effect=PermissionError("database unavailable")),
    )

    result = await collector.collect_db_stats()

    assert result["graph_outbox"]["available"] is False
    assert result["graph_outbox"]["projector"]["healthy"] is False
