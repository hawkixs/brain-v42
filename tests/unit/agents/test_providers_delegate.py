"""The three ``*Provider`` classes must be THIN delegators, nothing more.

``build_command`` / ``child_environment`` must return exactly what the moved
module-level functions return for the same inputs, and ``run`` must forward to
the moved ``run_*`` function with the same arguments -- never a reimplemented
or reinterpreted version of either. This is the test the delivery contract
(Brain ticket c31bad72) asks for: the ``AgentProvider`` protocol, the three
adapters and the typed result must ship WITH dedicated unit tests, not as
~230 untested lines of "moved code" that nothing constructs or exercises.

Reuses the same fixed placeholder inputs
``tests/fixtures/agents_golden/capture.py`` and
``tests/unit/agents/test_golden_commands.py`` already use, so a reviewer
reading both files side by side sees the same story: the golden test proves
the moved functions did not drift from pre-extraction behaviour, this one
proves the Provider wrapper does not add behaviour of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_v42.agents.providers.agy import AgyProvider, build_agy_command
from brain_v42.agents.providers.claude import (
    ClaudeProvider,
    build_claude_command,
    claude_child_environment,
)
from brain_v42.agents.providers.codex import (
    CodexProvider,
    _codex_child_environment,
    build_codex_command,
)
from brain_v42.agents.spec import RunSpec

PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
PROJECT_KEY = "brain-v42"
GOLDEN_MODEL = "golden-model"
GOLDEN_MCP_URL = "http://127.0.0.1:8765/mcp"


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


# --- CodexProvider -----------------------------------------------------------


def _codex_spec(phase: str) -> RunSpec:
    return RunSpec(
        phase=phase,
        prompt="GOLDEN PROMPT",
        project_key=PROJECT_KEY,
        model=GOLDEN_MODEL,
        reasoning_effort="medium",
        report_log=Path("/golden/workspace/report.log"),
        events_log=Path("/golden/workspace/events.jsonl"),
        stderr_log=Path("/golden/workspace/stderr.log"),
        workspace=Path("/golden/workspace"),
        mcp_url=GOLDEN_MCP_URL,
        executable="codex",
    )


def test_codex_provider_build_command_matches_the_moved_function() -> None:
    spec = _codex_spec("scan")
    provider = CodexProvider()

    assert provider.build_command(spec) == build_codex_command(
        phase=spec.phase,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        report_log=spec.report_log,
        workspace=spec.workspace,
        codex_executable=spec.executable,
        mcp_url=spec.mcp_url,
    )


def test_codex_provider_child_environment_matches_the_moved_function() -> None:
    spec = _codex_spec("clean")
    provider = CodexProvider()
    environ = _synthetic_environ()

    assert provider.child_environment(spec, environ) == _codex_child_environment(
        project_key=spec.project_key, phase=spec.phase, environ=environ
    )


def test_codex_provider_run_forwards_to_the_moved_run_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _codex_spec("connect")
    provider = CodexProvider()
    calls: list[dict[str, object]] = []

    def fake_run_codex(**kwargs: object) -> int:
        calls.append(kwargs)
        return 42

    monkeypatch.setattr("brain_v42.agents.providers.codex.run_codex", fake_run_codex)
    monkeypatch.setattr(
        "brain_v42.agents.providers.codex.brain_tool_call_completed", lambda _log: False
    )

    result = provider.run(spec)

    assert result.exit_code == 42
    assert len(calls) == 1
    assert calls[0] == {
        "prompt": spec.prompt,
        "phase": spec.phase,
        "project_key": spec.project_key,
        "model": spec.model,
        "reasoning_effort": spec.reasoning_effort,
        "timeout_seconds": spec.timeout_seconds,
        "report_log": spec.report_log,
        "events_log": spec.events_log,
        "stderr_log": spec.stderr_log,
        "codex_executable": spec.executable,
        "workspace": spec.workspace,
    }


def test_codex_provider_reports_tokens_as_not_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No untested token parser: RunResult.tokens is None until one is added
    with its own fixture-backed test (see the delivery review that removed
    the previous, wrong-key-name guess)."""
    spec = _codex_spec("synth")
    provider = CodexProvider()
    monkeypatch.setattr("brain_v42.agents.providers.codex.run_codex", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "brain_v42.agents.providers.codex.brain_tool_call_completed", lambda _log: False
    )

    result = provider.run(spec)

    assert result.tokens is None


# --- AgyProvider ---------------------------------------------------------


def _agy_spec(phase: str) -> RunSpec:
    return RunSpec(
        phase=phase,
        prompt="GOLDEN PROMPT",
        project_key=PROJECT_KEY,
        model=GOLDEN_MODEL,
        timeout_seconds=300.0,
        events_log=Path("/golden/workspace/events.jsonl"),
        report_log=Path("/golden/workspace/report.log"),
        stderr_log=Path("/golden/workspace/stderr.log"),
        mcp_url=GOLDEN_MCP_URL,
        executable="agy",
    )


def test_agy_provider_build_command_matches_the_moved_function() -> None:
    spec = _agy_spec("promote")
    provider = AgyProvider(guard_path=Path("/golden/guard/agy_tool_guard.sh"))

    assert provider.build_command(spec) == build_agy_command(
        model=spec.model,
        prompt=spec.prompt,
        agy_executable=spec.executable,
        timeout_seconds=spec.timeout_seconds,
    )


def test_agy_provider_child_environment_is_honestly_none() -> None:
    """agy's bearer travels through the ephemeral HOME's mcp_config.json, not
    a capability-scoped child environment -- see run_agy's own inline
    construction. The provider must say so, not invent one."""
    spec = _agy_spec("reorg")
    provider = AgyProvider(guard_path=Path("/golden/guard/agy_tool_guard.sh"))

    assert provider.child_environment(spec, _synthetic_environ()) is None


def test_agy_provider_run_forwards_to_the_moved_run_agy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _agy_spec("scan")
    guard_path = Path("/golden/guard/agy_tool_guard.sh")
    provider = AgyProvider(guard_path=guard_path)
    calls: list[dict[str, object]] = []

    def fake_run_agy(**kwargs: object) -> int:
        calls.append(kwargs)
        return 7

    monkeypatch.setattr("brain_v42.agents.providers.agy.run_agy", fake_run_agy)
    monkeypatch.setattr(
        "brain_v42.agents.providers.agy.brain_tool_call_completed", lambda _log: False
    )

    result = provider.run(spec)

    assert result.exit_code == 7
    assert len(calls) == 1
    assert calls[0] == {
        "prompt": spec.prompt,
        "phase": spec.phase,
        "project_key": spec.project_key,
        "model": spec.model,
        "timeout_seconds": spec.timeout_seconds,
        "events_log": spec.events_log,
        "report_log": spec.report_log,
        "stderr_log": spec.stderr_log,
        "guard_path": guard_path,
        "agy_executable": spec.executable,
    }


# --- ClaudeProvider --------------------------------------------------------


def _claude_spec(phase: str) -> RunSpec:
    return RunSpec(
        phase=phase,
        prompt="GOLDEN PROMPT",
        project_key=PROJECT_KEY,
        model=GOLDEN_MODEL,
        max_turns=40,
        timeout_seconds=300.0,
        raw_log=Path("/golden/workspace/raw.log"),
        mcp_config_path=Path("/golden/workspace/mcp-config.json"),
        mcp_url=GOLDEN_MCP_URL,
        executable="claude",
    )


def test_claude_provider_build_command_matches_the_moved_function() -> None:
    spec = _claude_spec("synth")
    provider = ClaudeProvider()

    assert provider.build_command(spec) == build_claude_command(
        phase=spec.phase,
        model=spec.model,
        max_turns=spec.max_turns,
        mcp_config_path=spec.mcp_config_path,
        claude_executable=spec.executable,
    )


def test_claude_provider_child_environment_matches_the_moved_function() -> None:
    spec = _claude_spec("promote")
    provider = ClaudeProvider()
    environ = _synthetic_environ()

    assert provider.child_environment(spec, environ) == claude_child_environment(
        project_key=spec.project_key, phase=spec.phase, environ=environ
    )


def test_claude_provider_run_forwards_to_the_moved_run_claude_using_raw_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact field the CLI shim's ``--raw-log`` argument carries -- no
    fallback onto events_log/report_log, which claude_runner.py never uses."""
    spec = _claude_spec("reorg")
    provider = ClaudeProvider()
    calls: list[dict[str, object]] = []

    def fake_run_claude(**kwargs: object) -> int:
        calls.append(kwargs)
        return 3

    monkeypatch.setattr("brain_v42.agents.providers.claude.run_claude", fake_run_claude)
    monkeypatch.setattr(
        "brain_v42.agents.providers.claude.brain_tool_call_completed", lambda _log: False
    )

    result = provider.run(spec)

    assert result.exit_code == 3
    assert len(calls) == 1
    assert calls[0] == {
        "prompt": spec.prompt,
        "phase": spec.phase,
        "project_key": spec.project_key,
        "model": spec.model,
        "max_turns": spec.max_turns,
        "timeout_seconds": spec.timeout_seconds,
        "raw_log": spec.raw_log,
        "claude_executable": spec.executable,
    }


def test_claude_provider_run_requires_raw_log() -> None:
    spec = _claude_spec("scan")
    provider = ClaudeProvider()
    object.__setattr__(spec, "raw_log", None)

    with pytest.raises(AssertionError, match="raw_log"):
        provider.run(spec)
