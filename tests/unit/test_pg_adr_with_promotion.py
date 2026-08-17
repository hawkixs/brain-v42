from __future__ import annotations

import uuid

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

from brain_v42.db.tables import adrs, dream_promotions, learnings
from brain_v42.models.adr import ADRCreate, AlternativeConsidered
from brain_v42.repositories.pg_adr import PgADRRepo, SourceLearningNotFound
from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def isolated_pk(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Unique project_key per test + teardown that deletes every row created
    under that key in FK-safe order (dream_promotions → adrs → learnings).
    Prevents fixtures from persisting in a real DB if the tests are ever
    pointed at a shared instance — caught 2026-04-21 after 250 fixture rows
    had leaked into prod brain-v42.
    """
    pk = make_unit_project_key("t-adr-prom")
    yield pk
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                dream_promotions.delete().where(
                    dream_promotions.c.source_learning_id.in_(
                        sa.select(learnings.c.id).where(learnings.c.project_key == pk)
                    )
                )
            )
            await session.execute(adrs.delete().where(adrs.c.project_key == pk))
            await session.execute(learnings.delete().where(learnings.c.project_key == pk))


@pytest.mark.asyncio
async def test_create_with_promotion_writes_all_three_rows(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """create_with_promotion inserts adr + updates learning.metadata + inserts dream_promotions in one transaction."""
    # Seed a learning row.
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                learnings.insert()
                .values(
                    topic="test topic",
                    insight="test insight",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=["dream:agent"],
                )
                .returning(learnings.c.id)
            )
            source_id = result.scalar_one()

    repo = PgADRRepo(session_factory)
    data = ADRCreate(
        title="Test ADR",
        context="Some context.",
        decision="Some decision.",
        consequences="Some consequences.",
        project_key=isolated_pk,
        alternatives_considered=[AlternativeConsidered(title="A", description="r")],
        tags=["dream:promoted"],
    )
    adr = await repo.create_with_promotion(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=None,
    )

    assert adr.status == "accepted"
    assert adr.decided_at is not None

    async with session_factory() as session:
        # Learning metadata updated
        lrow = (
            await session.execute(
                sa.select(learnings.c.metadata).where(learnings.c.id == source_id)
            )
        ).scalar_one()
        assert lrow["target_entity_id"] == str(adr.id)
        assert "promoted_at" in lrow

        # dream_promotions row inserted
        prow = (
            (
                await session.execute(
                    sa.select(dream_promotions).where(
                        dream_promotions.c.source_learning_id == source_id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert prow["target_type"] == "adr"
        assert prow["target_adr_id"] == adr.id
        assert prow["target_runbook_id"] is None


@pytest.mark.asyncio
async def test_create_with_promotion_aborts_on_duplicate_source(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """Second call for same source_learning_id raises (partial unique index)."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                learnings.insert()
                .values(
                    topic="dup",
                    insight="dup",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=[],
                )
                .returning(learnings.c.id)
            )
            source_id = result.scalar_one()

    repo = PgADRRepo(session_factory)
    data = ADRCreate(
        title="ADR1",
        context="c",
        decision="d",
        consequences="q",
        project_key=isolated_pk,
        alternatives_considered=[],
        tags=[],
    )
    await repo.create_with_promotion(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=None,
    )
    with pytest.raises(sa.exc.IntegrityError):
        await repo.create_with_promotion(
            data=data,
            embedding=None,
            source_learning_id=source_id,
            auto_accept=True,
            dream_run_id=None,
        )


@pytest.mark.asyncio
async def test_create_with_promotion_proposed_when_auto_accept_false(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """auto_accept=False → status='proposed' + decided_at=None."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                learnings.insert()
                .values(
                    topic="t",
                    insight="i",
                    project_key=isolated_pk,
                    source_type="experience",
                    confidence="high",
                    tags=[],
                )
                .returning(learnings.c.id)
            )
            source_id = result.scalar_one()

    repo = PgADRRepo(session_factory)
    adr = await repo.create_with_promotion(
        data=ADRCreate(
            title="t",
            context="c",
            decision="d",
            consequences="q",
            project_key=isolated_pk,
            alternatives_considered=[],
            tags=[],
        ),
        embedding=None,
        source_learning_id=source_id,
        auto_accept=False,
        dream_run_id=None,
    )
    assert adr.status == "proposed"
    assert adr.decided_at is None


@pytest.mark.asyncio
async def test_create_with_promotion_raises_on_missing_source(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """Non-existent source_learning_id raises SourceLearningNotFound (not generic IntegrityError)."""
    repo = PgADRRepo(session_factory)
    data = ADRCreate(
        title="t",
        context="c",
        decision="d",
        consequences="q",
        project_key=isolated_pk,
        alternatives_considered=[],
        tags=[],
    )
    with pytest.raises(SourceLearningNotFound):
        await repo.create_with_promotion(
            data=data,
            embedding=None,
            source_learning_id=uuid.uuid4(),  # random, not in DB
            auto_accept=True,
            dream_run_id=None,
        )
