"""`dream_promotions` rows with no `dream_run_id` are counted nowhere.

Facts (measured 2026-09-06): `dream_promotions.dream_run_id` is
`ON DELETE SET NULL` (`dream_promotions_dream_run_id_fkey`), and 1 row for the
night — 32 all-time — carries a `NULL` there. Such a row is invisible to
`format_reconciliation_line`: it names no `(phase, project)` pair, so it can
neither inflate nor deflate `pairs_written`. It is equally invisible to
dream.sh's `FAIL_TOTAL`, which only ever counts `dream_runs` rows. Nothing in
the morning report says these promotions exist.

This module gives them a line: `PROMOTIONS orphan (dream_run_id NULL):
night=N all_time=M`. `night` alone would under-report the backlog it exists
to surface — it only catches a promotion nulled the same day it was
created, missing one nulled by a deletion on a LATER night (measured
2026-09-06: `night=1`, `all_time=32`) — so both are printed, and each is
independently `UNMEASURED` when its own query cannot be read: this check
must never be the reason the morning report fails, the same best-effort
posture as `roadmap_shrink_tally` and `reorg_tally`.
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


def test_orphan_promotions_line_prints_both_counts_labelled() -> None:
    line = post_run_alert.format_orphan_promotions_line(
        post_run_alert.OrphanPromotionsTally(night=1, all_time=32)
    )

    assert line == "PROMOTIONS orphan (dream_run_id NULL): night=1 all_time=32"


def test_orphan_promotions_line_prints_zero_explicitly() -> None:
    """A clean night still prints the line — silence would read as unmeasured,
    not as zero."""
    line = post_run_alert.format_orphan_promotions_line(
        post_run_alert.OrphanPromotionsTally(night=0, all_time=0)
    )

    assert line == "PROMOTIONS orphan (dream_run_id NULL): night=0 all_time=0"


def test_orphan_promotions_line_is_unmeasured_by_default() -> None:
    """ABSENT IS NOT ZERO (same rule as `RoadmapShrinkTally`, `ReorgTally`)."""
    line = post_run_alert.format_orphan_promotions_line(post_run_alert.OrphanPromotionsTally())

    assert line == "PROMOTIONS orphan (dream_run_id NULL): night=UNMEASURED all_time=UNMEASURED"


def test_orphan_promotions_line_measures_each_count_independently() -> None:
    """A broken `all_time` COUNT must not hide a working `night` COUNT, or the
    reverse: this is the whole point of printing both — see the MAJOR fix
    this test pins (a night-only count silently under-reports the backlog)."""
    line = post_run_alert.format_orphan_promotions_line(
        post_run_alert.OrphanPromotionsTally(night=1, all_time=None)
    )

    assert line == "PROMOTIONS orphan (dream_run_id NULL): night=1 all_time=UNMEASURED"


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_counts_both_night_and_all_time() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_count_result(1), _count_result(32)])

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert tally.night_measured
    assert tally.all_time_measured
    assert tally.night == 1
    assert tally.all_time == 32
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_first_query_is_scoped_to_the_night() -> None:
    """No `dream_run_id` to join through — that absence is the whole point —
    so the night is read off `created_at` cast to a plain date, the same
    pattern `fetch_mute_transitions` uses for the freshness tables."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_count_result(0), _count_result(0)])

    await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    night_query = session.execute.await_args_list[0].args[0]
    compiled = str(night_query.compile())
    assert "dream_promotions" in compiled
    assert "dream_run_id IS NULL" in compiled
    assert "CAST(dream_promotions.created_at AS DATE) = " in compiled


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_second_query_is_unscoped_all_time() -> None:
    """`all_time` is the whole backlog, not one calendar day of it: its query
    must carry NO date filter, or it degenerates into a second `night`."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_count_result(0), _count_result(0)])

    await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    all_time_query = session.execute.await_args_list[1].args[0]
    compiled = str(all_time_query.compile())
    assert "dream_promotions" in compiled
    assert "dream_run_id IS NULL" in compiled
    assert "created_at" not in compiled


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_is_unmeasured_when_the_table_is_unreachable() -> None:
    """A broken query must never be the reason the morning report fails —
    same posture as the file-derived tallies (`roadmap_shrink_tally` on
    `OSError`, `reorg_tally` on an unreadable trailer)."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=Exception("relation does not exist"))

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.night_measured
    assert not tally.all_time_measured
    assert post_run_alert.format_orphan_promotions_line(tally).endswith(
        "night=UNMEASURED all_time=UNMEASURED"
    )


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_measures_all_time_when_only_night_fails() -> None:
    """The MAJOR fix this module exists for: the two counts fail
    INDEPENDENTLY. A broken night-scoped COUNT must not take the backlog
    count down with it."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[Exception("relation does not exist"), _count_result(32)]
    )
    session.rollback = AsyncMock()

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.night_measured
    assert tally.all_time_measured
    assert tally.all_time == 32


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_measures_night_when_only_all_time_fails() -> None:
    """The mirror case: a broken unscoped COUNT must not take the night's own
    count down with it."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[_count_result(1), Exception("relation does not exist")]
    )
    session.rollback = AsyncMock()

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert tally.night_measured
    assert tally.night == 1
    assert not tally.all_time_measured


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_rolls_back_before_moving_on_to_all_time() -> None:
    """On the real driver a failed statement aborts the WHOLE transaction:
    this function shares its session with `review_and_render`, called right
    after it in `_run`, and runs a SECOND query of its own right after the
    first. Swallowing the exception without rolling back would leave the
    session poisoned for both. `AsyncMock(spec=AsyncSession)` has no real
    transaction to poison, so only an explicit assertion on `rollback`, plus
    proof a downstream read still works, can catch its removal."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[Exception("relation does not exist"), _count_result(32), _count_result(7)]
    )
    session.rollback = AsyncMock()

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.night_measured
    assert tally.all_time == 32
    session.rollback.assert_awaited_once()

    # A downstream read on the SAME session must still work — proof this
    # module does not leave the transaction poisoned for its caller.
    downstream = await session.execute(None)
    assert downstream.scalar_one() == 7


@pytest.mark.asyncio
async def test_fetch_orphan_promotions_survives_a_rollback_that_itself_fails() -> None:
    """Minor fix: the rollback inside the except block is guarded on its own.
    A driver that cannot even roll back (connection already dropped) must
    still make this function return UNMEASURED rather than raise — a WARN
    line must never turn into a crash."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[Exception("relation does not exist"), Exception("relation does not exist")]
    )
    session.rollback = AsyncMock(side_effect=Exception("connection already closed"))

    tally = await post_run_alert.fetch_orphan_promotions(session, RUN_DATE)

    assert not tally.night_measured
    assert not tally.all_time_measured


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
        AsyncMock(return_value=post_run_alert.OrphanPromotionsTally(night=1, all_time=32)),
    )

    return_code = await post_run_alert._run(RUN_DATE, phases_ok=62, phases_skipped=0)

    assert return_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("RECONCILIATION ")
    assert lines[1] == "PROMOTIONS orphan (dream_run_id NULL): night=1 all_time=32"


@pytest.mark.asyncio
async def test_run_without_phases_ok_prints_neither_machine_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manual replay with no `--phases-ok` cannot reconcile OK_TOTAL against
    anything, so RECONCILIATION does not print. PROMOTIONS orphan has no such
    dependency of its own — it is withheld here only because it is gated on
    the same flag, kept as the marker of the automated (dream.sh) invocation
    so a manual replay never emits half of the machine-line pair."""
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
    orphan = AsyncMock(return_value=post_run_alert.OrphanPromotionsTally(night=1, all_time=32))
    monkeypatch.setattr(post_run_alert, "fetch_orphan_promotions", orphan)

    await post_run_alert._run(RUN_DATE)

    out = capsys.readouterr().out
    assert "PROMOTIONS orphan" not in out
    orphan.assert_not_awaited()
