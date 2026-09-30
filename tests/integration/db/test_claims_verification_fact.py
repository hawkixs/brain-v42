"""Database evidence for the nightly claim-verification fact."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
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
    dream_runs,
    knowledge_claim_verdicts,
    knowledge_claims,
    knowledge_fact_definitions,
    project_contexts,
)
from brain_v42.facts.model import FactTarget, SourceIdentity, Unreadable
from brain_v42.facts.nightly import issuer_for, key_for
from brain_v42.facts.probes.claims_verification_last_night import ClaimsVerificationLastNightProbe
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.sources import PostgresSourceFactory, PostgresSourceSession
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


# The session-scoped pair shadows the directory's shared database, so the
# conftest guards (`check_db_connection` consumes `engine` at session scope)
# never touch `brain_test`. The tests themselves use `fact_engine`: the
# "no wet run" and "no claims" states need an EMPTY database each, and the
# claim ledger is append-only, so a shared one cannot be reset between tests.


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimfact") as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def fact_database_url() -> Iterator[str]:
    # Synchronous on purpose: `fresh_head_database` drives its own event loop.
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimfact") as url:
        yield url


@pytest_asyncio.fixture
async def fact_engine(fact_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(fact_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _run(session: AsyncSession, run_date: date, *, dry: bool, status: str) -> int:
    return (
        await session.execute(
            sa.insert(dream_runs)
            .values(
                run_date=run_date,
                phase="verify",
                project_key="*",
                phase_dry_run=dry,
                status=status,
            )
            .returning(dream_runs.c.id)
        )
    ).scalar_one()


async def _claim(session: AsyncSession, *, retired: bool = False) -> UUID:
    project = f"fact-{uuid4().hex[:16]}"
    fact = f"claim_fact_{uuid4().hex}"
    await session.execute(
        sa.insert(project_contexts).values(
            project_key=project, name=project, description="fact test"
        )
    )
    entity = (
        await session.execute(
            sa.insert(brain_entities)
            .values(
                entity_type="learning",
                entity_key=f"claim-fact-{uuid4()}",
                project_key=project,
                scope_kind="project",
                lifecycle="active",
            )
            .returning(brain_entities.c.id)
        )
    ).scalar_one()
    await session.execute(
        sa.insert(knowledge_fact_definitions).values(
            fact_name=fact,
            definition_version=1,
            target="production",
            ttl_seconds=60,
            timeout_seconds=3,
            policies={},
            value_schema={"lag": "int"},
            digest="a" * 64,
        )
    )
    return (
        await session.execute(
            sa.insert(knowledge_claims)
            .values(
                entity_ref_id=entity,
                entity_type="learning",
                project_key=project,
                claim_key=uuid4().hex * 2,
                statement="The measured lag remains below five.",
                fact_name=fact,
                definition_version=1,
                target="production",
                expected={"path": "/lag", "op": "lte", "value": 5},
                expected_resolved={"path": "/lag", "op": "lte", "value": 5},
                validity_seconds=3600,
                provenance="declared",
                declared_by="fact-test",
                declared_at=datetime.now(UTC),
                retired_at=datetime.now(UTC) if retired else None,
            )
            .returning(knowledge_claims.c.id)
        )
    ).scalar_one()


async def _verdict(
    session: AsyncSession,
    claim_id: UUID,
    verdict: str,
    issuer: str,
    key: str,
    emitted_at: datetime,
) -> None:
    await session.execute(
        sa.insert(knowledge_claim_verdicts).values(
            claim_id=claim_id,
            verdict=verdict,
            reason=None,
            measurement={"fact": "claim_fact", "definition_version": 1, "target": "production"},
            measurement_digest=None,
            observation_id=uuid4(),
            issuer_identity=issuer,
            issuer_kind="robot",
            request_fingerprint="a" * 64,
            outcome_fingerprint="b" * 64,
            idempotency_key=key,
            emitted_at=emitted_at,
        )
    )


async def _measure(engine: AsyncEngine) -> dict[str, object]:
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        return dict(
            await ClaimsVerificationLastNightProbe().measure(PostgresSourceSession(session))
        )


async def _measure_via_registry(engine: AsyncEngine) -> Unreadable:
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: PostgresSourceFactory(async_sessionmaker(engine))},
        expected={
            FactTarget.PRODUCTION: SourceIdentity(
                system_identifier="1",
                database="disposable",
                server_addr="127.0.0.1",
                server_port=5432,
            )
        },
    )
    registry.register(ClaimsVerificationLastNightProbe())
    registry.freeze()
    try:
        result = await registry.measure("claims_verification_last_night")
    finally:
        await registry.aclose()
    assert isinstance(result, Unreadable)
    return result


async def test_no_wet_run_is_unreadable_even_when_a_dry_run_exists(
    fact_engine: AsyncEngine,
) -> None:
    with pytest.raises(ValueError, match="no wet verify run"):
        await _measure(fact_engine)
    assert (await _measure_via_registry(fact_engine)).error_code == "probe_error"
    async with AsyncSession(fact_engine) as session, session.begin():
        await _run(session, date(2026, 9, 28), dry=True, status="done")
    with pytest.raises(ValueError, match="no wet verify run"):
        await _measure(fact_engine)
    assert (await _measure_via_registry(fact_engine)).error_code == "probe_error"


async def test_wet_done_run_with_no_claims_reports_measured_zeroes(
    fact_engine: AsyncEngine,
) -> None:
    async with AsyncSession(fact_engine) as session, session.begin():
        run_id = await _run(session, date(2026, 9, 27), dry=False, status="done")
    assert await _measure(fact_engine) == {
        "run_date": "2026-09-27",
        "run_id": run_id,
        "status": "done",
        "holds": 0,
        "falsified": 0,
        "unreadable": 0,
        "verdicts": 0,
        "eligible_now": 0,
    }


async def test_run_order_key_bound_counts_and_uncapped_eligibility(
    fact_engine: AsyncEngine,
) -> None:
    run_date = date(2026, 9, 27)
    now = datetime.now(UTC)
    async with AsyncSession(fact_engine) as session, session.begin():
        run_id = await _run(session, run_date, dry=False, status="partial")
        await _run(session, run_date, dry=False, status="fail")
        await _run(session, date(2026, 9, 28), dry=True, status="done")
        fresh = await _claim(session)
        stale = await _claim(session)
        retry = await _claim(session)
        never = await _claim(session)
        retired = await _claim(session, retired=True)
        issuer, key = issuer_for(run_id), key_for(run_date)
        await _verdict(session, fresh, "holds", issuer, key, now)
        await _verdict(session, stale, "falsified", issuer, key, now - timedelta(days=1))
        await _verdict(session, retry, "holds", "test:conclusive", "prior", now)
        await _verdict(session, retry, "unreadable", issuer, key, now)
        await _verdict(session, retired, "holds", issuer, key, now)
        await _verdict(session, never, "unreadable", issuer, "wrong-key", now)
        await _verdict(session, never, "unreadable", "test:other", key, now)

    assert await _measure(fact_engine) == {
        "run_date": "2026-09-27",
        "run_id": run_id,
        "status": "partial",
        "holds": 2,
        "falsified": 1,
        "unreadable": 1,
        "verdicts": 4,
        "eligible_now": 3,
    }

    async with AsyncSession(fact_engine) as session, session.begin():
        newer = await _run(session, date(2026, 9, 29), dry=False, status="timeout")
    latest = await _measure(fact_engine)
    assert latest["run_id"] == newer
    assert latest["status"] == "timeout"
    assert latest["verdicts"] == 0
    assert latest["eligible_now"] == 3
