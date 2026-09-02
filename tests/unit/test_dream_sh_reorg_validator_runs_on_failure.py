"""The REORG validator must run ALSO when the phase has failed.

THIS FILE TOUCHES THE NIGHT'S ENGINE. `scripts/dream.sh` runs from the repository
every night, with no restart: a change there is active from the merge onwards.

The block was guarded by `[[ "$name" == "reorg" && "$phase_rc" == "0" ]]`. The
second condition removes the check from exactly the case where it serves: a REORG
phase that fails or times out may have written BEFORE dying, and it is those
writes — partial, re-read by nobody — that most need to be confronted with the
project's perimeter. A green phase, by contrast, at least emitted its report and
followed its prompt to the end.

The trap when closing this hole is double accounting: the `case "$phase_rc"` that
follows classifies the phase as FAILED or TIMED_OUT depending on the code. Setting
`phase_rc=1` on a phase already at 2 would move a budget overrun into the hard
failure bucket, and the night would report an incident that did not happen instead
of the one that did. The validator's verdict is therefore added to the log, never
to the classification, when the phase has already fallen.

These tests do not inspect the script's TEXT: they extract the real block and
EXECUTE it under bash with stubs for `log` and `uv`, in the form established by
test_dream_sh_exit_code.py. A test that greps for a string proves the text exists;
this one proves bash takes the right decision.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCK_START = "# --- REORG: post-phase validator"
_BLOCK_END = '    case "$phase_rc" in'


def _reorg_validator_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index(_BLOCK_START)
    end = content.index(_BLOCK_END, start)
    return content[start:end]


def _run_block(
    tmp_path: Path,
    *,
    phase_rc: int,
    validator_rc: int,
    name: str = "reorg",
) -> tuple[subprocess.CompletedProcess[str], str, int]:
    """Run the real block; return (process, captured `uv` argv, final phase_rc)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    uv_calls = tmp_path / "uv_calls.txt"
    uv_calls.write_text("", encoding="utf-8")
    # run_phase has already written this file by the time the block runs —
    # including on a failing phase, the redirection creating it before the agent.
    (log_dir / f"2026-08-20_brain-v42_{name}.log").write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-20",
            "PROJECT_KEY=brain-v42",
            f"name={shlex.quote(name)}",
            f"phase_rc={phase_rc}",
            "DRY_RUN=false",
            "BRAIN_DREAM_REORG_DRY_RUN=false",
            # Set by the snapshot block, which runs higher up in the same
            # iteration (test_dream_sh_reorg_tags_snapshot.py covers it).
            f"REORG_TAGS_BEFORE={shlex.quote(str(log_dir / 'tags_before.json'))}",
            f"UV_CALLS={shlex.quote(str(uv_calls))}",
            f"VALIDATOR_RC={validator_rc}",
            'log() { printf "%s\\n" "$*"; }',
            # Two distinct calls go through this stub: fetching the `dream_runs`
            # id (`uv run python -c …`) and the validator itself. Only the second
            # carries an interesting return code.
            "uv() {",
            '  printf "%s\\n" "$*" >> "$UV_CALLS"',
            '  case "$*" in',
            '    *reorg_validate*) return "$VALIDATOR_RC" ;;',
            '    *) printf "4242" ; return 0 ;;',
            "  esac",
            "}",
            "",
            _reorg_validator_block(),
            "",
            'printf "PHASE_RC=%s\\n" "$phase_rc"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    # Harness guard, not a behavioural assertion: under `set -u` a forgotten
    # variable would exit 1 and turn green a test expecting 1.
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    final_rc = int(proc.stdout.rsplit("PHASE_RC=", 1)[1].strip())
    return proc, uv_calls.read_text(encoding="utf-8"), final_rc


@pytest.mark.parametrize("phase_rc", [0, 1, 2])
def test_the_validator_runs_whatever_the_phase_returned(tmp_path: Path, phase_rc: int) -> None:
    """Failure (1) and overrun (2) are precisely the cases to check.

    A phase that died mid-course may have crossed the project boundary before
    dying. The validator is the last place that can still say so: `brain_list` is
    the only CRUD tool with no scope check of its own, its bound living in the
    middleware alone.
    """
    _, uv_calls, _ = _run_block(tmp_path, phase_rc=phase_rc, validator_rc=0)

    assert "scripts.dream.reorg_validate" in uv_calls, (
        f"phase_rc={phase_rc} : le validateur n'a pas été invoqué. Appels vus :\n{uv_calls}"
    )
    assert "--project-key brain-v42" in uv_calls, (
        "le validateur a tourné sans périmètre — la garde de projet serait morte"
    )
    assert "--tags-before-json" in uv_calls, (
        "le validateur a tourné sans instantané : il n'aurait aucun « avant » à "
        "comparer, et la panne masquée de la Partie 1 redeviendrait invisible"
    )
    assert "--run-date" not in uv_calls, (
        "le drapeau du contrôle creux `updated_at >= run_date` est encore passé"
    )
    assert "reorg.events.jsonl" in uv_calls, (
        "le validateur a tourné sans le flux d'événements : le contrôle de symétrie "
        "rapport ↔ appels observés n'aurait rien à confronter"
    )


def test_a_rejected_report_fails_a_phase_that_was_green(tmp_path: Path) -> None:
    """Counter-proof: on a green phase, the verdict still counts."""
    proc, _, final_rc = _run_block(tmp_path, phase_rc=0, validator_rc=1)

    assert final_rc == 1, "un rapport rejeté doit encore faire rougir une phase verte"
    assert "validator rejected REORG report; see validation detail" in proc.stdout


@pytest.mark.parametrize(("phase_rc", "label"), [(1, "échec dur"), (2, "dépassement")])
def test_a_failed_phase_keeps_its_own_classification(
    tmp_path: Path, phase_rc: int, label: str
) -> None:
    """The validator's verdict is added to the log, not to the classification.

    The costly case is `phase_rc=2`: the `case` that follows puts a 2 into
    TIMED_OUT_PHASES and a 1 into FAILED_PHASES. Overwriting the 2 with a 1 would
    make the night report a hard failure instead of the budget overrun that
    actually happened — and the operator would look for the wrong breakage.
    """
    proc, _, final_rc = _run_block(tmp_path, phase_rc=phase_rc, validator_rc=1)

    assert final_rc == phase_rc, (
        f"{label} : phase_rc est passé de {phase_rc} à {final_rc} — la phase serait "
        f"comptée deux fois, et dans le mauvais seau"
    )
    assert "validator rejected REORG report; see validation detail" in proc.stdout, (
        "le verdict doit rester LISIBLE même quand il ne change pas la classification : "
        "sans journal, un franchissement de frontière sur une phase déjà tombée serait muet"
    )


def test_the_block_ignores_every_other_phase(tmp_path: Path) -> None:
    """Witness: the block must not fire on scan, synth or promote."""
    _, uv_calls, final_rc = _run_block(tmp_path, phase_rc=1, validator_rc=1, name="scan")

    assert uv_calls.strip() == "", f"le bloc a tourné sur une phase scan : {uv_calls!r}"
    assert final_rc == 1


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)
