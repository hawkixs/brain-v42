"""Crash-safe PostgreSQL recovery protocol for the Neo4j projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


def _result(
    *,
    row: object | None = None,
    rows: list[object] | None = None,
    scalar: object | None = None,
) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one.return_value = row
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one_or_none.return_value = scalar
    return result


def _factory(*results: object) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=context), session


def _locked_row(
    *,
    generation: int = 7,
    owner_id: str | None = None,
    lease_until: datetime | None = None,
    lease_live: bool = False,
    armed_generation: int | None = 7,
    recovery_id: UUID | None = None,
    phase: str = "idle",
    completed_id: UUID | None = None,
) -> dict[str, object]:
    return {
        "generation": generation,
        "owner_id": owner_id,
        "lease_until": lease_until,
        "lease_live": lease_live,
        "armed_generation": armed_generation,
        "recovery_id": recovery_id,
        "recovery_phase": phase,
        "last_completed_recovery_id": completed_id,
    }


@pytest.mark.asyncio
async def test_prepare_recovery_interlock_and_requeue_share_one_transaction() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionRecoveryLease,
        ProjectionRecoveryPreparation,
        ProjectionRequeueReport,
    )

    recovery_id = uuid4()
    lease_until = datetime.now(UTC) + timedelta(minutes=5)
    locked = _result(row=_locked_row())
    prepared = _result(
        row={
            "recovery_id": recovery_id,
            "owner_id": "recovery-worker",
            "generation": 8,
            "lease_until": lease_until,
            "recovery_phase": "prepared",
        }
    )
    entities = _result(rows=[{"id": 1}, {"id": 2}])
    relations = _result(rows=[{"id": 3}])
    factory, session = _factory(locked, prepared, entities, relations)
    repo = PgGraphLedgerRepo(factory)

    result = await repo.prepare_projection_recovery(
        recovery_id,
        "recovery-worker",
        lease_seconds=300,
    )

    assert result == ProjectionRecoveryPreparation(
        status="started",
        lease=ProjectionRecoveryLease(
            recovery_id=recovery_id,
            owner_id="recovery-worker",
            generation=8,
            lease_until=lease_until,
            phase="prepared",
        ),
        requeued=ProjectionRequeueReport(entity_events=2, relation_events=1),
    )
    assert session.execute.await_count == 4
    sql = [" ".join(str(call.args[0]).lower().split()) for call in session.execute.await_args_list]
    assert "for update" in sql[0]
    assert "recovery_id" in sql[1]
    assert "recovery_phase = 'prepared'" in sql[1]
    assert "neo4j_armed_generation = null" in sql[1]
    assert "generation = locked.generation + 1" in sql[1]
    assert all("on conflict" in statement for statement in sql[2:])
    assert all("graph_outbox" in statement for statement in sql[2:])
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    factory.assert_called_once()


@pytest.mark.asyncio
async def test_prepare_recovery_rolls_back_interlock_and_requeue_together() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    recovery_id = uuid4()
    factory, session = _factory(
        _result(row=_locked_row()),
        _result(
            row={
                "recovery_id": recovery_id,
                "owner_id": "recovery-worker",
                "generation": 8,
                "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                "recovery_phase": "prepared",
            }
        ),
        RuntimeError("injected requeue failure"),
    )
    repo = PgGraphLedgerRepo(factory)

    with pytest.raises(RuntimeError, match="injected requeue failure"):
        await repo.prepare_projection_recovery(
            recovery_id,
            "recovery-worker",
            lease_seconds=300,
        )

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_recovery_id_resumes_without_generation_bump_or_requeue() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    recovery_id = uuid4()
    lease_until = datetime.now(UTC) + timedelta(minutes=5)
    factory, session = _factory(
        _result(
            row=_locked_row(
                generation=8,
                owner_id="recovery-worker",
                lease_until=lease_until,
                lease_live=True,
                armed_generation=None,
                recovery_id=recovery_id,
                phase="prepared",
            )
        ),
        _result(
            row={
                "recovery_id": recovery_id,
                "owner_id": "recovery-worker",
                "generation": 8,
                "lease_until": lease_until,
                "recovery_phase": "prepared",
            }
        ),
    )
    repo = PgGraphLedgerRepo(factory)

    result = await repo.prepare_projection_recovery(
        recovery_id,
        "recovery-worker",
        lease_seconds=300,
    )

    assert result is not None
    assert result.status == "resumed"
    assert result.lease is not None and result.lease.generation == 8
    assert result.requeued is None
    assert session.execute.await_count == 2
    resume_sql = " ".join(str(session.execute.await_args_list[1].args[0]).lower().split())
    assert "graph_outbox" not in resume_sql
    assert "set generation =" not in resume_sql
    assert "set recovery_phase =" not in resume_sql
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["other_recovery", "live_runtime_lease"])
async def test_prepare_recovery_refuses_conflicting_work_without_mutation(conflict: str) -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    requested_id = uuid4()
    if conflict == "other_recovery":
        row = _locked_row(
            generation=8,
            owner_id="other-worker",
            lease_until=datetime.now(UTC) - timedelta(minutes=1),
            lease_live=False,
            armed_generation=None,
            recovery_id=uuid4(),
            phase="prepared",
        )
    else:
        row = _locked_row(
            owner_id="runtime-worker",
            lease_until=datetime.now(UTC) + timedelta(minutes=1),
            lease_live=True,
        )
    factory, session = _factory(_result(row=row))
    repo = PgGraphLedgerRepo(factory)

    result = await repo.prepare_projection_recovery(
        requested_id,
        "recovery-worker",
        lease_seconds=300,
    )

    assert result is None
    assert session.execute.await_count == 1
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_recovery_refuses_a_different_logical_owner() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    recovery_id = uuid4()
    factory, session = _factory(
        _result(
            row=_locked_row(
                generation=8,
                owner_id="original-owner",
                lease_until=datetime.now(UTC) - timedelta(minutes=1),
                lease_live=False,
                armed_generation=None,
                recovery_id=recovery_id,
                phase="neo_ready",
            )
        )
    )
    repo = PgGraphLedgerRepo(factory)

    result = await repo.prepare_projection_recovery(
        recovery_id,
        "different-owner",
        lease_seconds=300,
    )

    assert result is None
    assert session.execute.await_count == 1
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_recovery_id_is_an_idempotent_noop() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    recovery_id = uuid4()
    factory, session = _factory(_result(row=_locked_row(generation=8, completed_id=recovery_id)))
    repo = PgGraphLedgerRepo(factory)

    result = await repo.prepare_projection_recovery(
        recovery_id,
        "recovery-worker",
        lease_seconds=300,
    )

    assert result is not None
    assert result.status == "completed"
    assert result.lease is None
    assert result.requeued is None
    assert session.execute.await_count == 1
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_recovery_neo_ready_is_an_exact_live_cas() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionRecoveryLease,
    )

    recovery_id = uuid4()
    lease_until = datetime.now(UTC) + timedelta(minutes=5)
    state = ProjectionRecoveryLease(
        recovery_id=recovery_id,
        owner_id="recovery-worker",
        generation=8,
        lease_until=lease_until,
        phase="prepared",
    )
    factory, session = _factory(
        _result(
            row={
                "recovery_id": recovery_id,
                "owner_id": state.owner_id,
                "generation": state.generation,
                "lease_until": lease_until,
                "recovery_phase": "neo_ready",
            }
        )
    )
    repo = PgGraphLedgerRepo(factory)

    ready = await repo.mark_projection_recovery_neo_ready(state, lease_seconds=300)

    assert ready is not None and ready.phase == "neo_ready"
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "recovery_id = :recovery_id" in sql
    assert "owner = :owner_id" in sql
    assert "generation = :generation" in sql
    assert "leased_until > clock_timestamp()" in sql
    assert "protocol_version = 2" in sql
    assert "recovery_phase in ('prepared', 'neo_ready')" in sql
    assert "neo4j_armed_generation = generation" in sql
    assert params["recovery_id"] == recovery_id
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_recovery_clears_interlock_in_one_exact_cas() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionRecoveryLease,
    )

    state = ProjectionRecoveryLease(
        recovery_id=uuid4(),
        owner_id="recovery-worker",
        generation=8,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        phase="neo_ready",
    )
    factory, session = _factory(_result(scalar=state.generation))
    repo = PgGraphLedgerRepo(factory)

    finalized = await repo.finalize_projection_recovery(state)

    assert finalized is True
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "last_completed_recovery_id = recovery_id" in sql
    assert "recovery_id = null" in sql
    assert "recovery_phase = 'idle'" in sql
    assert "owner = null" in sql
    assert "leased_until = null" in sql
    assert "neo4j_armed_generation = generation" in sql
    assert "recovery_id = :recovery_id" in sql
    assert "owner = :owner_id" in sql
    assert "generation = :generation" in sql
    assert "leased_until > clock_timestamp()" in sql
    assert params["recovery_id"] == state.recovery_id
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_recovery_transitions_fail_closed() -> None:
    from brain_v42.repositories.pg_graph_ledger import (
        PgGraphLedgerRepo,
        ProjectionRecoveryLease,
    )

    state = ProjectionRecoveryLease(
        recovery_id=uuid4(),
        owner_id="stale-worker",
        generation=8,
        lease_until=datetime.now(UTC) - timedelta(seconds=1),
        phase="prepared",
    )
    factory, session = _factory(_result(row=None), _result(scalar=None))
    repo = PgGraphLedgerRepo(factory)

    assert await repo.mark_projection_recovery_neo_ready(state, lease_seconds=300) is None
    assert await repo.finalize_projection_recovery(state) is False

    assert session.commit.await_count == 2
