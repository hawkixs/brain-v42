"""The two columns migration 049 added actually receive a value.

Ticket `554db5f8`. Measured on the run of 2026-09-03 06:00→07:41, the first night
049 could have written: `closed_inactive_count` NULL on **63 rows out of 63** --
including the sweep row, which had counted 71 -- and `thinking_tokens` NULL on the
five `extract/*` and `roadmap/*` rows, which DO carry a model. A column added,
merged, applied in production, and writing nothing.

These INSERTs are best-effort by design: each is wrapped in a bare `except` that
prints a warning and continues, because a telemetry row must never kill the phase
it observes. That property has a consequence for testing, and it is the whole
shape of this module: **a test that asserts "no exception was raised" proves
nothing at all here.** Every test below reads back the parameters the writer
actually bound, through a fake session factory that captures them.
"""

from __future__ import annotations

from typing import Any

import pytest


class _CapturingSession:
    """Captures what the writer BINDS, which is the only observable that matters."""

    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    async def execute(self, _statement: Any, parameters: dict[str, Any]) -> None:
        self._sink.append(parameters)

    def begin(self) -> Any:
        return _Nothing()

    async def __aenter__(self) -> _CapturingSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Nothing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _factory(sink: list[dict[str, Any]]) -> Any:
    def make() -> _CapturingSession:
        return _CapturingSession(sink)

    return make


class TestTheSweepWritesWhatItClosedOrWouldHaveClosed:
    """`closed_inactive_count` was NULL on 63/63 rows, sweep included."""

    @pytest.mark.asyncio
    async def test_a_wet_sweep_records_the_number_it_closed(self) -> None:
        from brain_v42.maintenance.session_sweep import record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound),
            "done",
            dry=False,
            duration_s=1.0,
            error=None,
            closed_inactive_count=71,
        )
        assert bound[0]["closed_inactive_count"] == 71

    @pytest.mark.asyncio
    async def test_a_dry_sweep_records_what_it_WOULD_have_closed(self) -> None:
        """The number the night was about, on the row that says it was a rehearsal.

        `phase_dry_run` is on the same row, so a consumer wanting only real
        closures filters on it. Writing NULL instead threw the rehearsal's whole
        measurement away to protect a filter nobody had written.
        """
        from brain_v42.maintenance.session_sweep import record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound),
            "done",
            dry=True,
            duration_s=1.0,
            error=None,
            closed_inactive_count=71,
        )
        assert bound[0]["closed_inactive_count"] == 71
        assert bound[0]["phase_dry_run"] is True

    @pytest.mark.asyncio
    async def test_a_night_that_failed_before_counting_still_records_NULL(self) -> None:
        """ "Not evaluated" is not "zero closures" -- ticket 24ca3b73, kept."""
        from brain_v42.maintenance.session_sweep import record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound),
            "failed",
            dry=False,
            duration_s=1.0,
            error="boom",
        )
        assert bound[0]["closed_inactive_count"] is None


class TestTheNvidiaRailNowDistinguishesZeroFromUnmeasured:
    """`thinking_tokens` was NULL on every extract/* and roadmap/* row that DID
    carry a model -- the first fix (hard-coding 0 as "the honest value today")
    corrected that, but was itself half-wrong: 0 and "not measured" collapsed
    into the same value again, just the other one. Measured 2026-09-07: models
    ran on both phases and every row still carried 0, because nothing threaded
    the real per-call measurement through -- the extractor was dead code.

    The NVIDIA rail now follows the same rule as the codex/agy/claude rail
    (`PhaseTelemetry`, `dream_parser.py`): `None` when nothing was measured
    (including "the call was never made"), `0` only when a real zero was
    reported.
    """

    @pytest.mark.parametrize("module", ["ticket_extract", "roadmap_curate"])
    @pytest.mark.asyncio
    async def test_the_default_is_none_not_zero(self, module: str) -> None:
        import importlib

        record_dream_run = importlib.import_module(f"brain_v42.scripts.{module}").record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound), "done", dry=False, duration_s=1.0, error=None, model="m"
        )
        assert "thinking_tokens" in bound[0]
        assert bound[0]["thinking_tokens"] is None

    @pytest.mark.parametrize("module", ["ticket_extract", "roadmap_curate"])
    @pytest.mark.asyncio
    async def test_a_measured_count_reaches_the_column(self, module: str) -> None:
        import importlib

        record_dream_run = importlib.import_module(f"brain_v42.scripts.{module}").record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound),
            "done",
            dry=False,
            duration_s=1.0,
            error=None,
            model="m",
            thinking_tokens=1234,
        )
        assert bound[0]["thinking_tokens"] == 1234

    @pytest.mark.parametrize("module", ["ticket_extract", "roadmap_curate"])
    @pytest.mark.asyncio
    async def test_a_measured_zero_is_not_coerced_back_to_none(self, module: str) -> None:
        import importlib

        record_dream_run = importlib.import_module(f"brain_v42.scripts.{module}").record_dream_run

        bound: list[dict[str, Any]] = []
        await record_dream_run(
            _factory(bound),
            "done",
            dry=False,
            duration_s=1.0,
            error=None,
            model="m",
            thinking_tokens=0,
        )
        assert bound[0]["thinking_tokens"] == 0


class TestTheReasoningCountIsReadFromWhateverTheProviderSends:
    """`None` means the call's usage did not carry a trustworthy reasoning
    count -- absent key, non-dict usage, wrong type, or a negative value.
    `0` means the provider explicitly reported zero. Collapsing the two (the
    previous contract here) is exactly what let 2026-09-07's rows read 0 while
    genuinely unmeasured.
    """

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            (None, None),
            ({}, None),
            ({"prompt_tokens": 10, "completion_tokens": 20}, None),
            ({"reasoning_tokens": 42}, 42),
            ({"reasoning_tokens": 0}, 0),
            ({"completion_tokens_details": {"reasoning_tokens": 7}}, 7),
            ({"completion_tokens_details": {"reasoning_tokens": 0}}, 0),
            ({"reasoning_tokens": None}, None),
            ({"reasoning_tokens": "nonsense"}, None),
            ({"completion_tokens_details": None}, None),
            ({"reasoning_tokens": -5}, None),
        ],
    )
    def test_it_survives_every_shape_the_provider_might_send(
        self, usage: dict[str, Any] | None, expected: int | None
    ) -> None:
        from brain_v42.scripts.ticket_extract import thinking_tokens_from_usage

        assert thinking_tokens_from_usage(usage) == expected

    def test_there_is_exactly_one_definition_of_it(self) -> None:
        """Two rails reading the same provider usage differently is the drift.

        The extractor is defined once, in `ticket_extract`, and imported into
        `roadmap_curate` under a private alias so this module never grows a
        second `thinking_tokens_from_usage` of its own.
        """
        import brain_v42.scripts.roadmap_curate as roadmap
        import brain_v42.scripts.ticket_extract as extract

        assert callable(extract.thinking_tokens_from_usage)
        assert not hasattr(roadmap, "thinking_tokens_from_usage"), (
            "a second definition appeared -- one usage, one reading"
        )


class TestCombineThinkingTokensFoldsMeasurements:
    """`combine_thinking_tokens` had ZERO direct tests: every run-level test
    only ever folded `None` and `17`, so a fold that collapsed a genuinely
    measured zero back to `None` (the exact conflation this module exists to
    forbid) could pass the whole suite unnoticed.
    """

    @pytest.mark.parametrize(
        ("current", "addition", "expected"),
        [
            (None, None, None),
            (None, 0, 0),
            (None, 5, 5),
            (0, None, 0),
            (3, 0, 3),
            (3, 4, 7),
        ],
    )
    def test_the_fold_table(
        self, current: int | None, addition: int | None, expected: int | None
    ) -> None:
        from brain_v42.scripts.ticket_extract import combine_thinking_tokens

        assert combine_thinking_tokens(current, addition) == expected

    def test_more_than_two_measurements_fold_left_to_right(self) -> None:
        """A whole night's worth of batches/threads, not just one call chain."""
        from brain_v42.scripts.ticket_extract import combine_thinking_tokens

        total: int | None = None
        for addition in (None, 5, None, 3, 0):
            total = combine_thinking_tokens(total, addition)

        assert total == 8

    def test_there_is_exactly_one_definition_of_the_combiner_too(self) -> None:
        import brain_v42.scripts.roadmap_curate as roadmap
        import brain_v42.scripts.ticket_extract as extract

        assert callable(extract.combine_thinking_tokens)
        assert not hasattr(roadmap, "combine_thinking_tokens"), (
            "a second definition appeared -- one fold, one place"
        )
