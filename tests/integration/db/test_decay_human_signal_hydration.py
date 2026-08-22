"""Le signal humain doit ARRIVER jusqu'au modèle, pas seulement exister en base.

`decay_human_signal_enabled` a été livré comme un interrupteur entre le signal
machine et le signal humain. Mesuré le 2026-08-22, c'était un interrupteur entre
le signal machine et **RIEN** : `brain_service` lit
`getattr(entity, "access_count_human", 0)` sur un modèle **Pydantic** qui ne
déclarait pas le champ, donc le `getattr` tombait toujours sur son défaut. Le
DecayFlusher, lui, lit les vraies colonnes en SQLAlchemy Core. Armer le drapeau
aurait fait **diverger les deux chemins** : l'un sur une constante, l'autre sur
la donnée.

**POURQUOI CE TEST EST EN INTÉGRATION, ET PAS UNITAIRE.**
`tests/unit/test_decay_human_signal.py` fabrique un `SimpleNamespace` portant les
deux attributs, puis recopie la logique de production dans le corps du test. Il
prouve la FORME du code et rien d'autre — c'est lui qui a masqué ce défaut
pendant tout le chantier. **Un test qui construit lui-même l'objet qu'il vérifie
ne prouve rien sur le chemin réel.** Ici l'objet vient d'une VRAIE ligne, lue par
le VRAI dépôt, hydratée par le VRAI modèle. C'est la seule forme qui pouvait
détecter le défaut, et c'est la seule qui le gardera fermé.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import learnings
from brain_v42.repositories.pg_learning import PgLearningRepo

pytestmark = pytest.mark.integration

_PROJECT = "integ-decay-human-signal"
#: Deux valeurs volontairement DIFFÉRENTES des compteurs machine : si le modèle
#: retombait sur le total, ou sur un défaut, l'assert le verrait.
_HUMAN_COUNT = 3
_MACHINE_COUNT = 400
_HUMAN_RECENCY = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
_MACHINE_RECENCY = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


@pytest.fixture
async def seeded_learning_id(db_session) -> uuid.UUID:
    """Écrire une vraie ligne portant les quatre compteurs, et rendre son id."""
    row_id = uuid.uuid4()
    await db_session.execute(
        sa.insert(learnings).values(
            id=row_id,
            project_key=_PROJECT,
            topic="hydratation du signal humain",
            insight="Une ligne réelle, pas un SimpleNamespace.",
            access_count=_MACHINE_COUNT,
            access_count_human=_HUMAN_COUNT,
            last_accessed_at=_MACHINE_RECENCY,
            last_accessed_at_human=_HUMAN_RECENCY,
        )
    )
    await db_session.commit()
    return row_id


class TestTheHumanSignalReachesTheModel:
    async def test_get_by_id_carries_both_human_columns(
        self, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """Le chemin de lecture unitaire — RETURNING */projection complète."""
        learning = await PgLearningRepo(session_factory).get_by_id(seeded_learning_id)

        assert learning is not None
        assert learning.access_count_human == _HUMAN_COUNT
        assert learning.last_accessed_at_human == _HUMAN_RECENCY
        # TÉMOIN NÉGATIF, dans le test lui-même : sans lui, un modèle qui
        # recopierait le compteur TOTAL dans le champ humain passerait.
        assert learning.access_count == _MACHINE_COUNT
        assert learning.last_accessed_at == _MACHINE_RECENCY
        assert learning.access_count_human != learning.access_count
        assert learning.last_accessed_at_human != learning.last_accessed_at

    async def test_search_path_carries_both_human_columns(
        self, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """Le chemin qui compte VRAIMENT : c'est lui qui alimente le decay.

        `_search_columns()` projette toutes les colonnes sauf `embedding` et
        `search_vector`, donc la donnée arrivait déjà dans la LIGNE. Elle était
        jetée par Pydantic, faute de champ déclaré. Ce test épingle la jonction
        exacte où elle se perdait.
        """
        repo = PgLearningRepo(session_factory)
        rows = await repo.list_all(project_key=_PROJECT, limit=10)

        found = [item for item in rows if item.id == seeded_learning_id]
        assert found, "la ligne semée doit être visible du chemin de liste"
        learning = found[0]
        assert learning.access_count_human == _HUMAN_COUNT
        assert learning.last_accessed_at_human == _HUMAN_RECENCY


class TestBothPathsReadTheSameValue:
    async def test_flusher_columns_and_model_fields_agree(
        self, db_session, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """La divergence est le vrai danger, pas l'absence.

        Le DecayFlusher lit `table.c.access_count_human` en Core ; le service lit
        `entity.access_count_human` en Pydantic. Tant que le modèle ne portait
        pas le champ, armer le drapeau donnait DEUX valeurs pour une même entité
        — une constante d'un côté, la donnée de l'autre. Ce test compare les deux
        lectures sur la même ligne, par les deux chemins réels.
        """
        core_row = (
            (
                await db_session.execute(
                    sa.select(
                        learnings.c.access_count_human,
                        learnings.c.last_accessed_at_human,
                    ).where(learnings.c.id == seeded_learning_id)
                )
            )
            .mappings()
            .one()
        )

        model = await PgLearningRepo(session_factory).get_by_id(seeded_learning_id)
        assert model is not None

        assert model.access_count_human == core_row["access_count_human"]
        assert model.last_accessed_at_human == core_row["last_accessed_at_human"]


class TestThePlanPathCarriesTheParentHumanSignal:
    """Les plans sont le SIXIÈME type suivi par le decay, et le seul asymétrique.

    Seul le PARENT (`indexed_plans`) porte les colonnes humaines ; les chunks ne
    les ont pas (vérifié dans `tables.py`). Le chemin machine le sait déjà et
    substitue les compteurs du parent (`brain_service`, branche `t == "plan"`).
    La branche humaine, elle, lisait l'attribut sur le CHUNK — introuvable, donc
    `0` et `None` **pour tout plan, toujours**. Ce n'était pas seulement une
    divergence d'avec le flusher : c'était structurellement inatteignable sans
    la jointure parent.
    """

    @pytest.fixture
    async def seeded_plan(self, db_session) -> uuid.UUID:
        from brain_v42.db.tables import indexed_plan_chunks, indexed_plans

        plan_id = uuid.uuid4()
        await db_session.execute(
            sa.insert(indexed_plans).values(
                id=plan_id,
                file_path=f"/tmp/{plan_id}.md",
                title="plan de sonde du signal humain",
                plan_type="plan",
                project_key=_PROJECT,
                content_hash=uuid.uuid4().hex,
                access_count=_MACHINE_COUNT,
                access_count_human=_HUMAN_COUNT,
                last_accessed_at=_MACHINE_RECENCY,
                last_accessed_at_human=_HUMAN_RECENCY,
            )
        )
        await db_session.execute(
            sa.insert(indexed_plan_chunks).values(
                id=uuid.uuid4(),
                plan_id=plan_id,
                section_title="hydratation",
                section_path="hydratation",
                content="Le signal humain du plan vient du parent, jamais du chunk.",
                section_order=1,
                word_count=10,
                embedding=[0.0] * 1536,
                project_key=_PROJECT,
                plan_type="plan",
                status="active",
                # `search_vector` n'est PAS une colonne générée sur les chunks :
                # sans la poser, le `@@` du FTS ne matche rien et le test
                # rougirait pour une raison qui n'est pas celle qu'il vise.
                search_vector=sa.func.to_tsvector(
                    "english",
                    "Le signal humain du plan vient du parent, jamais du chunk.",
                ),
            )
        )
        await db_session.commit()
        return plan_id

    async def test_chunk_carries_the_parent_human_counters(
        self, session_factory, seeded_plan: uuid.UUID
    ) -> None:
        from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService

        results = await IndexedPlanSearchService(session_factory).search(
            query="parent",
            project_key=_PROJECT,
            limit=10,
        )

        chunks = [chunk for chunk in results if chunk.plan_id == seeded_plan]
        assert chunks, "le chunk semé doit être visible du chemin de recherche"
        chunk = chunks[0]
        assert chunk.parent_access_count_human == _HUMAN_COUNT
        assert chunk.parent_last_accessed_at_human == _HUMAN_RECENCY
        # TÉMOIN NÉGATIF : le parent machine reste distinct du parent humain.
        assert chunk.parent_access_count == _MACHINE_COUNT
        assert chunk.parent_access_count_human != chunk.parent_access_count
