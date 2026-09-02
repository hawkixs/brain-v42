"""Integration: ``AutoLinker._find_similar``'s real SQL against Postgres+pgvector.

Ticket c623fa75 — of auto_linker's 36 unit tests, none makes Postgres parse this
query: 23 replace ``_find_similar`` with an AsyncMock, 10 call it against
``_RecordingSession``, a fake that returns ``fetchall() == []``. Invalid SQL (a lost
space between two arms of the UNION, a column renamed by a migration) would
therefore keep everything green; at run time the exception would be swallowed by
``auto_link``'s ``except Exception`` and CONNECT would return
``created=0 matched=0 skipped=0 errors=0`` — the night marked green WHILE LINKING
NOTHING.

These tests execute the real query, in both its variants (project-scoped and
unscoped — two distinct SQL texts), and prove that the lifecycle filter FILTERS
instead of merely existing: the target archived in the registry still carries its
embedding and would come out first (similarity 1.0) if the EXISTS predicate did not
hold it back. This is the transcription of the out-of-band triplet of 2026-08-18:
old predicate -> 1 row, qualified predicate -> 0 rows.

No skip condition specific to this file: it follows exactly the suite's regime
(``BRAIN_V42_TEST_DB_URL`` required), and the public CI runs it against its
``brain_test`` Postgres service.
"""

from __future__ import annotations

import uuid as uuid_module
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import _EMBEDDING_DIM
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.auto_linker import AutoLinker

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    # Kebab-case for the key validator; the session cleanup purges LIKE 'integ_%'
    # where '_' is a single-character wildcard, so 'integ-…' is purged.
    return f"integ-autolink-{uuid_module.uuid4().hex[:8]}"


def _marker_vec(dim: int = _EMBEDDING_DIM) -> list[float]:
    # Axis 1, not the axis 0 used by the other integration tests: any residue from
    # other files falls to similarity 0.0 and cannot evict our candidates (similarity
    # 1.0) from the unscoped variant's LIMIT.
    v = [0.0] * dim
    v[1] = 1.0
    return v


async def _archive_registry_row(
    session_factory: async_sessionmaker[AsyncSession],
    source_uuid: UUID,
) -> None:
    """Archive the entity IN THE REGISTRY (brain_entities), where the predicate reads."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                "UPDATE brain_entities SET lifecycle = 'archived' WHERE source_uuid = :source_uuid"
            ),
            {"source_uuid": source_uuid},
        )
        # rowcount == 1 also proves the 033 trigger did register the entity: a 0 here
        # would signal a dead registry, not a dead filter.
        assert result.rowcount == 1
        await session.commit()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    vec: list[float],
) -> tuple[UUID, UUID, UUID]:
    """Insert the source, the active target and the archived target; return their UUIDs."""
    repo = PgLearningRepo(session_factory)
    source = await repo.create(
        LearningCreate(
            topic="find_similar — source",
            insight="entité dont on cherche les voisines",
            project_key=project_key,
        ),
        embedding=vec,
    )
    active = await repo.create(
        LearningCreate(
            topic="find_similar — cible active",
            insight="doit être proposée",
            project_key=project_key,
        ),
        embedding=vec,
    )
    archived = await repo.create(
        LearningCreate(
            topic="find_similar — cible archivée",
            insight="embedding présent, endpoint refusé par le résolveur",
            project_key=project_key,
        ),
        embedding=vec,
    )
    await _archive_registry_row(session_factory, archived.id)
    return source.id, active.id, archived.id


async def test_scoped_variant_runs_and_lifecycle_filter_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scoped variant parses, returns the active ones (multi-table) and holds back the archived one."""
    project_key = _unique_key()
    vec = _marker_vec()
    source_id, active_id, archived_id = await _seed(session_factory, project_key, vec)
    cross_table = await PgDecisionRepo(session_factory).create(
        DecisionCreate(
            title="find_similar — cible decision",
            description="prouve que le bras decisions de l'UNION rend des lignes réelles",
            reasoning="un seul bras vert ne prouve pas les cinq",
            project_key=project_key,
        ),
        embedding=vec,
    )

    linker = AutoLinker(session_factory=session_factory, graph=None)
    rows = await linker._find_similar(
        entity_id=source_id,
        embedding=vec,
        limit=10,
        project_key=project_key,
    )

    ids = {row["id"] for row in rows}
    assert active_id in ids
    assert cross_table.id in ids
    # The heart of the ticket: the filter FILTERS. The archived embedding still
    # exists and would come out at similarity 1.0 without the qualified EXISTS
    # predicate.
    assert archived_id not in ids
    assert source_id not in ids

    by_id = {row["id"]: row for row in rows}
    assert by_id[active_id]["similarity"] == pytest.approx(1.0)
    assert by_id[cross_table.id]["entity_type"] == "Decision"


async def test_unscoped_variant_runs_and_lifecycle_filter_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The unscoped variant is ANOTHER SQL text: it must parse and filter too."""
    project_key = _unique_key()
    vec = _marker_vec()
    source_id, active_id, archived_id = await _seed(session_factory, project_key, vec)

    linker = AutoLinker(session_factory=session_factory, graph=None)
    rows = await linker._find_similar(entity_id=source_id, embedding=vec, limit=25)

    ids = {row["id"] for row in rows}
    assert active_id in ids
    assert archived_id not in ids
    assert source_id not in ids
