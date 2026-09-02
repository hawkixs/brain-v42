"""The REORG and PROMOTE validators' logs must say what happened.

Night of 19→20, measured: `dream.sh` printed "FAIL reorg — validator flagged
integrity issues (dream_runs marked partial)" and the `dream_runs` row stayed
`done`. The parenthesis was FALSE, and it was so on the sole strength of
`validator_rc != 0` — a non-zero return code says nothing about what the validator
managed to write before exiting. That night it had written nothing: its marking
crashed on a closed asyncio loop.

The marking fix (a single loop per validator) ships separately. It makes the
sentence true TODAY — but leaving it as is would keep an assertion nothing checks,
ready to lie again at the next defect of the write path. The log must report what
`dream.sh` OBSERVES (the validator rejected the report), not what it ASSUMES (a
row was marked elsewhere).

`connect` already tells the truth — "validator rejected CONNECT report; see
validation detail" — and `test_dream_sh_connect_validator.py` explicitly forbids
the "dream_runs marked partial" wording in its own file. This file is its mirror
for the other two validators, written in the same form.

THIS FILE TOUCHES THE NIGHT'S ENGINE. `scripts/dream.sh` runs from the repository
every night, with no restart: a change there is active from the merge onwards.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCKS = {
    "promote": ("# --- PROMOTE: post-phase validator", "# --- REORG: post-phase validator"),
    "reorg": ("# --- REORG: post-phase validator", '    case "$phase_rc" in'),
}


def _block(phase: str) -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start_marker, end_marker = _BLOCKS[phase]
    start = content.index(start_marker)
    end = content.index(end_marker, start)
    return content[start:end]


@pytest.mark.parametrize("phase", sorted(_BLOCKS))
def test_the_validator_failure_log_claims_nothing_it_did_not_observe(phase: str) -> None:
    block = _block(phase)

    assert "dream_runs marked partial" not in block, (
        f"le journal de {phase} affirme un marquage qu'il n'a pas observé : il ne "
        f"dispose que de `validator_rc`, qui ne dit rien de ce que le validateur a "
        f"réussi à écrire avant de sortir. C'est le mensonge mesuré le 19→20."
    )
    assert f"validator rejected {phase.upper()} report; see validation detail" in block, (
        "la formule doit être celle de connect, au mot près — trois journaux qui "
        "disent la même chose de trois façons se relisent trois fois"
    )


@pytest.mark.parametrize("phase", sorted(_BLOCKS))
def test_the_validator_still_runs_and_propagates_its_failure(phase: str) -> None:
    """The batch changes ONLY text: the wiring must stay intact."""
    block = _block(phase)

    assert f"scripts.dream.{phase}_validate" in block
    assert "validator_rc=$?" in block
    assert "phase_rc=1" in block
    assert "--dream-run-id" in block, (
        "sans l'id, le validateur n'a aucune ligne à marquer et le journal "
        "n'aurait effectivement rien à rapporter"
    )


def test_connect_remains_the_reference_wording() -> None:
    """The witness: if connect changed its wording, this mirror would be false."""
    content = DREAM_SH.read_text(encoding="utf-8")

    assert "validator rejected CONNECT report; see validation detail" in content


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)
