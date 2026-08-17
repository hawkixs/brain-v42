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

from brain_v42.db.tables import dream_promotions, learnings, runbooks
from brain_v42.models.runbook import RunbookCreate, RunbookStep
from brain_v42.repositories.pg_runbook import PgRunbookRepo
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
async def session_factory(
    _engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    # PgRunbookRepo() uses brain_v42.db.engine.get_session_factory() (module-level
    # singleton), which gets pinned to whichever loop touched it first. For test
    # isolation, override it to return our per-test factory so both the repo and
    # the test read/write on the same engine bound to the current event loop.
    monkeypatch.setattr("brain_v42.repositories.pg_base.get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture
async def isolated_pk(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Unique project_key per test + teardown in FK-safe order
    (dream_promotions → runbooks → learnings). Caught 2026-04-21 after
    fixture rows had leaked into prod brain-v42 when BRAIN_V42_TEST_DB_URL
    pointed at the shared DB.
    """
    pk = make_unit_project_key("t-run-prom")
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
            await session.execute(runbooks.delete().where(runbooks.c.project_key == pk))
            await session.execute(learnings.delete().where(learnings.c.project_key == pk))


@pytest.mark.asyncio
async def test_create_with_promotion_writes_all_three_rows(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """create_with_promotion inserts runbook + updates learning.metadata + inserts dream_promotions."""
    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="t",
                        insight="i",
                        project_key=isolated_pk,
                        source_type="experience",
                        confidence="high",
                        tags=["dream:agent"],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()

    repo = PgRunbookRepo()
    title = f"Test Runbook {uuid.uuid4().hex[:8]}"
    data = RunbookCreate(
        title=title,
        description="desc",
        project_key=isolated_pk,
        trigger="on failure",
        steps=[RunbookStep(order=1, title="step1")],
        rollback_steps=[],
        tags=["dream:promoted"],
    )
    runbook = await repo.create_with_promotion(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        dream_run_id=None,
    )

    assert runbook.title == title

    async with session_factory() as session:
        lmeta = (
            await session.execute(
                sa.select(learnings.c.metadata).where(learnings.c.id == source_id)
            )
        ).scalar_one()
        assert lmeta["target_entity_id"] == str(runbook.id)
        assert "promoted_at" in lmeta

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
        assert prow["target_type"] == "runbook"
        assert prow["target_runbook_id"] == runbook.id
        assert prow["target_adr_id"] is None


@pytest.mark.asyncio
async def test_create_with_promotion_duplicate_source_raises(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_pk: str,
) -> None:
    """Second call for same source_learning_id raises (partial unique index)."""
    async with session_factory() as session:
        async with session.begin():
            source_id = (
                await session.execute(
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
            ).scalar_one()

    repo = PgRunbookRepo()
    data = RunbookCreate(
        title=f"RB {uuid.uuid4().hex[:8]}",
        description="d",
        project_key=isolated_pk,
        trigger="t",
        steps=[RunbookStep(order=1, title="s")],
        rollback_steps=[],
        tags=[],
    )
    await repo.create_with_promotion(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        dream_run_id=None,
    )
    with pytest.raises(sa.exc.IntegrityError):
        await repo.create_with_promotion(
            data=data,
            embedding=None,
            source_learning_id=source_id,
            dream_run_id=None,
        )
