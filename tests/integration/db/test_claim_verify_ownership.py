"""Real PostgreSQL ownership and fenced run-row writes (ADR 27 lot C, T1.4).

NOTE for reviewers: this module SKIPPED in the container that wrote it (no
`BRAIN_V42_TEST_DB_URL`, no reachable PostgreSQL) -- it must be run for real
before `VerifyRunOwnership` is trusted, exactly like
`test_claim_nightly_selection.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from datetime import date

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import dream_runs
from brain_v42.repositories.pg_claim_nightly import (
    VerifyRunOwnership,
    VerifyRunOwnershipLost,
    finish_run,
    get_or_create_wet_run,
)
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimlock") as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _row(session_factory: async_sessionmaker[AsyncSession], run_id: int) -> sa.RowMapping:
    async with session_factory() as session:
        return (
            (await session.execute(sa.select(dream_runs).where(dream_runs.c.id == run_id)))
            .mappings()
            .one()
        )


async def test_acquire_is_exclusive_and_the_backend_is_idle_between_transactions(
    engine: AsyncEngine,
) -> None:
    first, second = VerifyRunOwnership(engine), VerifyRunOwnership(engine)
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
        await first.check()
    finally:
        await first.release()
        await second.release()
    third = VerifyRunOwnership(engine)
    assert await third.acquire()
    await third.release()


async def test_get_or_create_is_idempotent_by_run_date(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    run_date = date(2026, 9, 27)
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    try:
        first_id = await get_or_create_wet_run(owner, run_date)
        second_id = await get_or_create_wet_run(owner, run_date)
        assert first_id == second_id

        row = await _row(session_factory, first_id)
        assert row["phase"] == "verify"
        assert row["project_key"] == "*"
        assert row["phase_dry_run"] is False
        assert row["model"] is None
        assert row["status"] == "fail"
        assert row["error_message"] == "verify started; no terminal status recorded"
    finally:
        await owner.release()


async def test_finish_run_writes_the_terminal_status(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    try:
        run_id = await get_or_create_wet_run(owner, date(2026, 9, 28))
        await finish_run(owner, run_id, status="done", duration_s=1.5, error_message=None)

        row = await _row(session_factory, run_id)
        assert row["status"] == "done"
        assert row["duration_s"] == 1.5
        assert row["error_message"] is None
    finally:
        await owner.release()


async def test_a_second_wet_invocation_is_busy_and_writes_nothing(engine: AsyncEngine) -> None:
    first, second = VerifyRunOwnership(engine), VerifyRunOwnership(engine)
    assert await first.acquire()
    try:
        assert not await second.acquire()
    finally:
        await first.release()
        await second.release()


@pytest.mark.parametrize("replace_backend", [False, True])
async def test_invalidated_or_replaced_connection_is_permanently_fenced(
    engine: AsyncEngine, replace_backend: bool
) -> None:
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    try:
        connection = owner._connection
        original_pid = owner.backend_pid
        assert connection is not None
        await connection.invalidate()
        if replace_backend:
            assert await connection.scalar(sa.text("SELECT pg_backend_pid()")) != original_pid
            await connection.commit()

        with pytest.raises(VerifyRunOwnershipLost):
            await owner.check()
        assert owner.lost.is_set() and owner.backend_pid == original_pid

        with pytest.raises(VerifyRunOwnershipLost):
            async with owner.transaction():
                pytest.fail("a stale owner wrote the run row")
    finally:
        await owner.release()


async def test_same_backend_without_the_advisory_lock_is_rejected(engine: AsyncEngine) -> None:
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    try:
        connection = owner._connection
        assert connection is not None
        assert await connection.scalar(
            sa.text("SELECT pg_advisory_unlock(:key)"), {"key": owner.LOCK_KEY}
        )
        await connection.commit()

        with pytest.raises(VerifyRunOwnershipLost):
            await owner.check()
        assert owner.lost.is_set()
    finally:
        await owner.release()


async def test_lock_loss_followed_by_a_competing_invocation(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """R2-M1: a terminated backend must not let a stale owner write after a new one wins."""
    import asyncio

    run_date = date(2026, 9, 29)
    first = VerifyRunOwnership(engine)
    assert await first.acquire()
    first_run_id = await get_or_create_wet_run(first, run_date)

    async with engine.begin() as other:
        assert await other.scalar(
            sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": first.backend_pid}
        )

    second = VerifyRunOwnership(engine)
    # The terminated backend may take a moment to release its session advisory lock.
    acquired = False
    for _ in range(50):
        if await second.acquire():
            acquired = True
            break
        await asyncio.sleep(0.1)
    assert acquired
    try:
        second_run_id = await get_or_create_wet_run(second, run_date)
        assert second_run_id == first_run_id
        await finish_run(second, second_run_id, status="done", duration_s=2.0, error_message=None)

        with pytest.raises(VerifyRunOwnershipLost):
            await finish_run(
                first, first_run_id, status="fail", duration_s=9.0, error_message="stale write"
            )

        row = await _row(session_factory, first_run_id)
        assert row["status"] == "done"
    finally:
        with suppress(VerifyRunOwnershipLost):
            await first.release()
        await second.release()


async def test_release_unlocks_then_a_new_owner_can_acquire(engine: AsyncEngine) -> None:
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    await owner.release()
    await owner.release()  # idempotent

    next_owner = VerifyRunOwnership(engine)
    assert await next_owner.acquire()
    await next_owner.release()


async def test_transaction_requires_successful_acquisition(engine: AsyncEngine) -> None:
    owner = VerifyRunOwnership(engine)
    with pytest.raises(VerifyRunOwnershipLost):
        async with owner.transaction():
            pytest.fail("transaction without ownership")
    await owner.release()
    with pytest.raises(VerifyRunOwnershipLost):
        await owner.acquire()


async def test_a_failed_end_of_transaction_check_rolls_back_the_update(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An advisory unlock forced mid-transaction must roll the UPDATE back.

    Session-level advisory locks are per-backend: only the backend that took
    the lock can release it with `pg_advisory_unlock` (a call from another
    connection is a silent no-op against it, per PostgreSQL's own semantics --
    see `test_same_backend_without_the_advisory_lock_is_rejected` above, which
    already unlocks through the owner's own connection). The unlock must
    therefore run through `session`, which `owner.transaction()` binds to the
    SAME backend connection as the lock, to actually force the loss this test
    is proving.
    """
    owner = VerifyRunOwnership(engine)
    assert await owner.acquire()
    try:
        run_id = await get_or_create_wet_run(owner, date(2026, 9, 30))

        with pytest.raises(VerifyRunOwnershipLost):
            async with owner.transaction() as session:
                await session.execute(
                    sa.update(dream_runs)
                    .where(dream_runs.c.id == run_id)
                    .values(status="done", duration_s=1.0)
                )
                await session.execute(
                    sa.text("SELECT pg_advisory_unlock(:key)"), {"key": owner.LOCK_KEY}
                )

        row = await _row(session_factory, run_id)
        assert row["status"] == "fail"  # the D1 initial status, never "done"
    finally:
        with suppress(VerifyRunOwnershipLost):
            await owner.release()
