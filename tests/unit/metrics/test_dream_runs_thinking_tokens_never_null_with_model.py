"""Every `dream_runs` writer that names a model also writes `thinking_tokens`.

Migration 049 lesson (ticket `554db5f8`): a column can be added, merged and
applied in production while the writers that should fill it stay silent --
`closed_inactive_count` was NULL on 63/63 rows the first night, and
`thinking_tokens` was NULL on every `extract/*` and `roadmap/*` row that DID
carry a model. `tests/unit/test_dream_049_columns_are_written.py` closed that
gap for the NVIDIA rail (`ticket_extract`, `roadmap_curate`). This module
closes it for the remaining writers that can bind `model`: the shared parser
INSERT (`dream_parser._insert_dream_run`, the single site behind three CLIs --
`dream_parser`, `codex_dream_parser`, `agy_dream_parser`), and confirms the two
writers that never claim a model (`session_sweep`, the empty-pool `promote`
row) never fabricate a measurement either.

The invariant this module pins, read from the code rather than assumed:
whenever a writer's ``model`` argument is a real value AND its own telemetry
positively measured reasoning tokens for that call, that number reaches the
bound SQL parameter -- never dropped, never silently replaced by NULL.

One documented exception is deliberately NOT touched here. When the call
executed but the specific rail/version cannot report reasoning at all --
`telemetry` is entirely absent (a codex JSONL stream with no valid
`turn.completed`), or the field is simply missing from an otherwise-normal
usage payload (an older codex, a ChatGPT-authenticated run, Claude's OTEL
console, which never reports it) -- `PhaseTelemetry.thinking_tokens` stays
`None` and `_insert_dream_run` writes NULL, on purpose: "not measured" is not
"measured as zero" (dataclass docstring; tickets `76e11c9f`, `42b05302`; and
already pinned at the bind level by
`test_insert_dream_run_binds_the_project_key_before_phase_dry_run` in
`test_dream_parser_project_key.py`, with ``telemetry=None``). Collapsing that
NULL to 0 would erase a distinction the codebase spent two tickets
establishing, for rails that CAN report a nonzero count on other calls. This
module adds the symmetric, previously-missing half of that same pin: a REAL,
non-None telemetry object whose call genuinely reports zero-reasoning-known,
and a REAL, non-None telemetry object whose call DOES carry a count -- proving
the latter is never lost on the way to the bound parameter.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.metrics.dream_parser import PhaseTelemetry, _insert_dream_run


async def _insert_via_mock_asyncpg(**overrides: Any) -> list[Any]:
    """Run the real `_insert_dream_run` and return the positional bind args."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    kwargs: dict[str, Any] = {
        "run_date": "2026-09-02",
        "phase": "reorg",
        "model": "gpt-5-codex",
        "status": "done",
        "duration_s": 12.0,
        "telemetry": None,
        "project_key": "brain-v42",
        "error_message": None,
        "phase_dry_run": False,
    }
    kwargs.update(overrides)

    with patch(
        "brain_v42.metrics.dream_parser.asyncpg.connect",
        new=AsyncMock(return_value=mock_conn),
    ):
        await _insert_dream_run(**kwargs)

    _sql, *bind_args = mock_conn.execute.await_args.args
    return bind_args


# thinking_tokens is column $15 of 16 -- see the INSERT literal in
# `_insert_dream_run` and its column-order pin in
# `test_insert_dream_run_binds_the_project_key_before_phase_dry_run`.
_THINKING_TOKENS_INDEX = 14


class TestTheSharedParserInsertNeverDropsAMeasuredReasoningCount:
    """One INSERT site behind three CLIs: `dream_parser`, `codex_dream_parser`,
    `agy_dream_parser`. Nothing, before this test, pinned at the bound-SQL-
    parameter level that a reasoning count the rail DID measure actually
    reaches the column rather than being lost to the
    ``telemetry.xxx if telemetry else None`` chain.
    """

    async def test_a_measured_reasoning_count_reaches_the_column(self) -> None:
        telemetry = PhaseTelemetry(input_tokens=3938, output_tokens=1929, thinking_tokens=1929)

        bind_args = await _insert_via_mock_asyncpg(model="gpt-5-codex", telemetry=telemetry)

        assert bind_args[_THINKING_TOKENS_INDEX] == 1929

    async def test_a_zero_measured_reasoning_count_is_not_confused_with_unmeasured(self) -> None:
        """0 reached the column is 0, not re-coerced to anything else."""
        telemetry = PhaseTelemetry(input_tokens=100, output_tokens=50, thinking_tokens=0)

        bind_args = await _insert_via_mock_asyncpg(model="sonnet", telemetry=telemetry)

        assert bind_args[_THINKING_TOKENS_INDEX] == 0

    async def test_a_rail_that_did_not_report_reasoning_still_writes_null_not_zero(self) -> None:
        """Deliberate exception, not a gap: see the module docstring.

        The existing pin in `test_dream_parser_project_key.py` only exercises
        `telemetry=None` (a total parse failure). This exercises a REAL,
        non-None telemetry object -- input/output tokens genuinely measured --
        whose `thinking_tokens` field simply never got set, which is exactly
        the shape `parse_otel_log` (Claude) and an older `codex`/`agy` stream
        produce. Coercing this to 0 would be the wrong fix: it would claim
        "measured, zero" about a call this rail cannot even see into.
        """
        telemetry = PhaseTelemetry(input_tokens=100, output_tokens=50)  # thinking_tokens: None

        bind_args = await _insert_via_mock_asyncpg(model="sonnet", telemetry=telemetry)

        assert bind_args[_THINKING_TOKENS_INDEX] is None


class TestWritersThatNeverClaimAModelNeverClaimAReasoningCount:
    """`session_sweep` and the empty-pool `promote` row hardcode `model=NULL`
    because no model was called -- so the "carries a model" precondition of
    this lot's invariant never applies to them. Pinned here so the two stay
    honest about not fabricating a measurement, and so a future edit that adds
    a real `model` to either path trips a test in the same file that already
    reasons about the pairing.
    """

    async def test_sweep_binds_no_model_and_claims_no_thinking_tokens_column(self) -> None:
        from brain_v42.maintenance.session_sweep import record_dream_run

        bound: list[dict[str, Any]] = []

        class _CapturingSession:
            async def execute(self, _statement: Any, parameters: dict[str, Any]) -> None:
                bound.append(parameters)

            async def __aenter__(self) -> _CapturingSession:
                return self

            async def __aexit__(self, *_exc: object) -> bool:
                return False

            def begin(self) -> Any:
                return self

        await record_dream_run(
            lambda: _CapturingSession(), "done", dry=False, duration_s=1.0, error=None
        )

        assert bound[0].get("model") is None or "model" not in bound[0]
        assert "thinking_tokens" not in bound[0]

    async def test_promote_empty_pool_binds_no_model_and_no_thinking_tokens(self) -> None:
        from scripts.dream import _promote_helpers

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=context)

        await _promote_helpers._record_empty_pool(
            factory, dt.date(2026, 8, 8), 1.0, project_key="brain-v42"
        )

        statement = session.execute.await_args.args[0]
        params = statement.compile().params

        assert params["model"] is None
        assert "thinking_tokens" not in params


class TestTheNvidiaRailAlreadyClosedThisGap:
    """`ticket_extract` and `roadmap_curate` are named in this lot's scope too.
    Their contract is already pinned in
    `tests/unit/test_dream_049_columns_are_written.py`
    (`TestTheNvidiaRailWritesAnIntegerNeverNull`); this is a thin,
    non-duplicating sanity check that both still expose the same signature so
    a future refactor cannot silently drop the default out from under that
    other module.
    """

    @pytest.mark.parametrize("module_name", ["ticket_extract", "roadmap_curate"])
    def test_thinking_tokens_defaults_to_a_non_null_int(self, module_name: str) -> None:
        import importlib
        import inspect

        module = importlib.import_module(f"brain_v42.scripts.{module_name}")
        signature = inspect.signature(module.record_dream_run)

        default = signature.parameters["thinking_tokens"].default

        assert default is not None
        assert isinstance(default, int)
