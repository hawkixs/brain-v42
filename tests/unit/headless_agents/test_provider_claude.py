"""The generic ``claude -p`` adapter: MCP config and command from a profile."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from headless_agents import procgroup
from headless_agents.capability import (
    INVALID_USAGE_EXIT_CODE,
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

_OAUTH = {"accessToken": "at-1", "refreshToken": "rt-1", "expiresAt": 1}
_MCP_OAUTH = {"Gmail|x": {"accessToken": "gmail-secret"}}


def _write_credentials(config_dir: Path, **overrides: object) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".credentials.json"
    document: dict[str, object] = {"mcpOAuth": _MCP_OAUTH, "claudeAiOauth": _OAUTH}
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def _operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every run in this module reads a fake operator home, never the real one."""
    home = tmp_path / "operator-home"
    _write_credentials(home / ".claude")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


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

    def test_shell_without_a_server_allows_bash_alone(self, tmp_path: Path) -> None:
        """No MCP server means no ``mcp__*`` entries: ``Bash`` alone must still
        reach ``--allowedTools``, or a shell-capable workspace with no server
        would get a shell it can never actually invoke."""
        command = claude.build_claude_command(
            model="m",
            max_turns=3,
            mcp_config_path=tmp_path / "m.json",
            mcp=None,
            workspace=Workspace(path=tmp_path, write=True, shell=True),
        )
        assert command[command.index("--allowedTools") + 1] == "Bash"


def _fake(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _fake_argv_json(tmp_path: Path) -> str:
    """A fake that answers with ``json.dumps(argv[1:])`` instead of a real
    answer: one array element per argument, so a multi-line
    ``--append-system-prompt`` value survives as ONE JSON string (embedded
    newlines escaped) rather than being torn across several printed lines."""
    script = tmp_path / "fake-claude-argv"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"
        "python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' \"$@\"\n",
        encoding="utf-8",
    )
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

    def test_write_mode_timeout_stays_the_timeout_code(self, tmp_path: Path) -> None:
        """The 0/124 early returns come BEFORE the write-taint check: a child
        that itself exits 124 in a writable workspace is still a timeout, not
        an ordinary failure -- 124 already means "prove nothing" on this rail."""
        ws = tmp_path / "ws"
        ws.mkdir()
        code = claude.run_claude(
            prompt="p",
            model="m",
            max_turns=1,
            timeout_seconds=30,
            raw_log=tmp_path / "raw.log",
            mcp=None,
            executable=_fake(tmp_path, "exit 124"),
            workspace=Workspace(path=ws, write=True),
        )
        assert code == TIMEOUT_EXIT_CODE

    def test_write_mode_failure_keeps_a_non_fallback_code(self, tmp_path: Path) -> None:
        """``failure_code_after_a_write`` only rewrites the two codes a chain
        would read as "provably no write" (3, 4); an ordinary code like 7 is
        already an ordinary failure and travels through unchanged."""
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
            workspace=Workspace(path=ws, write=True),
        )
        assert code == 7


def test_max_append_system_prompt_bytes_is_the_kernel_arg_limit() -> None:
    """One page below ``MAX_ARG_STRLEN`` (32 pages of 4 KiB): measured against
    ``/bin/true --append-system-prompt`` -- 131072 bytes raises E2BIG via
    Popen, 131071 does not."""
    assert claude.MAX_APPEND_SYSTEM_PROMPT_BYTES == 131_071


class TestAppendSystemPromptLimit:
    def test_a_preamble_over_the_limit_refuses_before_any_spawn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A preamble too big for one argv element is a usage error, never a
        switchover: the fake would ``touch`` a marker file if it ever ran, and
        it must not run at all."""
        monkeypatch.setattr(claude, "MAX_APPEND_SYSTEM_PROMPT_BYTES", 20)
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        marker = tmp_path / "spawned.marker"
        raw_log = tmp_path / "raw.log"
        spec = RunSpec(
            prompt="do the thing",
            model="m",
            max_turns=1,
            profile=CapabilityProfile(workspace=Workspace(path=ws_dir)),
            executable=_fake(tmp_path, f"touch {marker}"),
            raw_log=raw_log,
            environment={"PATH": "/usr/bin"},
        )
        result = claude.ClaudeProvider().run(spec)
        assert result.exit_code == INVALID_USAGE_EXIT_CODE
        assert not marker.exists()
        assert result.text is None
        assert "too long for argv" in raw_log.read_text(encoding="utf-8")


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
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert {k: v for k, v in env.items() if k not in ("HOME", "CLAUDE_CONFIG_DIR")} == {
            "PATH": "/usr/bin",
            "EXAMPLE_TOKEN": "t",
        }
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
        """The fake prints its argv as a JSON array instead of answering, so the
        answer text IS the command claude was launched with -- one array element
        per argument, embedded newlines intact -- the one place a
        provider-level test can see what reached ``--append-system-prompt``.

        Write mode: the preamble carries the user-scope AND the
        repository-scope file -- it is the only channel for both, per
        ``rail_preamble``."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (tmp_path / "CLAUDE.md").write_text("Repository rules.", encoding="utf-8")
        user_file = tmp_path / "user.md"
        user_file.write_text("User rules.", encoding="utf-8")
        workspace = Workspace(path=ws_dir, write=True)
        bundle = resolve_context(level="full", repository_root=tmp_path, user_files=(user_file,))
        answer_log = tmp_path / "answer.log"
        spec = RunSpec(
            prompt="do the thing",
            model="m",
            max_turns=1,
            profile=CapabilityProfile(workspace=workspace),
            executable=_fake_argv_json(tmp_path),
            raw_log=tmp_path / "raw.log",
            report_log=answer_log,
            context=bundle,
            environment={"PATH": "/usr/bin"},
        )
        result = claude.ClaudeProvider().run(spec)
        assert result.exit_code == 0
        argv = json.loads(answer_log.read_text(encoding="utf-8"))
        preamble = argv[argv.index("--append-system-prompt") + 1]
        assert str(ws_dir) in preamble
        assert "User rules." in preamble
        assert "Repository rules." in preamble
        # A writable workspace arms the .git tripwire: a clean run reports [].
        assert result.workspace == workspace_summary(workspace, ())
        assert result.context == tuple(bundle.to_list())


class TestBuildCommandPreviewsTheRun:
    def test_build_command_previews_the_workspace_and_preamble_run_uses(
        self, tmp_path: Path
    ) -> None:
        """``build_command`` is the dry-run preview: a caller that inspects it
        before launching must see the SAME ``--restricted``/permission-mode and
        the same ``--append-system-prompt`` value ``run`` actually launches
        with, not the pre-workspace ``bypassPermissions``."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (tmp_path / "CLAUDE.md").write_text("Repository rules.", encoding="utf-8")
        workspace = Workspace(path=ws_dir, write=True)
        bundle = resolve_context(level="full", repository_root=tmp_path)
        spec = RunSpec(
            prompt="p",
            model="m",
            max_turns=2,
            profile=CapabilityProfile(workspace=workspace),
            context=bundle,
            extra={"mcp_config_path": tmp_path / "m.json"},
        )
        command = claude.ClaudeProvider().build_command(spec)
        assert command[command.index("--permission-mode") + 1] == "acceptEdits"
        assert "--restricted" in command
        preamble = command[command.index("--append-system-prompt") + 1]
        assert str(ws_dir) in preamble


class TestTheProviderDiesWithHa:
    """Spec 0.5.0 §3.8.2: the child gets a death signal, and an interrupted
    wait kills its process group before the interruption propagates."""

    def test_the_child_is_started_with_the_death_signal_preexec(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        assert _run(tmp_path) == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert callable(kwargs["preexec_fn"])
        assert kwargs["start_new_session"] is True

    def test_an_interrupted_wait_kills_the_group_and_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _Interrupted(_FakeProcess):
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                raise KeyboardInterrupt

        fake = _Interrupted(returncode=0)
        _install(monkeypatch, fake)
        with pytest.raises(KeyboardInterrupt):
            _run(tmp_path)
        assert fake.returncode == -9, "terminate_process_group was not called"


class TestTheOperatorConfigurationIsNotInherited:
    """Spec 0.5.0 §3.8.0 (G7), operator decision Q68=a: claude runs with a
    per-run HOME and CLAUDE_CONFIG_DIR holding only a copy of the Claude
    OAuth entry -- no CLAUDE.md, skills, plugins, hooks, settings or user MCP
    servers to load, and none of the operator's other OAuth tokens (the MCP
    ones for Gmail, Drive...). An explicit --mcp-config server still loads,
    which --safe-mode would drop (measured, learning 5ffb9e1b)."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess) -> dict[str, object]:
        seen: dict[str, object] = {}

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen["env"] = dict(env)
            config = Path(env["CLAUDE_CONFIG_DIR"])
            credentials = config / ".credentials.json"
            seen["credentials"] = (
                json.loads(credentials.read_text()) if credentials.exists() else None
            )
            seen["mode"] = credentials.stat().st_mode & 0o777 if credentials.exists() else None
            seen["home_entries"] = sorted(p.name for p in Path(env["HOME"]).iterdir())
            seen["cwd"] = kwargs["cwd"]
            fake.bind(kwargs["stdout"])
            if "rotate" in seen:
                credentials.write_text(json.dumps(seen["rotate"]), encoding="utf-8")
            return fake

        monkeypatch.setattr(claude.subprocess, "Popen", popen)
        monkeypatch.setattr(claude, "terminate_process_group", lambda process: process.kill())
        return seen

    def test_the_child_gets_a_private_home_and_config_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _operator_home: Path
    ) -> None:
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        assert _run(tmp_path) == 0
        env = seen["env"]
        assert isinstance(env, dict)
        home, config = Path(env["HOME"]), Path(env["CLAUDE_CONFIG_DIR"])
        assert not home.is_relative_to(_operator_home)
        assert not config.is_relative_to(_operator_home)
        assert home.parent == config.parent == seen["cwd"]
        assert seen["home_entries"] == []
        assert not home.exists() and not config.exists(), "removed after the run"

    def test_only_the_claude_oauth_entry_is_copied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        _run(tmp_path)
        assert seen["credentials"] == {"claudeAiOauth": _OAUTH}
        assert seen["mode"] == 0o600

    def test_the_operators_claude_config_dir_is_the_source(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom = tmp_path / "custom-config"
        _write_credentials(custom, claudeAiOauth={"accessToken": "custom", "refreshToken": "r"})
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        _run(
            tmp_path,
            environment={
                "PATH": "/usr/bin",
                "EXAMPLE_TOKEN": "t",
                "CLAUDE_CONFIG_DIR": str(custom),
            },
        )
        assert seen["credentials"] == {
            "claudeAiOauth": {"accessToken": "custom", "refreshToken": "r"}
        }
        env = seen["env"]
        assert isinstance(env, dict)
        assert env["CLAUDE_CONFIG_DIR"] != str(custom)

    def test_an_api_key_runs_without_any_credentials_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _operator_home: Path
    ) -> None:
        (_operator_home / ".claude" / ".credentials.json").unlink()
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        code = _run(
            tmp_path,
            environment={"PATH": "/usr/bin", "EXAMPLE_TOKEN": "t", "ANTHROPIC_API_KEY": "k"},
        )
        assert code == 0
        assert seen["credentials"] is None

    def test_no_credentials_at_all_is_provider_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _operator_home: Path
    ) -> None:
        (_operator_home / ".claude" / ".credentials.json").unlink()
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0))
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
        assert "env" not in seen, "claude must not be started"
        assert "no Claude credentials" in (tmp_path / "out" / "raw.log").read_text()

    def test_a_rotated_token_is_written_back_keeping_the_other_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _operator_home: Path
    ) -> None:
        rotated = {"accessToken": "at-2", "refreshToken": "rt-2", "expiresAt": 2}
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        seen["rotate"] = {"claudeAiOauth": rotated}
        assert _run(tmp_path) == 0
        real = _operator_home / ".claude" / ".credentials.json"
        assert json.loads(real.read_text()) == {"mcpOAuth": _MCP_OAUTH, "claudeAiOauth": rotated}
        assert real.stat().st_mode & 0o777 == 0o600

    def test_a_rotation_is_not_written_over_a_file_changed_meanwhile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _operator_home: Path
    ) -> None:
        real = _operator_home / ".claude" / ".credentials.json"
        seen = self._capture(monkeypatch, _FakeProcess(returncode=0, output="ok\n"))
        newer = {"mcpOAuth": _MCP_OAUTH, "claudeAiOauth": {"accessToken": "other-session"}}

        original_bind = _FakeProcess.bind

        def bind_and_race(self: _FakeProcess, stream: object) -> None:
            real.write_text(json.dumps(newer), encoding="utf-8")
            original_bind(self, stream)

        monkeypatch.setattr(_FakeProcess, "bind", bind_and_race)
        seen["rotate"] = {"claudeAiOauth": {"accessToken": "at-2", "refreshToken": "rt-2"}}
        _run(tmp_path)
        assert json.loads(real.read_text()) == newer
        assert "not written back" in (tmp_path / "out" / "raw.log").read_text()

    @pytest.mark.parametrize(
        "rotated",
        [
            "not json",
            [],
            {"claudeAiOauth": "x"},
            {"claudeAiOauth": {"accessToken": 1, "refreshToken": "r"}},
            {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "pad": "x" * 1_100_000}},
        ],
    )
    def test_a_malformed_rotation_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        _operator_home: Path,
        rotated: object,
    ) -> None:
        real = _operator_home / ".claude" / ".credentials.json"
        before = real.read_bytes()

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            target = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
            text = rotated if isinstance(rotated, str) else json.dumps(rotated)
            target.write_text(text, encoding="utf-8")
            fake.bind(kwargs["stdout"])
            return fake

        fake = _FakeProcess(returncode=0, output="ok\n")
        monkeypatch.setattr(claude.subprocess, "Popen", popen)
        monkeypatch.setattr(claude, "terminate_process_group", lambda process: process.kill())
        _run(tmp_path)
        assert real.read_bytes() == before


def test_the_workspace_none_docstring_says_no_built_in_tool() -> None:
    """Ticket 2901d5ba: the code passes --tools "" -- no built-in tool."""
    doc = claude.build_claude_command.__doc__ or ""
    assert "every tool" not in doc
    assert "no built-in tool" in doc


class _RecordedLifeline:
    def __init__(self, log: list[object]) -> None:
        log.append("start")
        self._log = log

    def attach(self, pgid: int) -> None:
        self._log.append(("watch", pgid))

    def release(self) -> None:
        self._log.append("release")


class TestTheGroupIsWatched:
    """Operator decision Q75=a: a watcher kills the provider's whole group if
    ha dies; the rail starts it on the provider's pid and releases it on every
    exit path."""

    def test_the_watcher_is_started_and_released(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log: list[object] = []
        monkeypatch.setattr(procgroup, "start_watcher", lambda: _RecordedLifeline(log))
        fake = _FakeProcess(returncode=0, output="ok\\n")
        _install(monkeypatch, fake)
        _run(tmp_path)
        assert log == ["start", ("watch", fake.pid), "release"]

    def test_the_watcher_is_released_on_interruption(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log: list[object] = []
        monkeypatch.setattr(procgroup, "start_watcher", lambda: _RecordedLifeline(log))

        class _Interrupted(_FakeProcess):
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                raise KeyboardInterrupt

        fake = _Interrupted(returncode=0)
        _install(monkeypatch, fake)
        with pytest.raises(KeyboardInterrupt):
            _run(tmp_path)
        assert log == ["start", ("watch", fake.pid), "release"]


class TestTheWriteBackIsSerialised:
    """Codex review of #206 (round 4): two concurrent claude runs that both
    rotated the login must not both see the real file "unchanged" and overwrite
    each other. The compare-and-replace holds an exclusive lock beside the file."""

    _HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("ok")
time.sleep(float(sys.argv[3]))
"""

    def test_the_write_back_waits_for_the_credentials_lock(
        self, tmp_path: Path, _operator_home: Path
    ) -> None:
        import subprocess
        import sys
        import time

        real = _operator_home / ".claude" / ".credentials.json"
        real_at_build = real.read_bytes()
        ephemeral = tmp_path / "ephemeral.json"
        rotated = {"accessToken": "at-2", "refreshToken": "rt-2"}
        ephemeral.write_text(json.dumps({"claudeAiOauth": rotated}))
        lock = claude.credentials_lock_path(real)
        assert lock.parent == real.parent
        ready = tmp_path / "ready"
        holder = subprocess.Popen([sys.executable, "-c", self._HOLD, str(lock), str(ready), "1.0"])
        try:
            while not ready.exists():
                time.sleep(0.02)
            start = time.monotonic()
            claude._persist_rotated_oauth(
                ephemeral=ephemeral,
                real=real,
                real_at_build=real_at_build,
                copied=_OAUTH,
                raw_log=tmp_path / "raw.log",
            )
            waited = time.monotonic() - start
        finally:
            holder.wait()
        assert waited >= 0.7, "the compare-and-replace ran without the lock"
        assert json.loads(real.read_text())["claudeAiOauth"] == rotated

    def test_a_second_concurrent_rotation_does_not_overwrite_the_first(
        self, tmp_path: Path, _operator_home: Path
    ) -> None:
        """Two runs copied the same file; the first write-back wins, the second
        sees the file changed and leaves it -- under the lock, in either order."""
        real = _operator_home / ".claude" / ".credentials.json"
        real_at_build = real.read_bytes()
        first, second = tmp_path / "first.json", tmp_path / "second.json"
        first.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1"}}))
        second.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2"}})
        )
        for ephemeral in (first, second):
            claude._persist_rotated_oauth(
                ephemeral=ephemeral,
                real=real,
                real_at_build=real_at_build,
                copied=_OAUTH,
                raw_log=tmp_path / "raw.log",
            )
        assert json.loads(real.read_text())["claudeAiOauth"]["accessToken"] == "a1"
        assert "changed meanwhile" in (tmp_path / "raw.log").read_text()
