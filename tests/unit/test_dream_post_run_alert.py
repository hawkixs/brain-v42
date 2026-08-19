"""Unit tests for the Dream post-run operational failure report."""

from __future__ import annotations

import datetime as dt
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _no_host_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Couper la dépendance de ces tests au drop-in systemd de la MACHINE.

    `fetch_failed_runs` appelle `expected_dream_phase_pairs()` sans argument,
    donc il lit `~/.config/systemd/user/brain-v42-dream.service.d/`. Tant que
    l'hôte n'avait pas de pool, la fonction rendait un ensemble vide et ces
    tests restaient verts sans le savoir. Le jour où le pool a été ouvert, deux
    d'entre eux sont passés au rouge — sur une machine, pas en CI.

    C'est la pire forme de dépendance : verte là où personne ne regarde, rouge
    là où le système tourne vraiment. Le défaut par défaut est donc « pas de
    pool », et un test qui veut le produit cartésien le pose explicitement.
    """
    monkeypatch.setattr(post_run_alert, "expected_dream_phase_pairs", set)


def _result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [SimpleNamespace(_mapping=row) for row in rows]
    return result


def test_missing_expected_phases_are_lexical_and_precede_persisted_failures() -> None:
    failed = post_run_alert.include_missing_expected_phases(
        [
            {"phase": "scan", "status": "done", "error_message": None},
            {"phase": "connect", "status": "fail", "error_message": "broken"},
        ],
        {"scan", "zeta", "alpha"},
    )

    assert [row["phase"] for row in failed] == ["alpha", "zeta", "connect"]
    assert [row["status"] for row in failed[:2]] == ["partial", "partial"]


def test_build_alert_insight_globally_caps_synthetic_and_persisted_details() -> None:
    run_date = dt.date(2026, 7, 27)
    details = [
        {
            "phase": "absent",
            "status": "partial",
            "error_message": "expected enabled phase missing from dream_runs",
        },
        *[
            {"phase": f"phase-{index:02d}", "status": "fail", "error_message": "failure"}
            for index in range(20)
        ],
    ]

    report = post_run_alert.build_alert_insight(run_date, details)
    report_details = [line for line in report.splitlines() if line.startswith("- ")]

    assert len(report_details) == 20
    assert report_details[0].startswith("- absent [partial]")
    assert "phase-18" in report
    assert "phase-19" not in report
    assert "1 additional failure record omitted" in report


def test_build_alert_insight_truncates_error_to_first_240_characters() -> None:
    report = post_run_alert.build_alert_insight(
        dt.date(2026, 7, 27),
        [{"phase": "connect", "status": "fail", "error_message": "x" * 300 + "\nsecond"}],
    )

    detail = next(line for line in report.splitlines() if line.startswith("- connect"))
    assert "x" * 240 in detail
    assert "x" * 241 not in detail
    assert "second" not in detail


@pytest.mark.asyncio
async def test_failed_run_report_never_accesses_learnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    persisted = [
        {
            "id": index,
            "phase": f"phase-{index:02d}",
            "status": "fail",
            "error_message": "broken",
            "created_at": dt.datetime(2026, 7, 27, 1, 0) - dt.timedelta(minutes=index),
        }
        for index in range(20)
    ]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    session.execute = AsyncMock(
        side_effect=[
            _result([{"phase": "scan", "status": "done"}]),
            _result(persisted),
            count_result,
        ]
    )
    session.commit = AsyncMock()
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"scan", "extract"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 7, 27))

    assert report is not None
    detail_lines = [line for line in report.splitlines() if line.startswith("- ")]
    assert len(detail_lines) == 20
    assert detail_lines[0].startswith("- extract [partial]")
    assert "phase-18 [fail]" in report
    assert "phase-19 [fail]" not in report
    assert "1 additional failure record omitted" in report
    assert session.execute.await_count == 3
    session.commit.assert_not_awaited()
    for call in session.execute.await_args_list:
        assert "learnings" not in str(call.args[0]).lower()
    assert "learnings" not in inspect.getsource(post_run_alert).lower()
    failures_statement = session.execute.await_args_list[1].args[0]
    rendered_failures_statement = str(
        failures_statement.compile(compile_kwargs={"literal_binds": True})
    )
    assert "ORDER BY dream_runs.created_at DESC, dream_runs.id DESC" in rendered_failures_statement
    # Le plafond de FETCH est en AMONT du groupement par projet : à 21, un projet
    # bruyant en fin de nuit remplissait les lignes remontées et les projets
    # calmes n'atteignaient jamais le groupeur — l'ordre étant `created_at DESC`,
    # ce sont les derniers projets servis qui gagnaient. Ce que ce test protège
    # reste le même : une requête BORNÉE, jamais un SELECT ouvert.
    assert f"LIMIT {post_run_alert.MAX_FETCHED_FAILURES}" in rendered_failures_statement
    assert post_run_alert.MAX_FETCHED_FAILURES <= 500, (
        "la requête doit rester bornée : c'est la propriété, pas le nombre"
    )


def _session_returning(observed: list[dict[str, object]]) -> AsyncMock:
    """Session mock fidèle au SQL de `fetch_failed_runs`.

    Les failures persistées ne sont PAS figées à vide : elles sont dérivées des
    rows observées par le même filtre `status in FAILED_STATUSES` que la requête
    réelle. Un harnais qui rendrait toujours `[]` court-circuiterait le statut —
    `include_missing_expected_phases` n'applique alors plus son filtre et seul
    l'ensemble des `phase` compte, si bien qu'une row observée devenue échouante
    ne ferait rougir aucune assertion.
    """
    session = AsyncMock(spec=AsyncSession)
    persisted = [row for row in observed if row.get("status") in post_run_alert.FAILED_STATUSES]
    count_result = MagicMock()
    count_result.scalar_one.return_value = len(persisted)
    session.execute = AsyncMock(side_effect=[_result(observed), _result(persisted), count_result])
    return session


@pytest.mark.asyncio
async def test_recorded_empty_pool_promote_row_raises_no_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La row écrite sur pool vide rend la phase OBSERVÉE : plus de partial de synthèse.

    Le statut vient du writer réel (`_promote_helpers`), pas d'une copie, et le
    harnais applique le vrai filtre `FAILED_STATUSES` : rendre cette row
    échouante là-bas fait rougir ici. Le message, lui, n'est pas observable à ce
    niveau tant que le statut ne l'est pas — il est épinglé chez le writer
    (`test_empty_pool_row_says_the_pool_was_empty`), pas revendiqué ici.
    """
    from scripts.dream._promote_helpers import EMPTY_POOL_STATUS

    session = _session_returning([{"phase": "promote", "status": EMPTY_POOL_STATUS}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is None


@pytest.mark.asyncio
async def test_observed_promote_row_with_a_failing_status_still_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garde du harnais : une row OBSERVÉE mais échouante doit encore sonner.

    Sans elle, `_session_returning` pourrait retomber à « aucune failure
    persistée » sans rien faire rougir, et le test du pool vide redeviendrait
    aveugle au statut — le défaut même que cette révision corrige.
    """
    session = _session_returning([{"phase": "promote", "status": "fail"}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is not None
    assert "- promote [fail]" in report


@pytest.mark.asyncio
async def test_expected_promote_that_never_wrote_a_row_still_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propriété d'observabilité : un promote qui CRASHE n'écrit rien et sonne encore.

    C'est l'assertion qui interdit la « simplification » consistant à retirer
    promote des phases attendues — les crashes des 2026-05-02/05-03 sont passés
    inaperçus deux jours exactement pour cette raison.
    """
    session = _session_returning([{"phase": "scan", "status": "done", "error_message": None}])
    monkeypatch.setattr(post_run_alert, "expected_dream_phases", lambda: {"promote", "scan"})

    report = await post_run_alert.write_alert_if_failed(session, dt.date(2026, 8, 8))

    assert report is not None
    assert "- promote [partial]: expected enabled phase missing from dream_runs" in report


def test_dream_script_keeps_post_run_report_in_dated_log_and_original_failure_exit() -> None:
    script = Path("scripts/dream.sh").read_text()

    assert "python -m scripts.dream.post_run_alert" in script
    assert '>> "$LOG_DIR/$TIMESTAMP.log" 2>&1' in script
    assert "exit 1" in script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report", "expected_first_line"),
    [
        ("bounded operational report", "bounded operational report"),
        (None, "no failures for 2026-07-27"),
    ],
)
async def test_run_keeps_operational_report_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report: str | None,
    expected_first_line: str,
) -> None:
    """Le rapport reste sur stdout — et la ligne machine le SUIT, toujours.

    Contrat élargi par le ticket `0a9c067e` : la ligne `COVERAGE …` est la
    dernière ligne de stdout Y COMPRIS les nuits sans anomalie. C'est tout
    l'objet du ticket — mettre côte à côte, chaque matin, ce que la nuit dit
    avoir fait et ce qu'elle a écrit. Un contrat « stdout vaut exactement le
    rapport » interdirait précisément la ligne qui manquait.
    """
    engine = MagicMock()
    engine.dispose = AsyncMock()
    session = AsyncMock(spec=AsyncSession)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_context)
    coverage = post_run_alert.coverage_fallback(expected=0, observed=0, missing=0)
    reporter = AsyncMock(return_value=post_run_alert.NightReport(report=report, coverage=coverage))
    monkeypatch.setattr(
        post_run_alert,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(post_run_alert, "create_async_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(post_run_alert, "async_sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(post_run_alert, "review_night", reporter)

    return_code = await post_run_alert._run(dt.date(2026, 7, 27))

    assert return_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == expected_first_line
    assert lines[-1].startswith("COVERAGE mode=fallback")
    reporter.assert_awaited_once_with(session, dt.date(2026, 7, 27), manifest=None)
    engine.dispose.assert_awaited_once()


def test_module_is_executable_as_main() -> None:
    assert callable(getattr(post_run_alert, "main", None))
