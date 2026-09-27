"""Real-PostgreSQL contracts for the nightly selection query (ADR 27 lot C, T1.3).

NOTE for reviewers: this module could not be executed against a real PostgreSQL
in the container that wrote it (no `BRAIN_V42_TEST_DB_URL`, no reachable
server) — it SKIPS cleanly via `_get_integration_db_url_or_skip`, per this
repository's own convention for DB-backed tests. The LATERAL-join selection
SQL in `repositories/pg_claim_nightly.py` is therefore UNVERIFIED against a
real server and must be run here before this PR merges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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
    knowledge_claim_verdicts,
    knowledge_claims,
    knowledge_fact_definitions,
    project_contexts,
)
from brain_v42.repositories.pg_claim_nightly import eligible_fact_names, select_nightly_claims
from brain_v42.repositories.pg_knowledge_claims import insert_claim
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimnight") as url:
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


async def _insert_claim(
    session: AsyncSession,
    *,
    project_key: str | None = None,
    validity_seconds: int = 600,
    retired: bool = False,
) -> tuple[UUID, str, str]:
    project_key = project_key or f"nightly-{uuid4().hex[:16]}"
    fact_name = f"nightly_probe_{uuid4().hex}"
    exists = (
        await session.execute(
            sa.select(project_contexts.c.project_key).where(
                project_contexts.c.project_key == project_key
            )
        )
    ).first()
    if exists is None:
        await session.execute(
            sa.insert(project_contexts).values(
                project_key=project_key, name=project_key, description="nightly selection fixture"
            )
        )
    entity_id = (
        await session.execute(
            sa.insert(brain_entities)
            .values(
                entity_type="learning",
                entity_key=f"nightly-entity-{uuid4()}",
                project_key=project_key,
                scope_kind="project",
                lifecycle="active",
            )
            .returning(brain_entities.c.id)
        )
    ).scalar_one()
    await session.execute(
        sa.insert(knowledge_fact_definitions).values(
            fact_name=fact_name,
            definition_version=1,
            target="production",
            ttl_seconds=30,
            timeout_seconds=1,
            policies={},
            value_schema={"lag": "int"},
            digest="a" * 64,
        )
    )
    row = await insert_claim(
        session,
        entity_ref_id=entity_id,
        entity_type="learning",
        project_key=project_key,
        claim_key="b" * 64,
        statement="The nightly-selected lag remains below five.",
        fact_name=fact_name,
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=validity_seconds,
        provenance="declared",
        declared_by="integration-test",
        declared_at=datetime.now(UTC),
    )
    if retired:
        await session.execute(
            sa.update(knowledge_claims)
            .where(knowledge_claims.c.id == row.id)
            .values(retired_at=datetime.now(UTC))
        )
    return row.id, fact_name, project_key


async def _insert_verdict(
    session: AsyncSession, *, claim_id: UUID, verdict: str, emitted_at: datetime
) -> None:
    await session.execute(
        sa.insert(knowledge_claim_verdicts).values(
            claim_id=claim_id,
            verdict=verdict,
            reason=None,
            measurement={"fact": "nightly", "definition_version": 1, "target": "production"},
            measurement_digest=None,
            observation_id=uuid4(),
            issuer_identity="test:nightly-seed",
            issuer_kind="robot",
            request_fingerprint="a" * 64,
            outcome_fingerprint="b" * 64,
            idempotency_key=f"seed-{uuid4()}",
            emitted_at=emitted_at,
        )
    )


async def test_a_never_verified_claim_is_selected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        claim_id, fact_name, _ = await _insert_claim(session)

    selected = await select_nightly_claims(session_factory, now=datetime.now(UTC), max_claims=200)

    assert claim_id in {claim.id for claim in selected}
    match = next(claim for claim in selected if claim.id == claim_id)
    assert match.fact_name == fact_name


async def test_expiry_boundary_is_inclusive_and_measured_from_emitted_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`now == emitted_at + validity` selects; one second earlier does not."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        expired_id, _, _ = await _insert_claim(session, validity_seconds=600)
        await _insert_verdict(
            session, claim_id=expired_id, verdict="holds", emitted_at=now - timedelta(seconds=600)
        )

        fresh_id, _, _ = await _insert_claim(session, validity_seconds=600)
        await _insert_verdict(
            session, claim_id=fresh_id, verdict="holds", emitted_at=now - timedelta(seconds=599)
        )

    selected = await select_nightly_claims(session_factory, now=now, max_claims=200)
    ids = {claim.id for claim in selected}

    assert expired_id in ids
    assert fresh_id not in ids


async def test_expiry_follows_emitted_at_never_recorded_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A claim recorded moments ago must still expire on an old conclusive `emitted_at`."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        claim_id, _, _ = await _insert_claim(session, validity_seconds=60)
        # recorded_at defaults to now(); the conclusive verdict is far older than validity_seconds.
        await _insert_verdict(
            session, claim_id=claim_id, verdict="holds", emitted_at=now - timedelta(days=1)
        )

    selected = await select_nightly_claims(session_factory, now=now, max_claims=200)

    assert claim_id in {claim.id for claim in selected}


async def test_falsified_counts_as_conclusive_and_expires_the_same_way(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        claim_id, _, _ = await _insert_claim(session, validity_seconds=600)
        await _insert_verdict(
            session, claim_id=claim_id, verdict="falsified", emitted_at=now - timedelta(seconds=1)
        )

    selected = await select_nightly_claims(session_factory, now=now, max_claims=200)

    assert claim_id not in {claim.id for claim in selected}


async def test_unreadable_latest_after_a_still_valid_holds_is_selected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Q1: re-select a claim whose latest attempt is unreadable, even if `holds` is still fresh."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        claim_id, _, _ = await _insert_claim(session, validity_seconds=600)
        await _insert_verdict(
            session, claim_id=claim_id, verdict="holds", emitted_at=now - timedelta(seconds=10)
        )
        await _insert_verdict(
            session, claim_id=claim_id, verdict="unreadable", emitted_at=now - timedelta(seconds=5)
        )

    selected = await select_nightly_claims(session_factory, now=now, max_claims=200)

    assert claim_id in {claim.id for claim in selected}


async def test_a_retired_claim_is_never_selected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        claim_id, _, _ = await _insert_claim(session, retired=True)

    selected = await select_nightly_claims(session_factory, now=datetime.now(UTC), max_claims=200)

    assert claim_id not in {claim.id for claim in selected}


async def test_per_project_round_robin_ranking_with_a_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Oldest of each project first, then the second oldest, ... under LIMIT."""
    now = datetime.now(UTC)
    project_a = f"nightly-a-{uuid4().hex[:8]}"
    project_b = f"nightly-b-{uuid4().hex[:8]}"
    project_c = f"nightly-c-{uuid4().hex[:8]}"
    async with session_factory() as session, session.begin():
        a_ids = [(await _insert_claim(session, project_key=project_a))[0] for _ in range(5)]
        b_ids = [(await _insert_claim(session, project_key=project_b))[0] for _ in range(2)]
        c_ids = [(await _insert_claim(session, project_key=project_c))[0] for _ in range(1)]

    selected = await select_nightly_claims(session_factory, now=now, max_claims=4)
    selected_ids = [claim.id for claim in selected]

    # rank 1 of every project (insertion order == age order here, oldest first), then rank 2.
    assert set(selected_ids[:3]) == {a_ids[0], b_ids[0], c_ids[0]}
    assert selected_ids[3] == a_ids[1]


async def test_eligible_fact_names_ignores_the_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        _, fact_one, _ = await _insert_claim(session)
        _, fact_two, _ = await _insert_claim(session)

    names = await eligible_fact_names(session_factory, now=datetime.now(UTC))

    assert fact_one in names
    assert fact_two in names


async def test_select_nightly_claims_opens_a_read_only_transaction(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Prove the function itself requests READ ONLY, by capturing its executed SQL."""
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await select_nightly_claims(session_factory, now=datetime.now(UTC), max_claims=200)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert any("READ ONLY" in statement.upper() for statement in statements)


async def test_eligible_fact_names_opens_a_read_only_transaction(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    statements: list[str] = []

    def _capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await eligible_fact_names(session_factory, now=datetime.now(UTC))
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert any("READ ONLY" in statement.upper() for statement in statements)
