"""Tests for scripts/dream/cross_project_resonance.py (Spec C MVP β)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from scripts.dream import cross_project_resonance as cpr
from scripts.dream.cross_project_resonance import (
    ResonancePair,
    build_report_path,
    render_markdown_report,
)

A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")


def _pair(**kw) -> ResonancePair:
    defaults = {
        "a_id": A,
        "b_id": B,
        "a_project": "brain-v42",
        "b_project": "red-shrik",
        "a_title": "Use Qodo-Embed-1.5B",
        "b_title": "Qodo-Embed for code embedding",
        "a_created_at": date(2026, 4, 15),
        "b_created_at": date(2026, 4, 22),
        "cosine": 0.91,
        "domain": "ml",
    }
    defaults.update(kw)
    return ResonancePair(**defaults)


class TestResonancePair:
    def test_dedup_key_stable_across_id_order(self):
        p1 = _pair(a_id=A, b_id=B)
        p2 = _pair(a_id=B, b_id=A)
        assert p1.dedup_key == p2.dedup_key

    def test_dedup_key_differs_by_domain(self):
        assert _pair(domain="ml").dedup_key != _pair(domain="memory").dedup_key

    def test_hint_drift_on_numeric_divergence(self):
        p = _pair(a_title="Cosine 0.92 for dedup", b_title="Cosine 0.85 for dedup")
        assert p.hint.startswith("drift candidate")
        assert "0.92" in p.hint and "0.85" in p.hint

    def test_hint_convergence_without_divergence(self):
        assert _pair().hint.startswith("convergence likely")

    def test_format_insight_includes_both_projects_and_hint(self):
        text = _pair().format_insight()
        assert "[brain-v42]" in text and "[red-shrik]" in text
        assert "cosine=0.910" in text
        assert "Hint:" in text


class TestReport:
    def test_report_groups_by_domain_with_counts(self):
        pairs = [_pair(), _pair(domain="memory", cosine=0.83)]
        md = render_markdown_report(pairs, threshold=0.80, run_id=42, report_date="2026-06-12")
        assert "# Cross-Project Resonance — 2026-06-12" in md
        assert "Pairs found: 2" in md
        assert "Run ID: 42" in md
        assert "## Domain: ml (1 pair" in md
        assert "## Domain: memory (1 pair" in md
        assert "cosine=0.91" in md

    def test_report_zero_pairs(self):
        md = render_markdown_report([], threshold=0.80, run_id=42, report_date="2026-06-12")
        assert "Pairs found: 0" in md
        assert "No cross-project resonance pairs above threshold this run." in md

    def test_build_report_path_uses_date(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = build_report_path("2026-06-12")
        assert p.name == "cross_project_resonance_2026-06-12.md"
        assert p.parent.name == "dream"
        assert p.parent.parent.name == "artifacts"


def _settings(enabled: bool) -> MagicMock:
    s = MagicMock()
    s.brain_dream_cross_project_enabled = enabled
    s.postgres_url = "postgresql+asyncpg://u:p@h:5432/db"
    return s


class TestMainGates:
    @pytest.mark.asyncio
    async def test_disabled_env_exits_0_without_db(self):
        with patch.object(cpr, "_load_settings", return_value=_settings(False)):
            rc = await cpr.run(mode="dry_run", domains=None, date_str=None)
        assert rc == 0

    @pytest.mark.asyncio
    async def test_missing_threshold_exits_1(self):
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr.thresholds, "by_name", return_value=None),
        ):
            rc = await cpr.run(mode="dry_run", domains=None, date_str=None)
        assert rc == 1


class TestMainFlow:
    def _wire(self, graph_ids, pair_rows):
        """Patch all I/O seams; return dict of mocks."""
        m = {
            "graph": AsyncMock(),
            "repo": AsyncMock(),
            "start": AsyncMock(return_value=42),  # _insert_run -> run_id
            "finish": AsyncMock(),  # _finish_run
            "exists": AsyncMock(return_value=False),  # _learning_exists
            "write_learning": AsyncMock(),  # _write_learning
            "write_report": MagicMock(),  # _write_report_file
        }
        m["graph"].fetch_decision_ids_in_domain.side_effect = graph_ids
        m["repo"].fetch_cross_project_resonance_pairs.return_value = pair_rows
        m["deps"] = (MagicMock(), m["graph"], m["repo"])  # (session_factory, graph, repo)
        return m

    def _row(self, cosine=0.9):
        return {
            "a_id": A,
            "b_id": B,
            "a_project": "brain-v42",
            "b_project": "red-shrik",
            "a_title": "t1",
            "b_title": "t2",
            "a_created_at": date(2026, 4, 1),
            "b_created_at": date(2026, 4, 2),
            "cosine": cosine,
        }

    @pytest.mark.asyncio
    async def test_dry_run_writes_report_no_learnings(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", m["write_report"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="dry_run", domains=None, date_str="2026-06-12")
        assert rc == 0
        m["write_report"].assert_called_once()
        m["write_learning"].assert_not_called()
        m["finish"].assert_awaited_once()
        assert m["finish"].call_args.kwargs.get("status", m["finish"].call_args[0][-1]) == "done"

    @pytest.mark.asyncio
    async def test_domain_below_min_is_skipped(self):
        m = self._wire(graph_ids=[[str(A)]] * 9, pair_rows=[])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
        ):
            await cpr.run(mode="dry_run", domains=None, date_str="2026-06-12")
        m["repo"].fetch_cross_project_resonance_pairs.assert_not_called()

    @pytest.mark.asyncio
    async def test_wet_writes_learnings_with_dedup(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        m["exists"].return_value = False
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_learning_exists", m["exists"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        assert rc == 0
        m["write_learning"].assert_awaited_once()
        pair_arg = m["write_learning"].call_args[0][1]  # (session_factory_or_run_id, pair, ...)
        assert isinstance(pair_arg, cpr.ResonancePair)

    @pytest.mark.asyncio
    async def test_wet_skips_existing_dedup_key(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        m["exists"].return_value = True
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_learning_exists", m["exists"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        m["write_learning"].assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_marks_run_fail_and_reraises(self):
        m = self._wire(graph_ids=RuntimeError("neo4j down"), pair_rows=[])
        m["graph"].fetch_decision_ids_in_domain.side_effect = RuntimeError("neo4j down")
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            pytest.raises(RuntimeError),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        status_arg = m["finish"].call_args.kwargs.get("status") or m["finish"].call_args[0][-1]
        assert status_arg == "fail"

    @pytest.mark.asyncio
    async def test_pairs_capped_at_max_per_night(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        rows = [self._row(cosine=0.80 + i / 1000) for i in range(30)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=rows)
        report_mock = MagicMock()
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", report_mock),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        pairs_arg = report_mock.call_args[0][1]
        assert len(pairs_arg) == cpr.MAX_PAIRS_PER_NIGHT
        assert pairs_arg[0].cosine >= pairs_arg[-1].cosine

    @pytest.mark.asyncio
    async def test_domain_ids_capped_at_max_decisions(self):
        ids = [str(UUID(int=i)) for i in range(250)]  # > MAX_DECISIONS_PER_DOMAIN
        m = self._wire(graph_ids=[ids], pair_rows=[])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        sent_ids = m["repo"].fetch_cross_project_resonance_pairs.call_args.kwargs["ids"]
        assert len(sent_ids) == cpr.MAX_DECISIONS_PER_DOMAIN

    @pytest.mark.asyncio
    async def test_wet_blocked_when_inner_recheck_disabled(self):
        """Spec safeguard 3c: env re-read just before WET writes must block."""
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids], pair_rows=[self._row()])
        with (
            patch.object(cpr, "_load_settings", side_effect=[_settings(True), _settings(False)]),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        assert rc == 1
        m["write_learning"].assert_not_called()


class TestReportFile:
    def test_report_file_overwritten_on_rerun(self, tmp_path):
        p = tmp_path / "r.md"
        cpr._write_report_file(p, [], threshold=0.8, run_id=1, report_date="2026-06-12")
        cpr._write_report_file(p, [], threshold=0.8, run_id=2, report_date="2026-06-12")
        assert "Run ID: 2" in p.read_text()
        assert "Run ID: 1" not in p.read_text()
