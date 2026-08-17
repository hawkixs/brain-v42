"""Neo4j half of the crash-safe graph projection recovery protocol."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_graph_ledger import ProjectionRecoveryLease
from brain_v42.services.neo4j_graph_projection_writer import Neo4jGraphProjectionWriter


class _Result:
    def __init__(self, record: dict[str, Any] | None) -> None:
        self._record = record

    async def single(self) -> dict[str, Any] | None:
        return self._record


class _Transaction:
    def __init__(self, *records: dict[str, Any] | None) -> None:
        self._records = list(records)
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.committed = False
        self.rolled_back = False
        self.cancelled = False
        self._closed = False
        self.cancel_on_run = False

    async def run(self, query: str, parameters: dict[str, Any]) -> _Result:
        if self.cancel_on_run:
            raise asyncio.CancelledError
        self.queries.append((str(query), dict(parameters)))
        record = self._records.pop(0) if self._records else None
        return _Result(record)

    async def commit(self) -> None:
        self.committed = True
        self._closed = True

    async def rollback(self) -> None:
        self.rolled_back = True
        self._closed = True

    def cancel(self) -> None:
        self.cancelled = True
        self._closed = True

    def closed(self) -> bool:
        return self._closed


class _Session:
    def __init__(self, transaction: _Transaction) -> None:
        self._transaction = transaction

    async def begin_transaction(self, **_kwargs: Any) -> _Transaction:
        return self._transaction


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _Driver:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction
        self.sessions = 0

    def session(self) -> _SessionContext:
        self.sessions += 1
        return _SessionContext(_Session(self.transaction))


def _state(*, generation: int = 8, phase: str = "prepared") -> Any:
    return ProjectionRecoveryLease(
        recovery_id=uuid4(),
        owner_id="recovery-owner",
        generation=generation,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        phase=phase,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_reset_wipes_projection_and_installs_the_recovery_marker_atomically() -> None:
    state = _state()
    transaction = _Transaction(
        {"accepted": True, "current_generation": 7},
        {"deleted_nodes": 4},
        {"current_generation": 8},
    )
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.reset_for_recovery(state)

    assert (result.accepted, result.current_generation, result.deleted_nodes) == (True, 8, 4)
    assert transaction.committed is True
    assert transaction.rolled_back is False
    assert len(transaction.queries) == 3
    observe_query, delete_query, install_query = [query for query, _ in transaction.queries]
    assert "fence.recovery_id = $recovery_id" in observe_query
    assert "fence.generation <= $generation" in observe_query
    assert "fence.recovery_id IS NULL" in observe_query
    assert "fence.generation < $generation" in observe_query
    assert (
        "fence.generation <= $generation"
        not in observe_query.split("fence.recovery_id IS NULL", maxsplit=1)[1]
    )
    assert "NOT business:BrainProjectionFence" not in delete_query
    for label in (
        "Project",
        "Domain",
        "Decision",
        "Learning",
        "Snippet",
        "Runbook",
        "ADR",
        "Feature",
        "Plan",
        "BrainProjectionCursor",
    ):
        assert f"business:{label}" in delete_query
    assert "DETACH DELETE business" in delete_query
    assert "MERGE (fence:BrainProjectionFence" in install_query
    assert "fence.recovery_id = $recovery_id" in install_query
    for _query, params in transaction.queries:
        assert params["recovery_id"] == str(state.recovery_id)
        assert params["owner_id"] == state.owner_id
        assert params["generation"] == state.generation


@pytest.mark.asyncio
async def test_reset_rejects_a_finalized_same_or_newer_generation_without_deleting() -> None:
    state = _state()
    transaction = _Transaction({"accepted": False, "current_generation": 8})
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.reset_for_recovery(state)

    assert result.accepted is False
    assert result.current_generation == 8
    assert result.deleted_nodes == 0
    assert transaction.rolled_back is True
    assert transaction.committed is False
    assert len(transaction.queries) == 1


@pytest.mark.asyncio
async def test_reset_accepts_neo_ready_phase_for_rebuild_on_doubt() -> None:
    state = _state(phase="neo_ready")
    transaction = _Transaction(
        {"accepted": True, "current_generation": -1},
        {"deleted_nodes": 0},
        {"current_generation": state.generation},
    )
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.reset_for_recovery(state)

    assert result.accepted is True
    assert result.current_generation == state.generation
    assert transaction.committed is True
    observe_query, observe_params = transaction.queries[0]
    assert "$allow_equal_finalized" in observe_query
    assert observe_params["allow_equal_finalized"] is True


@pytest.mark.asyncio
async def test_reset_refuses_a_non_prepared_postgres_phase_before_neo4j() -> None:
    transaction = _Transaction()
    driver = _Driver(transaction)
    writer = Neo4jGraphProjectionWriter(driver, timeout=0.2)

    with pytest.raises(ValueError, match="prepared or neo_ready PostgreSQL recovery"):
        await writer.reset_for_recovery(_state(phase="idle"))

    assert driver.sessions == 0


@pytest.mark.asyncio
async def test_reset_cancellation_cancels_the_explicit_transaction() -> None:
    transaction = _Transaction()
    transaction.cancel_on_run = True
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await writer.reset_for_recovery(_state())

    assert transaction.cancelled is True
    assert transaction.rolled_back is False


@pytest.mark.asyncio
async def test_finalize_removes_only_the_exact_recovery_marker_and_is_idempotent() -> None:
    state = _state(phase="neo_ready")
    transaction = _Transaction({"accepted": True, "current_generation": 8})
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.finalize_recovery(state)

    assert result.accepted is True
    assert result.current_generation == state.generation
    assert transaction.committed is True
    query, params = transaction.queries[0]
    assert "fence.protocol_version = 2" in query
    assert "fence.generation = $generation" in query
    assert "fence.owner_id = $owner_id" in query
    assert "fence.recovery_id = $recovery_id" in query
    assert "fence.recovery_id IS NULL" in query
    assert "REMOVE fence.recovery_id" in query
    assert params["recovery_id"] == str(state.recovery_id)


@pytest.mark.asyncio
async def test_finalize_rejects_a_conflicting_marker_without_mutation() -> None:
    transaction = _Transaction({"accepted": False, "current_generation": 9})
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.finalize_recovery(_state(phase="neo_ready"))

    assert result.accepted is False
    assert result.current_generation == 9
    assert transaction.rolled_back is True
    assert transaction.committed is False


@pytest.mark.asyncio
async def test_finalize_observes_a_missing_disposable_projection_as_rejected() -> None:
    transaction = _Transaction({"accepted": False, "current_generation": -1})
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)

    result = await writer.finalize_recovery(_state(phase="neo_ready"))

    assert result.accepted is False
    assert result.current_generation == -1
    query = transaction.queries[0][0]
    assert "OPTIONAL MATCH (fence:BrainProjectionFence" in query
    assert transaction.rolled_back is True


@pytest.mark.asyncio
async def test_runtime_activation_never_bootstraps_or_crosses_a_recovery_marker() -> None:
    transaction = _Transaction({"accepted": False, "current_generation": -1})
    writer = Neo4jGraphProjectionWriter(_Driver(transaction), timeout=0.2)
    leadership = SimpleNamespace(owner_id="runtime", generation=1, armed=False)

    result = await writer.activate_generation(leadership)

    assert result.accepted is False
    query = transaction.queries[0][0]
    assert "OPTIONAL MATCH (fence:BrainProjectionFence" in query
    assert "MERGE (fence:BrainProjectionFence" not in query
    assert "fence.recovery_id IS NULL" in query
    assert transaction.rolled_back is True
