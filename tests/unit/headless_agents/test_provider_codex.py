"""The generic ``codex exec`` adapter: command from a profile, run with a
caller-supplied environment, exit codes the chain can read.
"""

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
from headless_agents.profile import CapabilityProfile, McpServer, Workspace
from headless_agents.providers import codex
from headless_agents.spec import RunSpec

URL = "http://127.0.0.1:8765/mcp"


def _server(**overrides: object) -> McpServer:
    fields: dict[str, object] = {
        "name": "example",
        "url": URL,
        "bearer": SecretStr("token-placeholder"),
        "bearer_env_var": "EXAMPLE_TOKEN",
        "headers": {"X-Agent": "example-run", "X-Profile": "native"},
        "tools": ("example_search", "example_get"),
    }
    fields.update(overrides)
    return McpServer(**fields)  # type: ignore[arg-type]


def _overrides(command: list[str]) -> list[str]:
    return [command[i + 1] for i, item in enumerate(command) if item == "-c"]


class TestBuildCodexCommand:
    def test_with_a_server_declares_it_exactly_once_in_declaration_order(
        self, tmp_path: Path
    ) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="high",
            report_log=tmp_path / "report.log",
            workspace=tmp_path,
            mcp=_server(),
        )
        assert command[:2] == ["codex", "exec"]
        assert "--ephemeral" in command and "--ignore-user-config" in command
        assert command[-3:] == ["--output-last-message", str(tmp_path / "report.log"), "-"]
        overrides = _overrides(command)
        server = [item for item in overrides if item.startswith("mcp_servers.example.")]
        assert server == [
            f'mcp_servers.example.url="{URL}"',
            'mcp_servers.example.bearer_token_env_var="EXAMPLE_TOKEN"',
            'mcp_servers.example.http_headers={"X-Agent"="example-run","X-Profile"="native"}',
            "mcp_servers.example.required=true",
            'mcp_servers.example.enabled_tools=["example_search","example_get"]',
            'mcp_servers.example.default_tools_approval_mode="approve"',
            "mcp_servers.example.startup_timeout_sec=15",
            "mcp_servers.example.tool_timeout_sec=180",
        ]
        assert 'model_reasoning_effort="high"' in overrides

    def test_without_a_server_declares_none(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="medium",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
        )
        assert not any(item.startswith("mcp_servers.") for item in _overrides(command))

    def test_refuses_a_blank_model_or_an_unknown_effort(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="model"):
            codex.build_codex_command(
                model=" ",
                reasoning_effort="medium",
                report_log=tmp_path / "r",
                workspace=tmp_path,
                mcp=None,
            )
        with pytest.raises(ValueError, match="reasoning"):
            codex.build_codex_command(
                model="m",
                reasoning_effort="extreme",
                report_log=tmp_path / "r",
                workspace=tmp_path,
                mcp=None,
            )

    def test_executable_is_the_callers(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="medium",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            executable="/opt/codex",
        )
        assert command[0] == "/opt/codex"


class TestBuildCodexCommandWorkspace:
    def test_no_workspace_is_unchanged(self, tmp_path: Path) -> None:
        base = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
        )
        assert (
            codex.build_codex_command(
                model="m",
                reasoning_effort="low",
                report_log=tmp_path / "r",
                workspace=tmp_path,
                mcp=None,
                workspace_mode=None,
            )
            == base
        )

    def test_read_only_enables_shell_inside_read_only_sandbox(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path),
        )
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "features.shell_tool=true" in _overrides(command)
        assert "features.shell_tool=false" not in _overrides(command)

    def test_write_without_shell(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True),
        )
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        assert "features.shell_tool=false" in _overrides(command)
        assert "project_doc_max_bytes=65536" in _overrides(command)

    def test_write_with_shell(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True, shell=True),
        )
        assert "features.shell_tool=true" in _overrides(command)

    def test_shell_tool_appears_exactly_once_in_every_mode(self, tmp_path: Path) -> None:
        """``-c`` is last-wins for codex (unmeasured): the disabled-feature loop
        and the read/write branch must never both emit ``features.shell_tool``."""
        modes: list[Workspace | None] = [
            None,
            Workspace(path=tmp_path),
            Workspace(path=tmp_path, write=True),
            Workspace(path=tmp_path, write=True, shell=True),
        ]
        for mode in modes:
            command = codex.build_codex_command(
                model="m",
                reasoning_effort="low",
                report_log=tmp_path / "r",
                workspace=tmp_path,
                mcp=None,
                workspace_mode=mode,
            )
            shell_flags = [
                item for item in _overrides(command) if item.startswith("features.shell_tool=")
            ]
            assert len(shell_flags) == 1, mode


class TestBuildCodexHome:
    def test_links_auth_only(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "auth.json").write_text("{}", encoding="utf-8")
        (real / "AGENTS.md").write_text("personal", encoding="utf-8")
        home = codex.build_codex_home(root=tmp_path / "eph", real_codex_home=real)
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"]
        assert (home / "auth.json").resolve() == real / "auth.json"
        assert home.stat().st_mode & 0o777 == 0o700


class TestWriteToolStarted:
    def test_write_tool_started(self, tmp_path: Path) -> None:
        log = tmp_path / "e.jsonl"
        log.write_text(
            '{"type":"item.started","item":{"type":"command_execution","status":"in_progress"}}\n'
        )
        assert codex.write_tool_started(log) is True
        log.write_text('{"type":"item.completed","item":{"type":"agent_message"}}\n')
        assert codex.write_tool_started(log) is False
        assert codex.write_tool_started(tmp_path / "absent") is True


def _events(*lines: dict[str, object]) -> str:
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def _completed_call(server: str) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {"type": "mcp_tool_call", "server": server, "status": "completed", "error": None},
    }


def _turn_completed() -> dict[str, object]:
    return {
        "type": "turn.completed",
        "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5},
    }


class TestToolCallCompleted:
    def test_true_only_for_a_completed_call_on_the_named_server(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_completed_call("other")), encoding="utf-8")
        assert codex.tool_call_completed(log, server="example") is False
        log.write_text(_events(_completed_call("example")), encoding="utf-8")
        assert codex.tool_call_completed(log, server="example") is True

    def test_absent_or_garbled_log_proves_nothing(self, tmp_path: Path) -> None:
        assert codex.tool_call_completed(tmp_path / "absent", server="example") is False
        log = tmp_path / "events.jsonl"
        log.write_text("not json\n", encoding="utf-8")
        assert codex.tool_call_completed(log, server="example") is False


class TestEventStreamError:
    def test_a_clean_stream_with_a_server_call_passes(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_completed_call("example"), _turn_completed()), encoding="utf-8")
        assert codex.event_stream_error(log, server="example") is None

    def test_without_a_server_no_call_is_required(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_turn_completed()), encoding="utf-8")
        assert codex.event_stream_error(log, server=None) is None

    def test_a_missing_call_on_the_named_server_is_an_error(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_turn_completed()), encoding="utf-8")
        error = codex.event_stream_error(log, server="example")
        assert error is not None and "example" in error

    @pytest.mark.parametrize(
        "lines, fragment",
        [
            ((), "no JSONL"),
            ((_completed_call("example"),), "turn.completed"),
            (({"type": "turn.failed"},), "terminal"),
            (
                (
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 1},
                    },
                ),
                "input_tokens",
            ),
        ],
    )
    def test_fail_closed_shapes(
        self, tmp_path: Path, lines: tuple[dict[str, object], ...], fragment: str
    ) -> None:
        log = tmp_path / "events.jsonl"
        if lines:
            log.write_text(_events(*lines), encoding="utf-8")
        error = codex.event_stream_error(log, server="example")
        assert error is not None and fragment in error


class TestToolCallStarted:
    """What the stream proves at the moment the runner's deadline fires.

    Fail-closed the other way round from ``tool_call_completed``: whatever
    cannot be read answers "a call may have started".
    """

    def test_an_empty_stream_or_a_turn_without_a_tool_item_proves_no_call(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text("", encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is False
        chatter = _events(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "x"}},
        )
        log.write_text(chatter, encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is False

    def test_a_tool_item_on_the_server_counts_in_any_state(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        started = {
            "type": "item.started",
            "item": {"type": "mcp_tool_call", "server": "example", "status": "in_progress"},
        }
        log.write_text(_events(started), encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is True
        log.write_text(_events(_completed_call("example")), encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is True

    def test_a_tool_item_on_another_server_does_not_count(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_completed_call("elsewhere")), encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is False

    def test_an_absent_or_truncated_stream_proves_nothing(self, tmp_path: Path) -> None:
        """A line cut by the kill (``{"type":"item.sta``) is exactly the moment a
        call may be leaving: it must read as "started", never as empty."""
        assert codex.tool_call_started(tmp_path / "absent.jsonl", server="example") is True
        log = tmp_path / "events.jsonl"
        log.write_text('{"type":"turn.started"}\n{"type":"item.sta', encoding="utf-8")
        assert codex.tool_call_started(log, server="example") is True


class _FakeProcess:
    """Stands in for ``subprocess.Popen``: writes what the test dictates."""

    def __init__(
        self,
        *,
        returncode: int,
        events: str = "",
        report: str = "",
        hang: bool = False,
    ) -> None:
        self._returncode = returncode
        self._events = events
        self._report = report
        self._hang = hang
        self.returncode: int | None = None
        self.pid = 4242
        self.stdout_stream = None

    def bind(self, *, events_stream: object, report_log: Path) -> None:
        self._events_stream = events_stream
        self._report_log = report_log

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        # A hang writes what it had produced so far, THEN stalls: that is
        # what the runner sees on disk when its own deadline fires.
        self._events_stream.write(self._events)  # type: ignore[attr-defined]
        self._events_stream.flush()  # type: ignore[attr-defined]
        if self._hang:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        if self._report:
            self._report_log.write_text(self._report, encoding="utf-8")
        self.returncode = self._returncode
        return None, None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def logs(tmp_path: Path) -> dict[str, Path]:
    return {
        "report_log": tmp_path / "out" / "report.log",
        "events_log": tmp_path / "out" / "events.jsonl",
        "stderr_log": tmp_path / "out" / "stderr.log",
    }


def _install(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess, report_log: Path
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        fake.bind(events_stream=kwargs["stdout"], report_log=report_log)
        return fake

    monkeypatch.setattr(codex.subprocess, "Popen", popen)
    monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
    return captured


def _run(logs: dict[str, Path], **overrides: object) -> int:
    kwargs: dict[str, object] = {
        "prompt": "PROMPT",
        "model": "m",
        "reasoning_effort": "medium",
        "timeout_seconds": 5.0,
        "mcp": _server(),
        "environment": {"PATH": "/usr/bin", "EXAMPLE_TOKEN": "token-placeholder"},
        "workspace": logs["report_log"].parent / "ws",
        **logs,
    }
    kwargs.update(overrides)
    return codex.run_codex(**kwargs)  # type: ignore[arg-type]


class TestRunCodex:
    def test_success_needs_a_report_and_a_valid_stream(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(
            returncode=0, events=_events(_completed_call("example"), _turn_completed()), report="R"
        )
        captured = _install(monkeypatch, fake, logs["report_log"])

        assert _run(logs) == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["env"] == {"PATH": "/usr/bin", "EXAMPLE_TOKEN": "token-placeholder"}
        assert kwargs["start_new_session"] is True

    def test_refuses_to_start_when_the_environment_lacks_the_bearer_variable(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0), logs["report_log"])
        assert _run(logs, environment={"PATH": "/usr/bin"}) == 1
        assert "EXAMPLE_TOKEN" in logs["stderr_log"].read_text(encoding="utf-8")
        assert "command" not in captured

    def test_no_server_needs_no_bearer(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        _install(monkeypatch, fake, logs["report_log"])
        assert _run(logs, mcp=None, environment={"PATH": "/usr/bin"}) == 0

    def test_a_launch_failure_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        def popen(command: list[str], **kwargs: object) -> None:
            raise OSError("no such binary")

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        assert _run(logs) == PROVIDER_FALLBACK_EXIT_CODE
        assert "unable to start" in logs["stderr_log"].read_text(encoding="utf-8")

    def test_a_failure_without_a_completed_call_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=7, events=""), logs["report_log"])
        assert _run(logs) == PROVIDER_FALLBACK_EXIT_CODE

    def test_a_failure_after_a_completed_call_keeps_its_own_code(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=7, events=_events(_completed_call("example")))
        _install(monkeypatch, fake, logs["report_log"])
        assert _run(logs) == 7

    def test_an_exit_without_a_report_or_with_an_invalid_stream_never_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=0, events=_events(_completed_call("example")), report="")
        _install(monkeypatch, fake, logs["report_log"])
        assert _run(logs) == 1
        assert "without a final report" in logs["stderr_log"].read_text(encoding="utf-8")

    def test_a_timeout_after_a_started_call_is_never_a_switchover(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        """``item.started`` precedes the call's execution (measured on the
        live stream, 2026-09-20): a call in flight is visible, and a stream
        that shows one may have written."""
        started = _events(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "server": "example",
                    "tool": "example_search",
                    "status": "in_progress",
                },
            }
        )
        _install(
            monkeypatch, _FakeProcess(returncode=0, events=started, hang=True), logs["report_log"]
        )
        assert _run(logs) == TIMEOUT_EXIT_CODE
        completed = _events(_completed_call("example"))
        _install(
            monkeypatch, _FakeProcess(returncode=0, events=completed, hang=True), logs["report_log"]
        )
        assert _run(logs) == TIMEOUT_EXIT_CODE

    def test_a_timeout_without_a_started_call_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        """An empty stream, or a turn that never reached a tool item: no call
        was issued to the server, so the run may be handed to the next link."""
        _install(monkeypatch, _FakeProcess(returncode=0, hang=True), logs["report_log"])
        assert _run(logs) == TIMEOUT_REPLAYABLE_EXIT_CODE
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "deadline" in stderr and "no tool call started" in stderr

        chatter = _events(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "hm"}},
        )
        _install(
            monkeypatch, _FakeProcess(returncode=0, events=chatter, hang=True), logs["report_log"]
        )
        assert _run(logs) == TIMEOUT_REPLAYABLE_EXIT_CODE

    def test_a_child_exiting_124_is_read_as_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=124), logs["report_log"])
        assert _run(logs) == TIMEOUT_EXIT_CODE

    def test_a_childs_own_fallback_code_after_a_completed_call_never_advances_a_chain(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        """Codex exiting 3 or 4 by itself after writing must be an ordinary
        failure, or the chain would replay a run that provably wrote."""
        for code in (PROVIDER_FALLBACK_EXIT_CODE, TIMEOUT_REPLAYABLE_EXIT_CODE):
            fake = _FakeProcess(returncode=code, events=_events(_completed_call("example")))
            _install(monkeypatch, fake, logs["report_log"])
            assert _run(logs) == 1, code

    def test_an_already_expired_deadline_is_a_timeout_and_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        """See the opencode twin: an exhausted budget is not a dead link."""
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        monkeypatch.setattr(codex.time, "monotonic", lambda: 1000.0)
        assert _run(logs, timeout_seconds=60.0, deadline=999.0) == TIMEOUT_EXIT_CODE
        assert "command" not in captured
        assert "deadline" in logs["stderr_log"].read_text(encoding="utf-8")

    def test_a_non_positive_timeout_is_refused_before_launch(self, logs: dict[str, Path]) -> None:
        with pytest.raises(ValueError, match="timeout"):
            _run(logs, timeout_seconds=0)

    def test_a_deadline_caps_the_timeout_handed_to_communicate(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        seen: list[float | None] = []
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        original = fake.communicate

        def communicate(
            input: str | None = None, timeout: float | None = None
        ) -> tuple[None, None]:
            seen.append(timeout)
            return original(input=input, timeout=timeout)

        fake.communicate = communicate  # type: ignore[method-assign]
        _install(monkeypatch, fake, logs["report_log"])
        monkeypatch.setattr(codex.time, "monotonic", lambda: 1000.0)
        assert _run(logs, mcp=None, timeout_seconds=60.0, deadline=1002.5) == 0
        assert seen == [2.5]


class TestRunCodexWorkspace:
    """``workspace_capability`` end to end: ephemeral ``CODEX_HOME``, the
    missing-auth refusal before spawn, and the write-taint rules that reuse
    ``write_tool_started`` where the MCP-server predicates have no server to
    read."""

    def _real_codex_home(self, tmp_path: Path, *, with_auth: bool) -> Path:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        if with_auth:
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
        return real_home

    def test_read_only_workspace_gets_an_ephemeral_codex_home(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """The ephemeral ``CODEX_HOME`` is torn down as soon as the run ends
        (see the sibling removal test below), so what it looked like WHILE
        the child ran has to be captured synchronously, inside the fake
        ``Popen`` call itself -- not re-read from disk afterwards."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured: dict[str, object] = {}

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            captured["command"] = command
            captured["kwargs"] = kwargs
            env = kwargs["env"]
            assert isinstance(env, dict)
            home = Path(env["CODEX_HOME"])
            captured["codex_home"] = str(home)
            captured["codex_home_mode"] = home.stat().st_mode & 0o777
            captured["codex_home_auth_target"] = (home / "auth.json").resolve()
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert captured["codex_home"] != str(real_home)
        assert captured["codex_home_mode"] == 0o700
        assert captured["codex_home_auth_target"] == real_home / "auth.json"
        assert kwargs["cwd"] == ws.resolve()
        command = captured["command"]
        assert isinstance(command, list)
        assert command[command.index("-C") + 1] == str(ws.resolve())

    def test_missing_real_auth_json_refuses_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path, with_auth=False)
        captured = _install(monkeypatch, _FakeProcess(returncode=0), logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == PROVIDER_FALLBACK_EXIT_CODE
        assert "command" not in captured
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "auth.json not found" in stderr and str(real_home) in stderr

    def test_write_mode_taint_after_a_started_write_keeps_the_childs_code(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        started_write = _events(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "status": "in_progress"},
            }
        )
        fake = _FakeProcess(returncode=3, events=started_write)
        _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws, write=True),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == 1

    def test_write_mode_timeout_after_a_started_write_is_never_replayable(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        started_write = _events(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "status": "in_progress"},
            }
        )
        fake = _FakeProcess(returncode=0, events=started_write, hang=True)
        _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws, write=True),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == TIMEOUT_EXIT_CODE

    def test_ephemeral_codex_home_is_removed_after_the_run(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """The ephemeral ``CODEX_HOME`` lives inside a context manager: it must
        be gone once ``run_codex`` returns, success or not."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=7, events="")
        captured = _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == PROVIDER_FALLBACK_EXIT_CODE
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert not Path(env["CODEX_HOME"]).exists()

    def test_write_mode_failure_without_a_write_event_stays_replayable(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """A write-mode workspace does not by itself taint a run: without a
        ``command_execution``/``file_change`` event, a failure is still
        provably a no-write and stays replayable elsewhere."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=3, events="")
        _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws, write=True),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == PROVIDER_FALLBACK_EXIT_CODE

    def test_read_only_workspace_timeout_without_a_write_event_stays_replayable(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """``workspace_write`` is ``False`` for a read-only workspace: the new
        write-taint branch of ``_deadline_exit_code`` must never fire for it,
        so a hang with no event at all stays the ordinary replayable 4."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=0, hang=True)
        _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == TIMEOUT_REPLAYABLE_EXIT_CODE


class TestPersistRotatedAuth:
    """Codex may refresh its OAuth token by an atomic replace (write temp +
    rename), which turns the ephemeral ``auth.json`` SYMLINK into a regular
    file holding the new token. That file must be rescued back to the real
    ``CODEX_HOME`` before the ephemeral home is removed, on every exit path."""

    def _real_codex_home(self, tmp_path: Path, *, content: str = "{}") -> Path:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        (real_home / "auth.json").write_text(content, encoding="utf-8")
        return real_home

    def test_a_rotated_regular_file_is_written_back_atomically(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            # Simulate codex's own atomic replace: unlink the symlink, write a
            # fresh regular file in its place -- exactly what a real refresh
            # (temp file + os.replace) leaves behind.
            auth.unlink()
            auth.write_text('{"rotated": true}', encoding="utf-8")
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == 0
        real_auth = real_home / "auth.json"
        assert real_auth.read_text(encoding="utf-8") == '{"rotated": true}'
        assert real_auth.stat().st_mode & 0o777 == 0o600

    def test_an_untouched_symlink_leaves_the_real_file_alone(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        real_auth = real_home / "auth.json"
        before_bytes = real_auth.read_bytes()
        before_mtime = real_auth.stat().st_mtime_ns
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        _install(monkeypatch, fake, logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == 0
        assert real_auth.read_bytes() == before_bytes
        assert real_auth.stat().st_mtime_ns == before_mtime

    def test_a_deleted_ephemeral_auth_leaves_the_real_file_alone(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        real_auth = real_home / "auth.json"
        before_bytes = real_auth.read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            (Path(env["CODEX_HOME"]) / "auth.json").unlink()
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
        )

        assert code == 0
        assert real_auth.read_bytes() == before_bytes


class TestCallerWording:
    def test_the_missing_call_message_is_the_callers_when_given(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        _install(monkeypatch, fake, logs["report_log"])
        # No completed call on the server: the run failed AND proved it wrote
        # nothing, so the chain may replay it elsewhere.
        assert (
            _run(logs, missing_call_message="no completed Brain MCP tool call")
            == PROVIDER_FALLBACK_EXIT_CODE
        )
        assert "no completed Brain MCP tool call" in logs["stderr_log"].read_text(encoding="utf-8")

    def test_the_temp_prefix_is_the_callers_when_given(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        assert _run(logs, mcp=None, workspace=None, temp_prefix="caller-prefix-") == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert Path(str(kwargs["cwd"])).name.startswith("caller-prefix-")


class TestCodexProvider:
    def test_build_command_reads_the_profile(self, tmp_path: Path) -> None:
        spec = RunSpec(
            prompt="P",
            model="m",
            profile=CapabilityProfile(mcp=_server()),
            report_log=tmp_path / "r",
            workspace=tmp_path,
        )
        command = codex.CodexProvider().build_command(spec)
        assert any(item.startswith("mcp_servers.example.") for item in _overrides(command))

    def test_run_forwards_and_reports(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(codex, "run_codex", lambda **kwargs: calls.append(kwargs) or 3)
        spec = RunSpec(
            prompt="P",
            model="m",
            profile=CapabilityProfile(mcp=_server()),
            environment={"EXAMPLE_TOKEN": "t"},
            **logs,
        )
        result = codex.CodexProvider().run(spec)
        assert result.exit_code == 3
        assert result.provider == "codex"
        assert result.model == "m"
        assert result.tool_call_completed is False
        assert calls[0]["mcp"] == _server()
        assert calls[0]["environment"] == {"EXAMPLE_TOKEN": "t"}

    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_codex(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("the answer\n", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex, "run_codex", fake_run_codex)
        run_dir = tmp_path / "runs" / "r1"
        result = codex.CodexProvider().run(RunSpec(prompt="P", model="m", run_dir=run_dir))
        assert result.text == "the answer\n"
        assert result.run_id == "r1"
        assert result.report_path == run_dir / "report.log"
        assert result.events_log == run_dir / "events.jsonl"
        assert result.stderr_log == run_dir / "stderr.log"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()

    def test_an_explicit_log_path_wins_and_result_json_still_lands_in_run_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_codex(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(codex, "run_codex", fake_run_codex)
        explicit = tmp_path / "elsewhere" / "final.txt"
        run_dir = tmp_path / "runs" / "r2"
        result = codex.CodexProvider().run(
            RunSpec(prompt="P", model="m", run_dir=run_dir, report_log=explicit)
        )
        assert result.report_path == explicit
        assert result.text == "answer"
        assert result.events_log == run_dir / "events.jsonl"
        assert (run_dir / "result.json").is_file()


class TestCodexProviderWorkspace:
    def test_no_workspace_and_no_context_prompt_is_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(codex, "run_codex", lambda **kwargs: calls.append(kwargs) or 0)
        spec = RunSpec(prompt="do the thing", model="m", **logs)
        codex.CodexProvider().run(spec)
        assert calls[0]["prompt"] == "do the thing"
        assert calls[0]["workspace_capability"] is None

    def test_workspace_prepends_the_workspace_block(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(codex, "run_codex", lambda **kwargs: calls.append(kwargs) or 0)
        ws = tmp_path / "ws"
        ws.mkdir()
        workspace = Workspace(path=ws)
        spec = RunSpec(
            prompt="do the thing",
            model="m",
            profile=CapabilityProfile(workspace=workspace),
            **logs,
        )
        codex.CodexProvider().run(spec)
        prompt = calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert prompt.startswith(f'<workspace path="{ws}"')
        assert "do the thing" in prompt
        assert calls[0]["workspace_capability"] == workspace
        assert calls[0]["workspace"] is None

    def test_build_command_previews_the_workspace_sandbox(self, tmp_path: Path) -> None:
        """``build_command`` is the dry-run preview: it must show the same
        ``--sandbox``/``-C`` ``run`` actually launches with."""
        ws = tmp_path / "ws"
        ws.mkdir()
        workspace = Workspace(path=ws, write=True)
        spec = RunSpec(
            prompt="p",
            model="m",
            profile=CapabilityProfile(workspace=workspace),
            report_log=tmp_path / "r",
        )
        command = codex.CodexProvider().build_command(spec)
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        assert command[command.index("-C") + 1] == str(ws)

    def test_run_result_carries_workspace_and_context(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        monkeypatch.setattr(codex, "run_codex", lambda **kwargs: 0)
        ws = tmp_path / "ws"
        ws.mkdir()
        workspace = Workspace(path=ws)
        spec = RunSpec(
            prompt="p",
            model="m",
            profile=CapabilityProfile(workspace=workspace),
            **logs,
        )
        result = codex.CodexProvider().run(spec)
        assert result.workspace == {"path": str(ws), "write": False, "shell": False}
        assert result.context is None
