"""Real PostgreSQL classification proof for graph outbox go/no-go metrics.

The counts are database-wide and the setup rewrites the whole outbox, so the
module measures a PRIVATE head database (``private_head_engine``), never the
shared ``brain_test`` where other runs write (ticket a4044c4d).
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector

pytestmark = pytest.mark.integration


def _metrics_engine() -> MagicMock:
    """A pool double: only the outbox half of ``collect_db_stats`` is under test."""
    metrics_engine = MagicMock()
    metrics_engine.sync_engine.pool.size.return_value = 5
    metrics_engine.sync_engine.pool.checkedout.return_value = 0
    metrics_engine.sync_engine.pool.checkedin.return_value = 1
    metrics_engine.sync_engine.pool.overflow.return_value = -4
    metrics_engine.sync_engine.pool._max_overflow = 10
    return metrics_engine


@pytest.fixture
def measured_engine(private_head_engine: AsyncEngine) -> AsyncEngine:
    """The database this module measures: its own, dropped with the module."""
    return private_head_engine


async def test_graph_outbox_metrics_classify_ready_claimed_delayed_and_exhausted(
    measured_engine: AsyncEngine,
) -> None:
    connection = await measured_engine.connect()
    outer = await connection.begin()
    factory = async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    entity_ids = [uuid4() for _ in range(4)]

    try:
        await connection.execute(
            sa.text("UPDATE graph_outbox SET delivered_at = clock_timestamp()")
        )
        for position, entity_id in enumerate(entity_ids):
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_entities "
                    "(id, entity_type, entity_key, scope_kind) "
                    "VALUES (:id, 'decision', :key, 'global')"
                ),
                {"id": entity_id, "key": f"metrics-outbox-{position}-{entity_id}"},
            )

        await connection.execute(
            sa.text(
                """
                INSERT INTO graph_outbox (
                    entity_id, aggregate_revision, operation, available_at,
                    leased_until, lease_owner, lease_generation,
                    delivered_at, last_error_code, created_at
                ) VALUES
                    (:ready_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     NULL, NULL, NULL, NULL, NULL,
                     clock_timestamp() - INTERVAL '2 minutes'),
                    (:claimed_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     clock_timestamp() + INTERVAL '5 minutes',
                     'metrics-projector', 42, NULL, NULL,
                     clock_timestamp() - INTERVAL '90 seconds'),
                    (:delayed_id, 1, 'upsert_entity',
                     clock_timestamp() + INTERVAL '5 minutes',
                     NULL, NULL, NULL, NULL, NULL,
                     clock_timestamp() - INTERVAL '1 minute'),
                    (:exhausted_id, 1, 'upsert_entity',
                     clock_timestamp() - INTERVAL '1 minute',
                     NULL, NULL, NULL, NULL, 'max_attempts',
                     clock_timestamp() - INTERVAL '3 minutes')
                """
            ),
            {
                "ready_id": entity_ids[0],
                "claimed_id": entity_ids[1],
                "delayed_id": entity_ids[2],
                "exhausted_id": entity_ids[3],
            },
        )
        await connection.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET generation = 42,
                    owner = 'metrics-projector',
                    leased_until = clock_timestamp() + INTERVAL '5 minutes',
                    neo4j_armed_generation = 42,
                    recovery_id = NULL,
                    recovery_phase = 'idle'
                WHERE slot = 'neo4j'
                """
            )
        )

        graph_outbox = (
            await MetricsCollector(
                engine=_metrics_engine(),
                session_factory=factory,
            ).collect_db_stats()
        )["graph_outbox"]

        assert graph_outbox["available"] is True
        assert graph_outbox["pending"] == 3
        assert graph_outbox["ready"] == 1
        assert graph_outbox["claimed"] == 1
        assert graph_outbox["exhausted"] == 1
        assert 119.0 <= graph_outbox["oldest_pending_age_seconds"] < 150.0
        assert graph_outbox["projector"] == {
            "generation": 42,
            "armed": True,
            "lease_active": True,
            "recovery_active": False,
            # An exhausted event is never healthy, even under an armed live lease (1146a1db).
            "healthy": False,
        }
    finally:
        await outer.rollback()
        await connection.close()


async def test_a_pending_row_committed_by_another_writer_never_reaches_the_counts(
    engine: AsyncEngine,
    measured_engine: AsyncEngine,
) -> None:
    """Ticket a4044c4d, the metrics twin: the counts are database-wide by contract.

    Measuring the shared ``brain_test`` folded every concurrent run's committed
    pending rows into ``pending``. The session-wide ``engine`` plays that run: it
    commits one pending row while the measuring transaction is open, and the
    module's own database must not see it.
    """
    entity_id = uuid4()
    connection = await measured_engine.connect()
    outer = await connection.begin()
    try:
        await connection.execute(
            sa.text("UPDATE graph_outbox SET delivered_at = clock_timestamp()")
        )
        async with engine.begin() as foreign:
            await foreign.execute(
                sa.text(
                    "INSERT INTO brain_entities (id, entity_type, entity_key, scope_kind) "
                    "VALUES (:id, 'decision', :key, 'global')"
                ),
                {"id": entity_id, "key": f"metrics-outbox-foreign-{entity_id}"},
            )
            await foreign.execute(
                sa.text(
                    "INSERT INTO graph_outbox (entity_id, aggregate_revision, operation, "
                    "available_at, created_at) VALUES "
                    "(:id, 1, 'upsert_entity', clock_timestamp(), clock_timestamp())"
                ),
                {"id": entity_id},
            )
        factory = async_sessionmaker(
            connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        graph_outbox = (
            await MetricsCollector(
                engine=_metrics_engine(),
                session_factory=factory,
            ).collect_db_stats()
        )["graph_outbox"]
        assert graph_outbox["pending"] == 0
    finally:
        await outer.rollback()
        await connection.close()
        async with engine.begin() as foreign:
            await foreign.execute(
                sa.text("DELETE FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
