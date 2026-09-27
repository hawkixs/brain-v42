"""Real-PostgreSQL run-lock fencing and process-death (ADR 27 lot C, T1.10).

NOTE for reviewers: this module SKIPPED in the container that wrote it (no
`BRAIN_V42_TEST_DB_URL`, no reachable PostgreSQL) -- every assertion below is
untested against a real server and must be run for real before PR 1 merges.
The process-death scenario in particular (a real subprocess, a table lock
from another connection, SIGTERM) is the least-verified test in this PR.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

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

from brain_v42.db.tables import (
    brain_entities,
    dream_runs,
    knowledge_claim_verdicts,
    project_contexts,
)
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.registry import FactRegistry
from brain_v42.repositories.pg_claim_nightly import (
    VerifyRunOwnership,
    VerifyRunOwnershipLost,
    get_or_create_wet_run,
)
from brain_v42.repositories.pg_knowledge_claims import insert_claim
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import asyncpg_dsn, fresh_head_database

pytestmark = pytest.mark.integration

_RUN_DATE = date(2026, 9, 27)


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


class _Source:
    async def identity(self) -> SourceIdentity:
        return SourceIdentity("1", "brain_test", "127.0.0.1", 5432)


class _Probe:
    name = "nightly_lock_probe"
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=60)
    timeout = timedelta(seconds=3)
    briefing = False
    policies: dict[str, int] = {}
    value_schema = {"revision": "string"}

    async def measure(self, source: _Source) -> dict[str, object]:
        return {"revision": "057"}


def _registry() -> FactRegistry:
    @asynccontextmanager
    async def source() -> AsyncIterator[_Source]:
        yield _Source()

    identity = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source}, expected={FactTarget.PRODUCTION: identity}
    )
    registry.register(_Probe())
    registry.freeze()
    return registry


async def test_a_second_wet_invocation_is_busy_while_the_first_holds_the_lock(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = VerifyRunOwnership(engine)
    assert await first.acquire()
    try:
        first_run_id = await get_or_create_wet_run(first, _RUN_DATE)

        second = VerifyRunOwnership(engine)
        assert not await second.acquire()
        await second.release()

        from brain_v42.repositories.pg_claim_nightly import finish_run

        await finish_run(first, first_run_id, status="done", duration_s=1.0, error_message=None)
    finally:
        await first.release()

    rerun = VerifyRunOwnership(engine)
    assert await rerun.acquire()
    try:
        rerun_id = await get_or_create_wet_run(rerun, _RUN_DATE)
        assert rerun_id == first_run_id
        async with session_factory() as session:
            row = (
                await session.execute(
                    sa.select(dream_runs.c.status).where(dream_runs.c.id == first_run_id)
                )
            ).scalar_one()
        assert row == "done"
    finally:
        await rerun.release()


async def test_lock_loss_followed_by_a_competing_invocation(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """R2-M1: a terminated backend must not let a stale owner write after a new one wins."""
    from brain_v42.repositories.pg_claim_nightly import finish_run

    first = VerifyRunOwnership(engine)
    assert await first.acquire()
    first_run_id = await get_or_create_wet_run(first, _RUN_DATE)
    first_backend_pid = first.backend_pid

    async with engine.begin() as other:
        assert await other.scalar(
            sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": first_backend_pid}
        )

    second = VerifyRunOwnership(engine)
    acquired = False
    for _ in range(50):
        if await second.acquire():
            acquired = True
            break
        await asyncio.sleep(0.1)
    assert acquired
    try:
        second_run_id = await get_or_create_wet_run(second, _RUN_DATE)
        assert second_run_id == first_run_id
        await finish_run(second, second_run_id, status="done", duration_s=1.0, error_message=None)

        with pytest.raises(VerifyRunOwnershipLost):
            await finish_run(
                first, first_run_id, status="fail", duration_s=9.0, error_message="stale write"
            )

        async with session_factory() as session:
            status = (
                await session.execute(
                    sa.select(dream_runs.c.status).where(dream_runs.c.id == first_run_id)
                )
            ).scalar_one()
        assert status == "done"
    finally:
        from contextlib import suppress

        with suppress(VerifyRunOwnershipLost):
            await first.release()
        await second.release()


async def _measure_identity(admin_url: str) -> dict[str, object]:
    import asyncpg

    connection = await asyncpg.connect(asyncpg_dsn(admin_url))
    try:
        row = await connection.fetchrow(
            "SELECT (SELECT system_identifier FROM pg_control_system())::text AS system_identifier, "
            "current_database() AS database, "
            "inet_server_addr()::text AS server_addr, "
            "inet_server_port() AS server_port"
        )
    finally:
        await connection.close()
    assert row is not None
    return {
        "system_identifier": row["system_identifier"],
        "database": row["database"],
        "server_addr": row["server_addr"],
        "server_port": row["server_port"],
    }


async def test_process_death_leaves_the_initial_status_and_releases_the_lock(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    migration_database_url: str,
) -> None:
    """The CLI runs as a real subprocess, blocked mid-verification by a table lock, then killed."""
    registry = _registry()
    assert await register_fact_definitions(registry, session_factory)

    project_key = f"nightly-lock-{uuid4().hex[:12]}"
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.insert(project_contexts).values(
                project_key=project_key, name=project_key, description="T1.10 fixture"
            )
        )
        entity_id = (
            await session.execute(
                sa.insert(brain_entities)
                .values(
                    entity_type="learning",
                    entity_key=f"nightly-lock-entity-{uuid4()}",
                    project_key=project_key,
                    scope_kind="project",
                    lifecycle="active",
                )
                .returning(brain_entities.c.id)
            )
        ).scalar_one()
        claim_row = await insert_claim(
            session,
            entity_ref_id=entity_id,
            entity_type="learning",
            project_key=project_key,
            claim_key="b" * 64,
            statement="The nightly lock probe reads revision 057.",
            fact_name=_Probe.name,
            definition_version=1,
            target="production",
            expected={"path": "/revision", "op": "eq", "value": "057"},
            expected_resolved={"path": "/revision", "op": "eq", "value": "057"},
            validity_seconds=600,
            provenance="declared",
            declared_by="integration-test",
            declared_at=datetime.now(UTC),
        )
    claim_id = claim_row.id

    identity = await _measure_identity(migration_database_url)

    # A real transaction, not AUTOCOMMIT: the lock is transaction-scoped and
    # must survive until the explicit rollback below releases it.
    blocker = await engine.connect()
    lock_txn = await blocker.begin()
    try:
        await blocker.execute(sa.text("LOCK TABLE knowledge_claims IN ACCESS EXCLUSIVE MODE"))

        env = {
            **os.environ,
            "POSTGRES_URL": migration_database_url,
            "BRAIN_FACTS_PRODUCTION_IDENTITY": json.dumps(identity),
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "brain_v42.maintenance.claim_verify",
            "--run-date",
            _RUN_DATE.isoformat(),
            "--wet",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Give the CLI time to reach the SELECT ... FOR UPDATE it blocks on.
        await asyncio.sleep(3)
        assert proc.returncode is None, "the CLI exited before it should have blocked"

        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=10)
    finally:
        await lock_txn.rollback()
        await blocker.close()

    async with session_factory() as session:
        run_row = (
            (
                await session.execute(
                    sa.select(dream_runs).where(
                        dream_runs.c.run_date == _RUN_DATE, dream_runs.c.phase == "verify"
                    )
                )
            )
            .mappings()
            .one()
        )
        verdict_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )
    assert run_row["status"] == "fail"
    assert run_row["error_message"] == "verify started; no terminal status recorded"
    assert verdict_count == 0

    new_owner = VerifyRunOwnership(engine)
    assert await new_owner.acquire()
    await new_owner.release()
