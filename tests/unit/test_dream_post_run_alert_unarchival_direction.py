"""Marche 2 de `55a21fb8` : une transition muette dit enfin son SENS.

La marche 1 a tari la source CONNUE — REORG déclare `judgment` — et a laissé
l'écriture humaine muette **exprès**, pour qu'elle reste comptée par la marche 0
et que la prochaine source non recensée se voie. Ce module ne défait pas ce
choix : il ajoute la seule chose qui manquait pour qu'un désarchivage cesse
d'être invisible, et qui ne coûte aucune migration.

**Le compteur ne lisait jamais `freshness_status`.** Son signal est la
conjonction « une transition a eu lieu » ET « sa provenance est absente ». Un
archivage et un DÉSARCHIVAGE y produisaient donc exactement la même ligne. Un
opérateur lisant « learnings 3 » ne pouvait pas savoir si trois entités étaient
sorties du corpus ou y étaient rentrées — alors que ce sont deux incidents
opposés, l'un qui perd de la connaissance et l'autre qui en ressuscite.

Mesuré en production le 2026-08-23, tête `046` : **44 transitions datées depuis
la 043**, dont **3 muettes**, toutes sur `learnings`, toutes vers `archived`.
Aucun retour à `fresh` muet à ce jour — donc ce module ne décrit pas un incident
en cours, il rend visible celui qui n'a pas encore eu lieu. Les 31 transitions
déclarées le sont toutes par `score` ; `revive`, `merge` et `judgment` n'ont
JAMAIS été écrits en production.

**Ce que la destination dit, et ce qu'elle ne dit pas.** Pour une ligne muette,
le statut courant EST la destination de sa dernière transition — une transition
postérieure qui aurait déclaré une source la ferait sortir du compte. La
destination est donc exacte. Mais `fresh` est un SUR-ENSEMBLE du désarchivage :
un `stale → fresh` y entre aussi. Distinguer les deux demanderait le statut
PRÉCÉDENT, que personne ne stocke — c'est-à-dire une colonne, donc une
migration. Le nombre est nommé « retours à `fresh` », jamais « désarchivages ».
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession

_ROW_COLUMNS = ("table_name", "night", "standing", "to_fresh_night", "to_fresh_standing")


def _result_rows(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        MagicMock(_mapping=dict(zip(_ROW_COLUMNS, row, strict=True))) for row in rows
    ]
    return result


def _count(table: str, night: int, standing: int, to_fresh: tuple[int, int] = (0, 0)):
    return post_run_alert.ProvenanceCount(
        table=table,
        night=night,
        standing=standing,
        to_fresh_night=to_fresh[0],
        to_fresh_standing=to_fresh[1],
    )


def _report(counts):
    return post_run_alert.ProvenanceReport(run_date=dt.date(2026, 8, 23), counts=tuple(counts))


def test_a_mute_return_to_fresh_is_named_in_the_block() -> None:
    """L'incident que la marche 0 voyait sans pouvoir le nommer."""
    block = _report(
        [
            _count("learnings", night=2, standing=5, to_fresh=(2, 3)),
            _count("decisions", night=0, standing=1),
        ]
    ).block
    joined = " ".join(block)

    assert "fresh" in joined, "la destination n'apparaît pas"
    assert "2" in joined and "3" in joined


def test_an_archival_only_night_does_not_claim_a_return_to_fresh() -> None:
    """Le témoin négatif : sans lui, la ligne pourrait être imprimée en dur."""
    report = _report([_count("learnings", night=3, standing=3)])

    assert report.to_fresh_night_total == 0
    assert report.to_fresh_standing_total == 0
    assert "désarchivage" not in " ".join(report.block).lower()


def test_the_totals_of_the_first_march_are_untouched() -> None:
    """La marche 2 AJOUTE une dimension, elle n'en remplace aucune."""
    report = _report(
        [
            _count("learnings", night=3, standing=7, to_fresh=(1, 2)),
            _count("snippets", night=0, standing=2),
        ]
    )

    assert report.night_total == 3
    assert report.standing_total == 9
    assert "mute_night=3" in report.machine_line
    assert "mute_standing=9" in report.machine_line


def test_the_machine_line_carries_the_direction_too() -> None:
    """Un signal que seul un humain peut lire n'entre dans aucun tableau."""
    line = _report(
        [
            _count("learnings", night=2, standing=4, to_fresh=(2, 3)),
            _count("adrs", night=1, standing=1),
        ]
    ).machine_line

    assert "mute_to_fresh_night=2" in line
    assert "mute_to_fresh_standing=3" in line


def test_a_return_to_fresh_can_never_exceed_the_mute_count_it_refines() -> None:
    """La borne qui rend le nombre lisible : c'est un SOUS-ensemble."""
    report = _report(
        [
            _count("learnings", night=2, standing=5, to_fresh=(2, 3)),
            _count("decisions", night=1, standing=1, to_fresh=(0, 1)),
        ]
    )

    assert report.to_fresh_night_total <= report.night_total
    assert report.to_fresh_standing_total <= report.standing_total


def test_the_number_is_never_called_a_desarchivage() -> None:
    """`stale → fresh` entre dans le même compte ; le mot serait plus fort que la mesure.

    Le statut PRÉCÉDENT n'est stocké nulle part. Le nommer « désarchivage »
    demanderait une colonne, donc une migration.
    """
    block = " ".join(_report([_count("learnings", night=2, standing=2, to_fresh=(2, 2))]).block)

    assert "fresh" in block
    assert "désarchivage" not in block.lower()
    assert "unarchive" not in block.lower()


@pytest.mark.asyncio
async def test_the_statement_reads_the_status_it_used_to_ignore() -> None:
    """Le compteur ne lisait JAMAIS `freshness_status` — c'est tout le trou.

    Épinglé sur le SQL COMPILÉ et non sur le texte du module : un test qui lit
    la source prouve qu'on a écrit quelque chose, jamais qu'on l'a envoyé.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result_rows([]))

    await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 23))

    statement = session.execute.await_args.args[0].compile()
    compiled = str(statement)
    # Les deux moitiés du signal de la marche 0 SURVIVENT à la restriction.
    assert "freshness_status_updated_at IS NOT NULL" in compiled
    assert "freshness_source IS NULL" in compiled
    # Et la destination, que la marche 0 n'interrogeait pas.
    assert "freshness_status =" in compiled, "la requête n'interroge pas la destination"
    # La VALEUR comparée, lue dans les paramètres liés : `'fresh'` n'apparaît pas
    # dans le SQL rendu, il y est un bindparam.
    assert post_run_alert.RETURNED_STATUS in statement.params.values()
    assert session.execute.await_count == 1, "toujours UNE seule requête pour les six tables"


def test_the_destination_compared_is_the_one_that_means_a_return() -> None:
    """Le trou que la mutation de contrôle a révélé, refermé.

    Mesuré le 2026-08-23 : en inversant `RETURNED_STATUS` en `"archived"`, les
    huit tests unitaires restaient VERTS et seul le témoin base réelle rougissait.
    La cause était que les assertions comparaient à la CONSTANTE — qui suivait la
    mutation. Une constante ne peut pas être son propre témoin.

    La valeur est aussi vérifiée APPARTENIR au vocabulaire fermé du modèle : la
    figer sans ça la rendrait fausse en silence si le vocabulaire changeait.
    """
    from typing import get_args

    from brain_v42.models.learning import LearningUpdate

    statuses = get_args(get_args(LearningUpdate.model_fields["freshness_status"].annotation)[0])

    assert post_run_alert.RETURNED_STATUS == "fresh"
    assert post_run_alert.RETURNED_STATUS in statuses
    assert set(statuses) == {"fresh", "stale", "archived"}


@pytest.mark.asyncio
async def test_the_two_new_columns_reach_the_report() -> None:
    """Un compteur calculé et non lu se lit exactement comme un compteur à zéro."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result_rows([("learnings", 4, 9, 3, 5)]))

    report = await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 23))

    assert report.night_total == 4
    assert report.standing_total == 9
    assert report.to_fresh_night_total == 3
    assert report.to_fresh_standing_total == 5
