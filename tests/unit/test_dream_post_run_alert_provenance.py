"""Marche 0 de `55a21fb8` : le trou de provenance devient COMPTÉ.

Le signal existait déjà — `freshness_status_updated_at IS NOT NULL AND
freshness_source IS NULL` est calculable depuis la migration 043, sans une ligne
de code. Ce qui manquait était un LECTEUR. Mesuré le 2026-08-22 sur la fenêtre
réelle (2026-08-10 → 2026-08-22, douze jours) : **3 transitions muettes sur 44**,
toutes sur `learnings`, toutes vers `archived`, toutes attribuables à REORG par
recoupement. Personne ne les avait vues parce que personne ne regardait.

Deux propriétés que ces tests épinglent, et qui sont le contrat de la marche 0 :

* **elle ne change AUCUN comportement.** Le compte ne fait jamais escalader la
  sortie du script. C'est ce qui la rend gratuite, et c'est ce qui permet de la
  livrer avant le correctif : si la marche 1 échoue partiellement, c'est ce
  compteur-ci qui le dira, et il faut qu'il soit déjà là et déjà cru.
* **le chiffre porte sa définition.** « 3 muettes » est une **borne HAUTE**, pas
  un compte : le trigger de la 043 documente son propre angle mort — deux
  transitions consécutives de MÊME source ne sont pas distinguables d'une source
  non redéclarée, donc la seconde retombe à `NULL`. Un compte publié sans cette
  phrase se lirait comme un nombre d'écrivains fautifs.
"""

from __future__ import annotations

import datetime as dt
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession


def _result(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        MagicMock(_mapping=dict(zip(("table_name", "night", "standing"), row, strict=True)))
        for row in rows
    ]
    return result


def _report(
    rows: list[tuple], run_date: dt.date = dt.date(2026, 8, 22)
) -> post_run_alert.ProvenanceReport:
    return post_run_alert.ProvenanceReport(
        run_date=run_date,
        counts=tuple(
            post_run_alert.ProvenanceCount(table=table, night=night, standing=standing)
            for table, night, standing in rows
        ),
    )


def test_a_mute_night_names_its_tables_and_both_numbers() -> None:
    block = _report([("learnings", 3, 3), ("decisions", 0, 5)]).block

    joined = " ".join(block)
    assert post_run_alert.PROVENANCE_HEADING in block[0]
    assert "learnings 3" in joined, "la table fautive doit être nommée, pas seulement comptée"
    assert "decisions" not in joined, "une table sans transition muette n'encombre pas la ligne"


def test_the_number_never_ships_without_its_definition() -> None:
    """Un chiffre nu mentirait : celui-ci est une BORNE HAUTE, et le dit."""
    joined = " ".join(_report([("learnings", 3, 3)]).block)

    assert "borne HAUTE" in joined
    assert "043" in joined, "l'angle mort doit être traçable jusqu'au trigger qui le documente"


def test_a_green_night_still_prints() -> None:
    """Le silence se lit « rien à signaler » ; zéro écrit se lit « mesuré à zéro ».

    C'est la discipline que le bloc de couverture applique déjà : imprimer même
    vert, pour que deux nuits soient comparables sans aller lire un journal.
    """
    block = _report([("learnings", 0, 0)]).block

    assert block, "une nuit sans transition muette imprime quand même son compte"
    assert "0" in " ".join(block)


def test_the_machine_line_carries_both_windows() -> None:
    """La nuit ET le cumul : deux fenêtres, deux nombres, jamais confondues."""
    line = _report([("learnings", 3, 7), ("snippets", 0, 2)]).machine_line

    assert "mute_night=3" in line
    assert "mute_standing=9" in line
    assert "run_date=2026-08-22" in line


def test_the_count_never_escalates() -> None:
    """La marche 0 ne change AUCUN comportement — c'est tout son intérêt.

    Sans ce témoin, une marche 0 qui ferait sortir le script en 2 rendrait la
    nuit rouge sur une observation, et le prochain la désarmerait.
    """
    noisy = _report([("learnings", 99, 999)])
    assert not hasattr(noisy, "escalates"), "le compte de provenance n'a PAS de verdict"

    # Le code de sortie reste piloté par la COUVERTURE seule. Épinglé sur la
    # source parce que c'est la propriété qu'un futur « tant qu'à faire » ferait
    # sauter en une ligne, et qu'aucun test de rendu ne la verrait tomber.
    source = inspect.getsource(post_run_alert.review_and_render)
    return_lines = [line for line in source.splitlines() if line.strip().startswith("return ")]
    assert return_lines == ["    return rendered, night.coverage.escalates"], return_lines


@pytest.mark.asyncio
async def test_a_declared_transition_is_not_counted() -> None:
    """Témoin négatif : ce qui déclare sa provenance ne compte pas comme muet.

    Sans lui, une requête qui compterait TOUTES les transitions rendrait 44 au
    lieu de 3 et se lirait comme une catastrophe.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result([("learnings", 3, 3), ("decisions", 0, 0)]))

    report = await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 22))

    assert report.night_total == 3
    assert report.standing_total == 3
    assert session.execute.await_count == 1, "une seule requête pour les six tables"


@pytest.mark.asyncio
async def test_the_statement_filters_on_both_halves_of_the_signal() -> None:
    """Le signal est une CONJONCTION, et les deux moitiés doivent y être.

    `freshness_source IS NULL` seul compterait aussi les lignes jamais passées
    par une transition depuis la 043 — soit presque tout le corpus.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result([]))

    await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 22))

    compiled = str(session.execute.await_args.args[0].compile())
    assert "freshness_status_updated_at IS NOT NULL" in compiled
    assert "freshness_source IS NULL" in compiled
    for table in ("decisions", "learnings", "snippets", "runbooks", "adrs", "indexed_plans"):
        assert f"FROM {table}" in compiled, f"{table} doit être dans le balayage"


@pytest.mark.asyncio
async def test_the_count_is_actually_PRINTED_not_merely_computed() -> None:
    """Le témoin qui manquait aux trois lots verts et inertes du 21-22/08.

    Un compteur livré, testé, et jamais câblé se lit exactement comme un
    compteur qui rend zéro. Ce test suit le chemin VIVANT : `review_night`
    interroge bien la provenance, et `render_stdout` l'imprime.
    """
    session = AsyncMock(spec=AsyncSession)
    coverage_rows = MagicMock()
    coverage_rows.all.return_value = []
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session.execute = AsyncMock(
        side_effect=[
            coverage_rows,
            coverage_rows,
            count_result,
            _result([("learnings", 3, 7)]),
        ]
    )

    rendered, escalates = await post_run_alert.review_and_render(session, dt.date(2026, 8, 22))

    assert session.execute.await_count == 4, "la lecture de provenance doit AVOIR eu lieu"
    assert escalates is False, "un compte muet n'escalade jamais"
    assert post_run_alert.PROVENANCE_HEADING in rendered
    assert "learnings 3" in rendered
    assert "mute_night=3 mute_standing=7" in rendered
    assert "borne HAUTE" in rendered


def test_an_existing_caller_without_provenance_still_renders() -> None:
    """La signature reste rétro-compatible : le bloc disparaît, rien ne casse."""
    coverage = post_run_alert.coverage_fallback(expected=1, observed=1, missing=0)

    rendered = post_run_alert.render_stdout(None, dt.date(2026, 8, 22), coverage)

    assert post_run_alert.PROVENANCE_HEADING not in rendered
    assert "dream_provenance" not in rendered
