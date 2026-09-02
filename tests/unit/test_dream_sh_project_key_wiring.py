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

from pathlib import Path

import pytest

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"
_FLAG = '--project-key "$PROJECT_KEY"'


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    assert start in content, f"marqueur de début absent : {start!r}"
    tail = content.split(start, maxsplit=1)[1]
    assert end in tail, f"marqueur de fin absent après {start!r} : {end!r}"
    return tail.split(end, maxsplit=1)[0]


@pytest.fixture
def parser_block() -> str:
    return _between(_content(), "local parser_args=(", 'return "$phase_rc"')


def test_the_flag_enters_the_shared_argument_array(parser_block: str) -> None:
    """In `parser_args`, hence before the rails diverge.

    It is the only position that serves them ALL in one gesture. The array is
    built once and consumed once per rail — there were two, there are three since
    agy arrived, and this position has not had to move.
    """
    assignment = parser_block.split("local scan_log=", maxsplit=1)[0]

    assert _FLAG in assignment


def test_every_rail_consumes_the_same_array(parser_block: str) -> None:
    """Proof that the position above is enough: no rail builds its own arguments.

    The count is derived from the parsers present, not frozen at a number. A
    fourth rail cobbling together its own arguments would slip under a literal
    updated by hand — which is exactly what this test must catch.
    """
    parsers = ("agy_dream_parser", "codex_dream_parser", "brain_v42.metrics.dream_parser")
    for parser in parsers:
        assert parser in parser_block, parser

    assert parser_block.count('"${parser_args[@]}"') == len(parsers)


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
        ("--- EXTRACT", "--- ROADMAP"),
        ("--- ROADMAP", "--- SWEEP"),
    ],
    ids=["sweep", "extract", "roadmap"],
)
def test_the_global_phases_receive_no_project_flag(start: str, end: str) -> None:
    """A global phase has no project to name: its `'*'` sentinel lives in its
    Python code, not on its command line."""
    block = _between(_content(), start, end)

    assert "--project-key" not in block
