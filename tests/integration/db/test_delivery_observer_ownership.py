"""Real PostgreSQL ownership and atomic publication on one fenced connection."""

import asyncio
from contextlib import suppress

import httpx
import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_receipts,
    delivery_snapshots,
)
from tests.integration.db.test_delivery_receipt_publication import (
    _evidence,
    _issuer,
    _now,
    _workflow,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _owner(engine):
    from brain_v42.delivery_observer.ownership import ObserverOwnership

    return ObserverOwnership(engine)


async def _publish(owner, repo, binding):
    async with owner.transaction() as session:
        now = await _now(session)
        return await repo.publish_observation(
            session, binding.id, binding.binding_version, _evidence(now), now, now
        )


async def _counts(factory, binding):
    async with factory() as session:
        counts = [
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(table)
                .where(table.c.binding_id == binding.id)
            )
            for table in (delivery_snapshots, delivery_confirmations)
        ]
        counts.append(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_receipts)
                .where(delivery_receipts.c.ticket_id == binding.ticket_id)
            )
        )
        pointer = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
        return counts, pointer


async def test_duplicate_observer_refused_and_connection_idle_between_transactions(engine):
    first, second = _owner(engine), _owner(engine)
    assert await first.acquire()
    try:
        assert not await second.acquire()
        async with engine.connect() as other:
            state = (
                await other.execute(
                    sa.text("SELECT state, xact_start FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": first.backend_pid},
                )
            ).one()
            assert state.state == "idle" and state.xact_start is None
        assert first.owned and not first.lost.is_set()
        await first.heartbeat()
    finally:
        await first.release()
        await second.release()
    third = _owner(engine)
    assert await third.acquire()
    await third.release()


async def test_real_success_commits_snapshot_confirmation_pointer_and_receipts(
    engine, session_factory
):
    _, binding, _ = await _workflow(session_factory)
    owner, repo = _owner(engine), _issuer(session_factory)
    assert await owner.acquire()
    try:
        confirmation = await _publish(owner, repo, binding)
        counts, row = await _counts(session_factory, binding)
        assert counts == [1, 1, 2]
        assert row["latest_success_confirmation_id"] == confirmation.id
        assert row["row_version"] == binding.binding_version + 1
    finally:
        await owner.release()


async def test_backend_killed_during_http_never_publishes_and_cannot_reacquire(
    engine, session_factory
):
    from brain_v42.delivery_observer.ownership import ObserverOwnershipLost

    _, binding, _ = await _workflow(session_factory)
    owner = _owner(engine)
    entered, finish = asyncio.Event(), asyncio.Event()
    assert await owner.acquire()

    async def handler(request):
        entered.set()
        await finish.wait()
        return httpx.Response(200, json={"complete": True})

    async def collect_then_publish():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await http.get("https://api.github.com/fixture")
        await _publish(owner, _issuer(session_factory), binding)

    job = asyncio.create_task(collect_then_publish())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with engine.begin() as other:
            assert await other.scalar(
                sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": owner.backend_pid}
            )
        finish.set()
        with pytest.raises(ObserverOwnershipLost):
            await asyncio.wait_for(job, 5)
        assert owner.lost.is_set() and not owner.owned
        assert (await _counts(session_factory, binding))[0] == [0, 0, 0]
        with pytest.raises(ObserverOwnershipLost):
            await owner.acquire()
    finally:
        job.cancel()
        with suppress(asyncio.CancelledError, ObserverOwnershipLost):
            await job
        await owner.release()


@pytest.mark.parametrize("replace_backend", [False, True])
async def test_invalidated_or_replaced_connection_is_permanently_fenced(engine, replace_backend):
    from brain_v42.delivery_observer.ownership import ObserverOwnershipLost

    owner = _owner(engine)
    assert await owner.acquire()
    try:
        connection = owner._connection
        original_pid = owner.backend_pid
        await connection.invalidate()
        if replace_backend:
            assert await connection.scalar(sa.text("SELECT pg_backend_pid()")) != original_pid
            await connection.commit()
        with pytest.raises(ObserverOwnershipLost):
            async with owner.transaction():
                pytest.fail("a stale owner admitted a publication")
        assert owner.lost.is_set() and owner.backend_pid == original_pid
    finally:
        await owner.release()


@pytest.mark.parametrize("operation", ["heartbeat", "publish"])
async def test_same_backend_without_advisory_lock_is_rejected(engine, session_factory, operation):
    from brain_v42.delivery_observer.ownership import ObserverOwnershipLost

    _, binding, _ = await _workflow(session_factory)
    owner = _owner(engine)
    assert await owner.acquire()
    try:
        assert await owner._connection.scalar(
            sa.text("SELECT pg_advisory_unlock(:key)"), {"key": owner.LOCK_KEY}
        )
        await owner._connection.commit()
        with pytest.raises(ObserverOwnershipLost):
            if operation == "heartbeat":
                await owner.heartbeat()
            else:
                await _publish(owner, _issuer(session_factory), binding)
        assert owner.lost.is_set()
        assert (await _counts(session_factory, binding))[0] == [0, 0, 0]
    finally:
        await owner.release()


async def test_two_concurrent_completions_use_one_serial_transaction_stream(
    engine, session_factory
):
    _, binding1, _ = await _workflow(session_factory)
    _, binding2, _ = await _workflow(session_factory)
    owner, repo = _owner(engine), _issuer(session_factory)
    assert await owner.acquire()
    first_entered, let_first_finish = asyncio.Event(), asyncio.Event()
    second_started, second_entered = asyncio.Event(), asyncio.Event()
    observed_pids = []

    async def first():
        async with owner.transaction() as session:
            first_entered.set()
            await let_first_finish.wait()
            observed_pids.append(await session.scalar(sa.text("SELECT pg_backend_pid()")))
            now = await _now(session)
            await repo.publish_observation(
                session, binding1.id, binding1.binding_version, _evidence(now), now, now
            )

    async def second():
        second_started.set()
        async with owner.transaction() as session:
            second_entered.set()
            observed_pids.append(await session.scalar(sa.text("SELECT pg_backend_pid()")))
            now = await _now(session)
            await repo.publish_observation(
                session, binding2.id, binding2.binding_version, _evidence(now), now, now
            )

    job1 = asyncio.create_task(first())
    await asyncio.wait_for(first_entered.wait(), 3)
    job2 = asyncio.create_task(second())
    try:
        await asyncio.wait_for(second_started.wait(), 3)
        # An independent round trip gives the second task time to actually try
        # admission while the first publication remains blocked.
        async with engine.connect() as probe:
            await probe.scalar(sa.text("SELECT 1"))
        assert not second_entered.is_set() and not job2.done()
        let_first_finish.set()
        await asyncio.wait_for(asyncio.gather(job1, job2), 10)
        assert observed_pids == [owner.backend_pid, owner.backend_pid]
        assert (await _counts(session_factory, binding1))[0] == [1, 1, 2]
        assert (await _counts(session_factory, binding2))[0] == [1, 1, 2]
    finally:
        let_first_finish.set()
        await owner.release()


async def test_exception_after_snapshot_insert_rolls_back_entire_publication(
    engine, session_factory, monkeypatch
):
    _, binding, _ = await _workflow(session_factory)
    owner, repo = _owner(engine), _issuer(session_factory)
    original = repo._snapshot_id

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected after snapshot")

    monkeypatch.setattr(repo, "_snapshot_id", fail_after_insert)
    assert await owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="injected after snapshot"):
            await _publish(owner, repo, binding)
        counts, row = await _counts(session_factory, binding)
        assert counts == [0, 0, 0]
        assert row["latest_success_confirmation_id"] is None
        assert row["row_version"] == binding.binding_version
        assert owner.owned
        monkeypatch.setattr(repo, "_snapshot_id", original)
        await _publish(owner, repo, binding)
        assert (await _counts(session_factory, binding))[0] == [1, 1, 2]
    finally:
        await owner.release()


async def test_cancellation_after_publication_rolls_back_and_release_allows_restart(
    engine, session_factory
):
    _, binding, _ = await _workflow(session_factory)
    owner, repo = _owner(engine), _issuer(session_factory)
    inserted, wait = asyncio.Event(), asyncio.Event()
    assert await owner.acquire()

    async def transaction():
        try:
            async with owner.transaction() as session:
                now = await _now(session)
                await repo.publish_observation(
                    session, binding.id, binding.binding_version, _evidence(now), now, now
                )
                inserted.set()
                await wait.wait()
        finally:
            await owner.release()

    job = asyncio.create_task(transaction())
    await asyncio.wait_for(inserted.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    assert (await _counts(session_factory, binding))[0] == [0, 0, 0]
    next_owner = _owner(engine)
    assert await next_owner.acquire()
    await next_owner.release()


async def test_transaction_requires_successful_acquisition_and_release_is_final(engine):
    from brain_v42.delivery_observer.ownership import ObserverOwnershipLost

    owner = _owner(engine)
    with pytest.raises(ObserverOwnershipLost):
        async with owner.transaction():
            pytest.fail("transaction without ownership")
    await owner.release()
    await owner.release()
    with pytest.raises(ObserverOwnershipLost):
        await owner.acquire()
