"""The `dream_runs` row written when the PROMOTE candidate pool is empty.

Since 041 (maturity filter on `access_count_human`, with no backfill), the pool
can legitimately be empty. Without a row, the expected phase becomes *absent* and
the alert manufactures a synthetic `partial` every night — a false alarm that
pushes towards undoing 041. The phase must therefore be OBSERVED, not removed
from the expected phases.
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
    """Run the REAL writer and return (session, compiled parameters)."""
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
    """TWO distinct dates, otherwise the assertion proves nothing.

    With a single date — all the more so `_record`'s default — the assertion
    compares a constant to itself and does not distinguish "the argument is wired"
    from "the argument is ignored". A hard-coded `run_date` (or one re-read
    through `dt.date.today()` after midnight) would write the row on another
    night: the promote phase would become ABSENT from `dream_runs` again for the
    run's date and `include_missing_expected_phases` would remanufacture its
    synthetic `partial` every night — exactly the bug this workstream fixes.
    """
    _, params = await _record(run_date=run_date)

    assert params["phase"] == "promote"
    assert params["run_date"] == run_date


async def test_empty_pool_row_carries_a_non_failing_status() -> None:
    """The status must fall outside FAILED_STATUSES — the real list, not a copy.

    `done` is the system's only non-failure status: `collector_dream` and
    `DreamRunService.last_failure` count anything `!= 'done'` as a failure. An
    invented "neutral" status (`skipped`, `noop`) would replace one false alarm
    with another, in the briefing this time.
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
    """`phase_dry_run` stays false: no dry rehearsal took place.

    `DreamRunService._clean_dry_streak` counts `done` + dry nights as evidence for
    switching a phase to WET. A night where NOTHING ran is not evidence.
    """
    _, params = await _record()

    assert params["phase_dry_run"] is False


async def test_empty_pool_row_is_committed() -> None:
    """Without a commit the row does not exist and the alert returns — the feature would be a no-op."""
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
    """An unreachable database gives rc != 0: dream.sh logs a WARN and the
    missing row re-lights the alert — never a silence."""
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
