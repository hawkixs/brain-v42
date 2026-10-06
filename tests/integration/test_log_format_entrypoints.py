"""Probe real process streams without starting services or opening a database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["mcp", "metrics"])
def test_json_startup_marker_uses_the_service_stream(service: str) -> None:
    if service == "mcp":
        configure = (
            "from brain_v42.mcp.server import _configure_stdio_logging\n"
            "_configure_stdio_logging()\n"
        )
    else:
        configure = (
            "from unittest.mock import MagicMock\n"
            "from brain_v42.metrics.__main__ import _configure_logging\n"
            "_configure_logging(MagicMock())\n"
        )
    environment = dict(os.environ)
    environment.update(
        BRAIN_LOG_FORMAT="json",
        BRAIN_POSTGRES_URL="postgresql+asyncpg://test@localhost/log_test",
        PYTHONPATH=str(_ROOT / "src"),
    )
    result = subprocess.run(
        [sys.executable, "-c", configure],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    output = result.stderr if service == "mcp" else result.stdout
    assert (result.stdout if service == "mcp" else result.stderr) == ""
    lines = output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "logging.configured"
    assert payload["renderer"] == "json"
    assert payload["service"] == f"brain-v42-{service}"
    assert payload["level"] == "info"
    assert type(payload["pid"]) is int
    assert payload["pid"] > 0
