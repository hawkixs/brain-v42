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
from headless_agents.context import resolve_context
from headless_agents.profile import CapabilityProfile, McpServer, Workspace
from headless_agents.providers import claude
from headless_agents.spec import RunSpec
from headless_agents.workspace import workspace_summary

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


class TestBuildClaudeCommandWorkspace:
    def test_no_workspace_command_is_unchanged(self, tmp_path: Path) -> None:
        base = claude.build_claude_command(
            model="m", max_turns=3, mcp_config_path=tmp_path / "m.json", mcp=None
        )
        assert (
            claude.build_claude_command(
                model="m",
                max_turns=3,
                mcp_config_path=tmp_path / "m.json",
                mcp=None,
                workspace=None,
                append_system_prompt=None,
            )
            == base
        )

    def test_read_only_workspace_flags(self, tmp_path: Path) -> None:
        command = claude.build_claude_command(
            model="m",
            max_turns=3,
            mcp_config_path=tmp_path / "m.json",
            mcp=None,
            workspace=Workspace(path=tmp_path),
        )
        assert "bypassPermissions" not in command
        assert command[command.index("--permission-mode") + 1] == "dontAsk"
        assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
        assert "--restricted" in command

    def test_write_workspace_flags(self, tmp_path: Path) -> None:
        command = claude.build_claude_command(
            model="m",
            max_turns=3,
            mcp_config_path=tmp_path / "m.json",
            mcp=None,
            workspace=Workspace(path=tmp_path, write=True),
        )
        assert command[command.index("--permission-mode") + 1] == "acceptEdits"
        assert command[command.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
        assert "--allowedTools" not in command

    def test_shell_adds_bash_to_tools_and_allowed(self, tmp_path: Path) -> None:
        mcp = McpServer(name="brain", url=URL, tools=("brain_search",))
        command = claude.build_claude_command(
            model="m",
            max_turns=3,
            mcp_config_path=tmp_path / "m.json",
            mcp=mcp,
            workspace=Workspace(path=tmp_path, write=True, shell=True),
        )
        assert command[command.index("--tools") + 1].endswith(",Bash")
        assert command[command.index("--allowedTools") + 1] == "mcp__brain__brain_search,Bash"

    def test_append_system_prompt(self, tmp_path: Path) -> None:
        command = claude.build_claude_command(
            model="m",
            max_turns=3,
            mcp_config_path=tmp_path / "m.json",
            mcp=None,
            append_system_prompt="PRE",
        )
        assert command[command.index("--append-system-prompt") + 1] == "PRE"


def _fake(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


class TestRunClaudeWorkspace:
    def test_workspace_is_the_cwd(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        answer = tmp_path / "answer.log"
        code = claude.run_claude(
            prompt="p",
            model="m",
            max_turns=1,
            timeout_seconds=30,
            raw_log=tmp_path / "raw.log",
            mcp=None,
            answer_log=answer,
            executable=_fake(tmp_path, "pwd"),
            workspace=Workspace(path=ws),
        )
        assert code == 0 and answer.read_text().strip() == str(ws)

    def test_write_mode_failure_is_never_replayable(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        code = claude.run_claude(
            prompt="p",
            model="m",
            max_turns=1,
            timeout_seconds=30,
            raw_log=tmp_path / "raw.log",
            mcp=None,
            executable=_fake(tmp_path, "exit 3"),
            workspace=Workspace(path=ws, write=True),
        )
        assert code == 1

    def test_read_only_failure_stays_replayable(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        code = claude.run_claude(
            prompt="p",
            model="m",
            max_turns=1,
            timeout_seconds=30,
            raw_log=tmp_path / "raw.log",
            mcp=None,
            executable=_fake(tmp_path, "exit 7"),
            workspace=Workspace(path=ws),
        )
        assert code == 3


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

    def test_an_answer_log_takes_stdout_alone_and_raw_log_keeps_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The OTEL console stream and CLI warnings travel on stderr: with an
        answer_log they stay in raw_log, where tool_call_completed reads them."""

        class TwoStreams(_FakeProcess):
            def communicate(
                self, input: str | None = None, timeout: float | None = None
            ) -> tuple[None, None]:
                self.streams["stdout"].write("the answer\n")
                self.streams["stderr"].write('body: "claude_code.api_request"\n')
                self.returncode = 0
                return None, None

        fake = TwoStreams(returncode=0)

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            fake.streams = {"stdout": kwargs["stdout"], "stderr": kwargs["stderr"]}
            return fake

        monkeypatch.setattr(claude.subprocess, "Popen", popen)
        answer_log = tmp_path / "out" / "report.log"
        assert _run(tmp_path, answer_log=answer_log) == 0
        assert answer_log.read_text(encoding="utf-8") == "the answer\n"
        raw = (tmp_path / "out" / "raw.log").read_text(encoding="utf-8")
        assert raw == 'body: "claude_code.api_request"\n'


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

    def test_text_is_only_what_this_run_appended_to_a_reused_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        raw_log = tmp_path / "raw.log"
        raw_log.write_text("PREVIOUS RUN\n", encoding="utf-8")

        def fake_run_claude(**kwargs: object) -> int:
            raw = kwargs["raw_log"]
            assert isinstance(raw, Path)
            with raw.open("a", encoding="utf-8") as stream:
                stream.write("THIS RUN\n")
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        result = claude.ClaudeProvider().run(RunSpec(prompt="P", model="m", raw_log=raw_log))
        assert result.text == "THIS RUN\n"

    def test_an_answer_that_is_json_comes_back_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A judge's verdict is JSON. A ``result`` key in it must not be read as
        claude's --output-format envelope, which this rail never requests."""
        verdict = '{"result": "pass", "score": 3}\n'

        def fake_run_claude(**kwargs: object) -> int:
            raw = kwargs["raw_log"]
            assert isinstance(raw, Path)
            raw.parent.mkdir(parents=True, exist_ok=True)
            with raw.open("a", encoding="utf-8") as stream:
                stream.write(verdict)
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        result = claude.ClaudeProvider().run(
            RunSpec(prompt="P", model="m", raw_log=tmp_path / "raw.log")
        )
        assert result.text == verdict

    def test_run_dir_keeps_the_answer_apart_from_the_raw_log(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With a report_log -- here the one run_dir gives -- stdout alone is the
        answer: the OTEL console stream and CLI warnings stay in raw.log."""

        def fake_run_claude(**kwargs: object) -> int:
            answer, raw = kwargs["answer_log"], kwargs["raw_log"]
            assert isinstance(answer, Path) and isinstance(raw, Path)
            answer.parent.mkdir(parents=True, exist_ok=True)
            answer.write_text("ok\n", encoding="utf-8")
            raw.write_text('body: "claude_code.api_request"\n', encoding="utf-8")
            return 0

        monkeypatch.setattr(claude, "run_claude", fake_run_claude)
        run_dir = tmp_path / "runs" / "r1"
        result = claude.ClaudeProvider().run(RunSpec(prompt="P", model="m", run_dir=run_dir))
        assert result.text == "ok\n"
        assert result.report_path == run_dir / "report.log"
        assert result.raw_log == run_dir / "raw.log"
        assert result.stderr_log is None
        assert result.run_id == "r1"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
        assert written["logs"]["report"] == str(run_dir / "report.log")
        assert written["logs"]["stderr"] is None

    def test_run_delivers_workspace_and_context_through_append_system_prompt(
        self, tmp_path: Path
    ) -> None:
        """The fake prints its argv (one per line) instead of answering, so the
        answer text IS the command claude was launched with: the one place a
        provider-level test can see what reached ``--append-system-prompt``."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (tmp_path / "CLAUDE.md").write_text("Repository rules.", encoding="utf-8")
        workspace = Workspace(path=ws_dir, write=True)
        bundle = resolve_context(level="full", repository_root=tmp_path)
        answer_log = tmp_path / "answer.log"
        spec = RunSpec(
            prompt="do the thing",
            model="m",
            max_turns=1,
            profile=CapabilityProfile(workspace=workspace),
            executable=_fake(tmp_path, 'printf "%s\\n" "$@"'),
            raw_log=tmp_path / "raw.log",
            report_log=answer_log,
            context=bundle,
            environment={"PATH": "/usr/bin"},
        )
        result = claude.ClaudeProvider().run(spec)
        assert result.exit_code == 0
        argv_lines = answer_log.read_text(encoding="utf-8").splitlines()
        assert "--append-system-prompt" in argv_lines
        preamble = argv_lines[argv_lines.index("--append-system-prompt") + 1]
        assert str(ws_dir) in preamble
        assert result.workspace == workspace_summary(workspace)
        assert result.context == tuple(bundle.to_list())
