"""The morning report says what REORG did, so a silent phase stops being silent.

Ticket `1597c36d`. Measured 2026-09-03: REORG ran on ten projects a night, in
WET, for twelve nights, updating 3 to 82 tags each night and archiving NOTHING --
and the morning report said not one word about it. `dream_runs` carries no REORG
counter, so nothing could have said it.

The counts exist, in the JSON line each project's report ends with. This block
reads them from there. What it does not read -- candidates examined, candidates
refused -- REORG states in prose, and a regex over a model's free French would be
a number nobody could trust.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts" / "dream"))

import post_run_alert  # noqa: E402

RUN_DATE = dt.date(2026, 9, 3)


def _log(directory: Path, project: str, *, updated: int, archived: int) -> None:
    body = (
        f"# Rapport REORG — {project} — {RUN_DATE.isoformat()}\n\n"
        "## Pollution archived\n\n"
        "Aucune entité archivée. Tous les titres correspondant à l'allowlist "
        "dépassent le seuil `access_count > 5`.\n\n"
        "=== REORG REPORT ===\n"
        '{"dry_run":false,"updated":['
        + ",".join(f'"u{i}"' for i in range(updated))
        + '],"archived":['
        + ",".join(f'"a{i}"' for i in range(archived))
        + "]}\n=== END ===\n"
    )
    (directory / f"{RUN_DATE.isoformat()}_{project}_reorg.log").write_text(body, encoding="utf-8")


class TestTheTallyReadsTheNightsReports:
    def test_it_sums_across_the_pool(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=28, archived=0)
        _log(tmp_path, "red-lab", updated=1, archived=0)
        _log(tmp_path, "red-shrik:agent", updated=0, archived=0)

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert (tally.projects, tally.updated, tally.archived) == (3, 29, 0)

    def test_another_nights_logs_are_not_counted(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=5, archived=1)
        (tmp_path / "2026-09-02_brain-v42_reorg.log").write_text(
            '=== REORG REPORT ===\n{"dry_run":false,"updated":["x"],"archived":["y"]}\n',
            encoding="utf-8",
        )

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert (tally.projects, tally.updated, tally.archived) == (1, 5, 1)

    def test_an_unreadable_night_is_skipped_and_never_raises(self, tmp_path: Path) -> None:
        """This block observes the night; it must never be why the report fails."""
        (tmp_path / f"{RUN_DATE.isoformat()}_broken_reorg.log").write_text(
            "=== REORG REPORT ===\n{not json at all\n", encoding="utf-8"
        )
        _log(tmp_path, "brain-v42", updated=2, archived=0)

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).updated == 2

    def test_no_logs_at_all_is_an_empty_tally(self, tmp_path: Path) -> None:
        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)
        assert (tally.projects, tally.updated, tally.archived) == (0, 0, 0)


class TestTheLineRendersTheNightThatWentUnnoticed:
    def test_the_fixture_of_2026_09_03_renders(self, tmp_path: Path) -> None:
        """28 tags, 0 archives: the exact shape nobody saw for twelve nights."""
        _log(tmp_path, "brain-v42", updated=28, archived=0)

        block = "\n".join(
            post_run_alert.build_reorg_block(
                RUN_DATE, post_run_alert.reorg_tally(RUN_DATE, tmp_path)
            )
        )

        assert block, "a night of tag work and no archive must not be silent"
        assert "0 archivage" in block
        assert "28 tag" in block

    def test_a_night_with_archives_says_so(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=13, archived=3)

        block = "\n".join(
            post_run_alert.build_reorg_block(
                RUN_DATE, post_run_alert.reorg_tally(RUN_DATE, tmp_path)
            )
        )

        assert "3 archivage" in block
        assert "Aucun archivage" not in block

    def test_a_night_with_no_work_at_all_stays_mute(self, tmp_path: Path) -> None:
        """A line repeated nightly with two zeros stops being read (4480d3df)."""
        _log(tmp_path, "brain-v42", updated=0, archived=0)

        assert (
            post_run_alert.build_reorg_block(
                RUN_DATE, post_run_alert.reorg_tally(RUN_DATE, tmp_path)
            )
            == []
        )

    def test_the_line_does_not_claim_a_candidate_count(self) -> None:
        """Candidates and refusals are prose; inventing them would be worse than silence."""
        block = "\n".join(
            post_run_alert.build_reorg_block(
                RUN_DATE, post_run_alert.ReorgTally(projects=10, archived=0, updated=28)
            )
        )
        assert "candidat" in block.lower(), "the absence must be stated, not hidden"
        assert "refus" in block.lower()
