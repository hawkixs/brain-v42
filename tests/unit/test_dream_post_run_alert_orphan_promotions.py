"""`dream_promotions` rows with no `dream_run_id` are counted nowhere.

Facts (measured 2026-09-06): `dream_promotions.dream_run_id` is
`ON DELETE SET NULL` (`dream_promotions_dream_run_id_fkey`), and 1 row for the
night — 32 all-time — carries a `NULL` there. Such a row is invisible to
`format_reconciliation_line`: it names no `(phase, project)` pair, so it can
neither inflate nor deflate `pairs_written`. It is equally invisible to
dream.sh's `FAIL_TOTAL`, which only ever counts `dream_runs` rows. Nothing in
the morning report says these promotions exist.

This module gives them a line: `PROMOTIONS orphan (dream_run_id NULL): N`,
printed next to `RECONCILIATION`, `UNMEASURED` when the table cannot be read —
this check must never be the reason the morning report fails, the same
best-effort posture as `roadmap_shrink_tally` and `reorg_tally`.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession

RUN_DATE = dt.date(2026, 9, 6)


def _count_result(n: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = n
    return result


def test_orphan_promotions_line_prints_the_measured_count() -> None:
    line = post_run_alert.format_orphan_promotions_line(
        post_run_alert.OrphanPromotionsTally(count=1)
    )

    assert line == "PROMOTIONS orphan (dream_run_id NULL): 1"


def test_orphan_promotions_line_prints_zero_explicitly() -> None:
    """A clean night still prints the line — silence would read as unmeasured,
    not as zero."""
    line = post_run_alert.format_orphan_promotions_line(
        post_run_alert.OrphanPromotionsTally(count=0)
    )

    assert line == "PROMOTIONS orphan (dream_run_id NULL): 0"


def test_orphan_promotions_line_is_unmeasured_by_default() -> None:
    """ABSENT IS NOT ZERO (same rule as `RoadmapShrinkTally`, `ReorgTally`)."""
    line = post_run_alert.format_orphan_promotions_line(post_run_alert.OrphanPromotionsTally())

    assert line == "PROMOTIONS orphan (dream_run_id NULL): UNMEASURED"


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_counts_the_nights_null_rows() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_count_result(1))

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert tally.measured
    assert tally.count == 1
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_filters_on_null_dream_run_id_and_the_night() -> None:
    """No `dream_run_id` to join through — that absence is the whole point —
    so the night is read off `created_at` cast to a plain date, the same
    pattern `fetch_mute_transitions` uses for the freshness tables."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_count_result(0))

    await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    compiled = str(session.execute.await_args.args[0].compile())
    assert "dream_promotions" in compiled
    assert "dream_run_id IS NULL" in compiled
    assert "CAST(dream_promotions.created_at AS DATE) = " in compiled


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_is_unmeasured_when_the_table_is_unreachable() -> None:
    """A broken query must never be the reason the morning report fails —
    same posture as the file-derived tallies (`roadmap_shrink_tally` on
    `OSError`, `reorg_tally` on an unreadable trailer)."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=Exception("relation does not exist"))

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.measured
    assert post_run_alert.format_orphan_promotions_line(tally).endswith("UNMEASURED")


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_rolls_back_before_returning_unmeasured() -> None:
    """On the real driver a failed statement aborts the WHOLE transaction:
    this function shares its session with `review_and_render`, called right
    after it in `_run`. Swallowing the exception without rolling back would
    leave that shared session poisoned, so the next unrelated SELECT would
    raise `InFailedSqlTransactionError` instead of succeeding — an error that
    no longer even names `dream_promotions`. `AsyncMock(spec=AsyncSession)`
    has no real transaction to poison, so only an explicit assertion on
    `rollback`, plus proof a downstream read still works, can catch its
    removal."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[Exception("relation does not exist"), _count_result(7)]
    )
    session.rollback = AsyncMock()

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.measured
    session.rollback.assert_awaited_once()

    # A downstream read on the SAME session must still work — proof this
    # module does not leave the transaction poisoned for its caller.
    downstream = await session.execute(None)
    assert downstream.scalar_one() == 7


@pytest.mark.asyncio
async def test_run_prints_orphan_promotions_line_with_phases_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    engine = MagicMock()
    engine.dispose = AsyncMock()
    session = AsyncMock(spec=AsyncSession)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_context)
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    coverage = post_run_alert.coverage_fallback(expected=0, observed=0, missing=0)
    monkeypatch.setattr(
        post_run_alert,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(post_run_alert, "create_async_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(post_run_alert, "async_sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(
        post_run_alert,
        "review_night",
        AsyncMock(return_value=post_run_alert.NightReport(report=None, coverage=coverage)),
    )
    monkeypatch.setattr(
        post_run_alert,
        "fetch_mute_transitions",
        AsyncMock(return_value=post_run_alert.ProvenanceReport(run_date=RUN_DATE, counts=())),
    )
    monkeypatch.setattr(
        post_run_alert,
        "fetch_orphan_promotions",
        AsyncMock(return_value=post_run_alert.OrphanPromotionsTally(count=1)),
    )

    return_code = await post_run_alert._run(RUN_DATE, phases_ok=62, phases_skipped=0)

    assert return_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("RECONCILIATION ")
    assert lines[1] == "PROMOTIONS orphan (dream_run_id NULL): 1"


@pytest.mark.asyncio
async def test_run_without_phases_ok_prints_neither_machine_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manual replay with no `--phases-ok` cannot reconcile OK_TOTAL against
    anything, so neither RECONCILIATION nor PROMOTIONS orphan claims to
    measure a night it was not told the shape of."""
    from types import SimpleNamespace

    engine = MagicMock()
    engine.dispose = AsyncMock()
    session = AsyncMock(spec=AsyncSession)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_context)

    coverage = post_run_alert.coverage_fallback(expected=0, observed=0, missing=0)
    monkeypatch.setattr(
        post_run_alert,
        "Settings",
        MagicMock(return_value=SimpleNamespace(postgres_url="postgresql+asyncpg://unused")),
    )
    monkeypatch.setattr(post_run_alert, "create_async_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(post_run_alert, "async_sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(
        post_run_alert,
        "review_night",
        AsyncMock(
            return_value=post_run_alert.NightReport(
                report="bounded operational report", coverage=coverage
            )
        ),
    )
    monkeypatch.setattr(
        post_run_alert,
        "fetch_mute_transitions",
        AsyncMock(return_value=post_run_alert.ProvenanceReport(run_date=RUN_DATE, counts=())),
    )
    orphan = AsyncMock(return_value=post_run_alert.OrphanPromotionsTally(count=1))
    monkeypatch.setattr(post_run_alert, "fetch_orphan_promotions", orphan)

    await post_run_alert._run(RUN_DATE)

    out = capsys.readouterr().out
    assert "PROMOTIONS orphan" not in out
    orphan.assert_not_awaited()
