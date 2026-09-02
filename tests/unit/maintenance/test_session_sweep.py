"""Sweep CLI contract: DRY by default, the 4 h rule CLOSED, a readable report.

Two safety boundaries, not one, and they compose: `--wet` decides whether the
sweep WRITES, `BRAIN_SESSION_INACTIVE_SWEEP_ENABLED` decides whether the 4 h rule
EXISTS. The second is new, and its closed default is not formal caution: this
phase runs WET every night, under `uv run` from the repository. Merging the rule
without a flag would arm it from the following night, with no restart and no
observation window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.config import Settings
from brain_v42.models.brain_session import (
    AGENT_INACTIVE_AFTER,
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    BrainSessionStatus,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _candidate(index: int, outcome: BrainSessionStatus) -> BrainSessionSweepCandidate:
    inactive = outcome is BrainSessionStatus.CLOSED_INACTIVE
    return BrainSessionSweepCandidate(
        id=uuid4(),
        project_key=f"projet-{index}",
        client_key=f"codex-factory-{index}",
        last_heartbeat_at=NOW - timedelta(days=10 + index),
        last_observed_at=(NOW - timedelta(hours=5 + index)) if inactive else None,
        outcome=outcome,
    )


def _result(
    *,
    dry_run: bool,
    count: int = 2,
    inactive: int = 0,
    rule_armed: bool = False,
) -> BrainSessionSweepResult:
    candidates = [_candidate(index, BrainSessionStatus.ABANDONED) for index in range(count)]
    candidates += [
        _candidate(100 + index, BrainSessionStatus.CLOSED_INACTIVE) for index in range(inactive)
    ]
    return BrainSessionSweepResult(
        candidates=candidates,
        dry_run=dry_run,
        cutoff=NOW - AUTO_STALE_AFTER,
        inactive_cutoff=(NOW - AGENT_INACTIVE_AFTER) if rule_armed or inactive else None,
        abandoned_count=0 if dry_run else count,
        closed_inactive_count=0 if dry_run else inactive,
    )


def test_dry_is_the_default_mode() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.wet is False


def test_threshold_default_comes_from_the_single_constant() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.older_than_days == AUTO_STALE_AFTER.days == 7


def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--older-than-days", "0"])


def test_dry_report_says_would_and_never_says_did() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True))

    assert "DRY" in report
    assert "auraient reçu" in report
    assert "ont reçu" not in report
    assert "projet-0" in report and "projet-1" in report
    assert "2026-07-31" in report  # the cutoff is rendered, not just the count


def test_wet_report_states_what_was_written() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=False))

    assert "WET" in report
    assert "2 sessions ont reçu" in report
    assert "auraient" not in report


def test_empty_sweep_is_reported_as_a_normal_night() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True, count=0))

    assert "aucune session à tarir" in report
    assert len(report.splitlines()) == 1, "aucune ligne de candidat"


class TestTheReportNeverMergesTheTwoOutcomes:
    """`abandoned` and `closed_inactive` are two facts, never one total."""

    def test_each_outcome_is_counted_and_named_separately(self) -> None:
        from brain_v42.maintenance.session_sweep import render_report

        report = render_report(_result(dry_run=False, count=1, inactive=2))

        assert "1 abandoned (7 j)" in report
        assert "2 closed_inactive (4 h)" in report
        # WITNESS: the total exists, but never ALONE — "3 sessions" without the
        # breakdown would read as "3 abandonments", and the gap between the two
        # rules is precisely what is being watched.
        assert "3 sessions ont reçu" in report

    def test_every_line_names_the_outcome_it_received(self) -> None:
        from brain_v42.maintenance.session_sweep import render_report

        lines = render_report(_result(dry_run=False, count=1, inactive=1)).splitlines()[1:]

        assert [line.split()[0] for line in lines] == ["abandoned", "closed_inactive"]

    def test_a_never_observed_session_reads_never_not_a_date(self) -> None:
        """`NULL` must read as "never observed", never as a blank."""
        from brain_v42.maintenance.session_sweep import render_report

        report = render_report(_result(dry_run=False, count=1))

        assert "observed=never" in report

    def test_a_closed_rule_says_so_instead_of_reading_as_zero_findings(self) -> None:
        """No silent ceiling: "off" ≠ "no inactive tracer".

        Without this line, a night with zero closures would read as "nothing to
        close" when the rule was not even evaluated.
        """
        from brain_v42.maintenance.session_sweep import render_report

        assert "inactive_cutoff=off" in render_report(_result(dry_run=True, count=0))
        assert "inactive_cutoff=off" not in render_report(
            _result(dry_run=True, count=0, rule_armed=True)
        )


@pytest.mark.asyncio
async def test_record_dream_run_never_raises_when_the_database_is_down() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    def broken_factory():
        raise RuntimeError("base injoignable")

    await record_dream_run(
        broken_factory, "done", dry=True, duration_s=1.0, error=None
    )  # must not raise


async def _capture_sweep_call(
    monkeypatch: pytest.MonkeyPatch, args: object, *, rule_armed: bool = False
) -> dict[str, object]:
    """Run `_run` while spying on the REAL call to `sweep_open_sessions`.

    Returns the kwargs the method actually received (``older_than``, ``reason``,
    ``dry_run``, ``close_inactive_after``) plus the return code — never an
    independent reconstruction of the implementation's f-string. Shared by every
    boundary test so they all spy on the same real path identically.
    """
    from brain_v42.maintenance.session_sweep import _run
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")
    monkeypatch.setenv("BRAIN_SESSION_INACTIVE_SWEEP_ENABLED", "true" if rule_armed else "false")
    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    monkeypatch.setattr(
        "brain_v42.maintenance.session_sweep.record_dream_run", AsyncMock(), raising=True
    )

    captured: dict[str, object] = {}

    async def fake_sweep(
        self: PgBrainSessionRepo,
        *,
        older_than: timedelta,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        close_inactive_after: timedelta | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        captured["older_than"] = older_than
        captured["reason"] = reason
        captured["dry_run"] = dry_run
        captured["close_inactive_after"] = close_inactive_after
        return BrainSessionSweepResult(
            candidates=[], dry_run=dry_run, cutoff=NOW, abandoned_count=0
        )

    monkeypatch.setattr(
        "brain_v42.repositories.pg_brain_session.PgBrainSessionRepo.sweep_open_sessions",
        fake_sweep,
        raising=True,
    )

    captured["rc"] = await _run(args)  # type: ignore[arg-type]
    return captured


@pytest.mark.asyncio
async def test_default_threshold_reason_matches_the_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the default threshold, the reason ACTUALLY passed must be the constant.

    Replaces the old decorative test that compared `AUTO_STALE_ABANDONMENT_REASON`
    with its own reconstruction (`f"auto_stale_{AUTO_STALE_AFTER.days}d"`) without
    ever touching `session_sweep.py` — a typo in `_run`'s template (say
    `f"auto_stale_{n}_days"`) would have slipped through. This one spies on the
    real call to `sweep_open_sessions`: if `AUTO_STALE_AFTER` ever changes without
    `AUTO_STALE_ABANDONMENT_REASON` following, the CLI would emit
    `auto_stale_<N>d` while this test still expects the constant — and it would
    fail here, at the place that matters.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["older_than"] == AUTO_STALE_AFTER
    assert captured["reason"] == AUTO_STALE_ABANDONMENT_REASON


@pytest.mark.asyncio
async def test_non_default_threshold_reason_reaches_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason written must reflect the threshold ACTUALLY used.

    Without this, `--older-than-days 30` would write
    `abandonment_reason='auto_stale_7d'` — a permanent audit lie, the deferred
    finding of Task 1. We spy on the real call to `sweep_open_sessions`, not on
    our own f-string.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args(["--older-than-days", "30"])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["older_than"] == timedelta(days=30)
    assert captured["reason"] == "auto_stale_30d"
    assert captured["reason"] != AUTO_STALE_ABANDONMENT_REASON


@pytest.mark.asyncio
async def test_default_invocation_reaches_the_repository_in_dry_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`args.wet is False` proves nothing about what the repository receives.

    Between the flag and the call there is a translation — `dry = not args.wet` —
    and it, not the parser default, is the phase's only safety boundary:
    inverting it really does abandon sessions on an invocation without `--wet`,
    and abandonment is irreversible. Measured 2026-08-07: that inversion survived
    the 6997 unit tests AND the 256 integration ones, because
    `_capture_sweep_call` already captured `dry_run` without anyone re-reading
    it. So we spy on the kwarg actually passed.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["dry_run"] is True


@pytest.mark.asyncio
async def test_wet_flag_reaches_the_repository_as_a_real_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the neighbouring test.

    Without it, a hardcoded `dry = True` would satisfy "DRY by default" while
    making `--wet` inoperative — the sweep would never do anything and the soak
    would look clean indefinitely. The two assertions are killed by distinct
    mutations: `dry = False` kills the default one, `dry = True` kills this one.
    """
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args(["--wet"])
    captured = await _capture_sweep_call(monkeypatch, args)

    assert captured["rc"] == 0
    assert captured["dry_run"] is False


class TestTheInactivityRuleIsDeliveredClosed:
    """The second safety boundary, and it is not `--wet`'s."""

    def test_the_flag_default_is_false(self) -> None:
        assert Settings.model_fields["brain_session_inactive_sweep_enabled"].default is False

    @pytest.mark.asyncio
    async def test_a_closed_flag_sends_no_threshold_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`None`, never `timedelta(0)`: the rule does not EXIST, it is not zero.

        A zero would make every tracer eligible that very instant. The two values
        look alike when read and have nothing in common when executed — hence the
        assertion on identity, not on truthiness.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        args = build_parser().parse_args(["--wet"])
        captured = await _capture_sweep_call(monkeypatch, args, rule_armed=False)

        assert captured["rc"] == 0
        assert captured["close_inactive_after"] is None

    @pytest.mark.asyncio
    async def test_an_armed_flag_sends_the_single_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: without it, a hardcoded `None` would pass its neighbour.

        The threshold passed is READ from `AGENT_INACTIVE_AFTER`, never copied:
        two copies of one threshold is the class of defect learning 8dc7e042
        records, and this one is already written in the model and in the ADR.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        args = build_parser().parse_args(["--wet"])
        captured = await _capture_sweep_call(monkeypatch, args, rule_armed=True)

        assert captured["rc"] == 0
        assert captured["close_inactive_after"] == AGENT_INACTIVE_AFTER
        assert AGENT_INACTIVE_AFTER == timedelta(hours=4)

    @pytest.mark.asyncio
    async def test_arming_the_rule_does_not_arm_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two boundaries compose, they do not replace each other.

        Arming the rule without `--wet` must stay a DRY: otherwise the act of
        observing would itself become the act of writing, and the observation
        window would not exist.
        """
        from brain_v42.maintenance.session_sweep import build_parser

        captured = await _capture_sweep_call(
            monkeypatch, build_parser().parse_args([]), rule_armed=True
        )

        assert captured["dry_run"] is True
        assert captured["close_inactive_after"] == AGENT_INACTIVE_AFTER
