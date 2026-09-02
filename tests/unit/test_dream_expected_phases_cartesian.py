"""The expectation becomes `{phase} × {pool project}` — otherwise it disarms itself.

Spec `2026-08-08-dream-project-pool-design.md` §6, "the non-negotiable constraint
that comes with it".

`expected_dream_phases()` turns "armed phase" into "alarm if absent from
`dream_runs`". It is the anti-silent-crash mechanism of 2026-05-02, when two
PROMOTE crashes went unnoticed for two days.

With several projects, it **disarms itself**: if a single project skips
`promote`, the phase stays "observed" globally thanks to the others, and the
alarm no longer rings. The mechanism does not break noisily — it becomes
silently useless, which is the worse of the two.

The switch is conditioned on KNOWING the pool. As long as the drop-in does not
carry `BRAIN_DREAM_PROJECT_POOL`, the pair is not computable and today's
behaviour is preserved identically: that is what makes this batch shippable
without a single night changing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.dream import post_run_alert

from brain_v42.dream_killswitches import parse_project_pool
from brain_v42.metrics.collector_dream import (
    expected_dream_phase_pairs,
    expected_dream_phases,
)

_DROP_IN_WITH_POOL = """\
[Service]
Environment=BRAIN_DREAM_PROMOTE_ENABLED=true
Environment=BRAIN_DREAM_REORG_ENABLED=true
Environment=BRAIN_DREAM_EXTRACT_ENABLED=true
Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42,red,red-lab:architect
"""

_DROP_IN_WITHOUT_POOL = """\
[Service]
Environment=BRAIN_DREAM_PROMOTE_ENABLED=true
Environment=BRAIN_DREAM_REORG_ENABLED=true
"""


# --- The pool parser --------------------------------------------------------


def test_the_pool_is_read_from_the_drop_in_and_split_on_commas() -> None:
    assert parse_project_pool(_DROP_IN_WITH_POOL) == [
        "brain-v42",
        "red",
        "red-lab:architect",
    ]


def test_an_absent_pool_key_yields_an_empty_list() -> None:
    """Not "brain-v42 by default".

    The parser cannot know `ExecStart=`'s positional argument. Returning a guessed
    value would manufacture expectations for a project the night may never have
    served — an invented alarm, exactly what `expected_dream_phases`'s docstring
    refuses for an unreadable drop-in.
    """
    assert parse_project_pool(_DROP_IN_WITHOUT_POOL) == []


def test_a_quoted_whitespace_value_does_not_silently_become_one_key() -> None:
    """`Environment="…=a b"` arrives whole, with its blank.

    Treating it as a single key would manufacture a `project_key` that
    canonicalize_project_key rejects. Here we return both keys, as `dream.sh`
    would hand back with exit 2: in both cases, the space-separated form does not
    shrink the pool silently.
    """
    content = '[Service]\nEnvironment="BRAIN_DREAM_PROJECT_POOL=alpha beta"\n'

    assert parse_project_pool(content) == ["alpha", "beta"]


def test_the_killswitch_flags_still_parse_next_to_a_list_valued_key() -> None:
    """The list key must not poison the shared `dict[str, bool]`.

    `parse_killswitches` coerces through `value.lower() == "true"`: a list key
    entering it would become `False` and would switch off a phase in the session
    briefing and in `/metrics`, without touching the night.
    """
    from brain_v42.dream_killswitches import parse_killswitches

    flags = parse_killswitches(_DROP_IN_WITH_POOL)

    assert flags == {"promote": True, "reorg": True, "extract": True}


# --- The cartesian product --------------------------------------------------


def test_loop_phases_are_multiplied_by_the_pool_and_globals_are_not(
    tmp_path: Path,
) -> None:
    """`promote`/`reorg` per project; `extract`/`roadmap`/`sweep` once.

    The three global phases have no project dimension — they write the sentinel
    `'*'` into `dream_runs.project_key`, and the expectation must speak the same
    language as what it compares against.
    """
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(_DROP_IN_WITH_POOL, encoding="utf-8")

    assert expected_dream_phase_pairs(drop_in) == {
        ("promote", "brain-v42"),
        ("promote", "red"),
        ("promote", "red-lab:architect"),
        ("reorg", "brain-v42"),
        ("reorg", "red"),
        ("reorg", "red-lab:architect"),
        ("extract", "*"),
    }


def test_without_a_pool_the_pairs_are_empty_and_the_flat_set_is_unchanged(
    tmp_path: Path,
) -> None:
    """The property that makes the batch shippable without a night changing."""
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(_DROP_IN_WITHOUT_POOL, encoding="utf-8")

    assert expected_dream_phase_pairs(drop_in) == set()
    assert expected_dream_phases(drop_in) == {"promote", "reorg"}


def test_an_unreadable_drop_in_expects_nothing(tmp_path: Path) -> None:
    """Same posture as `expected_dream_phases`: never manufacture an alarm."""
    assert expected_dream_phase_pairs(tmp_path / "absent.conf") == set()


# --- The disarming, which is the whole point of the batch -------------------


def test_one_project_missing_promote_still_alerts_when_the_others_ran_it() -> None:
    """THE flaw. Without the pairs, that night is green.

    `red` has no `promote` row. `brain-v42` has one. Compared on phase names
    alone, `promote` is "observed" and `red`'s absence disappears.
    """
    rows = [
        {"phase": "promote", "status": "done", "project_key": "brain-v42"},
        {"phase": "scan", "status": "done", "project_key": "red"},
    ]

    failed = post_run_alert.include_missing_expected_phases(
        rows,
        set(),
        persisted_failures=[],
        expected_pairs={("promote", "brain-v42"), ("promote", "red")},
    )

    assert len(failed) == 1
    assert failed[0]["phase"] == "promote"
    assert failed[0]["project_key"] == "red"
    assert failed[0]["status"] == "partial"


def test_a_fully_observed_cartesian_expectation_is_silent() -> None:
    rows = [
        {"phase": "promote", "status": "done", "project_key": "brain-v42"},
        {"phase": "promote", "status": "done", "project_key": "red"},
    ]

    failed = post_run_alert.include_missing_expected_phases(
        rows,
        set(),
        persisted_failures=[],
        expected_pairs={("promote", "brain-v42"), ("promote", "red")},
    )

    assert failed == []


def test_the_flat_path_survives_untouched_when_no_pairs_are_supplied() -> None:
    """Regression: with no pool, today's behaviour, identically."""
    rows = [{"phase": "scan", "status": "done"}]

    failed = post_run_alert.include_missing_expected_phases(
        rows, {"promote", "scan"}, persisted_failures=[]
    )

    assert len(failed) == 1
    assert failed[0]["phase"] == "promote"


# --- §11: the report is readable, at fifty lines as at five -----------------


def test_the_report_groups_its_lines_by_project() -> None:
    """Without grouping, one project's failure is drowned in a flat list."""
    failed = [
        {"phase": "synth", "status": "fail", "project_key": "red", "error_message": "boom"},
        {"phase": "scan", "status": "fail", "project_key": "brain-v42", "error_message": "bam"},
        {"phase": "reorg", "status": "fail", "project_key": "red", "error_message": "bim"},
        {"phase": "extract", "status": "fail", "project_key": "*", "error_message": "bum"},
    ]

    report = post_run_alert.build_alert_insight(__import__("datetime").date(2026, 8, 10), failed)

    assert "red:" in report
    assert "brain-v42:" in report
    # The sentinel reads as what it is: the phases with no project.
    assert "global:" in report


def test_the_per_project_cap_cannot_let_one_project_hide_another() -> None:
    """`MAX_REPORTED_FAILURES = 20` was sized for 9 phases a night.

    At ten projects the night counts 63 phases: a global cap would let the first
    project consume the twenty lines and "N additional records omitted" would mask
    WHOLE projects. The cap is therefore per project.
    """
    failed = [
        {
            "phase": f"phase-{index}",
            "status": "fail",
            "project_key": "noisy",
            "error_message": "boom",
        }
        for index in range(40)
    ] + [
        {
            "phase": "synth",
            "status": "fail",
            "project_key": "quiet",
            "error_message": "the one that matters",
        }
    ]

    report = post_run_alert.build_alert_insight(__import__("datetime").date(2026, 8, 10), failed)

    assert "the one that matters" in report, (
        "un projet bruyant a évincé un projet silencieux du rapport"
    )
    assert "omitted" in report


@pytest.mark.parametrize("phase", ["extract", "roadmap", "sweep"])
def test_the_three_global_phases_are_never_multiplied(tmp_path: Path, phase: str) -> None:
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(
        f"[Service]\nEnvironment=BRAIN_DREAM_{phase.upper()}_ENABLED=true\n"
        "Environment=BRAIN_DREAM_PROJECT_POOL=a,b,c\n",
        encoding="utf-8",
    )

    assert expected_dream_phase_pairs(drop_in) == {(phase, "*")}
