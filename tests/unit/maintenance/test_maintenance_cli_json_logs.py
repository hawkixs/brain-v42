"""The maintenance CLIs log JSON on stderr when BRAIN_LOG_FORMAT=json.

Measured on red-base 2026-10-13 right after the 0.6.12 deploy (ticket 8d053014):
`docker logs brain-v42-session-sweep` showed the engine lines of both CLIs as
coloured console text although the service sets BRAIN_LOG_FORMAT=json, because
the CLIs never configured structlog. Their report lines stay plain text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Nothing listens on port 1: the run fails after the engine is built, which is
# enough to make the engine log its lines without touching any database.
_UNREACHABLE_PG = "postgresql+asyncpg://brain:not-a-secret@127.0.0.1:1/brain"


@pytest.mark.parametrize(
    "module",
    ["brain_v42.maintenance.session_sweep", "brain_v42.maintenance.trace_retention"],
)
def test_cli_log_lines_are_json_on_stderr(module: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,  # no .env file to pick up
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "POSTGRES_URL": _UNREACHABLE_PG,
            "BRAIN_LOG_FORMAT": "json",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "\x1b[" not in result.stdout + result.stderr
    assert "SQLAlchemy" not in result.stdout, result.stdout
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert "SQLAlchemy async engine created" in [event["event"] for event in events], result.stderr
    for event in events:
        assert event["timestamp"].endswith("Z")
        assert "level" in event
