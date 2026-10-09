"""Exercise bootstrap resume fencing only on a disposable PostgreSQL database."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid5

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo
from brain_v42.scripts.graph_bootstrap import BOOTSTRAP_NAMESPACE
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import asyncpg_dsn, fresh_head_database, run_sql

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bootstrap_database_url() -> Iterator[str]:
    """Recovery rewrites the entire outbox, so the shared test database is unsuitable."""
    shared_url = _get_integration_db_url_or_skip()
    try:
        run_sql(asyncpg_dsn(shared_url), ["SELECT 1"])
    except Exception:  # noqa: BLE001 - availability probe must not expose credentials
        pytest.skip("PostgreSQL test database is not reachable")
    with fresh_head_database(shared_url, prefix="brain_bootstrap") as private_url:
        yield private_url


@pytest_asyncio.fixture
async def bootstrap_repo(bootstrap_database_url: str) -> AsyncIterator[PgGraphLedgerRepo]:
    engine = create_async_engine(bootstrap_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin() as outer:
                factory = async_sessionmaker(
                    connection,
                    class_=AsyncSession,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                async with factory.begin() as session:
                    await session.execute(
                        sa.text(
                            "UPDATE graph_projection_leases SET owner = NULL, leased_until = NULL, "
                            "recovery_id = NULL, recovery_phase = 'idle', "
                            "last_completed_recovery_id = NULL WHERE slot = 'neo4j'"
                        )
                    )
                yield PgGraphLedgerRepo(factory)
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("phase", ["prepared", "neo_ready"])
async def test_same_id_and_worker_resume_live_lease(
    bootstrap_repo: PgGraphLedgerRepo,
    phase: str,
) -> None:
    recovery_id = uuid5(BOOTSTRAP_NAMESPACE, "a1" * 32)
    worker_id = "graph-bootstrap-" + str(uuid5(BOOTSTRAP_NAMESPACE, str(recovery_id)))
    started = await bootstrap_repo.prepare_projection_recovery(
        recovery_id,
        worker_id,
        lease_seconds=600,
    )
    assert started is not None and started.status == "started"
    assert started.lease is not None
    state = started.lease
    if phase == "neo_ready":
        ready = await bootstrap_repo.mark_projection_recovery_neo_ready(state, lease_seconds=600)
        assert ready is not None
        state = ready

    resumed = await bootstrap_repo.prepare_projection_recovery(
        recovery_id,
        worker_id,
        lease_seconds=600,
    )

    assert resumed is not None and resumed.status == "resumed"
    assert resumed.lease is not None
    assert resumed.lease.owner_id == worker_id
    assert resumed.lease.generation == state.generation
    assert resumed.lease.phase == phase
    assert resumed.requeued is None


@pytest.mark.parametrize("different_id", [False, True])
async def test_live_recovery_refuses_other_owner_or_id(
    bootstrap_repo: PgGraphLedgerRepo,
    different_id: bool,
) -> None:
    recovery_id = uuid5(BOOTSTRAP_NAMESPACE, "a1" * 32)
    worker_id = "graph-bootstrap-" + str(uuid5(BOOTSTRAP_NAMESPACE, str(recovery_id)))
    started = await bootstrap_repo.prepare_projection_recovery(
        recovery_id,
        worker_id,
        lease_seconds=600,
    )
    assert started is not None and started.lease is not None
    attempted_id = uuid5(BOOTSTRAP_NAMESPACE, "b2" * 32) if different_id else recovery_id
    attempted_worker = worker_id if different_id else "other-worker"

    refused = await bootstrap_repo.prepare_projection_recovery(
        attempted_id,
        attempted_worker,
        lease_seconds=600,
    )

    assert refused is None
    resumed = await bootstrap_repo.prepare_projection_recovery(
        recovery_id,
        worker_id,
        lease_seconds=600,
    )
    assert resumed is not None and resumed.lease is not None
    assert resumed.lease.generation == started.lease.generation
