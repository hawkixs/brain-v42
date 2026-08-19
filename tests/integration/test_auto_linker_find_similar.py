"""Integration: le SQL réel de ``AutoLinker._find_similar`` contre Postgres+pgvector.

Ticket c623fa75 — sur les 36 tests unitaires d'auto_linker, aucun ne fait parser
cette requête par Postgres : 23 remplacent ``_find_similar`` par un AsyncMock,
10 l'appellent contre ``_RecordingSession``, un faux qui rend ``fetchall() == []``.
Un SQL invalide (espace perdu entre deux bras de l'UNION, colonne renommée par une
migration) garderait donc tout vert ; à l'exécution l'exception serait avalée par
le ``except Exception`` d'``auto_link`` et CONNECT sortirait
``created=0 matched=0 skipped=0 errors=0`` — la nuit marquée verte EN NE LIANT RIEN.

Ces tests exécutent la vraie requête, dans ses deux variantes (scopée projet et
non scopée — deux textes SQL distincts), et prouvent que le filtre lifecycle
FILTRE au lieu de seulement exister : la cible archivée au registre porte
toujours son embedding et sortirait en tête (similarité 1.0) si le prédicat
EXISTS ne la retenait pas. C'est la transcription du triplet hors-bande du
2026-08-18 : ancien prédicat -> 1 ligne, prédicat qualifié -> 0 ligne.

Aucune condition de skip propre à ce fichier : il suit exactement le régime de la
suite (``BRAIN_V42_TEST_DB_URL`` requis), et la CI publique le fait tourner sur
son service Postgres ``brain_test``.
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
    # Kebab-case pour le validateur de clé ; le cleanup de session purge
    # LIKE 'integ_%' où '_' est un joker mono-caractère, donc 'integ-…' est purgé.
    return f"integ-autolink-{uuid_module.uuid4().hex[:8]}"


def _marker_vec(dim: int = _EMBEDDING_DIM) -> list[float]:
    # Axe 1, pas l'axe 0 utilisé par les autres tests d'intégration : les résidus
    # éventuels d'autres fichiers tombent à similarité 0.0 et ne peuvent pas
    # évincer nos candidats (similarité 1.0) du LIMIT de la variante non scopée.
    v = [0.0] * dim
    v[1] = 1.0
    return v


async def _archive_registry_row(
    session_factory: async_sessionmaker[AsyncSession],
    source_uuid: UUID,
) -> None:
    """Archive l'entité AU REGISTRE (brain_entities), là où le prédicat lit."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                "UPDATE brain_entities SET lifecycle = 'archived' WHERE source_uuid = :source_uuid"
            ),
            {"source_uuid": source_uuid},
        )
        # rowcount == 1 prouve au passage que le trigger 033 a bien enregistré
        # l'entité : un 0 ici signalerait un registre mort, pas un filtre mort.
        assert result.rowcount == 1
        await session.commit()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    vec: list[float],
) -> tuple[UUID, UUID, UUID]:
    """Insère source, cible active et cible archivée ; rend leurs UUID."""
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
    """La variante scopée parse, rend les actives (multi-tables) et retient l'archivée."""
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
    # Le cœur du ticket : le filtre FILTRE. L'embedding archivé existe toujours
    # et sortirait à similarité 1.0 sans le prédicat EXISTS qualifié.
    assert archived_id not in ids
    assert source_id not in ids

    by_id = {row["id"]: row for row in rows}
    assert by_id[active_id]["similarity"] == pytest.approx(1.0)
    assert by_id[cross_table.id]["entity_type"] == "Decision"


async def test_unscoped_variant_runs_and_lifecycle_filter_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """La variante non scopée est un AUTRE texte SQL : elle doit parser et filtrer aussi."""
    project_key = _unique_key()
    vec = _marker_vec()
    source_id, active_id, archived_id = await _seed(session_factory, project_key, vec)

    linker = AutoLinker(session_factory=session_factory, graph=None)
    rows = await linker._find_similar(entity_id=source_id, embedding=vec, limit=25)

    ids = {row["id"] for row in rows}
    assert active_id in ids
    assert archived_id not in ids
    assert source_id not in ids
