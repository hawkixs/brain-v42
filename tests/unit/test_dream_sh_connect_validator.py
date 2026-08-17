from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _connect_validator_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index("# --- CONNECT: post-phase validator")
    end = content.index("# --- PROMOTE: post-phase validator")
    return content[start:end]


def test_connect_validator_runs_after_retry_and_propagates_failure() -> None:
    block = _connect_validator_block()
    assert '"$name" == "connect" && "$phase_rc" == "0"' in block
    assert "scripts.dream.connect_validate" in block
    assert '--report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log"' in block
    assert '--run-date "$TIMESTAMP"' in block
    assert "validator_rc=$?" in block
    assert "phase_rc=1" in block


def test_connect_validator_failure_log_is_truthful() -> None:
    block = _connect_validator_block()
    assert "dream_runs marked partial" not in block
    assert "validator rejected CONNECT report; see validation detail" in block


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)
