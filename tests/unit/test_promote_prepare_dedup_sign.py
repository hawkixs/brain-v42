"""Sign-symmetry guard for the PROMOTE dedup shadow verdict (W25 lot 1, graft
from angle A).

Real, verified trap: `pg_adr.py:605` returns `distance = 1.0 - similarity`
while `pg_runbook.py:345` returns `similarity` — same method name
(`semantic_search`), opposite sign, so an ADR duplicate at 0.93 would arrive
as 0.07 if `promote_prepare` ever routed through those high-level repos.

`compute_dedup` never calls `PgADRRepo`/`PgRunbookRepo` at all — it reads
`1 - (x.embedding <=> src.embedding)` directly, the same expression
`pg_base.search_vector` exposes as `similarity`. This test pins that the
resulting number is SIMILARITY (increasing toward 1 as embeddings get
closer), for BOTH families, at the point this lot reads them — so an
accidental future refactor toward the ADR repo's inverted `distance` would
be caught immediately, not sixteen nights later.
"""

from __future__ import annotations

import math
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream import promote_prepare
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.config import Settings
from brain_v42.db.tables import _EMBEDDING_DIM, adrs, learnings, runbooks
from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key


def _base_embedding() -> list[float]:
    return [1.0] + [0.0] * (_EMBEDDING_DIM - 1)


def _embedding_at_cosine(cosine: float) -> list[float]:
    return [cosine, math.sqrt(1.0 - cosine**2)] + [0.0] * (_EMBEDDING_DIM - 2)


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
async def isolated_pk() -> str:
    return make_unit_project_key("dedup-sign")


@pytest.fixture
def settings() -> Settings:
    return Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")


async def _seed_learning(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> uuid.UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic="sign-check",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=[],
                        embedding=_base_embedding(),
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()


async def _seed_adr(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    number: int,
    title: str,
    embedding: list[float],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                adrs.insert().values(
                    number=number,
                    title=title,
                    context="c",
                    decision="d",
                    consequences="e",
                    project_key=project_key,
                    embedding=embedding,
                )
            )


async def _seed_runbook(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    *,
    title: str,
    embedding: list[float],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                runbooks.insert().values(
                    title=title,
                    description="d",
                    project_key=project_key,
                    trigger="t",
                    embedding=embedding,
                )
            )


@pytest.mark.asyncio
async def test_adr_family_raw_cosine_increases_with_closeness(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """A CLOSER ADR must score HIGHER, never lower (guards the pg_adr.py:605
    distance inversion — `1.0 - similarity` would make this assertion fail).
    """
    learning_id = await _seed_learning(session_factory, isolated_pk)
    await _seed_adr(
        session_factory, isolated_pk, number=1, title="far", embedding=_embedding_at_cosine(0.2)
    )
    await _seed_adr(
        session_factory, isolated_pk, number=2, title="close", embedding=_embedding_at_cosine(0.9)
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    top3 = dedup["adr"]["top3"]
    assert len(top3) == 2
    # Ordered nearest-first: the CLOSER embedding (0.9) must rank above the
    # farther one (0.2), and its reported raw_cosine must be the LARGER one.
    assert top3[0]["title"] == "close"
    assert top3[0]["raw_cosine"] > top3[1]["raw_cosine"]
    assert top3[0]["raw_cosine"] == pytest.approx(0.9, abs=1e-4)
    assert top3[1]["raw_cosine"] == pytest.approx(0.2, abs=1e-4)


@pytest.mark.asyncio
async def test_runbook_family_raw_cosine_increases_with_closeness(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Same guarantee for runbooks — `pg_runbook.py:345` happens to already
    return similarity (not inverted), but this lot must not regress that by
    accident either. Same assertion shape as the ADR twin, deliberately: any
    future refactor that shares code between the two families must satisfy
    both.
    """
    learning_id = await _seed_learning(session_factory, isolated_pk)
    await _seed_runbook(
        session_factory, isolated_pk, title="far", embedding=_embedding_at_cosine(0.3)
    )
    await _seed_runbook(
        session_factory, isolated_pk, title="close", embedding=_embedding_at_cosine(0.85)
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    top3 = dedup["runbook"]["top3"]
    assert len(top3) == 2
    assert top3[0]["title"] == "close"
    assert top3[0]["raw_cosine"] > top3[1]["raw_cosine"]
    assert top3[0]["raw_cosine"] == pytest.approx(0.85, abs=1e-4)
    assert top3[1]["raw_cosine"] == pytest.approx(0.3, abs=1e-4)


@pytest.mark.asyncio
async def test_raw_cosine_is_never_negative_of_similarity(
    session_factory: async_sessionmaker[AsyncSession], isolated_pk: str, settings: Settings
) -> None:
    """Direct regression pin for the exact bug shape: `1.0 - similarity`
    would turn a near-identical embedding (cosine ~0.93) into ~0.07, which
    would then misclassify as "clear" instead of "block". If this ever
    regresses, this assertion — not a downstream band mismatch three steps
    later — is what fails.
    """
    learning_id = await _seed_learning(session_factory, isolated_pk)
    await _seed_adr(
        session_factory,
        isolated_pk,
        number=1,
        title="near-duplicate",
        embedding=_embedding_at_cosine(0.93),
    )

    async with session_factory() as session:
        dedup = await promote_prepare.compute_dedup(
            session, str(learning_id), isolated_pk, settings
        )

    raw_cosine = dedup["adr"]["nearest_raw_cosine"]
    assert raw_cosine > 0.9, (
        f"raw_cosine={raw_cosine!r} looks like an inverted distance "
        "(1.0 - similarity), not a similarity"
    )
