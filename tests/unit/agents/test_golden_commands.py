"""Golden regression: the NEW ``brain_v42.agents`` package must build the exact
same argv and child environment the OLD ``scripts/dream/*_runner.py`` code
built on ``main`` at ``81a6ffb468b0e2bcc58eba72d67858144763715d``, for every
provider and every Dream phase.

The fixtures in ``tests/fixtures/agents_golden/*.json`` were captured from that
pre-extraction code by ``tests/fixtures/agents_golden/capture.py`` -- see that
script's docstring for the exact inputs and the two redactions (the ephemeral
HOME's own path, and ``agy_tool_guard.sh``'s absolute path) that are
inherently host/checkout-dependent and therefore normalized before comparison.

This is lot 1 of the agent runtime extraction, Brain ticket c31bad72: the
whole point of moving code instead of rewriting it is that this file should
need to know nothing about *how* the package builds a command, only that it
builds the SAME one.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from brain_v42.agents.providers.codex import _codex_child_environment, build_codex_command

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "agents_golden"
PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
PROJECT_KEY = "brain-v42"
GOLDEN_MODEL = "golden-model"
GOLDEN_MCP_URL = "http://127.0.0.1:8765/mcp"
TOKEN_PLACEHOLDER = "<redacted-scoped-token>"
HOME_PLACEHOLDER = "<redacted-ephemeral-home>"
GUARD_PLACEHOLDER = "<redacted-guard-path>"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _registry() -> str:
    profiles = {
        f"{PROJECT_KEY}:{phase}": {"active": f"{phase}-active-token-placeholder", "accepted": []}
        for phase in PHASES
    }
    return json.dumps(profiles)


def _synthetic_environ() -> dict[str, str]:
    return {
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
        "MCP_HTTP_TOKEN": "admin-token-placeholder",
        "MCP_HTTP_DREAM_TOKENS": _registry(),
        "BRAIN_DREAM_MCP_URL": GOLDEN_MCP_URL,
        "PATH": "/usr/bin:/bin",
        "HOME": "/golden/real-home",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }


@pytest.mark.parametrize("phase", PHASES)
def test_codex_command_and_child_env_match_pre_extraction_fixture(phase: str) -> None:
    fixture = _fixture(f"codex-{phase}.json")

    command = build_codex_command(
        phase=phase,
        model=GOLDEN_MODEL,
        reasoning_effort="medium",
        report_log=Path("/golden/workspace/report.log"),
        workspace=Path("/golden/workspace"),
        codex_executable="codex",
        mcp_url=GOLDEN_MCP_URL,
    )
    child_env = _codex_child_environment(
        project_key=PROJECT_KEY, phase=phase, environ=_synthetic_environ()
    )

    assert command == fixture["argv"]
    assert child_env == fixture["child_env"]


@pytest.mark.parametrize("phase", PHASES)
def test_claude_command_and_child_env_match_pre_extraction_fixture(phase: str) -> None:
    claude = pytest.importorskip(
        "brain_v42.agents.providers.claude",
        reason="Claude adapter lands in stage 3 of the agent runtime extraction",
    )
    fixture = _fixture(f"claude-{phase}.json")

    mcp_config = claude.build_claude_mcp_config(phase=phase, mcp_url=GOLDEN_MCP_URL)
    command = claude.build_claude_command(
        phase=phase,
        model=GOLDEN_MODEL,
        max_turns=40,
        mcp_config_path=Path("/golden/workspace/mcp-config.json"),
        claude_executable="claude",
    )
    child_env = claude.claude_child_environment(
        project_key=PROJECT_KEY, phase=phase, environ=_synthetic_environ()
    )

    assert command == fixture["argv"]
    assert child_env == fixture["child_env"]
    assert mcp_config == fixture["mcp_config"]


@pytest.mark.parametrize("phase", PHASES)
def test_agy_command_and_ephemeral_home_match_pre_extraction_fixture(phase: str) -> None:
    from brain_v42.agents.providers.agy import build_agy_command
    from brain_v42.agents.sandbox import build_ephemeral_home

    fixture = _fixture(f"agy-{phase}.json")

    command = build_agy_command(
        model=GOLDEN_MODEL,
        prompt="GOLDEN PROMPT",
        agy_executable="agy",
        timeout_seconds=300.0,
    )
    assert command == fixture["argv"]

    token = f"{phase}-active-token-placeholder"
    guard_path = Path(GUARD_PLACEHOLDER)
    with tempfile.TemporaryDirectory(prefix="brain-v42-golden-agy-test-") as raw_root:
        root = Path(raw_root)
        home = build_ephemeral_home(
            root=root,
            phase=phase,
            project_key=PROJECT_KEY,
            environ=_synthetic_environ(),
            real_home=root / "real-home-absent",
            guard_path=guard_path,
            mcp_url=GOLDEN_MCP_URL,
        )
        home_str = str(home)
        home_files: dict[str, str] = {}
        for entry in sorted(home.rglob("*")):
            if not entry.is_file():
                continue
            relative = str(entry.relative_to(home))
            content = entry.read_text(encoding="utf-8")
            content = content.replace(token, TOKEN_PLACEHOLDER)
            content = content.replace(home_str, HOME_PLACEHOLDER)
            home_files[relative] = content

    assert home_files == fixture["home_files"]
