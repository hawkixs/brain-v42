"""Capture golden argv/child-env fixtures from the PRE-extraction runners.

Run exactly once, against the code at ``scripts/dream/{codex,agy,claude}_runner.py``
as it stood on ``main`` at ``81a6ffb468b0e2bcc58eba72d67858144763715d`` -- BEFORE
lot 1 of the agent runtime extraction (Brain ticket c31bad72) touched any of
those files. ``tests/unit/agents/test_golden_commands.py`` holds the new
``brain_v42.agents`` package to these fixtures as a byte-for-byte oracle.

Re-running this script after the extraction would defeat its purpose -- it
must capture what the OLD code answered, once, and never again. If a rail's
observable behaviour ever needs to change on purpose, that change belongs in a
new PR with its own review, not a silent re-capture.

Every input here is a fixed placeholder: paths under ``/golden/...``, models
named ``golden-model``, a synthetic capability registry with placeholder
tokens. None of it reflects production configuration (see ``dream.sh`` and
``docs/ARCHITECTURE.md`` for the real model/phase policy) -- this script only
proves the ARGV- and ENV-BUILDING CODE produces the same bytes before and
after the move.

Two values are inherently host/checkout-dependent and are replaced with a
stable placeholder before being written to the fixture (documented under each
fixture's ``"redacted"`` key):

- the ephemeral HOME's own absolute path (``build_ephemeral_home`` composes it
  under a tmpfs/tempdir root that differs on every run);
- the absolute path to ``agy_tool_guard.sh``, which the pre-extraction code
  resolves from ``__file__`` and which differs across checkouts.

Usage (from the repo root):
    PYTHONPATH=src <venv>/bin/python tests/fixtures/agents_golden/capture.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scripts.dream import agy_runner, claude_runner, codex_runner

FIXTURES_DIR = Path(__file__).resolve().parent
PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
PROJECT_KEY = "brain-v42"
GOLDEN_MODEL = "golden-model"
GOLDEN_MCP_URL = "http://127.0.0.1:8765/mcp"
TOKEN_PLACEHOLDER = "<redacted-scoped-token>"
HOME_PLACEHOLDER = "<redacted-ephemeral-home>"
GUARD_PLACEHOLDER = "<redacted-guard-path>"


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


def _write(name: str, payload: dict[str, object]) -> None:
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def capture_codex() -> None:
    for phase in PHASES:
        environ = _synthetic_environ()
        command = codex_runner.build_codex_command(
            phase=phase,
            model=GOLDEN_MODEL,
            reasoning_effort="medium",
            report_log=Path("/golden/workspace/report.log"),
            workspace=Path("/golden/workspace"),
            codex_executable="codex",
            mcp_url=GOLDEN_MCP_URL,
        )
        child_env = codex_runner._codex_child_environment(  # noqa: SLF001
            project_key=PROJECT_KEY, phase=phase, environ=environ
        )
        _write(
            f"codex-{phase}.json",
            {"provider": "codex", "phase": phase, "argv": command, "child_env": child_env},
        )


def capture_claude() -> None:
    for phase in PHASES:
        environ = _synthetic_environ()
        mcp_config = claude_runner.build_claude_mcp_config(phase=phase, mcp_url=GOLDEN_MCP_URL)
        command = claude_runner.build_claude_command(
            phase=phase,
            model=GOLDEN_MODEL,
            max_turns=40,
            mcp_config_path=Path("/golden/workspace/mcp-config.json"),
            claude_executable="claude",
        )
        child_env = claude_runner.claude_child_environment(
            project_key=PROJECT_KEY, phase=phase, environ=environ
        )
        _write(
            f"claude-{phase}.json",
            {
                "provider": "claude",
                "phase": phase,
                "argv": command,
                "child_env": child_env,
                "mcp_config": mcp_config,
            },
        )


def capture_agy() -> None:
    for phase in PHASES:
        environ = _synthetic_environ()
        command = agy_runner.build_agy_command(
            model=GOLDEN_MODEL,
            prompt="GOLDEN PROMPT",
            agy_executable="agy",
            timeout_seconds=300.0,
        )
        token = f"{phase}-active-token-placeholder"
        guard_path = str(agy_runner.GUARD_PATH)
        with tempfile.TemporaryDirectory(prefix="brain-v42-golden-agy-") as raw_root:
            root = Path(raw_root)
            home = agy_runner.build_ephemeral_home(
                root=root,
                phase=phase,
                project_key=PROJECT_KEY,
                environ=environ,
                real_home=root / "real-home-absent",
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
                content = content.replace(guard_path, GUARD_PLACEHOLDER)
                home_files[relative] = content
        _write(
            f"agy-{phase}.json",
            {
                "provider": "agy",
                "phase": phase,
                "argv": command,
                "home_files": home_files,
                "redacted": {
                    "token": f"the (project, phase) active bearer -> {TOKEN_PLACEHOLDER!r}",
                    "home": f"the ephemeral HOME's own absolute path -> {HOME_PLACEHOLDER!r}",
                    "guard_path": f"agy_tool_guard.sh's absolute path -> {GUARD_PLACEHOLDER!r}",
                },
            },
        )


def main() -> None:
    capture_codex()
    capture_claude()
    capture_agy()


if __name__ == "__main__":
    main()
