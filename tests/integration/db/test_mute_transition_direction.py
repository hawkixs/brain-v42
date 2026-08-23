"""Le compteur de transitions muettes sépare une SORTIE d'une RENTRÉE de corpus.

Marche 2 de `55a21fb8`, prouvée contre PostgreSQL et son trigger 043 — pas
contre un `MagicMock`. Les modules unitaires épinglent la forme de la requête ;
seul celui-ci prouve que le trigger, la contrainte et le compteur s'accordent sur
une vraie transition.

Le témoin porte les DEUX SENS dans la même transaction : une entité qui sort du
corpus (`fresh → archived`) et une qui y rentre (`archived → fresh`), toutes deux
MUETTES. Avant cette marche, elles produisaient deux lignes identiques. Sans le
second sens, un compteur qui rendrait simplement le total passerait le test.

Tout vit dans une transaction ANNULÉE, et les comptes sont des DELTAS mesurés
autour du témoin : `fetch_mute_transitions` balaie la table entière, donc une
assertion sur une valeur absolue dépendrait de l'état de la base d'intégration.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
import sqlalchemy as sa
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

_INSERT = sa.text(
    "INSERT INTO public.learnings (id, topic, insight, project_key, freshness_status) "
    "VALUES (:id, :topic, 'témoin de marche 2', 'brain-v42', :status)"
)
_TRANSITION = sa.text("UPDATE public.learnings SET freshness_status = :status WHERE id = :id")


async def _learnings_counts(
    connection: AsyncConnection, run_date: dt.date
) -> post_run_alert.ProvenanceCount:
    report = await post_run_alert.fetch_mute_transitions(connection, run_date)
    return next(count for count in report.counts if count.table == "learnings")


@pytest.mark.asyncio
async def test_a_mute_return_to_fresh_is_counted_apart_from_a_mute_archival(
    engine: AsyncEngine,
) -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    # DEUX sorties pour UNE rentrée, et l'asymétrie est le contrôle : à un
    # contre un, inverser la destination comparée rendrait le MÊME delta et le
    # test passerait sur du code faux. Ici, une inversion rend 2 au lieu de 1.
    leaving = (uuid4(), uuid4())
    returning = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            before = await _learnings_counts(connection, today)

            # Deux entités SORTENT du corpus, une y RENTRE. Aucune ne déclare de
            # provenance : le trigger de la 043 les rend toutes muettes.
            for entity_id in leaving:
                await connection.execute(
                    _INSERT, {"id": entity_id, "topic": f"sortie-{entity_id}", "status": "fresh"}
                )
                await connection.execute(_TRANSITION, {"id": entity_id, "status": "archived"})
            await connection.execute(
                _INSERT, {"id": returning, "topic": f"rentrée-{returning}", "status": "archived"}
            )
            await connection.execute(_TRANSITION, {"id": returning, "status": "fresh"})

            # Le trigger a bien fait son travail : daté, et provenance effacée.
            stamped = (
                await connection.execute(
                    sa.text(
                        "SELECT freshness_status, freshness_source, "
                        "freshness_status_updated_at IS NOT NULL AS dated "
                        "FROM public.learnings WHERE id = ANY(:ids) ORDER BY freshness_status"
                    ).bindparams(sa.bindparam("ids", value=[*leaving, returning]))
                )
            ).all()
            assert [(row[0], row[1], row[2]) for row in stamped] == [
                ("archived", None, True),
                ("archived", None, True),
                ("fresh", None, True),
            ]

            after = await _learnings_counts(connection, today)

            # TROIS transitions muettes de plus, dont UNE SEULE est un retour.
            assert after.night - before.night == 3
            assert after.standing - before.standing == 3
            assert after.to_fresh_night - before.to_fresh_night == 1
            assert after.to_fresh_standing - before.to_fresh_standing == 1
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_a_declared_return_to_fresh_is_not_muet_and_leaves_the_count_alone(
    engine: AsyncEngine,
) -> None:
    """Le témoin NÉGATIF : `brain_refresh_entity` déclare `revive`, donc rien à compter.

    Sans lui, un compteur qui ignorerait `freshness_source` compterait aussi les
    désarchivages déjà tracés — et le nombre cesserait de désigner un trou.
    """
    today = dt.datetime.now(tz=dt.UTC).date()
    revived = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            before = await _learnings_counts(connection, today)

            await connection.execute(
                _INSERT, {"id": revived, "topic": f"revive-{revived}", "status": "archived"}
            )
            await connection.execute(
                sa.text(
                    "UPDATE public.learnings SET freshness_status = 'fresh', "
                    "freshness_source = 'revive' WHERE id = :id"
                ),
                {"id": revived},
            )

            source = await connection.scalar(
                sa.text("SELECT freshness_source FROM public.learnings WHERE id = :id"),
                {"id": revived},
            )
            assert source == "revive", "le trigger a effacé une provenance pourtant redéclarée"

            after = await _learnings_counts(connection, today)
            assert after.night == before.night
            assert after.to_fresh_night == before.to_fresh_night
        finally:
            await transaction.rollback()
