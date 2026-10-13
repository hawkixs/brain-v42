"""Live rows are selected BEFORE the LIMIT, against real PostgreSQL + pgvector.

Ticket 73e68261: the search SQL applied LIMIT first and brain_service dropped
archived/merged rows afterwards, so a page could come back short of rows that
exist. Here the archived and merged rows are made the BEST matches on purpose
(closest vectors, highest FTS rank): a search that filters after the LIMIT
fills the page with them and returns fewer live rows than asked for.

Rows live under a unique project_key (the session cleanup removes ``integ-*``);
nothing is truncated and no other project's rows are read.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import learnings
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.learning_service import LearningService

pytestmark = pytest.mark.integration

_DIM = 1536
_LIVE_COUNT = 4
_ARCHIVED_COUNT = 5
_MERGED_COUNT = 3

_QUERY_VEC = [0.1] * _DIM
# Cosine-wise strictly farther from the query than _QUERY_VEC itself.
_LIVE_VEC = [0.1] * (_DIM - 1) + [0.9]


def _unique_key() -> str:
    return f"integ-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[LearningService, str, str, set[uuid.UUID]]:
    """(service, project_key, search word, live ids): 4 live + 5 archived + 3 merged."""
    repo = PgLearningRepo(session_factory=session_factory)
    pk = _unique_key()
    word = f"zq{uuid.uuid4().hex[:10]}"

    live = [
        await repo.create(
            LearningCreate(topic=f"{word} live {i}", insight="live row", project_key=pk),
            embedding=_LIVE_VEC,
        )
        for i in range(_LIVE_COUNT)
    ]
    # Best matches by both channels: the word three times, vector identical to the query.
    dead = [
        await repo.create(
            LearningCreate(
                topic=f"{word} {word} {word} dead {i}",
                insight=f"{word} {word} dead row",
                project_key=pk,
            ),
            embedding=_QUERY_VEC,
        )
        for i in range(_ARCHIVED_COUNT + _MERGED_COUNT)
    ]
    archived_ids = [r.id for r in dead[:_ARCHIVED_COUNT]]
    merged_ids = [r.id for r in dead[_ARCHIVED_COUNT:]]
    async with session_factory() as session:
        await session.execute(
            sa.update(learnings)
            .where(learnings.c.id.in_(archived_ids))
            .values(freshness_status="archived")
        )
        await session.execute(
            sa.update(learnings)
            .where(learnings.c.id.in_(merged_ids))
            .values(merged_into=live[0].id)
        )
        await session.commit()

    return LearningService(pg_repo=repo), pk, word, {r.id for r in live}


@pytest.mark.parametrize("limit", [3, 10])
async def test_fts_returns_min_limit_live_rows(
    seeded: tuple[LearningService, str, str, set[uuid.UUID]], limit: int
) -> None:
    svc, pk, word, live_ids = seeded

    found = await svc.search(word, project_key=pk, limit=limit, include_archived=False)

    assert {r.id for r in found} <= live_ids
    assert len(found) == min(limit, _LIVE_COUNT)


@pytest.mark.parametrize("limit", [3, 10])
async def test_vector_returns_min_limit_live_rows(
    seeded: tuple[LearningService, str, str, set[uuid.UUID]], limit: int
) -> None:
    svc, pk, _word, live_ids = seeded

    found = await svc.semantic_search(
        "unused", project_key=pk, limit=limit, embedding=_QUERY_VEC, include_archived=False
    )

    assert {r.id for r, _score in found} <= live_ids
    assert len(found) == min(limit, _LIVE_COUNT)


async def test_default_still_returns_the_archived_best_matches(
    seeded: tuple[LearningService, str, str, set[uuid.UUID]],
) -> None:
    """Control: without the flag the dead rows crowd the page, which is the bug's shape."""
    svc, pk, _word, live_ids = seeded

    found = await svc.semantic_search("unused", project_key=pk, limit=3, embedding=_QUERY_VEC)

    assert len(found) == 3
    assert not {r.id for r, _score in found} & live_ids
