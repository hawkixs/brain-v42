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
from headless_agents.profile import CapabilityProfile, McpServer
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
