"""Unit tests for dream metrics collection in MetricsCollector."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector


def _make_collector_with_dream_data(
    rows: list,
    history_rows: list | None = None,
    promote_outcome_row: tuple | None = None,
):
    """Create a MetricsCollector with a mocked session returning dream_runs data."""
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._session_factory = MagicMock()

    mock_session = AsyncMock()

    # execute order: phases → history → promote-outcome (optional)
    phases_result = MagicMock()
    phases_result.all.return_value = rows
    history_result = MagicMock()
    history_result.all.return_value = history_rows or []
    promote_result = MagicMock()
    promote_result.first.return_value = promote_outcome_row

    mock_session.execute = AsyncMock(side_effect=[phases_result, history_result, promote_result])
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    collector._session_factory.return_value = mock_session
    return collector


class TestCollectDreamMetrics:
    @pytest.fixture(autouse=True)
    def _no_operator_killswitches_in_unit_fixture(self):
        with patch("brain_v42.metrics.collector_dream.expected_dream_phases", return_value=set()):
            yield

    @pytest.mark.asyncio
    async def test_missing_expected_phase_is_partial_with_terminal_cause(self) -> None:
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                0,
                0,
                0,
                0,
                0.0,
                0,
                0,
                date(2026, 8, 1),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)

        with patch(
            "brain_v42.metrics.collector_dream.expected_dream_phases",
            return_value={"scan", "extract"},
        ):
            result = await collector.collect_dream_metrics()

        assert result["last_run"]["status"] == "partial"
        assert result["last_run"]["phases_fail"] == 1
        assert result["last_run"]["phases"]["extract"] == {
            "status": "partial",
            "model": None,
            "duration_s": 0.0,
            "cost_usd": 0.0,
            "tokens": 0,
            "api_calls": 0,
            "tool_calls": 0,
            "error_message": "expected enabled phase missing from dream_runs",
            "dry_run": False,
        }

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_runs(self) -> None:
        """Returns empty dict when dream_runs table has no data."""
        collector = _make_collector_with_dream_data([])
        result = await collector.collect_dream_metrics()
        assert result == {}

    @pytest.mark.asyncio
    async def test_last_run_aggregates_phases(self) -> None:
        """last_run contains per-phase detail and totals."""
        rows = [
            # phase, model, status, duration_s, in_tok, out_tok, cache_r, cache_c,
            # cost, api, tool, date, error_message, phase_dry_run
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                12000,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
            (
                "synth",
                "opus",
                "done",
                288.0,
                50000,
                15000,
                30000,
                5000,
                0.18,
                12,
                10,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        history = [
            (date(2026, 4, 5), 0.21, 68750, "success"),
        ]
        collector = _make_collector_with_dream_data(rows, history)
        result = await collector.collect_dream_metrics()

        assert result["last_run"]["date"] == "2026-04-05"
        assert result["last_run"]["status"] == "success"
        assert result["last_run"]["phases_ok"] == 2
        assert result["last_run"]["phases_fail"] == 0
        assert abs(result["last_run"]["total_cost_usd"] - 0.21) < 0.001
        assert result["last_run"]["total_tokens"] == 3500 + 250 + 50000 + 15000
        assert "scan" in result["last_run"]["phases"]
        assert "synth" in result["last_run"]["phases"]

    @pytest.mark.asyncio
    async def test_phase_detail_correct(self) -> None:
        """Each phase has the correct detail."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()

        scan = result["last_run"]["phases"]["scan"]
        assert scan["status"] == "done"
        assert scan["model"] == "sonnet"
        assert scan["tokens"] == 3750
        assert scan["api_calls"] == 6
        assert scan["tool_calls"] == 5

    @pytest.mark.asyncio
    async def test_failed_phase_counted(self) -> None:
        """Failed phases are counted in phases_fail."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
            (
                "clean",
                "sonnet",
                "timeout",
                300.0,
                0,
                0,
                0,
                0,
                0.0,
                0,
                0,
                date(2026, 4, 5),
                "oom",
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()

        assert result["last_run"]["phases_ok"] == 1
        assert result["last_run"]["phases_fail"] == 1
        assert result["last_run"]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_history_returned(self) -> None:
        """History contains aggregated per-date entries."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        history = [
            (date(2026, 4, 5), 0.42, 125000, "success"),
            (date(2026, 3, 29), 0.38, 110000, "success"),
        ]
        collector = _make_collector_with_dream_data(rows, history)
        result = await collector.collect_dream_metrics()

        assert len(result["history"]) == 2
        assert result["history"][0]["date"] == "2026-04-05"
        assert result["history"][1]["cost_usd"] == 0.38

    @pytest.mark.asyncio
    async def test_phase_exposes_error_message(self) -> None:
        """Failed phases expose their error_message for UI display."""
        rows = [
            (
                "clean",
                "sonnet",
                "timeout",
                300.0,
                0,
                0,
                0,
                0,
                0.0,
                0,
                0,
                date(2026, 4, 5),
                "phase exceeded 10min wallclock",
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["phases"]["clean"]["error_message"] == (
            "phase exceeded 10min wallclock"
        )

    @pytest.mark.asyncio
    async def test_phase_error_message_none_when_done(self) -> None:
        """Successful phases have error_message=None (not missing)."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["phases"]["scan"]["error_message"] is None

    @pytest.mark.asyncio
    async def test_promote_outcome_populated_when_promote_phase_ran(self) -> None:
        """When PROMOTE phase is in last_run, promote_outcome exposes dry_run + target_type."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
            (
                "promote",
                "opus",
                "done",
                180.0,
                2000,
                500,
                0,
                0,
                0.25,
                3,
                2,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        promote_outcome = ("dry_run", None, "dry_run rehearsal")
        collector = _make_collector_with_dream_data(rows, promote_outcome_row=promote_outcome)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["promote_outcome"] == {
            "dry_run": True,
            "target_type": "dry_run",
            "target_id": None,
            "reason": "dry_run rehearsal",
        }

    @pytest.mark.asyncio
    async def test_promote_outcome_real_adr_not_dry_run(self) -> None:
        """When PROMOTE materialized an ADR, dry_run=False and target_id is the ADR uuid."""
        rows = [
            (
                "promote",
                "opus",
                "done",
                180.0,
                2000,
                500,
                0,
                0,
                0.25,
                3,
                2,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        promote_outcome = ("adr", "abc-123", None)
        collector = _make_collector_with_dream_data(rows, promote_outcome_row=promote_outcome)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["promote_outcome"] == {
            "dry_run": False,
            "target_type": "adr",
            "target_id": "abc-123",
            "reason": None,
        }

    @pytest.mark.asyncio
    async def test_promote_outcome_absent_when_no_promote_phase(self) -> None:
        """Without a PROMOTE phase in last_run, promote_outcome is omitted (not None)."""
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 4, 5),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        assert "promote_outcome" not in result["last_run"]

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self) -> None:
        """DB error returns empty dict (graceful degradation)."""
        collector = MetricsCollector.__new__(MetricsCollector)
        collector._session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        collector._session_factory.return_value = mock_session

        result = await collector.collect_dream_metrics()
        assert result == {}


class TestDreamMetricsRoadmapSpec7:
    """Spec 2026-07-04 §7 — re-run dedup + per-phase dry_run (additive contract)."""

    @pytest.mark.asyncio
    async def test_phase_dry_run_flag_exposed(self) -> None:
        rows = [
            ("extract", None, "done", 12.0, 0, 0, 0, 0, 0.0, 0, 0, date(2026, 7, 4), None, True),
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                100,
                10,
                0,
                0,
                0.01,
                2,
                1,
                date(2026, 7, 4),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        assert result["last_run"]["phases"]["extract"]["dry_run"] is True
        assert result["last_run"]["phases"]["scan"]["dry_run"] is False

    @pytest.mark.asyncio
    async def test_last_run_query_dedups_reruns(self) -> None:
        """The last-run query takes the LAST row per phase (id DESC)."""
        collector = _make_collector_with_dream_data([])
        await collector.collect_dream_metrics()
        # first execute = the last-run query
        sql = str(collector._session_factory.return_value.execute.call_args_list[0][0][0])
        assert "DISTINCT ON (phase)" in sql
        assert "ORDER BY phase, id DESC" in sql

    @pytest.mark.asyncio
    async def test_history_status_dedups_but_costs_sum_all(self) -> None:
        rows = [
            ("scan", "sonnet", "done", 1.0, 0, 0, 0, 0, 0.0, 0, 0, date(2026, 7, 4), None, False),
        ]
        collector = _make_collector_with_dream_data(rows)
        await collector.collect_dream_metrics()
        sql = str(collector._session_factory.return_value.execute.call_args_list[1][0][0])
        assert "DISTINCT ON (run_date, phase)" in sql
        # the cost aggregates the whole dream_runs (not the deduplicated subset)
        assert "SUM(dr.cost_usd)" in sql

    @pytest.mark.asyncio
    async def test_contract_additive_existing_keys_unchanged(self) -> None:
        rows = [
            (
                "scan",
                "sonnet",
                "done",
                42.0,
                3500,
                250,
                0,
                0,
                0.03,
                6,
                5,
                date(2026, 7, 4),
                None,
                False,
            ),
        ]
        collector = _make_collector_with_dream_data(rows)
        result = await collector.collect_dream_metrics()
        scan = result["last_run"]["phases"]["scan"]
        for key in (
            "status",
            "model",
            "duration_s",
            "cost_usd",
            "tokens",
            "api_calls",
            "tool_calls",
            "error_message",
        ):
            assert key in scan


class TestExpectedDreamPhasesSweep:
    """The sweep phase is expected only if the operator explicitly armed it."""

    def _phases(self, tmp_path, body: str) -> set[str]:
        from brain_v42.metrics.collector_dream import expected_dream_phases

        drop_in = tmp_path / "killswitches.conf"
        drop_in.write_text(body)
        return expected_dream_phases(drop_in)

    def test_sweep_expected_when_the_killswitch_is_open(self, tmp_path) -> None:
        phases = self._phases(tmp_path, "[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=true\n")
        assert "sweep" in phases

    def test_sweep_not_expected_when_the_key_is_absent(self, tmp_path) -> None:
        phases = self._phases(tmp_path, "[Service]\nEnvironment=BRAIN_DREAM_ROADMAP_ENABLED=true\n")
        assert "sweep" not in phases

    def test_sweep_not_expected_when_the_killswitch_is_closed(self, tmp_path) -> None:
        phases = self._phases(tmp_path, "[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=false\n")
        assert "sweep" not in phases


class TestExpectedDreamPhasesPromote:
    """Promote stays EXPECTED for as long as its killswitch is open.

    The right answer to an empty pool is to write a real row, never to disarm the
    expectation: a promote that crashes writes nothing either, and that is exactly
    what this detector must keep catching (incidents of 2026-05-02 and
    2026-05-03, two days of silence).
    """

    def _phases(self, tmp_path, body: str) -> set[str]:
        from brain_v42.metrics.collector_dream import expected_dream_phases

        drop_in = tmp_path / "killswitches.conf"
        drop_in.write_text(body)
        return expected_dream_phases(drop_in)

    def test_promote_expected_when_the_killswitch_is_open(self, tmp_path) -> None:
        phases = self._phases(tmp_path, "[Service]\nEnvironment=BRAIN_DREAM_PROMOTE_ENABLED=true\n")
        assert "promote" in phases

    def test_promote_not_expected_when_the_killswitch_is_closed(self, tmp_path) -> None:
        phases = self._phases(
            tmp_path, "[Service]\nEnvironment=BRAIN_DREAM_PROMOTE_ENABLED=false\n"
        )
        assert "promote" not in phases
