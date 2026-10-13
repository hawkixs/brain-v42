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
from typing import NamedTuple

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from brain_v42.db.tables import learnings
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories import pg_base
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.learning_service import LearningService

pytestmark = pytest.mark.integration

_DIM = 1536
# 4 fresh rows + 1 whose freshness_status is NULL (the column is nullable; such a row is live).
_LIVE_COUNT = 5
_ARCHIVED_COUNT = 5
_MERGED_COUNT = 3

_QUERY_VEC = [0.1] * _DIM
# Cosine-wise strictly farther from the query than _QUERY_VEC itself.
_LIVE_VEC = [0.1] * (_DIM - 1) + [0.9]


def _unique_key() -> str:
    return f"integ-{uuid.uuid4().hex[:8]}"


class _Seed(NamedTuple):
    service: LearningService
    project_key: str
    word: str
    live_ids: set[uuid.UUID]
    null_status_id: uuid.UUID


@pytest_asyncio.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> _Seed:
    """5 live (one with NULL freshness_status) + 5 archived + 3 merged learnings."""
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
    null_status_id = live[-1].id
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
        await session.execute(
            sa.update(learnings)
            .where(learnings.c.id == null_status_id)
            .values(freshness_status=None)
        )
        await session.commit()

    return _Seed(LearningService(pg_repo=repo), pk, word, {r.id for r in live}, null_status_id)


@pytest.mark.parametrize("limit", [3, 10])
async def test_fts_returns_min_limit_live_rows(seeded: _Seed, limit: int) -> None:
    found = await seeded.service.search(
        seeded.word, project_key=seeded.project_key, limit=limit, include_archived=False
    )

    assert {r.id for r in found} <= seeded.live_ids
    assert len(found) == min(limit, _LIVE_COUNT)


@pytest.mark.parametrize("limit", [3, 10])
async def test_vector_returns_min_limit_live_rows(seeded: _Seed, limit: int) -> None:
    found = await seeded.service.semantic_search(
        "unused",
        project_key=seeded.project_key,
        limit=limit,
        embedding=_QUERY_VEC,
        include_archived=False,
    )

    assert {r.id for r, _score in found} <= seeded.live_ids
    assert len(found) == min(limit, _LIVE_COUNT)


async def test_a_null_freshness_status_row_is_live_for_both_searches(seeded: _Seed) -> None:
    """`freshness_status != 'archived'` is NULL, not true, for a NULL status: it would drop the row."""
    fts = await seeded.service.search(
        seeded.word, project_key=seeded.project_key, limit=20, include_archived=False
    )
    vec = await seeded.service.semantic_search(
        "unused",
        project_key=seeded.project_key,
        limit=20,
        embedding=_QUERY_VEC,
        include_archived=False,
    )

    assert seeded.null_status_id in {r.id for r in fts}
    assert seeded.null_status_id in {r.id for r, _score in vec}


async def test_default_still_returns_the_archived_best_matches(seeded: _Seed) -> None:
    """Control: without the flag the dead rows crowd the page, which is the bug's shape."""
    found = await seeded.service.semantic_search(
        "unused", project_key=seeded.project_key, limit=3, embedding=_QUERY_VEC
    )

    assert len(found) == 3
    assert not {r.id for r, _score in found} & seeded.live_ids


@pytest.mark.parametrize(
    "setting_sql",
    ["_SET_EF_SEARCH_SQL", "_SET_ITERATIVE_SCAN_SQL"],
)
async def test_a_failing_tuning_statement_does_not_fail_the_search(
    seeded: _Seed, monkeypatch: pytest.MonkeyPatch, setting_sql: str
) -> None:
    """PostgreSQL rejects an unknown parameter under the reserved `hnsw` prefix.

    Unprotected, that error aborts the transaction and the SELECT after it fails.
    """
    monkeypatch.setattr(
        pg_base, setting_sql, "SELECT set_config('hnsw.no_such_setting', 'x', true)"
    )
    with capture_logs() as logs:
        found = await seeded.service.semantic_search(
            "unused",
            project_key=seeded.project_key,
            limit=3,
            embedding=_QUERY_VEC,
            include_archived=False,
        )

    assert len(found) == 3
    assert {r.id for r, _score in found} <= seeded.live_ids
    assert any(e["event"] == "pg_base.vector_scan_tuning_failed" for e in logs)
