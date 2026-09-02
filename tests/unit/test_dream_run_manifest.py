"""The night manifest: what the night DECLARES, read back in the morning.

Ticket `0a9c067e`, reframed by its thread. The end-of-night comparator has always
existed (`post_run_alert.include_missing_expected_phases`) and it fired three
nights in a row; it is simply UNDER-SIZED — `LOOP_PHASES` carries only `promote`
and `reorg`, so the night of 2026-08-16 announced 20 missing phases when 60 were
missing.

Naively widening the expectation would manufacture false positives: a phase
skipped by the preflight or by a killswitch writes no row and owes none. This
module therefore carries the night's DECLARATION — its expectations, its skips
and their reason — through to the comparator, and translates it into a closed
partition.

The four classes, and why the fourth exists:

- A `skipped`   — preflight / killswitch: nobody tried to write;
- B `declared`  — dream.sh classified it failed/timeout, the absence is already said;
- C `silent`    — counted OK, no row: the hole the ticket denounces;
- D `writefail` — SKIPPED but a writer tried its row and FAILED.

D is not a subtlety: `scripts/dream.sh` pushes
`SKIPPED_PHASES+=("$PROJECT_KEY/promote")` OUTSIDE the `if (( record_rc == 0 ))`,
so "skipped" and "the row is written" are two independent facts. Subtracting all
skips would turn green a path where a `dream_runs` row is genuinely lost — a
regression on today's behaviour.
"""

from __future__ import annotations

import datetime as dt
import inspect
import random
from pathlib import Path

import pytest
from scripts.dream import run_manifest as rm

RUN_DATE = dt.date(2026, 8, 18)


def _line(*parts: str) -> str:
    """A line as dream.sh's `manifest_put` writes it: FOUR fields.

    `printf '%s\\t%s\\t%s\\t%s\\n'` always lays down the four separators, so the
    parser must accept empty trailing fields. A harness writing "clean"
    three-field lines would not test the real file.
    """
    padded = (*parts, "", "", "")[:4]
    return "\t".join(padded) + "\n"


def _manifest_text(
    *,
    expected: tuple[tuple[str, str], ...] = (),
    skipped: tuple[tuple[str, str, str], ...] = (),
    failed: tuple[tuple[str, str], ...] = (),
    timed_out: tuple[tuple[str, str], ...] = (),
    meta: dict[str, str] | None = None,
    finished: bool = True,
) -> str:
    head = {"run_date": RUN_DATE.isoformat(), **(meta or {})}
    body = "".join(_line("meta", key, value) for key, value in head.items())
    body += "".join(_line("expected", phase, project) for phase, project in expected)
    body += "".join(_line("skipped", phase, project, reason) for phase, project, reason in skipped)
    body += "".join(_line("failed", phase, project) for phase, project in failed)
    body += "".join(_line("timeout", phase, project) for phase, project in timed_out)
    if finished:
        body += _line("meta", "finished", "2026-08-18T07:09:32+02:00")
    return body


def _parse(**kwargs: object) -> rm.RunManifest:
    return rm.parse_run_manifest(_manifest_text(**kwargs))  # type: ignore[arg-type]


# --- The ticket's fact: 60 silent phases, not 20 ----------------------------


def test_a_night_that_wrote_almost_nothing_reports_every_silent_pair() -> None:
    """The nights of 2026-08-15 and 08-16: 63 phases announced, 2 rows written.

    Today's comparator expects only `promote` and `reorg` (plus the global ones),
    so it reported 20. Against the expectations DECLARED by the night, the hole is
    its true size.
    """
    projects = tuple(f"p{index}" for index in range(10))
    phases = ("scan", "clean", "connect", "synth", "promote", "reorg")
    expected = tuple((phase, project) for project in projects for phase in phases)
    expected += (("extract", "*"), ("roadmap", "*"), ("sweep", "*"))

    manifest = _parse(expected=expected)
    verdict = rm.classify_coverage({("extract", "*"), ("roadmap", "*")}, manifest)

    assert len(verdict.expected) == 63
    assert len(verdict.written) == 2
    assert len(verdict.silent) == 61
    assert verdict.skipped == frozenset()
    assert verdict.writefail == frozenset()


# --- Finding 1: class D, the one a blind subtraction would erase ------------


def test_a_skip_whose_row_write_failed_is_not_subtracted() -> None:
    manifest = _parse(
        expected=(("promote", "red-lab"),),
        skipped=(("promote", "red-lab", "empty-pool-unrecorded"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.writefail == frozenset({("promote", "red-lab")})
    assert verdict.silent == frozenset()
    assert verdict.skipped == frozenset()
    assert verdict.escalates is True


def test_a_skip_whose_row_was_recorded_and_is_present_is_simply_written() -> None:
    manifest = _parse(
        expected=(("promote", "red"),),
        skipped=(("promote", "red", "empty-pool-recorded"),),
    )
    verdict = rm.classify_coverage({("promote", "red")}, manifest)

    assert verdict.written == frozenset({("promote", "red")})
    assert verdict.skipped == frozenset()
    assert verdict.silent == frozenset()
    assert verdict.escalates is False


def test_a_skip_recorded_but_absent_from_the_table_is_silent() -> None:
    """The cruel case: the writer returned 0 and the row is not in the database.

    This is exactly the DSN regression of 2026-08-15 seen from promote. The
    manifest says "written", the table says no: that is a hole, not a skip.
    """
    manifest = _parse(
        expected=(("promote", "red"),),
        skipped=(("promote", "red", "empty-pool-recorded"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.silent == frozenset({("promote", "red")})
    assert verdict.skipped == frozenset()
    assert verdict.escalates is True


def test_an_unknown_skip_reason_keeps_the_pair_expected() -> None:
    """Fail-closed proved, not merely documented.

    An eighth skip site added tomorrow with fresh vocabulary makes the detector
    NOISY, never blind. That is the only acceptable direction of travel for a
    detector the ticket says shrank silently.
    """
    manifest = _parse(
        expected=(("scan", "red"),),
        skipped=(("scan", "red", "something-new"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.silent == frozenset({("scan", "red")})
    assert verdict.skipped == frozenset()


def test_the_no_row_reason_table_is_closed_to_two_entries() -> None:
    assert rm.NO_ROW_SKIP_REASONS == frozenset({"preflight", "killswitch"})


# --- Anti-false-positives: the thread's two legitimate nights ---------------


def test_a_preflight_night_reports_nothing_silent() -> None:
    projects = tuple(f"p{index}" for index in range(10))
    deep = ("synth", "promote", "reorg")
    expected = tuple((phase, project) for project in projects for phase in deep)
    skipped = tuple((phase, project, "preflight") for phase, project in expected)

    manifest = _parse(expected=expected, skipped=skipped)
    verdict = rm.classify_coverage(set(), manifest)

    assert len(verdict.skipped) == 30
    assert verdict.silent == frozenset()
    assert verdict.writefail == frozenset()
    assert verdict.escalates is False


def test_a_killswitch_skip_declared_by_the_night_beats_a_later_dropin_edit() -> None:
    """The drop-in is re-read at alert time, not at night time.

    Measured on 2026-08-18: `logs/dream/2026-08-18.log` says
    `SKIP sweep (killswitch BRAIN_DREAM_SWEEP_ENABLED=false)` while the live
    drop-in now carries `=true`. A replay reading the drop-in would invent an
    alarm; the night's declaration, for its part, no longer moves.
    """
    manifest = _parse(
        expected=(("sweep", "*"),),
        skipped=(("sweep", "*", "killswitch"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.skipped == frozenset({("sweep", "*")})
    assert verdict.silent == frozenset()


def test_a_phase_declared_failed_is_already_reported_and_does_not_escalate() -> None:
    manifest = _parse(
        expected=(("connect", "brain-v42"),),
        failed=(("connect", "brain-v42"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.declared == frozenset({("connect", "brain-v42")})
    assert verdict.silent == frozenset()
    assert verdict.escalates is False


def test_a_phase_declared_timed_out_is_declared_too() -> None:
    manifest = _parse(expected=(("clean", "red"),), timed_out=(("clean", "red"),))
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.declared == frozenset({("clean", "red")})
    assert verdict.silent == frozenset()


# --- The false green of the 19→20 night: declared failed, row written anyway --
#
# `declared` only looks at `manifest.failed` for MISSING pairs. A pair that does
# have ITS `dream_runs` row — but that the night declared `failed` — therefore
# fell into `written` and nowhere else: its declaration was thrown away silently.
# The 19→20 night paid for it, reorg declared `failed` and its row left `done`
# because `_mark_dream_run_partial` had crashed. The verdict announced 63/63 while
# reading an input file that said the opposite.
#
# `mismatch` OVERLAPS `written`, it is not a sixth class: the closed partition of
# the five classes stays the invariant, and `escalates` does not move. Turning the
# night red on this signal touches the engine; that is not here.


def test_a_pair_written_but_declared_failed_is_reported_as_a_mismatch() -> None:
    manifest = _parse(
        expected=(("reorg", "brain-v42"),),
        failed=(("reorg", "brain-v42"),),
    )
    verdict = rm.classify_coverage({("reorg", "brain-v42")}, manifest)

    assert verdict.mismatch == frozenset({("reorg", "brain-v42")})
    assert verdict.written == frozenset({("reorg", "brain-v42")})
    assert verdict.declared == frozenset(), "la paire n'est pas manquante"
    assert verdict.silent == frozenset()


def test_a_pair_written_but_declared_timed_out_is_a_mismatch_too() -> None:
    manifest = _parse(expected=(("clean", "red"),), timed_out=(("clean", "red"),))
    verdict = rm.classify_coverage({("clean", "red")}, manifest)

    assert verdict.mismatch == frozenset({("clean", "red")})


def test_a_declaration_without_a_row_is_declared_not_mismatch() -> None:
    """The two signals must never count the same pair."""
    manifest = _parse(
        expected=(("connect", "brain-v42"),),
        failed=(("connect", "brain-v42"),),
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.declared == frozenset({("connect", "brain-v42")})
    assert verdict.mismatch == frozenset()


def test_a_mismatch_reports_but_never_escalates() -> None:
    """REPORT ONLY. Escalating here would turn the night red — engine."""
    manifest = _parse(
        expected=(("reorg", "brain-v42"),),
        failed=(("reorg", "brain-v42"),),
        meta={"planned_phases": "1", "total_phases": "1"},
    )
    verdict = rm.classify_coverage({("reorg", "brain-v42")}, manifest)

    assert verdict.mismatch
    assert verdict.escalates is False


def test_the_mismatch_overlay_never_breaks_the_closed_partition() -> None:
    """`mismatch` overlaps `written`; the five classes stay disjoint."""
    rng = random.Random(20260820)
    phases = ("scan", "clean", "connect", "synth", "promote", "reorg")
    projects = ("red", "brain-v42", "red-lab")

    for _ in range(100):
        expected = tuple(
            (phase, project) for phase in phases for project in projects if rng.random() < 0.8
        )
        failed = tuple(pair for pair in expected if rng.random() < 0.3)
        timed_out = tuple(pair for pair in expected if rng.random() < 0.15)
        observed = {pair for pair in expected if rng.random() < 0.6}

        manifest = _parse(expected=expected, failed=failed, timed_out=timed_out)
        verdict = rm.classify_coverage(observed, manifest)

        assert verdict.mismatch <= verdict.written
        assert not (verdict.mismatch & verdict.declared)
        classes = (
            verdict.written,
            verdict.skipped,
            verdict.writefail,
            verdict.declared,
            verdict.silent,
        )
        assert sum(len(part) for part in classes) == len(verdict.expected)


def test_the_machine_line_carries_the_mismatch_counter() -> None:
    """Without a counter in the machine line, the signal does not reach journald."""
    manifest = _parse(
        expected=(("reorg", "brain-v42"), ("scan", "red")),
        failed=(("reorg", "brain-v42"),),
    )
    verdict = rm.classify_coverage({("reorg", "brain-v42"), ("scan", "red")}, manifest)

    line = rm.format_machine_line(verdict)
    assert "mismatch=1" in line
    assert line.startswith("COVERAGE mode=manifest ")


# --- Finding 2: the partition closes, always --------------------------------


def test_the_five_classes_partition_the_expected_set_on_a_hundred_manifests() -> None:
    """The invariant that makes the machine line additive.

    Revision 1 of the spec printed `expected=23 written=62`: two numbers side by
    side that nothing reconciles, i.e. the very flaw the ticket denounces. Here
    `written` is an INTERSECTION and the other four partition
    `expected − observed`, so the sum closes by construction.
    """
    rng = random.Random(20260818)
    phases = ("scan", "clean", "connect", "synth", "promote", "reorg")
    projects = ("red", "brain-v42", "red-lab")
    reasons = ("preflight", "killswitch", "empty-pool-recorded", "empty-pool-unrecorded", "weird")

    for _ in range(100):
        expected = tuple(
            (phase, project) for phase in phases for project in projects if rng.random() < 0.8
        )
        skipped = tuple(
            (phase, project, rng.choice(reasons))
            for phase, project in expected
            if rng.random() < 0.35
        )
        failed = tuple(pair for pair in expected if rng.random() < 0.2)
        timed_out = tuple(pair for pair in expected if rng.random() < 0.1)
        observed = {pair for pair in expected if rng.random() < 0.5}
        observed |= {("coverage", "*")} if rng.random() < 0.3 else set()

        manifest = _parse(expected=expected, skipped=skipped, failed=failed, timed_out=timed_out)
        verdict = rm.classify_coverage(observed, manifest)

        classes = (
            verdict.written,
            verdict.skipped,
            verdict.writefail,
            verdict.declared,
            verdict.silent,
        )
        total = sum(len(part) for part in classes)
        assert total == len(verdict.expected), (verdict, expected, observed)
        union: set[tuple[str, str]] = set()
        for part in classes:
            assert not (union & part), "les cinq classes doivent être DISJOINTES"
            union |= set(part)
        assert union == set(verdict.expected)
        assert verdict.extra == frozenset(observed) - frozenset(verdict.expected)


def test_a_pair_both_skipped_and_observed_is_counted_once_as_written() -> None:
    manifest = _parse(
        expected=(("promote", "red"), ("scan", "red")),
        skipped=(("promote", "red", "empty-pool-recorded"),),
    )
    verdict = rm.classify_coverage({("promote", "red"), ("scan", "red")}, manifest)

    assert verdict.written == frozenset({("promote", "red"), ("scan", "red")})
    assert len(verdict.written) + len(verdict.skipped) + len(verdict.writefail) + len(
        verdict.declared
    ) + len(verdict.silent) == len(verdict.expected)


def test_rows_outside_the_expected_set_are_extra_and_never_escalate() -> None:
    manifest = _parse(expected=(("scan", "red"),))
    verdict = rm.classify_coverage({("scan", "red"), ("coverage", "*")}, manifest)

    assert verdict.extra == frozenset({("coverage", "*")})
    assert verdict.escalates is False


# --- Finding 3: the self-check that carries ---------------------------------


def test_a_lost_expected_record_is_inconsistent() -> None:
    manifest = _parse(
        expected=tuple(("scan", f"p{index}") for index in range(57)),
        meta={"planned_phases": "63", "total_phases": "63"},
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.consistent is False
    assert verdict.escalates is True


def test_a_night_that_never_reached_every_phase_site_is_inconsistent() -> None:
    manifest = _parse(
        expected=tuple(("scan", f"p{index}") for index in range(63)),
        meta={"planned_phases": "63", "total_phases": "57"},
    )
    verdict = rm.classify_coverage(set(), manifest)

    assert verdict.consistent is False


def test_three_agreeing_counters_are_consistent() -> None:
    manifest = _parse(
        expected=tuple(("scan", f"p{index}") for index in range(63)),
        meta={"planned_phases": "63", "total_phases": "63"},
    )
    verdict = rm.classify_coverage({("scan", f"p{index}") for index in range(63)}, manifest)

    assert verdict.consistent is True
    assert verdict.escalates is False


def test_an_unparsable_counter_is_treated_as_inconsistent() -> None:
    manifest = _parse(expected=(("scan", "red"),), meta={"total_phases": "many"})
    verdict = rm.classify_coverage({("scan", "red")}, manifest)

    assert verdict.consistent is False


# --- Finding 6: an interrupted manifest is never allowed to be green --------


def test_a_manifest_without_its_closing_block_is_partial_and_escalates() -> None:
    manifest = _parse(
        expected=tuple(("scan", f"p{index}") for index in range(18)),
        meta={"planned_phases": "63"},
        finished=False,
    )
    verdict = rm.classify_coverage({("scan", f"p{index}") for index in range(18)}, manifest)

    assert manifest.complete is False
    assert verdict.complete is False
    assert verdict.mode == "manifest-partial"
    assert verdict.silent == frozenset()
    assert verdict.escalates is True, (
        "un manifeste tronqué ne peut pas produire « silent=0 donc tout va bien »"
    )


def test_a_complete_manifest_is_mode_manifest() -> None:
    manifest = _parse(expected=(("scan", "red"),))
    verdict = rm.classify_coverage({("scan", "red")}, manifest)

    assert verdict.mode == "manifest"
    assert verdict.complete is True


# --- Parsing: forward-tolerant, never silent --------------------------------


def test_the_sentinel_crosses_the_manifest_untouched() -> None:
    from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

    manifest = _parse(expected=(("extract", GLOBAL_PHASE_PROJECT_KEY),))

    assert manifest.expected == frozenset({("extract", "*")})


def test_the_module_never_canonicalizes_a_project_key() -> None:
    """`canonicalize_project_key` REJECTS the sentinel (`^[a-z0-9]+([:-][a-z0-9]+)*$`).

    An assertion on the SOURCE, like `test_dream_runs_project_key_writers.py`: the
    call would raise on the three global phases of EVERY night.

    The property aimed at is "neither import nor call", not "the name appears
    nowhere": forbidding the literal would also forbid EXPLAINING the trap in
    prose, which is exactly what makes a trap come back. The module in fact
    imports nothing at all from `brain_v42`, which is stronger.
    """
    source = inspect.getsource(rm)
    imports = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]

    assert not any("canonicalize_project_key" in line for line in imports)
    assert "canonicalize_project_key(" not in source
    assert not any("brain_v42" in line for line in imports), (
        "aucune dépendance au paquet : ce module est du transport, pas du domaine"
    )


def test_an_unknown_kind_is_ignored_and_a_malformed_line_is_warned() -> None:
    text = _manifest_text(expected=(("scan", "red"),))
    text += _line("brand-new-kind", "whatever", "red")
    text += "not-even-tab-separated\n"

    manifest = rm.parse_run_manifest(text)

    assert manifest.expected == frozenset({("scan", "red")})
    assert any("not-even-tab-separated" in warning for warning in manifest.warnings)
    assert not any("brand-new-kind" in warning for warning in manifest.warnings)


def test_blank_lines_are_not_warnings() -> None:
    manifest = rm.parse_run_manifest("\n" + _manifest_text(expected=(("scan", "red"),)) + "\n")

    assert manifest.warnings == ()


def test_meta_is_exposed_verbatim() -> None:
    manifest = _parse(expected=(("scan", "red"),), meta={"pool": "red,brain-v42"})

    assert manifest.meta["pool"] == "red,brain-v42"
    assert manifest.meta["run_date"] == "2026-08-18"


# --- Loading: four doors to the fallback, never a mute agreement ------------


def test_a_missing_file_falls_back(tmp_path: Path) -> None:
    assert rm.load_run_manifest(tmp_path / "absent.tsv", run_date=RUN_DATE) is None


def test_a_manifest_without_any_expected_record_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "m.tsv"
    path.write_text(_manifest_text(skipped=(("sweep", "*", "killswitch"),)), encoding="utf-8")

    assert rm.load_run_manifest(path, run_date=RUN_DATE) is None


def test_a_manifest_from_another_night_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "m.tsv"
    path.write_text(_manifest_text(expected=(("scan", "red"),)), encoding="utf-8")

    assert rm.load_run_manifest(path, run_date=dt.date(2026, 8, 17)) is None


def test_a_matching_manifest_loads(tmp_path: Path) -> None:
    path = tmp_path / "m.tsv"
    path.write_text(_manifest_text(expected=(("scan", "red"),)), encoding="utf-8")

    manifest = rm.load_run_manifest(path, run_date=RUN_DATE)

    assert manifest is not None
    assert manifest.expected == frozenset({("scan", "red")})


def test_an_unreadable_file_falls_back(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    assert rm.load_run_manifest(directory, run_date=RUN_DATE) is None


# --- Machine line: two shapes, never confusable -----------------------------


def test_the_manifest_machine_line_adds_up_to_expected() -> None:
    manifest = _parse(
        expected=(("scan", "red"), ("clean", "red"), ("sweep", "*")),
        skipped=(("sweep", "*", "killswitch"),),
        failed=(("clean", "red"),),
    )
    verdict = rm.classify_coverage({("scan", "red"), ("coverage", "*")}, manifest)
    line = rm.format_machine_line(verdict)

    assert line.startswith("COVERAGE mode=manifest ")
    fields = dict(token.split("=", 1) for token in line.split()[1:])
    assert fields["expected"] == "3"
    assert fields["written"] == "1"
    assert fields["skipped"] == "1"
    assert fields["declared"] == "1"
    assert fields["writefail"] == "0"
    assert fields["silent"] == "0"
    assert fields["extra"] == "1"
    summed = sum(
        int(fields[name]) for name in ("written", "skipped", "declared", "writefail", "silent")
    )
    assert summed == int(fields["expected"])


def test_the_partial_machine_line_names_planned_and_reached() -> None:
    manifest = _parse(expected=(("scan", "red"),), meta={"planned_phases": "63"}, finished=False)
    line = rm.format_machine_line(rm.classify_coverage(set(), manifest))

    assert line.startswith("COVERAGE mode=manifest-partial ")
    fields = dict(token.split("=", 1) for token in line.split()[1:])
    assert fields["planned"] == "63"
    assert fields["reached"] == "1"


def test_the_fallback_machine_line_uses_incomparable_field_names() -> None:
    """23 pairs expected from the drop-in against 62 written on 2026-08-18.

    `observed` is not included in `expected` and `silent` is not computable: the
    fallback line must SAY so, not lay down two numbers side by side.
    """
    line = rm.format_fallback_line(expected=23, observed=62, missing=2)

    assert line.startswith("COVERAGE mode=fallback ")
    assert "silent=unknown" in line
    assert "observed=62" in line
    assert "missing=2" in line
    assert "written=" not in line


def test_the_silent_line_lists_both_faulty_classes_and_is_bounded() -> None:
    expected = tuple(("scan", f"p{index:02d}") for index in range(15))
    manifest = _parse(
        expected=(*expected, ("promote", "red-lab")),
        skipped=(("promote", "red-lab", "empty-pool-unrecorded"),),
    )
    verdict = rm.classify_coverage(set(), manifest)
    line = rm.format_silent_line(verdict)

    assert line is not None
    assert line.startswith("COVERAGE_SILENT ")
    assert "and 5 more" in line
    assert "red-lab/promote" in line
    assert line.count("/") <= 12


def test_a_clean_night_has_no_silent_line() -> None:
    manifest = _parse(expected=(("scan", "red"),))
    verdict = rm.classify_coverage({("scan", "red")}, manifest)

    assert rm.format_silent_line(verdict) is None


@pytest.mark.parametrize(
    ("kwargs", "observed", "escalates"),
    [
        ({"expected": (("scan", "red"),)}, {("scan", "red")}, False),
        ({"expected": (("scan", "red"),)}, set(), True),
        (
            {
                "expected": (("scan", "red"),),
                "skipped": (("scan", "red", "killswitch"),),
            },
            set(),
            False,
        ),
        (
            {
                "expected": (("promote", "red"),),
                "skipped": (("promote", "red", "empty-pool-unrecorded"),),
            },
            set(),
            True,
        ),
        ({"expected": (("scan", "red"),), "failed": (("scan", "red"),)}, set(), False),
    ],
)
def test_escalation_is_silent_plus_writefail_plus_structure(
    kwargs: dict[str, object], observed: set[tuple[str, str]], escalates: bool
) -> None:
    verdict = rm.classify_coverage(observed, _parse(**kwargs))  # type: ignore[arg-type]

    assert verdict.escalates is escalates
