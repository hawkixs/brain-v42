"""dream.sh must MEASURE the tags' "before", otherwise the guard has nothing to compare.

THIS FILE TOUCHES THE NIGHT'S ENGINE. `scripts/dream.sh` runs from the repository
every night, with no restart: a change there is active from the merge onwards.

The REORG validator now requires a snapshot of the tags taken just before the
phase (`--tags-before-json`). It is the only "before" that is observed: the check
it replaces, `updated_at >= run_date`, was hollow because `DecayFlusher` refreshes
the timestamp every 300 s through a trigger with no `WHEN` clause — and because
REORG's own reads feed the flusher.

The snapshot is taken AFTER the killswitch (a phase that is cut does not pay for
the query) and BEFORE `run_phase_chain`, once for the two attempts the retry
budget allows: the night's "before" is the one preceding the FIRST write, not the
last.

These tests extract the real block and EXECUTE it under bash with stubs, in the
form of test_dream_sh_exit_code.py.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_BLOCK_START = "# --- REORG: pre-phase tags snapshot"
_BLOCK_END = "# `set -e` is active, so we must guard the call"


def _snapshot_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index(_BLOCK_START)
    end = content.index(_BLOCK_END, start)
    return content[start:end]


def _run_block(
    tmp_path: Path,
    *,
    name: str = "reorg",
    snapshot_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Return (process, captured `uv` argv, snapshot path as bash sees it)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    uv_calls = tmp_path / "uv_calls.txt"
    uv_calls.write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-20",
            "PROJECT_KEY=brain-v42",
            f"name={shlex.quote(name)}",
            "REORG_TAGS_BEFORE=",
            f"UV_CALLS={shlex.quote(str(uv_calls))}",
            f"SNAPSHOT_RC={snapshot_rc}",
            'log() { printf "%s\\n" "$*"; }',
            "uv() {",
            '  printf "%s\\n" "$*" >> "$UV_CALLS"',
            "  printf '{\"seeded\": []}'",
            '  return "$SNAPSHOT_RC"',
            "}",
            "",
            _snapshot_block(),
            "",
            'printf "TAGS_BEFORE=%s\\n" "$REORG_TAGS_BEFORE"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    tags_before = proc.stdout.rsplit("TAGS_BEFORE=", 1)[1].strip()
    return proc, uv_calls.read_text(encoding="utf-8"), tags_before


def test_the_snapshot_is_taken_for_the_project_of_the_run(tmp_path: Path) -> None:
    """A snapshot taken over the wrong corpus would be worse than no snapshot.

    It would compare every mutated entity to a foreign "before": tags that have
    not moved would pass for moved, and vice versa. The guard would become a
    generator of arbitrary verdicts that nothing would flag.
    """
    _, uv_calls, tags_before = _run_block(tmp_path)

    assert "scripts.dream.reorg_snapshot" in uv_calls, (
        f"aucun instantané pris avant la phase. Appels vus :\n{uv_calls}"
    )
    assert "--project-key brain-v42" in uv_calls
    assert tags_before.endswith("2026-08-20_brain-v42_reorg_tags_before.json"), (
        f"chemin d'instantané inattendu : {tags_before!r}"
    )
    assert Path(tags_before).read_text(encoding="utf-8") == '{"seeded": []}'


def test_a_failed_snapshot_is_announced_and_does_not_abort_the_night(
    tmp_path: Path,
) -> None:
    """Failing silently would make the validator's refusal incomprehensible.

    The validator will refuse the report for want of a readable snapshot — that is
    intended and fail-closed. But the operator must be able to trace the refusal
    back to its cause in one log line, instead of suspecting the agent's report.
    """
    proc, _, _ = _run_block(tmp_path, snapshot_rc=1)

    assert "pre-phase tags snapshot failed" in proc.stdout, (
        f"l'échec de l'instantané est muet. Journal :\n{proc.stdout}"
    )
    assert proc.returncode == 0, "l'échec de l'instantané ne doit pas tuer la nuit"


def test_the_block_ignores_every_other_phase(tmp_path: Path) -> None:
    """Witness: scan, synth and promote do not pay for the query."""
    _, uv_calls, tags_before = _run_block(tmp_path, name="synth")

    assert uv_calls.strip() == "", f"un instantané a été pris pour synth : {uv_calls!r}"
    assert tags_before == ""


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)
