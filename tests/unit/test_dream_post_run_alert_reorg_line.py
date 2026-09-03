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
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts" / "dream"))

import post_run_alert  # noqa: E402

RUN_DATE = dt.date(2026, 9, 3)
REPO_LOGS = Path(__file__).resolve().parents[2] / "logs" / "dream"

#: Anonymised copy of the 2026-09-03 brain-v42 REORG report, committed so the
#: replay runs in CI instead of skipping where `logs/dream/` does not exist.
REPLAY_FIXTURE = Path(__file__).parent / "data" / "2026-09-03_brain-v42_reorg.anonymised.log"
FIXTURE_DATE = dt.date(2026, 9, 3)

#: Three lines lifted from `logs/dream/2026-09-03_brain-v42_reorg.events.jsonl`,
#: the real codex stream of that night, with the bulky result payloads dropped
#: and the two entity ids passed through the SAME deterministic anonymisation
#: as the report fixture — `8424c8ad-…` is that night's first mutated id and
#: becomes `00000001-…`, `6af1aa1b-…` is the second and becomes `00000002-…`.
#: That is what keeps the declared-versus-observed cross-check possible: a
#: slice carrying real ids could no longer be correlated with a scrubbed
#: report, and the check was dropped in silence when the fixture landed.
#: A fixture written from the parser would only prove the parser agrees with
#: itself (learning 187f107c); these lines keep the real SHAPE.
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
        ("item.started", "item_156", "learning", "00000001-0000-4000-8000-000000000001"),
        ("item.completed", "item_156", "learning", "00000001-0000-4000-8000-000000000001"),
        ("item.started", "item_157", "decision", "00000002-0000-4000-8000-000000000002"),
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


class TestANightWhereEveryTrailerIsDamaged:
    """The confusion this lot claims to close, reappearing inside the lot itself.

    A report whose markers are present but whose payload is unreadable is NOT a
    report written in prose. Yet a night where every project is in that state
    left `with_trailer == 0`, fell into the `no_trailer` branch, and returned
    before reaching the line that counts unreadable reports — so the morning
    line said "la phase n'a parlé qu'en prose" and threw the count away.

    Found by adversarial review on 2026-09-04, and it is the same failure the
    ticket exists to remove: two different facts wearing one sentence.
    """

    def _broken(self, directory: Path, project: str) -> None:
        (directory / f"{RUN_DATE.isoformat()}_{project}_reorg.log").write_text(
            "# Rapport REORG\n\n=== REORG REPORT ===\n{not json at all}\n=== END ===\n",
            encoding="utf-8",
        )

    def test_every_trailer_damaged_is_not_reported_as_prose_only(self, tmp_path: Path) -> None:
        self._broken(tmp_path, "brain-v42")
        self._broken(tmp_path, "red-lab")

        block = _block(tmp_path)

        # The verdict must not be CLAIMED. The line is allowed to quote the wrong
        # reading in order to deny it — that is useful to the operator — so the
        # assertion targets the affirmative sentence, not the bare word.
        assert "qu'en prose, rien n'est comptable" not in block, (
            "a damaged trailer is not a phase that spoke prose — that reading is "
            "exactly what this ticket removes"
        )
        assert "illisible" in block.lower()
        assert "2 rapport" in block

    def test_every_trailer_damaged_has_its_own_outcome(self, tmp_path: Path) -> None:
        self._broken(tmp_path, "brain-v42")

        assert post_run_alert.reorg_tally(RUN_DATE, tmp_path).outcome == "unreadable"

    def test_prose_only_and_damaged_do_not_render_the_same_line(self, tmp_path: Path) -> None:
        (tmp_path / f"{RUN_DATE.isoformat()}_prose_reorg.log").write_text(
            "# Rapport REORG\n\nProse seule, aucun bloc machine.\n", encoding="utf-8"
        )
        prose = _block(tmp_path)
        (tmp_path / f"{RUN_DATE.isoformat()}_prose_reorg.log").unlink()
        self._broken(tmp_path, "brain-v42")
        damaged = _block(tmp_path)

        assert prose != damaged

    def test_a_damaged_report_beside_a_readable_one_is_still_counted(self, tmp_path: Path) -> None:
        """The mixed night: the readable projects must not hide the broken one."""
        self._broken(tmp_path, "broken")
        _log(tmp_path, "brain-v42", updated=4, archived=0)

        block = _block(tmp_path)

        assert "4 tag" in block
        assert "illisible" in block


class TestTagsOnlyMeansTagsActuallyMoved:
    """`tags_only` printed "la phase a travaillé les tags" with zero tag updates.

    The outcome fired whenever candidates had been examined and nothing archived,
    regardless of `updated`. A night that examined thirty-one candidates, refused
    them all and touched no tag was announced as a night of tag work — a sentence
    that states the opposite of what happened.
    """

    def test_a_night_that_moved_no_tag_is_not_called_tag_work(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "brain-v42",
            updated=0,
            archived=0,
            declared={
                "candidates_examined": 31,
                "archived": 0,
                "refused": {"already_archived": 31},
                "deferred": 0,
            },
        )

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)
        block = _block(tmp_path)

        assert tally.outcome == "refused_only"
        assert "travaillé les tags" not in block
        assert "Aucun archivage et aucun tag déplacé" in block

    def test_a_night_that_did_move_tags_still_says_so(self, tmp_path: Path) -> None:
        _log(
            tmp_path,
            "brain-v42",
            updated=28,
            archived=0,
            declared={
                "candidates_examined": 0,
                "archived": 0,
                "refused": {},
                "deferred": 0,
            },
        )

        tally = post_run_alert.reorg_tally(RUN_DATE, tmp_path)

        assert tally.outcome == "tags_only"
        assert "travaillé les tags" in _block(tmp_path)


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
    """A real transcript through parser, validator and line (learning 187f107c).

    THE FIXTURE IS COMMITTED, and that is a correction. These tests used to read
    `logs/dream/`, which is not tracked — so they skipped in CI, the one place
    they have to run, and a green pipeline said nothing about the replay. The
    anonymised copy under `tests/unit/data/` makes them run everywhere; a
    companion test compares it with the real log when that log exists, so the
    copy cannot drift away from the night it describes.
    """

    def _tally_dir(self, tmp_path: Path) -> Path:
        """The fixture under a name `reorg_tally`'s glob will find."""
        target = tmp_path / f"{FIXTURE_DATE.isoformat()}_brain-v42_reorg.log"
        target.write_text(REPLAY_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        return tmp_path

    def test_the_real_september_night_replays_end_to_end(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from dream.reorg_events import scan_events
        from dream.reorg_validate import parse_report, symmetry_warnings

        report = parse_report(REPLAY_FIXTURE.read_text(encoding="utf-8"))

        # The night as it really was: 20 tag mutations, no archive, no tally.
        assert report["found_marker"] is True
        assert len(report["updated_ids"]) == 20
        assert report["archived_ids"] == []
        assert report["declared"] is None

        scan = scan_events(REAL_EVENT_SLICE)
        assert scan.recognised is True
        assert scan.updated_ids, "the slice must observe something to correlate"
        # The cross-check the class advertises: every id the stream OBSERVED
        # appears among the ids the report DECLARED. Restored after the switch
        # to the anonymised fixture dropped it without a word.
        assert scan.updated_ids <= set(report["updated_ids"])

        # A legacy trailer must add no tally warning — twelve nights of these
        # exist and none of them is a fault.
        assert [w for w in symmetry_warnings(report, scan) if "add up" in w] == []

    def test_the_real_night_renders_as_legacy_in_the_morning_line(self, tmp_path: Path) -> None:
        directory = self._tally_dir(tmp_path)

        tally = post_run_alert.reorg_tally(FIXTURE_DATE, directory)

        assert tally.projects == 1
        assert tally.updated == 20
        assert tally.outcome == "legacy"
        assert (
            "sans décompte"
            in "\n".join(post_run_alert.build_reorg_block(FIXTURE_DATE, tally)).lower()
        )

    def test_the_fixture_carries_no_real_entity_id(self) -> None:
        """Anonymisation is part of the contract, not a one-off gesture.

        The synthetic ids are deterministic, so duplicates and cross-references
        survive; what must never come back is a real corpus UUID.
        """
        text = REPLAY_FIXTURE.read_text(encoding="utf-8")
        ids = set(
            re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)
        )

        assert ids, "the fixture lost its ids — the replay would assert on nothing"
        assert all(uid.startswith("0000") for uid in ids), sorted(ids)[:3]

    def test_the_fixture_header_counts_what_the_fixture_contains(self) -> None:
        """A number written by hand in a comment drifts from the file under it.

        The header said 20 — the length of the trailer's `updated` list — while
        28 distinct ids had been substituted, the other 8 living in the deferred-
        normalisation prose. Nobody would have noticed: a comment is not
        executed. This makes it executed.
        """
        text = REPLAY_FIXTURE.read_text(encoding="utf-8")
        header, body = text.split("-->\n", 1)
        claimed = int(re.search(r"The (\d+) entity UUIDs were replaced", header).group(1))
        present = len(
            set(
                re.findall(
                    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", body
                )
            )
        )

        assert claimed == present, (
            f"the fixture header claims {claimed} substituted ids, the file carries "
            f"{present} — recount rather than retyping"
        )

    def test_the_fixture_still_matches_the_night_it_copies(self) -> None:
        """Drift alarm on a hand-made copy (learning d43e760d).

        Compares the SHAPE that matters — the trailer — rather than the bytes,
        because the copy is deliberately anonymised. Skips off the server.
        """
        real = REPO_LOGS / "2026-09-03_brain-v42_reorg.log"
        if not real.exists():
            pytest.skip("logs/dream is not tracked; absent from CI and from a worktree")

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from dream.reorg_report import parse_trailer

        original = parse_trailer(real.read_text(encoding="utf-8", errors="replace"))
        copy = parse_trailer(REPLAY_FIXTURE.read_text(encoding="utf-8"))

        assert len(copy.updated_ids) == len(original.updated_ids)
        assert len(copy.archived_ids) == len(original.archived_ids)
        assert copy.dry_run == original.dry_run
        assert (copy.declared is None) == (original.declared is None)
