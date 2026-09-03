"""The morning report says what REORG did, and what it merely looked at.

Ticket `1597c36d` made the phase visible at all: REORG ran WET on ten projects
for twelve nights, updating 3 to 82 tags and archiving NOTHING, and the morning
report said not one word. Ticket `72dc9768` finishes the job. That first line
could count only mutations, because candidates and refusals existed solely in
the model's French prose, and it said so in its own text.

They are now in the trailer, and the line separates the two kinds of number it
prints:

  MEASURED — archives and tag updates, derived from the ids the report names and
  confronted with the event stream by `reorg_validate`. Evidence.
  DECLARED — candidates examined, refusals by reason, deferrals. The phase's own
  account of entities it did NOT touch. No call can confirm it (learning
  c34fb865), so it is labelled and never summed into the measured counters.

WHY THE MUTE IS GONE, AND IT IS A CONTRACT CHANGE. This file used to pin
`test_a_night_with_no_work_at_all_stays_mute`, on the ground that a line
repeated nightly with two zeros stops being read (4480d3df). That premise held
while "nothing happened" was a single undifferentiated fact. It no longer is:
a night with no report file, a night whose trailer predates the tally, and a
night that examined zero candidates are three different failures wearing the
same two zeros — and the middle one is a rail succeeding without producing,
which is the worst signal there is (learning 083d74e5). Each now gets its own
sentence, so the line varies with the night and 4480d3df's real concern — an
unchanging line — is still answered.

VOCABULARIES STAY APART (learning abfaf932). The trailer speaks English
snake_case (`already_archived`); this line speaks French prose to a human at
7am. Neither is generated from the other, so a rename on one side cannot
silently satisfy a check on the other.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts" / "dream"))

import post_run_alert  # noqa: E402

RUN_DATE = dt.date(2026, 9, 3)
REPO_LOGS = Path(__file__).resolve().parents[2] / "logs" / "dream"

#: Three lines lifted from `logs/dream/2026-09-03_brain-v42_reorg.events.jsonl`,
#: the real codex stream of that night, with the bulky result payloads dropped
#: and nothing else changed. A fixture written from the parser would only prove
#: the parser agrees with itself (learning 187f107c).
REAL_EVENT_SLICE = "\n".join(
    json.dumps(
        {
            "type": kind,
            "item": {
                "id": item_id,
                "type": "mcp_tool_call",
                "server": "brain-v42",
                "tool": "brain_update",
                "arguments": {"entity_type": entity, "entity_id": entity_id},
                "status": "completed",
            },
        }
    )
    for kind, item_id, entity, entity_id in (
        ("item.started", "item_156", "learning", "8424c8ad-21b2-4a5b-96ca-ef0a39b50a42"),
        ("item.completed", "item_156", "learning", "8424c8ad-21b2-4a5b-96ca-ef0a39b50a42"),
        ("item.started", "item_157", "decision", "6af1aa1b-4a62-4ddd-b6a7-8c98373ba7ab"),
    )
)


def _log(
    directory: Path,
    project: str,
    *,
    updated: int,
    archived: int,
    declared: dict | None = None,
    date: dt.date = RUN_DATE,
) -> Path:
    payload: dict = {
        "dry_run": False,
        "updated": [f"u{i}" for i in range(updated)],
        "archived": [f"a{i}" for i in range(archived)],
    }
    if declared is not None:
        payload["declared"] = declared
    body = (
        f"# Rapport REORG — {project} — {date.isoformat()}\n\n"
        "## Pollution archived\n\n"
        "Aucune entité archivée.\n\n"
        "=== REORG REPORT ===\n" + json.dumps(payload) + "\n=== END ===\n"
    )
    path = directory / f"{date.isoformat()}_{project}_reorg.log"
    path.write_text(body, encoding="utf-8")
    return path


def _block(directory: Path) -> str:
    return "\n".join(
        post_run_alert.build_reorg_block(RUN_DATE, post_run_alert.reorg_tally(RUN_DATE, directory))
    )


class TestTheTallyReadsTheNightsReports:
    def test_it_sums_across_the_pool(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=28, archived=0)
        _log(tmp_path, "red-lab", updated=1, archived=0)
        _log(tmp_path, "red-shrik:agent", updated=0, archived=0)

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert (tally.projects, tally.updated, tally.archived) == (3, 29, 0)

    def test_another_nights_logs_are_not_counted(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=5, archived=1)
        _log(tmp_path, "brain-v42", updated=9, archived=9, date=dt.date(2026, 9, 2))

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert (tally.projects, tally.updated, tally.archived) == (1, 5, 1)

    def test_an_unreadable_night_is_skipped_and_never_raises(self, tmp_path: Path) -> None:
        """This block observes the night; it must never be why the report fails."""
        (tmp_path / f"{RUN_DATE.isoformat()}_broken_reorg.log").write_text(
            "=== REORG REPORT ===\n{not json at all\n=== END ===\n", encoding="utf-8"
        )
        _log(tmp_path, "brain-v42", updated=2, archived=0)

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert tally.updated == 2
        assert tally.unreadable == 1

    def test_no_logs_at_all_is_an_empty_tally(self, tmp_path: Path) -> None:
        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)
        assert (tally.projects, tally.updated, tally.archived) == (0, 0, 0)

    def test_a_nested_declared_block_does_not_zero_the_tally(self, tmp_path: Path) -> None:
        """The trap this ticket had to defuse before it could add anything.

        The previous alert-side pattern was non-greedy and unanchored, so the
        first inner brace of `refused` ended the match. `json.loads` then raised
        inside an `except ValueError: continue`, and the night silently read as
        zero projects, zero tags, zero archives.
        """
        _log(
            tmp_path,
            "brain-v42",
            updated=28,
            archived=0,
            declared={
                "candidates_examined": 31,
                "archived": 0,
                "refused": {"already_archived": 28, "dream_managed": 3},
                "deferred": 0,
            },
        )

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert (tally.projects, tally.updated) == (1, 28)
        assert tally.candidates_examined == 31
        assert tally.refused == {"already_archived": 28, "dream_managed": 3}


class TestTheThreeShapesOfNothing:
    """Absent, legacy and zero are three facts, not one (learning 083d74e5)."""

    def test_no_report_file_at_all_is_named(self, tmp_path: Path) -> None:
        block = _block(tmp_path)

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).outcome == "no_report"
        assert block, "a night with no REORG report must not be silent"
        assert "aucun rapport" in block.lower()

    def test_a_trailer_without_a_tally_says_the_prompt_is_older(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=0, archived=0)

        block = _block(tmp_path)

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).outcome == "legacy"
        assert "sans décompte" in block.lower()

    def test_zero_candidates_examined_is_stated_not_muted(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={"candidates_examined": 0, "archived": 0, "refused": {}, "deferred": 0},
        )

        block = _block(tmp_path)

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).outcome == "idle"
        assert "0 candidat" in block

    def test_the_three_shapes_do_not_render_the_same_line(self, tmp_path: Path) -> None:
        """The point of the whole exercise, asserted directly."""
        absent = _block(tmp_path)
        _log(tmp_path, "brain-v42", updated=0, archived=0)
        legacy = _block(tmp_path)
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={"candidates_examined": 0, "archived": 0, "refused": {}, "deferred": 0},
        )
        idle = _block(tmp_path)

        assert len({absent, legacy, idle}) == 3

    def test_files_present_but_no_trailer_is_its_own_state(self, tmp_path: Path) -> None:
        (tmp_path / f"{RUN_DATE.isoformat()}_brain-v42_reorg.log").write_text(
            "# Rapport REORG\n\nProse seule, aucun bloc machine.\n", encoding="utf-8"
        )

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).outcome == "no_trailer"
        assert "sans bloc" in _block(tmp_path).lower()


class TestTheLineSeparatesEvidenceFromHearsay:
    def test_the_fixture_of_2026_09_03_still_renders(self, tmp_path: Path) -> None:
        """28 tags, 0 archives: the exact shape nobody saw for twelve nights."""
        _log(tmp_path, "brain-v42", updated=28, archived=0)

        block = _block(tmp_path)

        assert "0 archivage" in block
        assert "28 tag" in block

    def test_a_night_with_archives_says_so(self, tmp_path: Path) -> None:
        _log(tmp_path, "brain-v42", updated=13, archived=3)

        block = _block(tmp_path)

        assert "3 archivage" in block

    def test_declared_counts_are_labelled_as_declared(self, tmp_path: Path) -> None:
        """A reader must never mistake the phase's word for a measurement."""
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=3,
            declared={
                "candidates_examined": 31,
                "archived": 3,
                "refused": {"already_archived": 28},
                "deferred": 0,
            },
        )

        block = _block(tmp_path)

        declared_line = next(line for line in block.splitlines() if "31 candidat" in line)
        assert "éclaré" in declared_line, "the declared counts must carry the word"
        measured_line = next(line for line in block.splitlines() if "3 archivage" in line)
        assert "esuré" in measured_line

    def test_refusal_reasons_are_rendered_in_french_not_in_trailer_keys(
        self, tmp_path: Path
    ) -> None:
        """Independent vocabularies (abfaf932): a rename on one side must show.

        The line is written for a human at 7am; echoing the JSON keys would make
        the alarm a mirror of the detector.
        """
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={
                "candidates_examined": 30,
                "archived": 0,
                "refused": {"already_archived": 28, "access_above_threshold": 2},
                "deferred": 0,
            },
        )

        block = _block(tmp_path)

        assert "already_archived" not in block
        assert "access_above_threshold" not in block
        assert "déjà archivé" in block
        assert "trop lu" in block

    def test_an_unknown_reason_is_shown_verbatim_rather_than_dropped(self, tmp_path: Path) -> None:
        """Drift must reach the human, and the raw key is the only honest label."""
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={
                "candidates_examined": 2,
                "archived": 0,
                "refused": {"vibes": 2},
                "deferred": 0,
            },
        )

        assert "vibes" in _block(tmp_path)

    def test_a_tally_that_does_not_add_up_is_flagged_in_the_line(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={
                "candidates_examined": 31,
                "archived": 0,
                "refused": {"already_archived": 5},
                "deferred": 0,
            },
        )

        assert "incohérent" in _block(tmp_path).lower()

    def test_an_unreadable_report_is_counted_and_shown(self, tmp_path: Path) -> None:
        """Skipping must stay visible: a skipped project is not a quiet one."""
        (tmp_path / f"{RUN_DATE.isoformat()}_broken_reorg.log").write_text(
            "=== REORG REPORT ===\n{not json\n=== END ===\n", encoding="utf-8"
        )
        _log(tmp_path, "brain-v42", updated=4, archived=0)

        block = _block(tmp_path)

        assert "illisible" in block
        assert "1 rapport" in block


class TestReplayOfARealNight:
    """A real transcript through parser, validator and line (learning 187f107c)."""

    def test_the_real_september_night_replays_end_to_end(self) -> None:
        log = REPO_LOGS / "2026-09-03_brain-v42_reorg.log"
        if not log.exists():
            pytest.skip("logs/dream is not tracked; absent from a worktree checkout")

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from dream.reorg_events import scan_events
        from dream.reorg_validate import parse_report, symmetry_warnings

        report = parse_report(log.read_text(encoding="utf-8", errors="replace"))

        # The night as it really was: 20 tag mutations, no archive, no tally.
        assert report["found_marker"] is True
        assert len(report["updated_ids"]) == 20
        assert report["archived_ids"] == []
        assert report["declared"] is None

        # And the real stream slice observes the first of those ids, with no
        # ghost and no undeclared mutation among what it covers.
        scan = scan_events(REAL_EVENT_SLICE)
        assert scan.recognised is True
        assert scan.updated_ids <= set(report["updated_ids"])

        # A legacy trailer must add no tally warning — twelve nights of these
        # exist and none of them is a fault.
        assert [w for w in symmetry_warnings(report, scan) if "add up" in w] == []

    def test_the_real_night_renders_as_legacy_in_the_morning_line(self) -> None:
        if not (REPO_LOGS / "2026-09-03_brain-v42_reorg.log").exists():
            pytest.skip("logs/dream is not tracked; absent from a worktree checkout")

        tally = post_run_alert.reorg_tally(dt.date(2026, 9, 3), REPO_LOGS)

        assert tally.projects > 0
        assert tally.outcome == "legacy"
        assert (
            "sans décompte"
            in "\n".join(post_run_alert.build_reorg_block(dt.date(2026, 9, 3), tally)).lower()
        )
