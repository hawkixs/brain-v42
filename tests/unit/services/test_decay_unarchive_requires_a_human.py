"""Q1 — un ROBOT n'a pas le droit de faire ressortir une fiche de l'archive.

LE CHEMIN EXACT, remonté avant d'écrire une ligne de correctif. Une lecture —
d'où qu'elle vienne — passe par ``AccessLogger.log_access``, qui pose une ligne
``access_log`` portant l'acteur lu dans le ContextVar de la requête. Cinq
minutes plus tard ``DecayFlusher._flush`` agrège ces lignes, et
``_update_entities_batch`` recalcule un multiplicateur puis ÉCRIT
``freshness_status`` (avec ``freshness_source='score'``, 043). Le terme qui
bascule est ``access_factor`` : poids **0,3** sur cinq des six types (**0,2**
pour ``adr``), et une lecture d'il y a une minute le met à 1,0. Il n'est JAMAIS
dominé par l'âge — ``w_access >= w_age`` pour les six types, mesuré — donc la
formule courante « le plus lourd APRÈS l'âge » sous-estime sa place : il est en
fait le terme le plus lourd, à égalité avec l'âge pour ``decision``/``learning``
et avec la fréquence pour ``snippet``/``runbook``/``plan``; seul ``adr`` le voit
dominé, par la validation (0,5). Une seule relecture machine suffit
donc à franchir ``archive_threshold`` (0,2) — et, mesuré, à franchir aussi
``stale_threshold`` (0,5) d'un coup.

RECENSEMENT DES ÉCRIVAINS, par quatre motifs distincts (kwarg, clé de chaîne,
SQL brut, ``freshness_source``) — il y en a cinq, et **un seul** est piloté par
la lecture :

  - ``DecayFlusher._update_entities_batch``  source ``score``   <== CELUI-CI
  - ``brain_refresh_entity`` (decay_tools)   source ``revive``  geste délibéré
  - ``EntityMaintenanceService.refresh``     source ``revive``  geste délibéré
  - ``consolidation`` (fusion)               source ``merge``   va VERS l'archive
  - ``pg_indexed_plan_repo`` (upsert)        (aucune)           ré-indexation

MESURE DU 2026-08-22, rejouée et non recopiée — le mandat annonçait « ~2×/jour ».
Sur 7 jours de journal, 31 transitions de fraîcheur, dont :

    archived -> fresh   27      <== DÉSARCHIVAGES, 3,86/jour
    fresh    -> stale    3
    stale    -> fresh    1

Les désarchivages sont **87 % de toute l'activité de decay** : le comportement
observable dominant du decay est aujourd'hui de défaire son propre archivage.
Les 27 sont tombés à **04h UTC, les 27** — la fenêtre du dream. Et sur les 27
entités concernées, ``last_accessed_at_human IS NULL`` **27 fois sur 27**,
``access_count_human = 0`` **27 fois sur 27** : aucune n'a JAMAIS été lue par un
humain, pas une fois depuis la 041.

LE PIÈGE DU MANDAT, ET POURQUOI CETTE MESURE Y ÉCHAPPE. « Un contrôle est creux
dès que l'objet contrôlé peut influencer son signal. » La direction et l'heure
viennent du JOURNAL, qui ne touche aucune colonne. L'attribution vient de
``last_accessed_at_human``/``access_count_human``, que **seule** une lecture
humaine écrit — et la mesure a été prise en ``psql``, jamais par ``brain_get``
ni ``brain_search``, qui auraient posé les lignes ``access_log`` mêmes qu'elle
compte. Le sens de l'erreur est d'ailleurs favorable : une contamination aurait
éloigné ces colonnes de NULL/0, donc CACHÉ le résultat, jamais fabriqué.

CE QUE LA GARDE NE FAIT PAS, délibérément :
  - elle ne bloque pas l'ENTRÉE en archive — un robot garde le droit d'archiver ;
  - elle ne touche pas ``stale -> fresh`` — le mandat parle de l'ARCHIVE ;
  - elle ne gèle pas les compteurs : ``access_count`` et ``last_accessed_at``
    continuent d'être écrits. Ce sont des observations, pas la décision. Les
    geler perdrait de la donnée réelle et empêcherait une future lecture humaine
    de rouvrir la porte.
  - elle n'écrit PAS ``freshness_status`` quand elle bloque, donc elle n'a
    aucune provenance à redéclarer (043) : la ligne garde sa source d'origine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from brain_v42.services.decay_flusher import DecayFlusher, unarchive_is_robot_only


def _session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _run_one_flush(
    *,
    old_status: str,
    computed_status: str,
    count_human: int,
) -> list[dict[str, Any]]:
    """Faire tourner UN flush sur UNE entité et rendre les params écrits.

    Rend la liste des dictionnaires de bind passés aux UPDATE — c'est là, et
    seulement là, qu'on voit si ``freshness_status`` a été écrit.
    """
    entity_id = uuid4()
    now = datetime.now(tz=UTC)

    repo = AsyncMock()
    repo.aggregate_in_session = AsyncMock(
        return_value={
            ("learning", entity_id): {
                "max_accessed": now,
                "max_accessed_human": now if count_human else None,
                "count": 3,
                "count_human": count_human,
            }
        }
    )
    repo.purge_old = AsyncMock()

    select_result = MagicMock()
    select_result.mappings.return_value.all.return_value = [
        {
            "id": entity_id,
            "created_at": now - timedelta(days=400),
            "access_count": 90,
            "access_count_human": 0,
            "freshness_status": old_status,
            "last_accessed_at": now - timedelta(days=200),
            "last_accessed_at_human": None,
            "validated_at": None,
        }
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[select_result, MagicMock(), MagicMock()])

    calculator = MagicMock()
    calculator.compute_multiplier.return_value = 0.9
    calculator.freshness_status.return_value = computed_status

    flusher = DecayFlusher(
        session_factory=_session_factory(session),
        access_log_repo=repo,
        decay_calculator=calculator,
    )
    asyncio.run(flusher._flush())

    written: list[dict[str, Any]] = []
    for call in session.execute.await_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], list):
            written.extend(call.args[1])
    return written


class TestTheGuardItself:
    """La décision, isolée du flusher. Mutation de contrôle dans les DEUX SENS."""

    def test_a_robot_lifting_out_of_the_archive_is_refused(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=0)
            is True
        )

    def test_one_human_read_in_the_batch_is_enough_to_allow_it(self) -> None:
        """TÉMOIN NÉGATIF. Sans lui, bloquer TOUT rendrait le test vert."""
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=1)
            is False
        )

    def test_it_says_nothing_about_going_INTO_the_archive(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="fresh", new_status="archived", human_reads=0)
            is False
        )

    def test_it_says_nothing_about_stale_to_fresh(self) -> None:
        """Le mandat parle de l'ARCHIVE. Une garde plus large déborderait."""
        assert (
            unarchive_is_robot_only(old_status="stale", new_status="fresh", human_reads=0) is False
        )

    def test_an_archived_row_that_stays_archived_is_not_a_lift(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="archived", human_reads=0)
            is False
        )


class TestThroughTheFlusher:
    """Le même contrat, mais par le vrai chemin d'écriture."""

    def test_a_robot_read_does_not_lift_it_out_of_the_archive(self) -> None:
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=0)

        assert written, "le flush doit tout de même écrire les compteurs"
        assert all("freshness_status" not in params for params in written), (
            "une relecture ROBOT a réécrit freshness_status : la boucle n'est pas fermée"
        )

    def test_a_human_read_lifts_it_out_every_time(self) -> None:
        """TÉMOIN NÉGATIF, dans le test lui-même.

        Sans cette assertion, un correctif qui bloque TOUT désarchivage — y
        compris le cas légitime — laisserait le test précédent vert. C'est
        exactement la panne que le mandat demande d'exclure.
        """
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=1)

        statuses = [p["freshness_status"] for p in written if "freshness_status" in p]
        assert statuses == ["fresh"], f"une lecture HUMAINE doit désarchiver, écrit={written}"

    def test_a_robot_may_still_send_an_entity_INTO_the_archive(self) -> None:
        written = _run_one_flush(old_status="fresh", computed_status="archived", count_human=0)

        statuses = [p["freshness_status"] for p in written if "freshness_status" in p]
        assert statuses == ["archived"], "la garde ne doit pas empêcher d'archiver"

    def test_the_counters_keep_flowing_even_when_the_lift_is_refused(self) -> None:
        """Bloquer la DÉCISION, pas l'OBSERVATION.

        Geler les compteurs perdrait de la donnée réelle et empêcherait une
        lecture humaine ultérieure de rouvrir légitimement la porte.
        """
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=0)

        assert len(written) == 1
        params = written[0]
        assert params["access_count"] == 93
        assert params["last_accessed_at"] is not None
