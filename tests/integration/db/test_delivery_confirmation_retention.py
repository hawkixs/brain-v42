"""Deletion contracts on a private head database, never the shared test database."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain_v42.db.tables import (
    delivery_artifact_bindings as bindings,
)
from brain_v42.db.tables import (
    delivery_confirmations as confirmations,
)
from brain_v42.db.tables import (
    delivery_events as events,
)
from brain_v42.db.tables import (
    delivery_snapshots as snapshots,
)
from brain_v42.db.tables import (
    delivery_workflows as workflows,
)
from brain_v42.repositories.pg_delivery_retention import PgDeliveryConfirmationRetention
from tests.integration.db.test_delivery_receipt_issuance import (
    _issue,
    _issuer,
    _now,
    _proof,
    _publish,
    _publish_context,
    _repository_ref,
    _workflow,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
OLD = timedelta(days=30)


@pytest.fixture
async def factory(private_head_engine):
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    # Earlier tests' rows stay private and outside this test's retention window.
    async with factory.begin() as session:
        await session.execute(
            confirmations.update().values(
                collection_started_at=sa.func.now(), collection_finished_at=sa.func.now()
            )
        )
    return factory


async def _successes(factory, *, count=1, context=False):
    ticket, binding, _ = await _workflow(factory, refs=(_repository_ref(),) if context else ())
    async with factory.begin() as session:
        at = await _now(session)
        if context:
            await _publish_context(factory, session, ticket.id, at)
            pointer = workflows.c.latest_context_success_confirmation_id
            row = (
                (
                    await session.execute(
                        sa.select(workflows).where(workflows.c.ticket_id == ticket.id)
                    )
                )
                .mappings()
                .one()
            )
            subject = {
                "subject_kind": "repository_context",
                "ticket_id": ticket.id,
                "contract_revision": row["current_revision"],
                "attempt": row["attempt"],
                "context_set_digest": row["context_set_digest"],
            }
            original = await session.scalar(
                sa.select(pointer).where(workflows.c.ticket_id == ticket.id)
            )
        else:
            await _publish(
                factory, session, binding, _proof(at, state="open", integration_sha=None), at
            )
            original = await session.scalar(
                sa.select(bindings.c.latest_success_confirmation_id).where(
                    bindings.c.id == binding.id
                )
            )
            subject = {"subject_kind": "artifact_binding", "binding_id": binding.id}
        snapshot_id = await session.scalar(
            sa.select(confirmations.c.snapshot_id).where(confirmations.c.id == original)
        )
        ids = []
        for _ in range(count):
            ids.append(
                await session.scalar(
                    confirmations.insert()
                    .values(
                        **subject,
                        snapshot_id=snapshot_id,
                        outcome="success",
                        collection_started_at=at - OLD,
                        collection_finished_at=at - OLD,
                    )
                    .returning(confirmations.c.id)
                )
            )
    return ticket, binding, ids, snapshot_id


async def _present(factory, ids):
    async with factory() as session:
        return set(
            await session.scalars(sa.select(confirmations.c.id).where(confirmations.c.id.in_(ids)))
        )


async def _purge(factory, **kwargs):
    return await PgDeliveryConfirmationRetention(factory).purge(
        older_than=timedelta(days=14), batch_size=5000, max_batches=100, **kwargs
    )


async def test_dry_run_counts_and_deletes_nothing(factory):
    _, _, ids, _ = await _successes(factory, count=3)
    report = await _purge(factory, dry_run=True)
    assert report.candidates == 3 and report.deleted == 0
    assert await _present(factory, ids) == set(ids)


async def test_execute_deletes_only_old_unreferenced_successes(factory):
    _, _, ids, snapshot_id = await _successes(factory, count=3)
    async with factory.begin() as session:
        await session.execute(
            confirmations.update()
            .where(confirmations.c.id == ids[0])
            .values(collection_started_at=sa.func.now(), collection_finished_at=sa.func.now())
        )
    report = await _purge(factory, dry_run=False)
    assert report.candidates == 2 and report.deleted == 2
    assert await _present(factory, ids) == {ids[0]}
    async with factory() as session:
        assert (
            await session.scalar(sa.select(snapshots.c.id).where(snapshots.c.id == snapshot_id))
            == snapshot_id
        )


@pytest.mark.parametrize("context", [False, True])
async def test_never_deletes_binding_or_workflow_pointers(factory, context):
    ticket, binding, ids, _ = await _successes(factory, count=2, context=context)
    table = workflows if context else bindings
    predicate = table.c.ticket_id == ticket.id if context else table.c.id == binding.id
    pointers = (
        {
            "latest_context_attempt_confirmation_id": ids[0],
            "latest_context_success_confirmation_id": ids[1],
        }
        if context
        else {"latest_attempt_confirmation_id": ids[0], "latest_success_confirmation_id": ids[1]}
    )
    async with factory.begin() as session:
        await session.execute(table.update().where(predicate).values(**pointers))
    report = await _purge(factory, dry_run=False)
    assert report.deleted == 0
    assert await _present(factory, ids) == set(ids)


async def test_never_deletes_a_confirmation_named_in_a_receipt_proof(factory):
    ticket, binding, _ = await _workflow(factory, refs=(_repository_ref(),))
    async with factory.begin() as session:
        at = await _now(session)
        await _publish_context(factory, session, ticket.id, at)
        await _publish(factory, session, binding, _proof(at), at)
        receipt = await _issue(_issuer(factory), session, ticket.id)
        assert receipt is not None
        ids = list(
            await session.scalars(
                sa.select(confirmations.c.id).where(
                    sa.or_(
                        confirmations.c.binding_id == binding.id,
                        confirmations.c.ticket_id == ticket.id,
                    )
                )
            )
        )
        await session.execute(
            confirmations.update()
            .where(confirmations.c.id.in_(ids))
            .values(collection_started_at=at - OLD, collection_finished_at=at - OLD)
        )
        # Isolate the JSON protection from FK pointer protection.
        await session.execute(
            bindings.update()
            .where(bindings.c.id == binding.id)
            .values(latest_success_confirmation_id=None, latest_attempt_confirmation_id=None)
        )
        await session.execute(
            workflows.update()
            .where(workflows.c.ticket_id == ticket.id)
            .values(
                latest_context_success_confirmation_id=None,
                latest_context_attempt_confirmation_id=None,
            )
        )
    assert (await _purge(factory, dry_run=False)).deleted == 0
    assert await _present(factory, ids) == set(ids)


@pytest.mark.parametrize("column", ["payload", "result"])
@pytest.mark.parametrize("key", ["success_confirmation_id", "latest_attempt_confirmation_id"])
async def test_event_json_protects_nested_ids_and_tolerates_malformed_values(factory, column, key):
    ticket, _, ids, _ = await _successes(factory)
    async with factory.begin() as session:
        values = {"payload": {}, "result": {}}
        values[column] = {
            "proof": [
                {"nested": {key: str(ids[0])}},
                {key: "not-a-uuid"},
                {key: {"malformed": True}},
            ]
        }
        await session.execute(
            events.insert().values(
                ticket_id=ticket.id,
                operation="retention_test",
                actor_project="brain-v42",
                idempotency_key=str(uuid4()),
                request_digest="a" * 64,
                **values,
            )
        )
    assert (await _purge(factory, dry_run=True)).candidates == 0
    assert (await _purge(factory, dry_run=False)).deleted == 0
    assert await _present(factory, ids) == set(ids)


async def test_never_deletes_errors(factory):
    _, binding, _ = await _workflow(factory)
    at = datetime.now(UTC) - OLD
    async with factory.begin() as session:
        confirmation_id = await session.scalar(
            confirmations.insert()
            .values(
                subject_kind="artifact_binding",
                binding_id=binding.id,
                outcome="error",
                error_code="provider_unavailable",
                collection_started_at=at,
                collection_finished_at=at,
            )
            .returning(confirmations.c.id)
        )
    assert (await _purge(factory, dry_run=False)).deleted == 0
    assert await _present(factory, [confirmation_id]) == {confirmation_id}


async def test_batches_are_bounded(factory):
    _, _, ids, _ = await _successes(factory, count=5)
    report = await PgDeliveryConfirmationRetention(factory).purge(
        older_than=timedelta(days=14), batch_size=2, max_batches=1, dry_run=False
    )
    assert report.deleted == 2
    assert len(await _present(factory, ids)) == 3


async def test_window_below_seven_days_is_refused(factory):
    with pytest.raises(ValueError, match="seven"):
        await PgDeliveryConfirmationRetention(factory).purge(
            older_than=timedelta(days=6), batch_size=5000, max_batches=100, dry_run=False
        )
