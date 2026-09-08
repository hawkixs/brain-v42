"""This directory runs on its own database, created and destroyed per session.

Ticket `f7af0977`. The tests here downgrade and re-upgrade their database, and a
downgrade that drops a column NEVER gives its `attnum` back: the `pg_attribute`
row survives with `attisdropped`, and `VACUUM FULL` does not reclaim it. Against
a SHARED `brain_test` that made the database's life finite, and the number is not
comfortable — measured 2026-09-03, ONE run of `tests/integration/db` burns **77
dropped columns per decay table**. PostgreSQL's hard ceiling is 1600 columns, so
the shared database survived roughly **20 runs**, and on 2026-09-02 it reached it:
`ALTER TABLE snippets ADD COLUMN` failed with `TooManyColumnsError`, `alembic
upgrade head` could not complete, and 162 tests failed for a reason none of them
was about.

Recreating `brain_test` by hand fixes the symptom and restarts the clock. This
moves the clock somewhere it cannot hurt: the whole directory binds to a database
built by the alembic chain for this session and dropped after it, so the burn is
thrown away with it.

The retargeting works by rebinding `BRAIN_V42_TEST_DB_URL` before the engine is
built. A module that read that variable at IMPORT time would bypass it silently —
imports happen during collection, before any fixture runs — which is why
`tests/unit/test_migration_tests_do_not_capture_the_shared_url.py` refuses that
shape in source, with no database of its own.

`run_migrations` from the parent conftest still runs against the real shared
database, on purpose: it is what applies the schema family guard, and an
`alembic upgrade head` on a database already at head costs nothing and burns no
column.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

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
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_contract_revisions,
    delivery_dependencies,
    delivery_events,
    delivery_receipts,
    delivery_snapshots,
    delivery_workflows,
)
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """A database at head for this session, dropped after it. No rebinding here.

    Built once because it costs 0.93 s and the tests here leave it at head; the
    burn they inflict is thrown away with the database rather than accumulated in
    a shared one.
    """
    # The parent's resolver, not the raw variable: it SKIPS both when the variable
    # is unset and when it is set to something rejected (a production database, a
    # malformed URL). Reading the environment directly turned a clean skip into an
    # error on a rejected value, which the loud-skip banner exists to prevent.
    shared_url = _get_integration_db_url_or_skip()

    with fresh_head_database(shared_url, prefix="brain_migration") as disposable_url:
        yield disposable_url


@pytest.fixture(autouse=True)
def _point_the_environment_at_the_disposable_database(
    migration_database_url: str,
) -> Iterator[None]:
    """Rebind for the duration of ONE test in this directory, and no longer.

    The rebind is the only channel the alembic subprocesses have: each builds its
    environment from `os.environ` at call time, so none of them needs to know this
    fixture exists.

    Function-scoped ON PURPOSE. A session-scoped rebind was the first shape tried
    and it LEAKED: `tests/integration/db` sorts before `tests/integration/mcp`, so
    the variable was still rebound when the parent's shared engine was first
    built, and eight `mcp` tests ran against a database that was dropped at the
    end of the session. Measured: 8 failed, 433 passed. Narrow is not a style
    preference here.
    """
    shared_url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    os.environ["BRAIN_V42_TEST_DB_URL"] = migration_database_url
    try:
        yield
    finally:
        if shared_url is None:
            del os.environ["BRAIN_V42_TEST_DB_URL"]
        else:
            os.environ["BRAIN_V42_TEST_DB_URL"] = shared_url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Overrides the shared-database engine, for this directory only.

    Deliberately NOT depending on `run_migrations`: this database comes out of
    `alembic upgrade head` already, and depending on the parent's fixture would
    order it before the rebind above.
    """
    disposable_engine = create_async_engine(migration_database_url, poolclass=NullPool, echo=False)
    yield disposable_engine
    await disposable_engine.dispose()


async def _delivery_workflow_ticket_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> frozenset[UUID]:
    async with session_factory() as session:
        ticket_ids = await session.scalars(sa.select(delivery_workflows.c.ticket_id))
        return frozenset(ticket_ids)


async def _delete_delivery_workflow_graphs(
    session_factory: async_sessionmaker[AsyncSession],
    ticket_ids: frozenset[UUID],
) -> None:
    """Delete only the workflow graphs explicitly owned by one delivery test."""
    if not ticket_ids:
        return

    owned_ticket_ids = tuple(ticket_ids)
    async with session_factory() as session:
        async with session.begin():
            binding_ids = tuple(
                await session.scalars(
                    sa.select(delivery_artifact_bindings.c.id).where(
                        delivery_artifact_bindings.c.ticket_id.in_(owned_ticket_ids)
                    )
                )
            )
            evidence_owner = delivery_snapshots.c.ticket_id.in_(owned_ticket_ids)
            confirmation_owner = delivery_confirmations.c.ticket_id.in_(owned_ticket_ids)
            if binding_ids:
                evidence_owner = sa.or_(
                    evidence_owner,
                    delivery_snapshots.c.binding_id.in_(binding_ids),
                )
                confirmation_owner = sa.or_(
                    confirmation_owner,
                    delivery_confirmations.c.binding_id.in_(binding_ids),
                )

            await session.execute(
                delivery_workflows.update()
                .where(delivery_workflows.c.ticket_id.in_(owned_ticket_ids))
                .values(
                    latest_context_success_confirmation_id=None,
                    latest_context_attempt_confirmation_id=None,
                )
            )
            await session.execute(
                delivery_artifact_bindings.update()
                .where(delivery_artifact_bindings.c.ticket_id.in_(owned_ticket_ids))
                .values(
                    latest_success_confirmation_id=None,
                    latest_attempt_confirmation_id=None,
                )
            )
            await session.execute(
                delivery_receipts.delete().where(
                    delivery_receipts.c.ticket_id.in_(owned_ticket_ids)
                )
            )
            await session.execute(
                delivery_events.delete().where(delivery_events.c.ticket_id.in_(owned_ticket_ids))
            )
            await session.execute(delivery_confirmations.delete().where(confirmation_owner))
            await session.execute(delivery_snapshots.delete().where(evidence_owner))
            await session.execute(
                delivery_artifact_bindings.delete().where(
                    delivery_artifact_bindings.c.ticket_id.in_(owned_ticket_ids)
                )
            )
            await session.execute(
                delivery_dependencies.delete().where(
                    sa.or_(
                        delivery_dependencies.c.ticket_id.in_(owned_ticket_ids),
                        delivery_dependencies.c.upstream_ticket_id.in_(owned_ticket_ids),
                    )
                )
            )
            # The workflow and its current revision point at each other with
            # ON DELETE RESTRICT. PostgreSQL can validate their final absence
            # when both deletes belong to one data-modifying statement.
            deleted_revisions = (
                delivery_contract_revisions.delete()
                .where(delivery_contract_revisions.c.ticket_id.in_(owned_ticket_ids))
                .cte("deleted_delivery_contract_revisions")
            )
            await session.execute(
                delivery_workflows.delete()
                .where(delivery_workflows.c.ticket_id.in_(owned_ticket_ids))
                .add_cte(deleted_revisions)
            )

    residue_filters = {
        "delivery_workflows": (
            delivery_workflows,
            delivery_workflows.c.ticket_id.in_(owned_ticket_ids),
        ),
        "delivery_contract_revisions": (
            delivery_contract_revisions,
            delivery_contract_revisions.c.ticket_id.in_(owned_ticket_ids),
        ),
        "delivery_dependencies": (
            delivery_dependencies,
            sa.or_(
                delivery_dependencies.c.ticket_id.in_(owned_ticket_ids),
                delivery_dependencies.c.upstream_ticket_id.in_(owned_ticket_ids),
            ),
        ),
        "delivery_artifact_bindings": (
            delivery_artifact_bindings,
            delivery_artifact_bindings.c.ticket_id.in_(owned_ticket_ids),
        ),
        "delivery_snapshots": (delivery_snapshots, evidence_owner),
        "delivery_confirmations": (delivery_confirmations, confirmation_owner),
        "delivery_receipts": (
            delivery_receipts,
            delivery_receipts.c.ticket_id.in_(owned_ticket_ids),
        ),
        "delivery_events": (
            delivery_events,
            delivery_events.c.ticket_id.in_(owned_ticket_ids),
        ),
    }
    async with session_factory() as session:
        residue = {
            name: count
            for name, (table, owner_filter) in residue_filters.items()
            if (
                count := await session.scalar(
                    sa.select(sa.func.count()).select_from(table).where(owner_filter)
                )
            )
        }
    assert not residue, f"delivery test cleanup left owned rows: {residue}"


@pytest_asyncio.fixture(autouse=True)
async def _isolate_delivery_workflow_history(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Remove history owned by delivery tests while preserving prior rows.

    Migration modules intentionally remove the delivery tables themselves, so
    their teardown must never issue delivery-table queries. Delivery tests use
    the stable ``test_delivery_*`` inventory boundary.
    """
    if not request.path.name.startswith("test_delivery_"):
        yield
        return

    prior_ticket_ids = await _delivery_workflow_ticket_ids(session_factory)
    try:
        yield
    finally:
        current_ticket_ids = await _delivery_workflow_ticket_ids(session_factory)
        await _delete_delivery_workflow_graphs(
            session_factory,
            current_ticket_ids - prior_ticket_ids,
        )
