"""The generic ``claude -p`` adapter: MCP config and command from a profile."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from headless_agents.capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
)
from headless_agents.profile import CapabilityProfile, McpServer
from headless_agents.providers import claude
from headless_agents.spec import RunSpec

URL = "http://127.0.0.1:8765/mcp"


def _server(**overrides: object) -> McpServer:
    fields: dict[str, object] = {
        "name": "example",
        "url": URL,
        "bearer_env_var": "EXAMPLE_TOKEN",
        "headers": {"X-Agent": "example-run"},
        "tools": ("example_search", "example_get"),
    }
    fields.update(overrides)
    return McpServer(**fields)  # type: ignore[arg-type]


class TestBuildClaudeMcpConfig:
    def test_references_the_bearer_by_variable_never_by_value(self) -> None:
        config = claude.build_claude_mcp_config(_server(bearer=SecretStr("literal-secret")))
        assert config == {
            "mcpServers": {
                "example": {
                    "type": "http",
                    "url": URL,
                    "headers": {
                        "X-Agent": "example-run",
                        "Authorization": "Bearer ${EXAMPLE_TOKEN}",
                    },
                }
            }
        }
        assert "literal-secret" not in json.dumps(config)

    def test_no_server_is_an_empty_declaration(self) -> None:
        assert claude.build_claude_mcp_config(None) == {"mcpServers": {}}


class TestBuildClaudeCommand:
    def test_with_a_server_allows_exactly_its_tools(self, tmp_path: Path) -> None:
        command = claude.build_claude_command(
            model="m", max_turns=3, mcp_config_path=tmp_path / "mcp.json", mcp=_server()
        )
        assert command == [
            "claude",
            "-p",
            "-",
            "--model",
            "m",
            "--max-turns",
            "3",
            "--permission-mode",
            "bypassPermissions",
            "--tools",
            "",
            "--allowedTools",
            "mcp__example__example_search,mcp__example__example_get",
            "--mcp-config",
            str(tmp_path / "mcp.json"),
            "--strict-mcp-config",
        ]

    def test_without_a_server_allows_no_tool_at_all(self, tmp_path: Path) -> None:
        command = claude.build_claude_command(
            model="m", max_turns=1, mcp_config_path=tmp_path / "mcp.json", mcp=None
        )
        assert "--allowedTools" not in command
        assert command[command.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in command

    def test_refuses_a_blank_model_or_non_positive_turns(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="model"):
            claude.build_claude_command(
                model=" ", max_turns=1, mcp_config_path=tmp_path / "m", mcp=None
            )
        with pytest.raises(ValueError, match="max_turns"):
            claude.build_claude_command(
                model="m", max_turns=0, mcp_config_path=tmp_path / "m", mcp=None
            )


class TestToolCallCompleted:
    def test_reads_the_otel_console_record(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw.log"
        raw.write_text(
            'body: "claude_code.tool_result"\nattributes: { tool_name: "mcp_tool", success: "true" }\n',
            encoding="utf-8",
        )
        assert claude.tool_call_completed(raw) is True
        raw.write_text(
            'body: "claude_code.tool_result"\nattributes: { tool_name: "mcp_tool", success: "false" }\n',
            encoding="utf-8",
        )
        assert claude.tool_call_completed(raw) is False
        assert claude.tool_call_completed(tmp_path / "absent") is False


class _FakeProcess:
    def __init__(self, *, returncode: int, output: str = "", hang: bool = False) -> None:
        self._returncode = returncode
        self._output = output
        self._hang = hang
        self.returncode: int | None = None
        self.pid = 4242

    def bind(self, stream: object) -> None:
        self._stream = stream

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        if self._hang:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        self._stream.write(self._output)  # type: ignore[attr-defined]
        self.returncode = self._returncode
        return None, None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess) -> dict[str, object]:
    captured: dict[str, object] = {}

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        fake.bind(kwargs["stdout"])
        return fake

    monkeypatch.setattr(claude.subprocess, "Popen", popen)
    monkeypatch.setattr(claude, "terminate_process_group", lambda process: process.kill())
    return captured


def _run(tmp_path: Path, **overrides: object) -> int:
    kwargs: dict[str, object] = {
        "prompt": "PROMPT",
        "model": "m",
        "max_turns": 2,
        "timeout_seconds": 5.0,
        "raw_log": tmp_path / "out" / "raw.log",
        "mcp": _server(),
        "environment": {"PATH": "/usr/bin", "EXAMPLE_TOKEN": "t"},
    }
    kwargs.update(overrides)
    return claude.run_claude(**kwargs)  # type: ignore[arg-type]


class TestRunClaude:
    def test_writes_the_mcp_config_into_a_private_runtime_dir_and_runs_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        assert _run(tmp_path) == 0
        command = captured["command"]
        kwargs = captured["kwargs"]
        assert isinstance(command, list) and isinstance(kwargs, dict)
        config_path = Path(command[command.index("--mcp-config") + 1])
        assert config_path.parent == kwargs["cwd"]
        assert kwargs["env"] == {"PATH": "/usr/bin", "EXAMPLE_TOKEN": "t"}
        assert kwargs["stderr"] is subprocess.STDOUT
        assert (tmp_path / "out" / "raw.log").read_text(encoding="utf-8") == "ok\n"

    def test_refuses_to_start_without_the_bearer_variable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert _run(tmp_path, environment={"PATH": "/usr/bin"}) == 1
        assert "EXAMPLE_TOKEN" in (tmp_path / "out" / "raw.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_no_server_needs_no_bearer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=0))
        assert _run(tmp_path, mcp=None, environment={"PATH": "/usr/bin"}) == 0

    def test_launch_failure_and_silent_failure_are_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def popen(command: list[str], **kwargs: object) -> None:
            raise OSError("no such binary")

        monkeypatch.setattr(claude.subprocess, "Popen", popen)
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=5, output="no telemetry\n"))
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE

    def test_a_failure_after_a_completed_call_keeps_its_own_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = 'body: "claude_code.tool_result"\nattributes: { tool_name: "mcp_tool", success: "true" }\n'
        _install(monkeypatch, _FakeProcess(returncode=5, output=output))
        assert _run(tmp_path) == 5

    def test_a_childs_own_fallback_code_after_a_completed_call_never_advances_a_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """claude exiting 3 or 4 by itself after writing must be an ordinary
        failure, or the chain would replay a run that provably wrote."""
        output = 'body: "claude_code.tool_result"\nattributes: { tool_name: "mcp_tool", success: "true" }\n'
        for code in (PROVIDER_FALLBACK_EXIT_CODE, TIMEOUT_REPLAYABLE_EXIT_CODE):
            _install(monkeypatch, _FakeProcess(returncode=code, output=output))
            assert _run(tmp_path) == 1, code

    def test_timeouts(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """This rail never returns the replayable timeout: its only witness is
        the OTEL console stream, flushed on an interval, so an empty raw_log at
        the kill does not prove an empty run."""
        _install(monkeypatch, _FakeProcess(returncode=0, hang=True))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        assert _run(tmp_path) != TIMEOUT_REPLAYABLE_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=124))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        with pytest.raises(ValueError, match="timeout"):
            _run(tmp_path, timeout_seconds=-1)


class TestCallerWording:
    def test_the_temp_prefix_is_the_callers_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        assert (
            _run(tmp_path, mcp=None, environment={"PATH": "/usr/bin"}, temp_prefix="caller-") == 0
        )
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert Path(str(kwargs["cwd"])).name.startswith("caller-")


class TestClaudeProvider:
    def test_run_forwards_and_reports(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(claude, "run_claude", lambda **kwargs: calls.append(kwargs) or 0)
        spec = RunSpec(
            prompt="P",
            model="m",
            max_turns=4,
            profile=CapabilityProfile(mcp=_server()),
            raw_log=tmp_path / "raw.log",
            environment={"EXAMPLE_TOKEN": "t"},
        )
        result = claude.ClaudeProvider().run(spec)
        assert result.exit_code == 0 and result.provider == "claude"
        assert result.report_path == tmp_path / "raw.log"
        assert calls[0]["max_turns"] == 4 and calls[0]["mcp"] == _server()
