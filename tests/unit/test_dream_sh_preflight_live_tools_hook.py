"""The live-tool preflight hook in scripts/dream.sh — shell block EXECUTED.

Incident (2026-09-07 06:00): a phase prompt named a tool the running
`brain-mcp-http` process had never loaded (`scripts/` is read from the
repository at run time, `src/` only goes live at the next restart). Nothing
in `dream.sh` asked the LIVE server before starting the pool loop.

This test does not inspect the script's TEXT. It extracts the real hook
block from the real file and EXECUTES it under bash with a stub `uv`, the
same technique as `test_dream_sh_exit_code.py` — proof that bash takes the
right decision, not that a string is present.
"""

from __future__ import annotations

import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

_HOOK_START_ANCHOR = "# --- Live-tool preflight:"
_HOOK_END_ANCHOR = "# Serves ONE project: its six agent phases, in order, with their killswitches"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _hook_block() -> str:
    content = _source()
    start = content.index(_HOOK_START_ANCHOR)
    end = content.index(_HOOK_END_ANCHOR, start)
    return content[start:end]


def test_the_hook_calls_the_module_before_the_pool_loop() -> None:
    """Guard on the extraction: the anchors must still bracket the real call."""
    block = _hook_block()
    assert "scripts.dream.preflight_live_tools" in block
    assert "exit 1" in block


def _run(script: str, tmp_path: Path) -> tuple[int, str]:
    import subprocess

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-09-07",
            'log() { printf "%s\\n" "$*"; }',
            script,
            'echo "REACHED_AFTER_HOOK"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    return proc.returncode, proc.stdout


def test_night_continues_when_the_live_catalogue_is_complete(tmp_path: Path) -> None:
    harness = "\n".join(
        [
            'uv() { echo "OK preflight_live_tools — 6 phase prompt(s) checked"; return 0; }',
            _hook_block(),
        ]
    )
    rc, out = _run(harness, tmp_path)
    assert rc == 0
    assert "REACHED_AFTER_HOOK" in out


def test_the_whole_night_refuses_to_start_when_a_phase_names_a_dead_tool(
    tmp_path: Path,
) -> None:
    """The exact incident: the live server lacks a tool a prompt names."""
    harness = "\n".join(
        [
            'uv() { echo "FAIL preflight_live_tools — 1 phase/tool pair(s) the live '
            'server does not register:" >&2; echo "  promote: brain_promote_adr" >&2; '
            "return 2; }",
            _hook_block(),
        ]
    )
    rc, out = _run(harness, tmp_path)
    assert rc == 1, "a stale live catalogue must abort the whole night, not one phase"
    assert "REACHED_AFTER_HOOK" not in out
    assert "night aborted before the pool loop" in out


def test_the_missing_pair_reaches_the_dream_log_file(tmp_path: Path) -> None:
    harness = "\n".join(
        [
            'uv() { echo "FAIL preflight_live_tools — 1 phase/tool pair(s):" >&2; '
            'echo "  promote: brain_promote_adr" >&2; return 2; }',
            _hook_block(),
        ]
    )
    _run(harness, tmp_path)
    log_file = tmp_path / "logs" / "2026-09-07.log"
    assert log_file.exists()
    logged = log_file.read_text(encoding="utf-8")
    assert "brain_promote_adr" in logged
