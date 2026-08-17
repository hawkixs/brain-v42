"""Real PostgreSQL coverage for crash-safe graph recovery migration 035."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    AsyncTransaction,
    async_sessionmaker,
)

from brain_v42.repositories.pg_graph_ledger import (
    PgGraphLedgerRepo,
    ProjectionLeadership,
    ProjectionRequeueReport,
)

pytestmark = pytest.mark.integration


async def _isolated_factory(
    engine: AsyncEngine,
) -> tuple[
    AsyncConnection,
    AsyncTransaction,
    async_sessionmaker[AsyncSession],
]:
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return connection, transaction, factory


async def _reset_singleton(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET generation = 100,
                    owner = NULL,
                    leased_until = NULL,
                    neo4j_armed_generation = 100,
                    recovery_id = NULL,
                    recovery_phase = 'idle',
                    last_completed_recovery_id = NULL
                WHERE slot = 'neo4j'
                """
            )
        )


async def test_recovery_cycle_resumes_without_requeue_or_generation_bump(
    engine: AsyncEngine,
) -> None:
    connection, outer, factory = await _isolated_factory(engine)
    recovery_id = uuid4()
    try:
        await _reset_singleton(factory)
        repo = PgGraphLedgerRepo(factory)
        await repo.assert_schema_ready()

        started = await repo.prepare_projection_recovery(
            recovery_id,
            "recovery-a",
            lease_seconds=300,
        )
        assert started is not None and started.status == "started"
        assert started.lease is not None and started.lease.generation == 101
        assert started.lease.phase == "prepared"
        assert started.requeued is not None

        resumed = await repo.prepare_projection_recovery(
            recovery_id,
            "recovery-a",
            lease_seconds=300,
        )
        assert resumed is not None and resumed.status == "resumed"
        assert resumed.lease is not None and resumed.lease.generation == 101
        assert resumed.requeued is None

        assert await repo.acquire_leadership("runtime", lease_seconds=30) is None
        runtime_shape = ProjectionLeadership(
            owner_id="recovery-a",
            generation=101,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            armed=False,
        )
        assert await repo.arm_leadership(runtime_shape) is False
        assert await repo.release_leadership(runtime_shape) is False
        assert await repo.claim_pending(runtime_shape, limit=1, lease_seconds=30) == []

        ready = await repo.mark_projection_recovery_neo_ready(
            resumed.lease,
            lease_seconds=300,
        )
        assert ready is not None and ready.phase == "neo_ready"
        resumed_ready = await repo.prepare_projection_recovery(
            recovery_id,
            "recovery-a",
            lease_seconds=300,
        )
        assert resumed_ready is not None
        assert resumed_ready.status == "resumed"
        assert resumed_ready.lease is not None
        assert resumed_ready.lease.phase == "neo_ready"
        assert resumed_ready.lease.generation == 101
        assert resumed_ready.requeued is None

        assert await repo.finalize_projection_recovery(resumed_ready.lease) is True
        completed = await repo.prepare_projection_recovery(
            recovery_id,
            "recovery-a",
            lease_seconds=300,
        )
        assert completed is not None and completed.status == "completed"
        assert completed.lease is None and completed.requeued is None
    finally:
        await outer.rollback()
        await connection.close()


async def test_prepare_recovery_rolls_back_interlock_when_requeue_fails(
    engine: AsyncEngine,
) -> None:
    connection, outer, factory = await _isolated_factory(engine)

    class FailingRepo(PgGraphLedgerRepo):
        async def _requeue_full_projection_in_session(
            self,
            session: AsyncSession,
        ) -> ProjectionRequeueReport:
            raise RuntimeError("injected requeue failure")

    try:
        await _reset_singleton(factory)
        repo = FailingRepo(factory)
        with pytest.raises(RuntimeError, match="injected requeue failure"):
            await repo.prepare_projection_recovery(
                uuid4(),
                "recovery-failure",
                lease_seconds=300,
            )

        async with factory() as session:
            row = (
                (
                    await session.execute(
                        sa.text(
                            """
                            SELECT generation, recovery_id, recovery_phase,
                                   neo4j_armed_generation
                            FROM graph_projection_leases
                            WHERE slot = 'neo4j'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert row == {
            "generation": 100,
            "recovery_id": None,
            "recovery_phase": "idle",
            "neo4j_armed_generation": 100,
        }
    finally:
        await outer.rollback()
        await connection.close()


@pytest.mark.parametrize(
    "invalid_state",
    [
        "prepared_but_armed",
        "neo_ready_but_unarmed",
        "active_without_owner",
    ],
)
async def test_database_constraint_rejects_incoherent_recovery_state(
    engine: AsyncEngine,
    invalid_state: str,
) -> None:
    connection, outer, factory = await _isolated_factory(engine)
    try:
        await _reset_singleton(factory)
        values = {
            "recovery_id": uuid4(),
            "phase": "prepared",
            "owner": "recovery-a",
            "lease_until": datetime.now(UTC) + timedelta(minutes=5),
            "armed": None,
        }
        if invalid_state == "prepared_but_armed":
            values["armed"] = 100
        elif invalid_state == "neo_ready_but_unarmed":
            values["phase"] = "neo_ready"
        else:
            values["owner"] = None

        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    sa.text(
                        """
                        UPDATE graph_projection_leases
                        SET recovery_id = :recovery_id,
                            recovery_phase = :phase,
                            owner = :owner,
                            leased_until = :lease_until,
                            neo4j_armed_generation = :armed
                        WHERE slot = 'neo4j'
                        """
                    ),
                    values,
                )
                await session.commit()
            await session.rollback()
    finally:
        await outer.rollback()
        await connection.close()
