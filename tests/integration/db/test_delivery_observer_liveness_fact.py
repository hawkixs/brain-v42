"""Database evidence for the delivery observer liveness fact."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_artifact_bindings, delivery_confirmations
from brain_v42.facts.probes.delivery_observer_liveness import DeliveryObserverLivenessProbe
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.repositories.pg_delivery import DELIVERY_OBSERVER_LOCK
from tests.integration.db.delivery_observer_cases import ObserverCase
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("observer_queue_isolation"),
]


async def test_fact_reads_overdue_work_and_bigint_advisory_lock(engine, session_factory):
    _, binding, _ = await ObserverCase(engine, session_factory).create(number=599)
    confirmation_at = datetime.now(UTC) + timedelta(seconds=700)
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == binding.id)
            .values(due_at=sa.func.clock_timestamp() - timedelta(seconds=700))
        )
        await session.execute(
            delivery_confirmations.insert().values(
                subject_kind="artifact_binding",
                binding_id=binding.id,
                outcome="error",
                error_code="provider_unavailable",
                collection_started_at=confirmation_at,
                collection_finished_at=confirmation_at,
            )
        )
    async with engine.connect() as lock_connection:
        async with session_factory() as session:
            unlocked = await DeliveryObserverLivenessProbe().measure(PostgresSourceSession(session))
        assert unlocked["lock_held"] is False
        await lock_connection.execute(
            sa.text("SELECT pg_advisory_lock(:key)"), {"key": DELIVERY_OBSERVER_LOCK}
        )
        try:
            async with session_factory() as session:
                value = await DeliveryObserverLivenessProbe().measure(
                    PostgresSourceSession(session)
                )
            assert value["lock_held"] is True
            assert value["overdue"] == 1
            assert value["last_confirmation_age_seconds"] == 0
            assert value["healthy"] is False
        finally:
            await lock_connection.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": DELIVERY_OBSERVER_LOCK}
            )
