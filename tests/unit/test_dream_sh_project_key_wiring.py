"""The wiring of `--project-key` in dream.sh, pinned by grep — and its limits.

Two properties to prove, and a third to PROTECT:

  - the flag goes into the SHARED `parser_args` array, hence into both rails
    (codex by default, claude as fallback). Wiring it into a single branch would
    let the other exit with `argparse` code 2, swallowed as `WARN … non-fatal`;
  - the `record-empty-pool` subcommand receives it too: `promote` is a PER-PROJECT
    phase, and since 041's maturity filter it is that path which writes its row
    most nights;
  - the GLOBAL phases receive NONE. `test_dream_sh_sweep.py` and
    `test_dream_sh_extract.py` already forbid it; the tests here say it again from
    the other side, so that a future batch does not "complete by symmetry" three
    blocks that have no project to name.
"""

import inspect
from pathlib import Path

import pytest

from brain_v42.agents import phase

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"
_FLAG = '--project-key "$PROJECT_KEY"'


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    assert start in content, f"marqueur de début absent : {start!r}"
    tail = content.split(start, maxsplit=1)[1]
    assert end in tail, f"marqueur de fin absent après {start!r} : {end!r}"
    return tail.split(end, maxsplit=1)[0]


# TRANSPOSED (lot 2, Brain ticket afd56820): the shared `parser_args` bash
# array these two tests used to slice out of `run_phase` no longer exists --
# it moved to `brain_v42.agents.phase.parser_argv`, one function every
# provider goes through. The guarantees below are unchanged: the project key
# reaches every rail from ONE position, not from per-rail duplicated code.


def _sample_paths() -> phase.PhasePaths:
    return phase.PhasePaths.build(
        log_dir=Path("/tmp/logs"),
        timestamp="2026-09-12",
        project_key="brain-v42",
        phase="scan",
        dream_dir=Path("/tmp/dream"),
    )


def test_the_flag_enters_the_shared_argument_array() -> None:
    """`--project-key` sits in `parser_argv`'s common prefix, built once and
    shared by every provider before the provider-specific tail is appended --
    the exact same relative position for all three, proving no rail branches
    off to build its own."""
    paths = _sample_paths()
    positions = set()
    for provider in ("agy", "codex", "claude"):
        argv = phase.parser_argv(
            provider,
            phase="scan",
            model="m",
            timestamp="2026-09-12",
            status="done",
            duration=1,
            project_key="brain-v42",
            effective_dry_run="false",
            scan_log=None,
            paths=paths,
        )
        assert "--project-key" in argv
        index = argv.index("--project-key")
        assert argv[index + 1] == "brain-v42"
        positions.add(index)

    assert len(positions) == 1, f"--project-key moved between rails: {positions}"


def test_every_rail_consumes_the_same_array() -> None:
    """Proof that the position above is enough: every rail names its own
    parser module (`phase.parser_module`), and `phase.run_phase` builds and
    spawns the parser argv from exactly one call site -- no rail forks its
    own argument-building code."""
    parsers = {
        "agy": "brain_v42.metrics.agy_dream_parser",
        "codex": "brain_v42.metrics.codex_dream_parser",
        "claude": "brain_v42.metrics.dream_parser",
    }
    for provider, module in parsers.items():
        assert phase.parser_module(provider) == module

    source = inspect.getsource(phase.run_phase)
    assert source.count("parser_argv(") == 1


def test_the_empty_pool_row_is_recorded_for_a_named_project() -> None:
    empty_branch = _between(
        _content(),
        'if [[ "$pool_size" -eq 0 ]]; then',
        "export PROMOTE_CANDIDATE_POOL_JSON",
    )

    assert "scripts.dream._promote_helpers record-empty-pool" in empty_branch
    assert _FLAG in empty_branch


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("--- SWEEP", "=== Dream finished"),
        ("--- EXTRACT", "--- SWEEP"),
    ],
    ids=["sweep", "extract"],
)
def test_the_global_phases_receive_no_project_flag(start: str, end: str) -> None:
    """A global phase has no project to name: its `'*'` sentinel lives in its
    Python code, not on its command line."""
    block = _between(_content(), start, end)

    assert "--project-key" not in block
