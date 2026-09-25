"""The generic ``codex exec`` adapter: command from a profile, run with a
caller-supplied environment, exit codes the chain can read.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from headless_agents import procgroup
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


@pytest.fixture(autouse=True)
def _operator_codex_login(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every codex run needs a login now (operator decision Q80 = a): the tests
    get a fake one under a fake HOME, never the operator's real ~/.codex."""
    home = tmp_path_factory.mktemp("operator-home")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(
        '{"tokens": {"account_id": "acct-1", "access_token": "t"}}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


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

    def test_write_without_shell_flag_still_enables_the_shell_tool(self, tmp_path: Path) -> None:
        """Operator decision 2026-09-24: codex's shell runs inside its OWN OS
        sandbox (writes confined to the writable roots, network off), so
        ``Workspace.shell`` no longer changes codex's command -- unlike before
        0.4.0 hardening, where ``shell=False`` in a writable workspace turned
        the shell tool off and left codex with no way to read at all."""
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True),
        )
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        assert "features.shell_tool=true" in _overrides(command)
        assert "features.shell_tool=false" not in _overrides(command)
        assert "project_doc_max_bytes=0" in _overrides(command)

    def test_write_with_shell_is_byte_identical_to_write_without_it(self, tmp_path: Path) -> None:
        """``Workspace.shell`` is a no-op for codex now: both values of the
        flag produce the exact same command."""
        without = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True),
        )
        with_shell = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True, shell=True),
        )
        assert with_shell == without

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
        env = kwargs["env"]
        assert isinstance(env, dict)
        # Q80 = a: the run's own CODEX_HOME is added, nothing else.
        assert {k: v for k, v in env.items() if k != "CODEX_HOME"} == {
            "PATH": "/usr/bin",
            "EXAMPLE_TOKEN": "token-placeholder",
        }
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

    def test_relative_codex_home_refuses_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 7: a relative ``CODEX_HOME`` is refused before any spawn,
        the same way a missing ``auth.json`` is -- resolving it against an
        unstated cwd would be ambiguous, not a directory this runtime chose."""
        captured = _install(monkeypatch, _FakeProcess(returncode=0), logs["report_log"])
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": "relative/codex/home"},
        )

        assert code == PROVIDER_FALLBACK_EXIT_CODE
        assert "command" not in captured
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "CODEX_HOME must be an absolute path" in stderr

    def _fake_operator_pwd(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        """Round 3, finding 1: the fallback root must come from the OS user
        database, never from any environment variable -- so tests point
        ``pwd.getpwuid`` itself at a fake operator home, rather than setting
        ``HOME`` (which no longer has any effect on this choice)."""
        monkeypatch.setattr(
            codex.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_dir=str(home))
        )

    def _neutralize_conventional_tmp(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Point BOTH hardcoded-tmp unsafe roots (the literal ``/tmp`` and
        ``tempfile.gettempdir()``) at a fake, disjoint directory.

        pytest's own ``tmp_path`` fixture lives under the REAL ``/tmp`` on
        this machine: without this, every "here is a SAFE root" fixture
        built under ``tmp_path`` would be judged unsafe by the literal
        ``/tmp`` check alone, with no way to construct a counter-example."""
        fake_system_tmp = tmp_path / "system-tmp"
        fake_system_tmp.mkdir()
        monkeypatch.setattr(codex, "_CONVENTIONAL_TMP_ROOT", fake_system_tmp)
        monkeypatch.setattr(codex.tempfile, "gettempdir", lambda: str(fake_system_tmp))
        return fake_system_tmp

    def test_ephemeral_home_root_never_sits_under_tmp(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 2 finding 2 (as tightened by round 3 finding 1): with no
        ``XDG_RUNTIME_DIR``, the ephemeral home must land under the
        operator's OWN (pwd-derived) ``~/.cache/headless-agents/codex-homes/``
        -- never under ``tempfile.gettempdir()``, which the
        ``workspace-write`` sandbox can itself write into."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        operator_home = tmp_path / "operator-home"
        operator_home.mkdir()
        self._fake_operator_pwd(monkeypatch, operator_home)
        fake_system_tmp = self._neutralize_conventional_tmp(monkeypatch, tmp_path)
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
        env = kwargs["env"]
        assert isinstance(env, dict)
        codex_home = Path(env["CODEX_HOME"])
        cache_root = (operator_home / ".cache" / "headless-agents" / "codex-homes").resolve()
        assert cache_root in codex_home.resolve().parents
        resolved_system_tmp = fake_system_tmp.resolve()
        assert resolved_system_tmp != codex_home.resolve()
        assert resolved_system_tmp not in codex_home.resolve().parents
        assert cache_root.stat().st_mode & 0o777 == 0o700

    def test_fallback_home_root_ignores_a_sandboxed_home_equal_to_tmpdir(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 1(a): ``sandbox.sandbox_environment`` sets a
        sandboxed child's ``HOME`` equal to its ``TMPDIR`` -- a fallback
        built from the child's ``HOME`` would then sit right back inside the
        sandbox. The fallback must come from the operator's real pwd entry
        and land OUTSIDE that ``TMPDIR``, regardless of what ``HOME`` says."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        operator_home = tmp_path / "operator-home"
        operator_home.mkdir()
        self._fake_operator_pwd(monkeypatch, operator_home)
        self._neutralize_conventional_tmp(monkeypatch, tmp_path)
        sandbox_home = tmp_path / "sandbox-home"
        sandbox_home.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={
                "PATH": "/usr/bin",
                "CODEX_HOME": str(real_home),
                # The exact sandbox_environment() shape: HOME == TMPDIR.
                "HOME": str(sandbox_home),
                "TMPDIR": str(sandbox_home),
            },
        )

        assert code == 0
        env = captured["kwargs"]["env"]  # type: ignore[index]
        assert isinstance(env, dict)
        codex_home = Path(env["CODEX_HOME"])
        cache_root = (operator_home / ".cache" / "headless-agents" / "codex-homes").resolve()
        assert cache_root in codex_home.resolve().parents
        resolved_sandbox_home = sandbox_home.resolve()
        assert resolved_sandbox_home != codex_home.resolve()
        assert resolved_sandbox_home not in codex_home.resolve().parents

    def test_xdg_runtime_dir_under_tmpdir_is_rejected_fallback_used(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 1(b): an ``XDG_RUNTIME_DIR`` that itself sits
        under ``TMPDIR`` is not a safe candidate -- the fallback root is
        used instead, never the tmpfs-but-still-sandboxed one."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        operator_home = tmp_path / "operator-home"
        operator_home.mkdir()
        self._fake_operator_pwd(monkeypatch, operator_home)
        self._neutralize_conventional_tmp(monkeypatch, tmp_path)
        tmpdir_value = tmp_path / "tmpdir-root"
        tmpdir_value.mkdir()
        xdg_runtime = tmpdir_value / "user-1000"
        xdg_runtime.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={
                "PATH": "/usr/bin",
                "CODEX_HOME": str(real_home),
                "TMPDIR": str(tmpdir_value),
                "XDG_RUNTIME_DIR": str(xdg_runtime),
            },
        )

        assert code == 0
        env = captured["kwargs"]["env"]  # type: ignore[index]
        assert isinstance(env, dict)
        codex_home = Path(env["CODEX_HOME"])
        cache_root = (operator_home / ".cache" / "headless-agents" / "codex-homes").resolve()
        assert cache_root in codex_home.resolve().parents
        assert tmpdir_value.resolve() not in codex_home.resolve().parents

    def test_every_candidate_unsafe_refuses_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 1(c): when even the pwd-derived fallback sits
        under an unsafe root (here, ``TMPDIR``), the run fails closed --
        exit 3, no spawn -- rather than build a reachable ``CODEX_HOME``."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        captured = _install(monkeypatch, _FakeProcess(returncode=0), logs["report_log"])
        tmpdir_value = tmp_path / "tmpdir-root"
        tmpdir_value.mkdir()
        operator_home = tmpdir_value / "operator-home-inside-tmp"
        operator_home.mkdir()
        self._fake_operator_pwd(monkeypatch, operator_home)
        ws = tmp_path / "ws"
        ws.mkdir()

        code = _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={
                "PATH": "/usr/bin",
                "CODEX_HOME": str(real_home),
                "TMPDIR": str(tmpdir_value),
            },
        )

        assert code == PROVIDER_FALLBACK_EXIT_CODE
        assert "command" not in captured
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "no codex home root outside the sandbox's writable roots" in stderr

    def test_no_passwd_entry_refuses_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """A uid with no passwd entry (a container's arbitrary uid) has no
        operator home to fall back on: exit 3, no spawn, not a traceback."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        captured = _install(monkeypatch, _FakeProcess(returncode=0), logs["report_log"])

        def no_entry(uid: int) -> object:
            raise KeyError(f"getpwuid(): uid not found: {uid}")

        monkeypatch.setattr(codex.pwd, "getpwuid", no_entry)
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
        assert "no codex home root outside the sandbox's writable roots" in stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file anyway")
    def test_unreadable_real_auth_disables_rescue_but_the_run_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 5: the real file existing (checked before spawn)
        does not mean it is READABLE. A permission error taking the
        build-time snapshot must not break ``run_codex`` -- the rescue is
        simply unavailable for this run, logged once, and the run proceeds."""
        real_home = self._real_codex_home(tmp_path, with_auth=True)
        real_auth = real_home / "auth.json"
        real_auth.chmod(0o000)
        try:
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
        finally:
            real_auth.chmod(0o600)

        assert code == 0
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "rescue disabled for this run" in stderr
        assert "PermissionError" in stderr

    def test_non_utf8_real_auth_disables_rescue_but_the_run_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 5: a real ``auth.json`` that is not valid UTF-8
        (``_account_id`` alone only ever caught ``JSONDecodeError``) must
        not raise out of ``run_codex`` either."""
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        (real_home / "auth.json").write_bytes(b"\xff\xfe not valid utf-8")
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
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "rescue disabled for this run" in stderr
        assert "UnicodeDecodeError" in stderr


#: A well-formed auth.json shape: the account-id match is the write-back's
#: central guard, so every legitimate-rotation test needs one that parses.
def _auth_json(*, account_id: str = "acct-1", access_token: str = "old") -> str:
    return json.dumps({"tokens": {"account_id": account_id, "access_token": access_token}})


class TestPersistRotatedAuth:
    """Codex may refresh its OAuth token by an atomic replace (write temp +
    rename), which turns the ephemeral ``auth.json`` SYMLINK into a regular
    file holding the new token. That file must be rescued back to the real
    ``CODEX_HOME`` before the ephemeral home is removed, on every exit path
    -- but ONLY when it is provably codex's own rotation, never anything a
    sandboxed agent could have forged in its place."""

    def _real_codex_home(self, tmp_path: Path, *, content: str = _auth_json()) -> Path:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        (real_home / "auth.json").write_text(content, encoding="utf-8")
        return real_home

    def _popen_replacing_ephemeral_auth(
        self, fake: _FakeProcess, logs: dict[str, Path], *, new_content: str
    ) -> Any:
        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            # Simulate codex's own atomic replace: unlink the symlink, write a
            # fresh regular file in its place -- exactly what a real refresh
            # (temp file + os.replace) leaves behind.
            auth.unlink()
            auth.write_text(new_content, encoding="utf-8")
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        return popen

    def test_a_rotated_regular_file_is_written_back_atomically(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        rotated = _auth_json(access_token="new")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=rotated),
        )
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
        assert real_auth.read_text(encoding="utf-8") == rotated
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

    def test_write_back_never_changes_the_runs_exit_code_on_os_error(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 1: a filesystem failure during the write-back must never
        raise out of the ``finally`` and replace the exit code ``_run``
        already decided -- it is logged as one non-secret stderr line
        instead."""
        real_home = self._real_codex_home(tmp_path)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        rotated = _auth_json(access_token="new")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=rotated),
        )
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())

        def raising_replace(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(codex.os, "replace", raising_replace)
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
        assert (real_home / "auth.json").read_text(encoding="utf-8") != rotated
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "codex auth.json rotation not persisted" in stderr
        assert rotated not in stderr and "new" not in stderr

    def test_a_forged_account_id_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 3: a ``workspace-write`` sandbox can replace the ephemeral
        ``auth.json`` with bytes of its OWN choosing, not just relay codex's
        own rotation. A different ``account_id`` is the sandbox's content,
        not codex's -- refused, real file untouched."""
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        forged = _auth_json(account_id="attacker-acct", access_token="forged")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=forged),
        )
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
        assert (real_home / "auth.json").read_bytes() == real_before
        assert "not persisted" in logs["stderr_log"].read_text(encoding="utf-8")

    def test_non_json_content_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content="not json at all"),
        )
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
        assert (real_home / "auth.json").read_bytes() == real_before

    def test_an_oversize_candidate_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        oversized = json.dumps(
            {"tokens": {"account_id": "acct-1", "padding": "x" * (codex._MAX_ROTATED_AUTH_BYTES)}}
        )
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=oversized),
        )
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
        assert (real_home / "auth.json").read_bytes() == real_before

    def test_a_deeply_nested_candidate_never_changes_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 2: ``json.loads`` on sandbox-chosen bytes can
        raise ``RecursionError`` -- a ``RuntimeError``, not a ``ValueError``
        -- which the round-2 wrapper's narrower ``except (OSError,
        ValueError)`` would have let escape the ``finally`` and replace the
        exit code ``_run`` already decided."""
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        # Comfortably past Python's default recursion limit (1000), and
        # comfortably under the 65536-byte size cap so the RECURSION path is
        # what fires, not the size check.
        deeply_nested = "[" * 50_000
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=deeply_nested),
        )
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
        assert (real_home / "auth.json").read_bytes() == real_before
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "codex auth.json rotation not persisted: RecursionError" in stderr

    def test_a_fifo_at_the_ephemeral_auth_does_not_hang(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 3 finding 3: a FIFO swapped in for the ephemeral
        ``auth.json`` must not block the ``open()`` inside the ``finally``
        (a read-only open of a FIFO with no writer blocks forever without
        ``O_NONBLOCK``). Run the whole thing on a background thread with a
        join timeout as the test's own safety net against a real hang."""
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            auth.unlink()
            os.mkfifo(auth)
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        ws = tmp_path / "ws"
        ws.mkdir()

        outcome: dict[str, int] = {}

        def target() -> None:
            outcome["code"] = _run(
                logs,
                mcp=None,
                workspace=None,
                workspace_capability=Workspace(path=ws),
                environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home)},
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive(), "run_codex hung reading a FIFO auth.json"
        assert outcome.get("code") == 0
        assert (real_home / "auth.json").read_bytes() == real_before

    def test_a_symlink_swapped_in_for_a_symlink_is_never_followed(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 3: ``O_NOFOLLOW`` refuses the read even when the ephemeral
        ``auth.json`` is (still, or again) a symlink -- this time pointed at
        an attacker-controlled file elsewhere, not the real one."""
        real_home = self._real_codex_home(tmp_path)
        real_before = (real_home / "auth.json").read_bytes()
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(_auth_json(access_token="via-symlink"), encoding="utf-8")
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            auth.unlink()
            auth.symlink_to(elsewhere)
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
        assert (real_home / "auth.json").read_bytes() == real_before

    def test_compare_and_swap_refuses_a_real_file_that_moved_on(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 4: between the digest snapshot (home build time) and the
        write-back (run end), something else changed the real file -- an
        operator re-login, or another run's own rotation. The write-back
        must skip rather than clobber it, even though the candidate is
        itself a legitimately-shaped rotation."""
        real_home = self._real_codex_home(tmp_path)
        real_auth = real_home / "auth.json"
        rotated = _auth_json(access_token="new")
        concurrent = _auth_json(access_token="concurrent-login")
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            auth.unlink()
            auth.write_text(rotated, encoding="utf-8")
            # Something else -- another process -- rewrote the REAL file
            # while this run was in flight.
            real_auth.write_text(concurrent, encoding="utf-8")
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
        # The concurrent write wins: this run's own rotation is skipped, not
        # clobbering what the other process just wrote.
        assert real_auth.read_text(encoding="utf-8") == concurrent
        # Round 3, finding 2: the log carries the exception CLASS NAME only,
        # never its message -- "changed since ..." is gone from the log.
        stderr = logs["stderr_log"].read_text(encoding="utf-8")
        assert "codex auth.json rotation not persisted: ValueError" in stderr
        assert "changed since" not in stderr

    def test_a_symlinked_real_auth_survives_with_its_target_updated(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Finding 5: a dotfile manager may symlink the real ``auth.json``
        elsewhere. The write-back replaces the TARGET the symlink points at,
        so the symlink itself survives -- never a plain-file auth.json where
        a symlink used to be."""
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        vault_auth = vault / "auth.json"
        vault_auth.write_text(_auth_json(access_token="vault-old"), encoding="utf-8")
        (real_home / "auth.json").symlink_to(vault_auth)
        rotated = _auth_json(access_token="vault-new")
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=rotated),
        )
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
        assert (real_home / "auth.json").is_symlink()
        assert os.readlink(real_home / "auth.json") == str(vault_auth)
        assert vault_auth.read_text(encoding="utf-8") == rotated

    def test_the_temp_file_and_directory_are_flushed_and_fsynced_before_and_after_replace(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Round 2 finding 6 (the temp file, before ``os.replace``) and round
        3 finding 7 (the real file's parent directory, after ``os.replace``,
        so the renamed directory entry itself survives a crash): mechanical
        check that both get exactly one ``os.fsync`` call each."""
        real_home = self._real_codex_home(tmp_path)
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        rotated = _auth_json(access_token="new")
        monkeypatch.setattr(
            codex.subprocess,
            "Popen",
            self._popen_replacing_ephemeral_auth(fake, logs, new_content=rotated),
        )
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        fsync_calls: list[int] = []
        original_fsync = codex.os.fsync
        monkeypatch.setattr(
            codex.os, "fsync", lambda fd: (fsync_calls.append(fd), original_fsync(fd))[1]
        )
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
        assert (real_home / "auth.json").read_text(encoding="utf-8") == rotated
        assert len(fsync_calls) == 2


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

    def test_writable_preamble_mentions_the_shell_even_without_the_shell_flag(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        """Operator decision 2026-09-24: codex's shell tool is on in every
        writable workspace, ``Workspace.shell`` or not -- the preamble must
        say so instead of describing a read tool the model does not have."""
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(codex, "run_codex", lambda **kwargs: calls.append(kwargs) or 0)
        ws = tmp_path / "ws"
        ws.mkdir()
        workspace = Workspace(path=ws, write=True)
        spec = RunSpec(
            prompt="do the thing",
            model="m",
            profile=CapabilityProfile(workspace=workspace),
            **logs,
        )
        codex.CodexProvider().run(spec)
        prompt = calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "apply_patch" in prompt
        assert "shell" in prompt

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


class TestWriteRunsCloseTheTmpRoots:
    """Spec 0.5.0 §3.8.0: a ``workspace-write`` sandbox treats ``/tmp`` and
    ``$TMPDIR`` as writable roots besides the workspace -- a second repository
    placed there could be written. Write runs close both, and the run's
    ``TMPDIR`` points at a per-run scratch directory holding no repository."""

    def test_a_write_run_excludes_both_tmp_roots(self, tmp_path: Path) -> None:
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=Workspace(path=tmp_path, write=True),
        )
        assert "sandbox_workspace_write.exclude_slash_tmp=true" in _overrides(command)
        assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in _overrides(command)

    @pytest.mark.parametrize("workspace", ["read", "none"])
    def test_other_runs_are_unchanged(self, tmp_path: Path, workspace: str) -> None:
        mode = Workspace(path=tmp_path) if workspace == "read" else None
        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "r",
            workspace=tmp_path,
            mcp=None,
            workspace_mode=mode,
        )
        assert not [item for item in _overrides(command) if "sandbox_workspace_write" in item]

    def test_a_write_run_gets_a_scratch_tmpdir(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        (real_home / "auth.json").write_text(_auth_json(), encoding="utf-8")
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        seen: dict[str, object] = {}

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            scratch = Path(env["TMPDIR"])
            seen["tmpdir"] = scratch
            seen["existed"] = scratch.is_dir()
            seen["empty"] = not any(scratch.iterdir())
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
            workspace_capability=Workspace(path=ws, write=True),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home), "TMPDIR": "/tmp"},
        )

        assert code == 0
        scratch = seen["tmpdir"]
        assert isinstance(scratch, Path)
        assert seen["existed"] and seen["empty"]
        assert scratch != Path("/tmp")
        assert not scratch.is_relative_to(ws)
        assert not scratch.exists(), "the scratch TMPDIR is removed after the run"

    def test_a_read_only_run_keeps_its_tmpdir(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        (real_home / "auth.json").write_text(_auth_json(), encoding="utf-8")
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        seen: dict[str, object] = {}

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen["tmpdir"] = env.get("TMPDIR")
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        ws = tmp_path / "ws"
        ws.mkdir()

        _run(
            logs,
            mcp=None,
            workspace=None,
            workspace_capability=Workspace(path=ws),
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real_home), "TMPDIR": "/x"},
        )

        assert seen["tmpdir"] == "/x"


class TestTheProviderDiesWithHa:
    """Spec 0.5.0 §3.8.2 (see the claude rail's test of the same name)."""

    def test_the_child_is_started_with_the_death_signal_preexec(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        captured = _install(monkeypatch, fake, logs["report_log"])
        _run(logs)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert callable(kwargs["preexec_fn"])

    def test_an_interrupted_wait_kills_the_group_and_propagates(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]
    ) -> None:
        killed: list[object] = []

        class _Interrupted(_FakeProcess):
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                raise KeyboardInterrupt

        fake = _Interrupted(returncode=0, events="", report="")
        _install(monkeypatch, fake, logs["report_log"])
        monkeypatch.setattr(codex, "terminate_process_group", killed.append)
        with pytest.raises(KeyboardInterrupt):
            _run(logs)
        assert killed == [fake]


class _RecordedLifeline:
    def __init__(self, log: list[object]) -> None:
        log.append("start")
        self._log = log

    def child_attach(self, preexec_fn: object) -> object:
        """The provider's child names its own group to the watcher before exec."""
        self._log.append("child_attach")
        return preexec_fn

    def release(self) -> None:
        self._log.append("release")


class TestTheGroupIsWatched:
    """Operator decision Q75=a: a watcher kills the provider's whole group if
    ha dies; the rail starts it on the provider's pid and releases it on every
    exit path."""

    def test_the_watcher_is_started_and_released(self, monkeypatch, logs) -> None:  # type: ignore[no-untyped-def]
        log: list[object] = []
        monkeypatch.setattr(procgroup, "start_watcher", lambda: _RecordedLifeline(log))
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        _install(monkeypatch, fake, logs["report_log"])
        _run(logs)
        assert log == ["start", "child_attach", "release"]

    def test_the_watcher_is_released_on_interruption(self, monkeypatch, logs) -> None:  # type: ignore[no-untyped-def]
        log: list[object] = []
        monkeypatch.setattr(procgroup, "start_watcher", lambda: _RecordedLifeline(log))

        class _Interrupted(_FakeProcess):
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                raise KeyboardInterrupt

        fake = _Interrupted(returncode=0, events="", report="")
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        _install(monkeypatch, fake, logs["report_log"])
        with pytest.raises(KeyboardInterrupt):
            _run(logs)
        assert log == ["start", "child_attach", "release"]


class TestEveryRunGetsAnEphemeralCodexHome:
    """Operator decision Q80=a (spec 0.5.0 §3.8.0): measured by the live
    isolation proof on codex 0.156.0, a run without a workspace capability used
    the real CODEX_HOME, and --ignore-user-config did not stop codex loading the
    operator's ~/.codex/AGENTS.md. Every run now gets the ephemeral CODEX_HOME
    workspace runs already had: a private directory holding only auth.json."""

    @staticmethod
    def _real_home(tmp_path: Path) -> Path:
        real = tmp_path / "real-codex-home"
        real.mkdir()
        (real / "auth.json").write_text(_auth_json(), encoding="utf-8")
        (real / "AGENTS.md").write_text("operator instructions\n", encoding="utf-8")
        return real

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path]) -> dict[str, object]:
        fake = _FakeProcess(returncode=0, events=_events(_turn_completed()), report="R")
        seen: dict[str, object] = {}

        def popen(command: list[str], **kwargs: object) -> _FakeProcess:
            env = kwargs.get("env")
            assert isinstance(env, dict), "a run must carry its CODEX_HOME in env"
            home = Path(env["CODEX_HOME"])
            seen["home"] = home
            seen["entries"] = sorted(p.name for p in home.iterdir())
            seen["cwd"] = kwargs["cwd"]
            fake.bind(events_stream=kwargs["stdout"], report_log=logs["report_log"])
            return fake

        monkeypatch.setattr(codex.subprocess, "Popen", popen)
        monkeypatch.setattr(codex, "terminate_process_group", lambda process: process.kill())
        return seen

    def test_a_bare_run_gets_an_ephemeral_codex_home(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real = self._real_home(tmp_path)
        seen = self._capture(monkeypatch, logs)
        code = _run(
            logs,
            mcp=None,
            workspace=None,
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(real)},
        )
        assert code == 0
        home = seen["home"]
        assert isinstance(home, Path)
        assert home != real and not home.is_relative_to(real)
        assert seen["entries"] == ["auth.json"], "AGENTS.md and config never reach the run"
        assert not home.exists(), "torn down after the run"

    def test_a_legacy_workspace_run_gets_one_too(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real = self._real_home(tmp_path)
        seen = self._capture(monkeypatch, logs)
        ws = tmp_path / "legacy-ws"
        ws.mkdir()
        _run(
            logs, mcp=None, workspace=ws, environment={"PATH": "/usr/bin", "CODEX_HOME": str(real)}
        )
        assert seen["entries"] == ["auth.json"]
        assert seen["cwd"] == ws.resolve()

    def test_an_inherited_environment_is_copied_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        real = self._real_home(tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(real))
        monkeypatch.setenv("KEEP_ME", "yes")
        seen = self._capture(monkeypatch, logs)
        assert _run(logs, mcp=None, workspace=None, environment=None) == 0
        assert seen["entries"] == ["auth.json"]

    def test_a_bare_run_without_auth_is_provider_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, logs: dict[str, Path], tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty-codex-home"
        empty.mkdir()
        seen = self._capture(monkeypatch, logs)
        code = _run(
            logs,
            mcp=None,
            workspace=None,
            environment={"PATH": "/usr/bin", "CODEX_HOME": str(empty)},
        )
        assert code == PROVIDER_FALLBACK_EXIT_CODE
        assert "home" not in seen, "codex must not be started"


class TestTheAuthWriteBackIsSerialised:
    """Codex review of #207 (round 3): with an ephemeral CODEX_HOME on every
    run, two concurrent runs that both rotated could both see the real
    ``auth.json`` unchanged and overwrite each other. The compare-and-replace
    holds an exclusive lock beside the file, as claude's does (#206)."""

    _HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("ok")
time.sleep(float(sys.argv[3]))
"""

    def _setup(self, tmp_path: Path, rotated: str) -> tuple[Path, Path, str]:
        import hashlib

        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        real = real_home / "auth.json"
        real.write_text(_auth_json(), encoding="utf-8")
        ephemeral_home = tmp_path / "ephemeral"
        ephemeral_home.mkdir()
        (ephemeral_home / "auth.json").write_text(rotated, encoding="utf-8")
        return real, ephemeral_home, hashlib.sha256(real.read_bytes()).hexdigest()

    def test_the_write_back_waits_for_the_auth_lock(self, tmp_path: Path) -> None:
        import sys
        import time

        rotated = _auth_json(access_token="new")
        real, ephemeral_home, digest = self._setup(tmp_path, rotated)
        lock = codex.auth_lock_path(real)
        assert lock.parent == real.parent
        ready = tmp_path / "ready"
        holder = subprocess.Popen([sys.executable, "-c", self._HOLD, str(lock), str(ready), "1.0"])
        try:
            while not ready.exists():
                time.sleep(0.02)
            start = time.monotonic()
            codex._persist_rotated_auth_or_raise(
                ephemeral_home=ephemeral_home,
                real_auth_target=real,
                real_auth_digest_at_build=digest,
                real_account_id_at_build="acct-1",
            )
            waited = time.monotonic() - start
        finally:
            holder.wait()
        assert waited >= 0.7, "the compare-and-replace ran without the lock"
        assert real.read_text(encoding="utf-8") == rotated

    def test_a_busy_auth_lock_writes_nothing_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import time

        monkeypatch.setattr(codex, "_AUTH_LOCK_SECONDS", 0.2)
        real, ephemeral_home, digest = self._setup(tmp_path, _auth_json(access_token="new"))
        before = real.read_bytes()
        ready = tmp_path / "ready"
        holder = subprocess.Popen(
            [sys.executable, "-c", self._HOLD, str(codex.auth_lock_path(real)), str(ready), "2.0"]
        )
        try:
            while not ready.exists():
                time.sleep(0.02)
            with pytest.raises(TimeoutError):
                codex._persist_rotated_auth_or_raise(
                    ephemeral_home=ephemeral_home,
                    real_auth_target=real,
                    real_auth_digest_at_build=digest,
                    real_account_id_at_build="acct-1",
                )
        finally:
            holder.kill()
            holder.wait()
        assert real.read_bytes() == before
