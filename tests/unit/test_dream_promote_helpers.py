"""La row `dream_runs` écrite quand le pool de candidats PROMOTE est vide.

Depuis la 041 (filtre de maturité sur `access_count_human`, sans backfill), le
pool peut légitimement être vide. Sans row, la phase attendue devient *absente*
et l'alerte fabrique un `partial` de synthèse chaque nuit — une fausse alarme
qui pousse à défaire la 041. La phase doit donc être OBSERVÉE, pas retirée des
phases attendues.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import _promote_helpers
from scripts.dream.post_run_alert import FAILED_STATUSES
from sqlalchemy.ext.asyncio import AsyncSession


def _session_and_factory() -> tuple[AsyncMock, MagicMock]:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return session, MagicMock(return_value=context)


async def _record(
    run_date: dt.date = dt.date(2026, 8, 8),
    duration_s: float = 12.5,
    project_key: str = "brain-v42",
):
    """Exécuter le VRAI writer et rendre (session, paramètres compilés)."""
    session, factory = _session_and_factory()
    await _promote_helpers._record_empty_pool(
        factory, run_date, duration_s, project_key=project_key
    )
    statement = session.execute.await_args.args[0]
    return session, statement.compile().params


@pytest.mark.parametrize("run_date", [dt.date(2026, 8, 8), dt.date(2031, 3, 14)])
async def test_empty_pool_row_targets_the_promote_phase_of_the_run_date(
    run_date: dt.date,
) -> None:
    """DEUX dates distinctes, sinon l'assertion ne prouve rien.

    Avec une seule date — a fortiori celle du défaut de `_record` — l'assertion
    compare une constante à elle-même et ne distingue pas « l'argument est
    câblé » de « l'argument est ignoré ». Un `run_date` figé en dur (ou relu via
    `dt.date.today()` après minuit) écrirait la row sur une autre nuit : la
    phase promote redeviendrait ABSENTE de `dream_runs` pour la date du run et
    `include_missing_expected_phases` refabriquerait son `partial` de synthèse
    chaque nuit — exactement le bug que ce chantier corrige.
    """
    _, params = await _record(run_date=run_date)

    assert params["phase"] == "promote"
    assert params["run_date"] == run_date


async def test_empty_pool_row_carries_a_non_failing_status() -> None:
    """Le statut doit sortir de FAILED_STATUSES — la liste réelle, pas une copie.

    `done` est le seul statut non-échec du système : `collector_dream` et
    `DreamRunService.last_failure` comptent tout `!= 'done'` comme un échec.
    Un statut « neutre » inventé (`skipped`, `noop`) remplacerait une fausse
    alarme par une autre, dans le briefing cette fois.
    """
    _, params = await _record()

    assert params["status"] not in FAILED_STATUSES
    assert params["status"] == "done"


async def test_empty_pool_row_says_the_pool_was_empty() -> None:
    _, params = await _record()

    assert "empty candidate pool" in str(params["error_message"]).lower()


async def test_empty_pool_row_carries_the_measured_duration() -> None:
    _, params = await _record(duration_s=12.5)

    assert params["duration_s"] == 12.5


async def test_empty_pool_row_is_not_counted_as_a_clean_dry_night() -> None:
    """`phase_dry_run` reste faux : aucune répétition à blanc n'a eu lieu.

    `DreamRunService._clean_dry_streak` compte les nuits `done` + dry comme
    preuve pour basculer une phase en WET. Une nuit où RIEN n'a tourné n'est
    pas une preuve.
    """
    _, params = await _record()

    assert params["phase_dry_run"] is False


async def test_empty_pool_row_is_committed() -> None:
    """Sans commit, la row n'existe pas et l'alerte revient — la feature serait un no-op."""
    session, _ = await _record()

    session.commit.assert_awaited_once()


def test_cli_records_the_row_for_the_requested_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = AsyncMock()
    monkeypatch.setattr(_promote_helpers, "_build_factory", MagicMock(return_value="factory"))
    monkeypatch.setattr(_promote_helpers, "_record_empty_pool", recorder)
    monkeypatch.setattr(
        _promote_helpers,
        "Settings",
        MagicMock(return_value=MagicMock(postgres_url="postgresql+asyncpg://unused")),
    )

    return_code = _promote_helpers.main(
        [
            "record-empty-pool",
            "--date",
            "2026-08-08",
            "--duration-seconds",
            "3.5",
            "--project-key",
            "brain-v42",
        ]
    )

    assert return_code == 0
    recorder.assert_awaited_once_with("factory", dt.date(2026, 8, 8), 3.5, project_key="brain-v42")


def test_cli_rejects_an_invalid_date_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = AsyncMock()
    monkeypatch.setattr(_promote_helpers, "_build_factory", MagicMock(return_value="factory"))
    monkeypatch.setattr(_promote_helpers, "_record_empty_pool", recorder)
    monkeypatch.setattr(
        _promote_helpers,
        "Settings",
        MagicMock(return_value=MagicMock(postgres_url="postgresql+asyncpg://unused")),
    )

    return_code = _promote_helpers.main(
        ["record-empty-pool", "--date", "pas-une-date", "--project-key", "brain-v42"]
    )

    assert return_code == 1
    recorder.assert_not_awaited()


def test_cli_reports_a_database_failure_as_a_non_zero_return_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Une base injoignable rend rc != 0 : dream.sh journalise un WARN et
    l'absence de row rallume l'alerte — jamais un silence."""
    monkeypatch.setattr(_promote_helpers, "_build_factory", MagicMock(return_value="factory"))
    monkeypatch.setattr(
        _promote_helpers, "_record_empty_pool", AsyncMock(side_effect=RuntimeError("base absente"))
    )
    monkeypatch.setattr(
        _promote_helpers,
        "Settings",
        MagicMock(return_value=MagicMock(postgres_url="postgresql+asyncpg://unused")),
    )

    return_code = _promote_helpers.main(
        ["record-empty-pool", "--date", "2026-08-08", "--project-key", "brain-v42"]
    )

    assert return_code == 1
    assert "base absente" in capsys.readouterr().err
