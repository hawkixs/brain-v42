"""The morning report surfaces ROADMAP's own shrink signal, not just its fallback.

Context (ticket for this lot). `roadmap_curate` prints, per batch:

    [i/N] project: … (Ns · shrunk · model=…)

`shrunk` marks a batch whose card list was reduced because a full attempt
timed out — a LEADING INDICATOR of a fallback night, visible today only by
grepping the dated log by hand:

    logs/dream/2026-09-04_roadmap.log:  3 of 10 batches shrunk
    logs/dream/2026-09-05_roadmap.log:  5 of 10 batches shrunk

This is a DIFFERENT signal from the DEGRADED rubric next to it in the report:
DEGRADED reads `dream_runs.error_message` and says which MODEL served the
night; this reads the dated `roadmap.log` itself and says how many BATCHES
needed shrinking, which can happen even on the primary model.

ABSENT IS NOT ZERO (learning 083d74e5, followed here as in `reorg_report`):
a night with no roadmap log, or a log with no batch line at all, prints
UNMEASURED — never `0/0`, which would read as a clean night.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from scripts.dream import post_run_alert

FIXTURE_DIR = Path(__file__).parent / "data"


def _write(directory: Path, run_date: dt.date, fixture_name: str) -> Path:
    """Drop a fixture excerpt under the filename `roadmap_shrink_tally` globs for."""
    target = directory / f"{run_date.isoformat()}_roadmap.log"
    target.write_text((FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


class TestTheTallyReadsTheNightsRoadmapLog:
    def test_a_real_night_with_three_shrunk_batches_out_of_ten(self, tmp_path: Path) -> None:
        _write(tmp_path, dt.date(2026, 9, 4), "2026-09-04_roadmap.excerpt.log")

        tally = post_run_alert.roadmap_shrink_tally(dt.date(2026, 9, 4), tmp_path)

        assert (tally.shrunk, tally.total) == (3, 10)
        assert tally.measured is True

    def test_a_real_night_with_five_shrunk_batches_out_of_ten(self, tmp_path: Path) -> None:
        _write(tmp_path, dt.date(2026, 9, 5), "2026-09-05_roadmap.excerpt.log")

        tally = post_run_alert.roadmap_shrink_tally(dt.date(2026, 9, 5), tmp_path)

        assert (tally.shrunk, tally.total) == (5, 10)
        assert tally.measured is True

    def test_another_nights_log_is_not_counted(self, tmp_path: Path) -> None:
        _write(tmp_path, dt.date(2026, 9, 4), "2026-09-04_roadmap.excerpt.log")

        tally = post_run_alert.roadmap_shrink_tally(dt.date(2026, 9, 5), tmp_path)

        assert tally.measured is False

    def test_a_missing_log_is_unmeasured_not_zero(self, tmp_path: Path) -> None:
        tally = post_run_alert.roadmap_shrink_tally(dt.date(2026, 9, 6), tmp_path)

        assert tally.measured is False
        assert tally.total is None

    def test_a_log_with_no_batch_line_is_unmeasured(self, tmp_path: Path) -> None:
        """Header lines only — the phase crashed before its first batch."""
        path = tmp_path / "2026-09-06_roadmap.log"
        path.write_text(
            "2026-09-06 07:00:00 [info     ] SQLAlchemy async engine created\n",
            encoding="utf-8",
        )

        tally = post_run_alert.roadmap_shrink_tally(dt.date(2026, 9, 6), tmp_path)

        assert tally.measured is False
        assert tally.total is None


class TestTheLine:
    def test_it_renders_the_ratio_when_measured(self) -> None:
        tally = post_run_alert.RoadmapShrinkTally(shrunk=5, total=10)

        assert post_run_alert.build_roadmap_shrink_line(tally) == "ROADMAP shrunk batches: 5/10"

    def test_it_prints_unmeasured_never_zero_over_zero(self) -> None:
        tally = post_run_alert.RoadmapShrinkTally()

        line = post_run_alert.build_roadmap_shrink_line(tally)

        assert line == "ROADMAP shrunk batches: UNMEASURED"
        assert "0/0" not in line

    def test_zero_shrunk_out_of_a_measured_total_is_still_a_ratio(self) -> None:
        """A clean night — measured, and genuinely zero — is not UNMEASURED."""
        tally = post_run_alert.RoadmapShrinkTally(shrunk=0, total=10)

        assert post_run_alert.build_roadmap_shrink_line(tally) == "ROADMAP shrunk batches: 0/10"


class TestWiringIntoTheMorningReport:
    def test_the_line_appears_in_render_stdout(self, tmp_path: Path, monkeypatch) -> None:
        _write(tmp_path, dt.date(2026, 9, 5), "2026-09-05_roadmap.excerpt.log")
        monkeypatch.setattr(post_run_alert, "default_log_dir", lambda: tmp_path)
        coverage = post_run_alert.coverage_fallback(expected=1, observed=1, missing=0)

        rendered = post_run_alert.render_stdout(None, dt.date(2026, 9, 5), coverage)

        assert "ROADMAP shrunk batches: 5/10" in rendered

    def test_an_absent_roadmap_log_still_renders_unmeasured(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(post_run_alert, "default_log_dir", lambda: tmp_path)
        coverage = post_run_alert.coverage_fallback(expected=1, observed=1, missing=0)

        rendered = post_run_alert.render_stdout(None, dt.date(2026, 9, 5), coverage)

        assert "ROADMAP shrunk batches: UNMEASURED" in rendered
