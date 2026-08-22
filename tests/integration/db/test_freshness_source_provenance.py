"""Chaque transition de `freshness_status` doit dire d'où elle vient.

La 043 pose un vocabulaire FERMÉ de quatre termes et une doctrine explicite :
*une provenance absente se voit, une provenance fausse se croit*. Le trigger la
remet donc à `NULL` dès qu'un écrivain ne la redéclare pas.

**Cette doctrine ne tient que si l'absence est RARE.** Recensement du lot B,
rejoué ici : six écrivains de `freshness_status`, dont **cinq** muets — la
colonne disait « inconnu » pour presque tout, et l'écrivain qui JUGE était parmi
les muets.

**POURQUOI CES TESTS ÉCRIVENT PUIS RELISENT DEPUIS LA BASE.** C'est le TRIGGER
qui nulle la provenance, pas le code applicatif. Un test qui vérifierait la
valeur passée au `values()` prouverait qu'on l'a écrite, jamais qu'elle a
survécu — et c'est exactement la moitié qui manque. On relit donc la ligne.

**DEUX PIÈGES DU TRIGGER, câblés dans les fixtures et non découverts par accident :**

1. Il ne se déclenche QUE si le statut CHANGE (`WHEN OLD IS DISTINCT FROM NEW`).
   Semer une ligne déjà au statut cible rendrait ces tests verts sans que le
   trigger ait jamais tourné. Chaque fixture sème donc le statut OPPOSÉ.
2. Il nulle aussi la source quand elle est réécrite À L'IDENTIQUE. Les lignes
   sont donc semées à `freshness_source = NULL`, pour que la redéclaration soit
   bien DISTINCTE.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import learnings

pytestmark = pytest.mark.integration

_PROJECT = "integ-freshness-source"


async def _seed(db_session, *, status: str) -> uuid.UUID:
    """Une vraie ligne, au statut OPPOSÉ de celui que l'écrivain va poser."""
    row_id = uuid.uuid4()
    await db_session.execute(
        sa.insert(learnings).values(
            id=row_id,
            project_key=_PROJECT,
            topic="provenance du statut de fraîcheur",
            insight="Le trigger nulle ce que l'écrivain ne redéclare pas.",
            freshness_status=status,
            freshness_source=None,
        )
    )
    await db_session.commit()
    return row_id


async def _read_source(db_session, row_id: uuid.UUID) -> str | None:
    db_session.expire_all()
    return (
        await db_session.execute(
            sa.select(learnings.c.freshness_source).where(learnings.c.id == row_id)
        )
    ).scalar_one()


async def _read_status(db_session, row_id: uuid.UUID) -> str:
    db_session.expire_all()
    return (
        await db_session.execute(
            sa.select(learnings.c.freshness_status).where(learnings.c.id == row_id)
        )
    ).scalar_one()


class TestTheTriggerIsActuallyExercised:
    """Témoin de MÉCANISME : sans lui, les tests suivants pourraient être creux.

    Si le trigger ne tournait pas — statut inchangé, ou trigger absent — une
    provenance écrite survivrait trivialement et tous les tests de ce fichier
    passeraient en ne prouvant rien du tout.
    """

    async def test_a_writer_that_stays_mute_loses_its_provenance(self, db_session) -> None:
        """TÉMOIN NÉGATIF de référence : muet ⇒ NULL, par le trigger."""
        row_id = await _seed(db_session, status="stale")
        await db_session.execute(
            sa.update(learnings)
            .where(learnings.c.id == row_id)
            .values(freshness_status="fresh")  # aucune source redéclarée
        )
        await db_session.commit()

        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) is None

    async def test_an_unchanged_status_does_not_fire_the_trigger(self, db_session) -> None:
        """Le piège n° 1, épinglé : réécrire le MÊME statut ne déclenche rien.

        Une source posée sur une réécriture à statut constant SURVIT — non pas
        parce que l'écrivain l'a bien déclarée, mais parce que le trigger ne
        s'est jamais exécuté. Un test semé au statut cible serait donc vert à
        tort. C'est ce test qui rend les autres lisibles.
        """
        row_id = await _seed(db_session, status="fresh")
        await db_session.execute(
            sa.update(learnings)
            .where(learnings.c.id == row_id)
            .values(freshness_status="fresh", freshness_source="revive")
        )
        await db_session.commit()

        assert await _read_source(db_session, row_id) == "revive"


class TestMechanicalWritersDeclareTheirProvenance:
    async def test_merge_declares_merge(self, db_session, session_factory) -> None:
        from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
        from brain_v42.services.consolidation import ConsolidationJob

        source_id = await _seed(db_session, status="fresh")
        target_id = await _seed(db_session, status="fresh")

        await ConsolidationJob(session_factory, PgConsolidationLogRepo(session_factory)).merge(
            "learning", source_id, target_id
        )

        assert await _read_status(db_session, source_id) == "archived"
        assert await _read_source(db_session, source_id) == "merge"

    async def test_the_refresh_tool_declares_revive(self, db_session, session_factory) -> None:
        """`brain_refresh_entity` — le même geste que la route passerelle, par MCP."""
        from typing import Any

        from brain_v42.mcp.tools.decay_tools import register_decay_tools

        class _CollectingMCP:
            def __init__(self) -> None:
                self.registered: dict[str, Any] = {}

            def tool(self, **_kwargs: Any) -> Any:
                def decorator(fn: Any) -> Any:
                    self.registered[fn.__name__] = fn
                    return fn

                return decorator

        row_id = await _seed(db_session, status="archived")
        mcp = _CollectingMCP()
        register_decay_tools(mcp, session_factory)

        await mcp.registered["brain_refresh_entity"]("learning", str(row_id))

        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) == "revive"

    async def test_gateway_refresh_declares_revive(self, db_session, session_factory) -> None:
        from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

        row_id = await _seed(db_session, status="archived")

        refreshed = await EntityMaintenanceService(session_factory).refresh("learning", row_id)

        assert refreshed is not None
        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) == "revive"
