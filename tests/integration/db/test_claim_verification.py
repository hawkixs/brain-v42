"""Real-PostgreSQL contracts for append-only claim verification."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
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
from brain_v42.facts.canonical import MAX_CANONICAL_BYTES, canonical_json
from brain_v42.facts.model import FactTarget, SourceIdentity
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.repositories.pg_claim_verdicts import VerdictRow
from brain_v42.repositories.pg_knowledge_claims import insert_claim
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


class _Probe:
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=30)
    timeout = timedelta(seconds=1)
    briefing = False
    policies: Mapping[str, int] = {}
    value_schema = {"lag": "int"}

    def __init__(self, name: str, value: int = 3, error: Exception | None = None) -> None:
        self.name = name
        self.value = value
        self.error = error
        self.runs = 0

    async def measure(self, source: object) -> Mapping[str, object]:
        self.runs += 1
        if self.error is not None:
            raise self.error
        return {"lag": self.value}


class _SlowProbe(_Probe):
    """Holds its measurement open, so a second transaction meets the claim's row lock."""

    def __init__(self, name: str, *, delay: float, value: int = 3) -> None:
        super().__init__(name, value=value)
        self.delay = delay
        self.started = asyncio.Event()
        self.finished_at: float | None = None

    async def measure(self, source: object) -> Mapping[str, object]:
        self.runs += 1
        self.started.set()
        await asyncio.sleep(self.delay)
        self.finished_at = time.monotonic()
        return {"lag": self.value}


class _FullSizeProbe(_Probe):
    """Returns a value whose canonical JSON is exactly the largest a fact may carry."""

    value_schema = {"lag": "int", "note": "string"}

    def __init__(self, name: str) -> None:
        super().__init__(name, value=3)
        base = len(canonical_json({"lag": 3, "note": ""}).encode("utf-8"))
        self.full_value: dict[str, object] = {
            "lag": 3,
            "note": "x" * (MAX_CANONICAL_BYTES - base),
        }

    async def measure(self, source: object) -> Mapping[str, object]:
        self.runs += 1
        return dict(self.full_value)


class _Source:
    async def identity(self) -> SourceIdentity:
        return SourceIdentity("1", "brain_test", "127.0.0.1", 5432)


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """Own a disposable DB because this module writes undeletable ledger rows."""
    with fresh_head_database(
        _get_integration_db_url_or_skip(), prefix="brain_claim_verdicts"
    ) as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


def _registry(probe: _Probe) -> FactRegistry:
    @asynccontextmanager
    async def source() -> AsyncIterator[_Source]:
        yield _Source()

    identity = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source},
        expected={FactTarget.PRODUCTION: identity},
    )
    registry.register(probe)
    registry.freeze()
    return registry


async def _insert_claim(
    session: AsyncSession, *, value_schema: Mapping[str, str] | None = None
) -> tuple[UUID, str, str, UUID]:
    """Write a project, an anchor, a definition and a claim in the CALLER's transaction."""
    project_key = f"verify-{uuid4().hex[:16]}"
    fact_name = f"verification_probe_{uuid4().hex}"
    await session.execute(
        sa.insert(project_contexts).values(
            project_key=project_key, name=project_key, description="verification fixture"
        )
    )
    entity_id = (
        await session.execute(
            sa.insert(brain_entities)
            .values(
                entity_type="learning",
                entity_key=f"verify-entity-{uuid4()}",
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
            value_schema=dict(value_schema or {"lag": "int"}),
            digest="a" * 64,
        )
    )
    row = await insert_claim(
        session,
        entity_ref_id=entity_id,
        entity_type="learning",
        project_key=project_key,
        claim_key="b" * 64,
        statement="The verified lag remains below five.",
        fact_name=fact_name,
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="integration-test",
        declared_at=datetime.now(UTC),
    )
    return row.id, fact_name, project_key, entity_id


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    value_schema: Mapping[str, str] | None = None,
) -> tuple[UUID, str, str]:
    async with session_factory() as session, session.begin():
        claim_id, fact_name, project_key, _ = await _insert_claim(
            session, value_schema=value_schema
        )
    return claim_id, fact_name, project_key


async def _count(
    session_factory: async_sessionmaker[AsyncSession], table: sa.Table, column: str, value: UUID
) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count()).select_from(table).where(table.c[column] == value)
        )
    return int(count or 0)


async def test_real_registry_measurement_appends_a_durable_holds_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing the append RETURNING path would lose the server-created durable evidence."""
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _Probe(fact_name, value=3)
    result = await ClaimVerificationService(_registry(probe), session_factory).verify(
        claim_id,
        issuer_identity="mcp:integration",
        issuer_kind="robot",
        idempotency_key="holds-1",
    )

    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    sa.select(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.id == result.id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert result.verdict == "holds"
    assert stored["measurement"] == result.measurement
    assert stored["measurement_digest"] == result.measurement_digest
    assert result.seq > 0
    assert probe.runs == 1


async def test_exact_duplicate_request_converges_on_one_row_and_one_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping idempotency before probing would make retry volume create new evidence."""
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _Probe(fact_name, value=9)
    service = ClaimVerificationService(_registry(probe), session_factory)
    first = await service.verify(
        claim_id, issuer_identity="mcp:integration", issuer_kind="robot", idempotency_key="same"
    )
    replay = await service.verify(
        claim_id, issuer_identity="mcp:integration", issuer_kind="robot", idempotency_key="same"
    )

    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(knowledge_claim_verdicts)
            .where(knowledge_claim_verdicts.c.claim_id == claim_id)
        )
    assert first == replay
    assert first.verdict == "falsified"
    assert count == 1
    assert probe.runs == 1


async def test_probe_failure_is_durable_unreadable_and_replays_without_another_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Turning probe failures into exceptions would make retry paths lose audit evidence."""
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _Probe(fact_name, error=TimeoutError())
    service = ClaimVerificationService(_registry(probe), session_factory)

    first = await service.verify(
        claim_id,
        issuer_identity="mcp:integration",
        issuer_kind="robot",
        idempotency_key="timeout",
    )
    replay = await service.verify(
        claim_id,
        issuer_identity="mcp:integration",
        issuer_kind="robot",
        idempotency_key="timeout",
    )

    assert first == replay
    assert first.verdict == "unreadable"
    assert first.reason == "probe:probe_error"
    assert first.measurement_digest is None
    assert probe.runs == 1


async def test_scope_refusal_does_not_probe_or_expose_a_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Applying project scope after lookup would reveal whether a foreign claim exists."""
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _Probe(fact_name)

    with pytest.raises(ClaimVerificationError) as error:
        await ClaimVerificationService(_registry(probe), session_factory).verify(
            claim_id,
            issuer_identity="mcp:integration",
            issuer_kind="robot",
            idempotency_key="scope",
            project_key="other-project",
        )

    assert error.value.code == "claim_not_found"
    assert probe.runs == 0


async def test_postgres_refuses_verdict_update_and_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing either append-only trigger would permit a stored server verdict to change."""
    claim_id, fact_name, _ = await _claim(session_factory)
    verdict = await ClaimVerificationService(_registry(_Probe(fact_name)), session_factory).verify(
        claim_id,
        issuer_identity="mcp:integration",
        issuer_kind="robot",
        idempotency_key="append-only",
    )

    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="knowledge_claim_verdicts is append-only"):
            async with session.begin_nested():
                await session.execute(
                    sa.update(knowledge_claim_verdicts)
                    .where(knowledge_claim_verdicts.c.id == verdict.id)
                    .values(reason="rewritten")
                )
        with pytest.raises(DBAPIError, match="knowledge_claim_verdicts is append-only"):
            async with session.begin_nested():
                await session.execute(
                    sa.delete(knowledge_claim_verdicts).where(
                        knowledge_claim_verdicts.c.id == verdict.id
                    )
                )


async def test_concurrent_duplicate_requests_converge_on_one_row_and_one_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Four retries racing: the row lock turns three of them into replays.

    Without `FOR UPDATE`, the losers would miss the winner's uncommitted row, measure,
    and hit the unique constraint -- an error for a retry that must replay.
    """
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _SlowProbe(fact_name, delay=0.3)
    service = ClaimVerificationService(_registry(probe), session_factory)

    results = await asyncio.gather(
        *(
            service.verify(
                claim_id,
                issuer_identity="mcp:integration",
                issuer_kind="robot",
                idempotency_key="concurrent",
            )
            for _ in range(4)
        )
    )

    assert all(result == results[0] for result in results)
    assert await _count(session_factory, knowledge_claim_verdicts, "claim_id", claim_id) == 1
    assert probe.runs == 1


async def test_a_second_key_forces_one_fresh_observation_instead_of_the_used_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first verdict leaves its observation in the registry cache.

    A second request under another key would read that cached observation again;
    `(claim_id, observation_id)` is unique, so the service must force ONE fresh
    reading and bind the new key to it.
    """
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _Probe(fact_name, value=3)
    service = ClaimVerificationService(_registry(probe), session_factory)

    first = await service.verify(
        claim_id, issuer_identity="mcp:integration", issuer_kind="robot", idempotency_key="key-a"
    )
    second = await service.verify(
        claim_id, issuer_identity="mcp:integration", issuer_kind="robot", idempotency_key="key-b"
    )

    assert first.observation_id != second.observation_id
    assert second.measurement["source_kind"] == "probe"
    assert second.seq > first.seq
    assert await _count(session_factory, knowledge_claim_verdicts, "claim_id", claim_id) == 2
    assert probe.runs == 2


async def test_a_caller_rollback_removes_claim_anchor_and_verdict_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Given the caller's session, the service must not commit anything of its own.

    The claim, its anchor and the verdict live or die with the caller's transaction:
    this is what lets a writer take its first verdict in the same request.
    """
    async with session_factory() as session:
        transaction = await session.begin()
        claim_id, fact_name, _, entity_id = await _insert_claim(session)
        verdict = await ClaimVerificationService(
            _registry(_Probe(fact_name)), session_factory
        ).verify(
            claim_id,
            issuer_identity="mcp:integration",
            issuer_kind="robot",
            idempotency_key="rollback",
            session=session,
        )
        assert verdict.verdict == "holds"
        await transaction.rollback()

    assert await _count(session_factory, knowledge_claim_verdicts, "id", verdict.id) == 0
    assert await _count(session_factory, knowledge_claims, "id", claim_id) == 0
    assert await _count(session_factory, brain_entities, "id", entity_id) == 0


async def test_a_concurrent_retirement_waits_for_the_verdict_then_refuses_the_next(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retirement takes the same row lock the verification holds through its probe.

    Without the lock, the retirement would land while the probe still runs, and a
    verdict would then be written for a claim already retired.
    """
    claim_id, fact_name, _ = await _claim(session_factory)
    probe = _SlowProbe(fact_name, delay=0.5)
    service = ClaimVerificationService(_registry(probe), session_factory)
    retired_at_monotonic: list[float] = []

    async def retire() -> None:
        await probe.started.wait()
        async with session_factory() as session, session.begin():
            await session.execute(
                sa.update(knowledge_claims)
                .where(knowledge_claims.c.id == claim_id)
                .values(retired_at=sa.func.now())
            )
            retired_at_monotonic.append(time.monotonic())

    verdict, _ = await asyncio.gather(
        service.verify(
            claim_id, issuer_identity="mcp:integration", issuer_kind="robot", idempotency_key="race"
        ),
        retire(),
    )

    assert isinstance(verdict, VerdictRow)
    assert verdict.verdict == "holds"
    assert probe.finished_at is not None
    assert retired_at_monotonic[0] >= probe.finished_at
    with pytest.raises(ClaimVerificationError) as error:
        await service.verify(
            claim_id,
            issuer_identity="mcp:integration",
            issuer_kind="robot",
            idempotency_key="after-retirement",
        )
    assert error.value.code == "claim_retired"
    assert probe.runs == 1


async def test_full_size_evidence_is_stored_whole_in_server_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A value at the largest canonical size a fact may carry is stored untruncated.

    Three verdicts on one claim come back in the order the server assigned, and the
    stored measurement is the whole value with its own digest.
    """
    claim_id, fact_name, _ = await _claim(
        session_factory, value_schema={"lag": "int", "note": "string"}
    )
    probe = _FullSizeProbe(fact_name)
    assert len(canonical_json(probe.full_value).encode("utf-8")) == MAX_CANONICAL_BYTES
    service = ClaimVerificationService(_registry(probe), session_factory)

    verdicts = [
        await service.verify(
            claim_id,
            issuer_identity="mcp:integration",
            issuer_kind="robot",
            idempotency_key=f"full-{index}",
        )
        for index in range(3)
    ]

    assert [verdict.seq for verdict in verdicts] == sorted(verdict.seq for verdict in verdicts)
    assert len({verdict.seq for verdict in verdicts}) == 3
    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    sa.select(knowledge_claim_verdicts)
                    .where(knowledge_claim_verdicts.c.claim_id == claim_id)
                    .order_by(knowledge_claim_verdicts.c.seq)
                )
            )
            .mappings()
            .all()
        )
    assert [row["id"] for row in stored] == [verdict.id for verdict in verdicts]
    for row in stored:
        assert row["verdict"] == "holds"
        assert row["measurement"]["value"] == probe.full_value
        assert row["measurement_digest"] is not None
