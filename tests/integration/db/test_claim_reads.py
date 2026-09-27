"""Real PostgreSQL read contracts over a disposable append-only claim ledger."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
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
from brain_v42.repositories.pg_knowledge_claims import insert_claim
from brain_v42.services.claim_read_service import ClaimReadError, ClaimReadService
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration


# Session scope, not module: these fixtures shadow session-scoped conftest fixtures
# that consume `engine`; a module scope raises ScopeMismatch (see test_claim_write_path).
@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """Committed verdicts cannot be cleaned from the shared disposable DB."""
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claim_reads") as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def seeded(
    engine: AsyncEngine,
) -> tuple[async_sessionmaker[AsyncSession], dict[str, object]]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    projects = [f"read-{uuid4().hex[:14]}" for _ in range(2)]
    entries = [uuid4() for _ in range(3)]
    fact_name = f"claim_read_{uuid4().hex}"
    async with factory() as session, session.begin():
        for project in projects:
            await session.execute(
                sa.insert(project_contexts).values(
                    project_key=project, name=project, description="claim read fixture"
                )
            )
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
        anchors: list[UUID] = []
        for index, entry_id in enumerate(entries):
            project = projects[0] if index < 2 else projects[1]
            anchor = (
                await session.execute(
                    sa.insert(brain_entities)
                    .values(
                        entity_type="learning",
                        entity_key=f"claim-read-{uuid4()}",
                        source_uuid=entry_id,
                        project_key=project,
                        scope_kind="project",
                        lifecycle="active",
                    )
                    .returning(brain_entities.c.id)
                )
            ).scalar_one()
            anchors.append(anchor)
        claims = []
        for index, anchor in enumerate(anchors):
            claim = await insert_claim(
                session,
                entity_ref_id=anchor,
                entity_type="learning",
                project_key=projects[0] if index < 2 else projects[1],
                claim_key=f"{index + 1:064x}",
                statement=f"Lag claim {index}",
                fact_name=fact_name,
                definition_version=1,
                target="production",
                expected={"path": "/lag", "op": "lte", "value": 5},
                expected_resolved={"path": "/lag", "op": "lte", "value": 5},
                validity_seconds=60,
                provenance="declared",
                declared_by="tester",
                declared_at=now,
            )
            claims.append(claim)
        await session.execute(
            sa.update(knowledge_claims)
            .where(knowledge_claims.c.id == claims[1].id)
            .values(retired_at=now)
        )
        # Sequence, rather than the deliberately reversed observation clocks, picks the result.
        for index, (kind, seconds_ago, reason) in enumerate(
            [("holds", 10, None), ("falsified", 30, None), ("unreadable", 5, "probe:timeout")]
        ):
            await session.execute(
                sa.insert(knowledge_claim_verdicts).values(
                    claim_id=claims[0].id,
                    verdict=kind,
                    reason=reason,
                    measurement={"status": "measured", "value": {"lag": index}},
                    measurement_digest=None if kind == "unreadable" else "b" * 64,
                    observation_id=uuid4(),
                    issuer_identity="mcp:read-test",
                    issuer_kind="robot",
                    request_fingerprint="c" * 64,
                    outcome_fingerprint="d" * 64,
                    idempotency_key=f"read-{index}",
                    emitted_at=now - timedelta(seconds=seconds_ago),
                )
            )
    return factory, {
        "now": now,
        "projects": projects,
        "entries": entries,
        "claims": claims,
        "fact_name": fact_name,
    }


async def test_batch_is_one_select_by_authoritative_entry_and_scope(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]], engine: AsyncEngine
) -> None:
    factory, data = seeded
    entries = data["entries"]
    projects = data["projects"]
    statements: list[str] = []

    def collect(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", collect)
    try:
        result = await ClaimReadService(factory, clock=lambda: data["now"]).batch_summaries(
            [("learning", entry) for entry in entries], trusted_project_key=projects[0]
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", collect)
    assert len(statements) == 1
    assert statements[0].lstrip().startswith("SELECT")
    assert len(result[("learning", entries[0])]) == 1
    assert result[("learning", entries[1])] == ()  # Retired occurrences are omitted.
    assert result[("learning", entries[2])] == ()  # Trusted project scope is enforced.
    state = result[("learning", entries[0])][0]
    assert state.status == "falsified"
    assert state.conclusive.seq < state.latest.seq
    assert state.latest.reason == "probe:timeout"
    assert state.conclusive.emitted_at < state.conclusive.recorded_at


async def test_list_pages_reapply_scope_and_retirement_filters(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]],
) -> None:
    factory, data = seeded
    project = data["projects"][0]
    service = ClaimReadService(factory, clock=lambda: data["now"])
    first = await service.list_claims(
        project_key=project, include_retired=True, limit=1, trusted_project_key=project
    )
    second = await service.list_claims(
        project_key=project,
        include_retired=True,
        after_seq=first.next_after_seq,
        limit=1,
        trusted_project_key=project,
    )
    assert [state.claim.id for state in first.items] == [data["claims"][0].id]
    assert [state.claim.id for state in second.items] == [data["claims"][1].id]
    assert second.items[0].retired_at is not None
    assert second.next_after_seq is None
    active = await service.list_claims(project_key=project, trusted_project_key=project)
    assert [state.claim.id for state in active.items] == [data["claims"][0].id]
    foreign = await service.list_claims(
        project_key=data["projects"][1], trusted_project_key=project
    )
    assert foreign.items == ()


async def test_history_pages_use_sequence_and_empty_history_is_valid(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]],
) -> None:
    factory, data = seeded
    service = ClaimReadService(factory, clock=lambda: data["now"])
    first = await service.history(
        data["claims"][0].id, limit=2, trusted_project_key=data["projects"][0]
    )
    second = await service.history(
        data["claims"][0].id,
        after_seq=first.next_after_seq,
        limit=2,
        trusted_project_key=data["projects"][0],
    )
    assert [v.verdict for v in first.verdicts] == ["holds", "falsified"]
    assert [v.verdict for v in second.verdicts] == ["unreadable"]
    assert second.next_after_seq is None
    empty = await service.history(data["claims"][1].id, trusted_project_key=data["projects"][0])
    assert empty.verdicts == ()
    assert empty.claim.retired_at is not None


async def test_foreign_and_missing_history_share_refusal(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]],
) -> None:
    factory, data = seeded
    service = ClaimReadService(factory, clock=lambda: data["now"])
    codes = []
    for claim_id in (data["claims"][2].id, uuid4()):
        with pytest.raises(ClaimReadError) as error:
            await service.history(claim_id, trusted_project_key=data["projects"][0])
        codes.append(error.value.code)
    assert codes == ["claim_not_found", "claim_not_found"]


async def test_moved_anchor_cannot_expose_immutable_claim_project(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]],
) -> None:
    factory, data = seeded
    old_project = f"read-old-{uuid4().hex[:12]}"
    new_project = f"read-new-{uuid4().hex[:12]}"
    entry_id = uuid4()
    async with factory() as session, session.begin():
        for project in (old_project, new_project):
            await session.execute(
                sa.insert(project_contexts).values(
                    project_key=project, name=project, description="moved anchor fixture"
                )
            )
        anchor_id = (
            await session.execute(
                sa.insert(brain_entities)
                .values(
                    entity_type="learning",
                    entity_key=f"moved-anchor-{uuid4()}",
                    source_uuid=entry_id,
                    project_key=old_project,
                    scope_kind="project",
                    lifecycle="active",
                )
                .returning(brain_entities.c.id)
            )
        ).scalar_one()
        claim = await insert_claim(
            session,
            entity_ref_id=anchor_id,
            entity_type="learning",
            project_key=old_project,
            claim_key="e" * 64,
            statement="Old project evidence",
            fact_name=data["fact_name"],
            definition_version=1,
            target="production",
            expected={"path": "/lag", "op": "lte", "value": 5},
            expected_resolved={"path": "/lag", "op": "lte", "value": 5},
            validity_seconds=60,
            provenance="declared",
            declared_by="tester",
            declared_at=data["now"],
        )
        await session.execute(
            sa.update(brain_entities)
            .where(brain_entities.c.id == anchor_id)
            .values(project_key=new_project)
        )
    service = ClaimReadService(factory, clock=lambda: data["now"])
    batch = await service.batch_summaries([("learning", entry_id)], trusted_project_key=new_project)
    assert batch[("learning", entry_id)] == ()
    assert (await service.list_claims(trusted_project_key=new_project)).items == ()
    with pytest.raises(ClaimReadError) as error:
        await service.history(claim.id, trusted_project_key=new_project)
    assert error.value.code == "claim_not_found"


async def test_reads_leave_claim_and_verdict_ledgers_unchanged(
    seeded: tuple[async_sessionmaker[AsyncSession], dict[str, object]],
) -> None:
    factory, data = seeded

    async def counts() -> tuple[int, int]:
        async with factory() as session:
            return (
                await session.scalar(sa.select(sa.func.count()).select_from(knowledge_claims)),
                await session.scalar(
                    sa.select(sa.func.count()).select_from(knowledge_claim_verdicts)
                ),
            )

    before = await counts()
    service = ClaimReadService(factory, clock=lambda: data["now"])
    await service.batch_summaries([("learning", data["entries"][0])])
    await service.list_claims(include_retired=True)
    await service.history(data["claims"][0].id)
    assert await counts() == before
