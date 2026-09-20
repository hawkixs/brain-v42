"""The generic ``agy`` adapter: ephemeral HOME from a profile, guard proven
before launch, report extracted from the event stream.
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
from headless_agents.profile import CapabilityProfile, Credentials, McpServer, ToolGuard
from headless_agents.providers import agy
from headless_agents.spec import RunSpec

URL = "http://127.0.0.1:8765/mcp"
DENYING_GUARD = """#!/usr/bin/env bash
payload=$(cat)
case "$payload" in
  *call_mcp_tool*) printf '{"decision":"allow"}' ;;
  *) printf '{"decision":"deny"}' ;;
esac
"""


def _guard(tmp_path: Path, body: str = DENYING_GUARD) -> ToolGuard:
    path = tmp_path / "guard.sh"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return ToolGuard(path=path, hook_name="example-guard")


def _profile(tmp_path: Path, **overrides: object) -> CapabilityProfile:
    fields: dict[str, object] = {
        "mcp": McpServer(
            name="example",
            url=URL,
            bearer=SecretStr("scoped-token"),
            headers={"X-Agent": "example-run"},
            tools=("example_search",),
        ),
        "guard": _guard(tmp_path),
        "credentials": Credentials(paths=(".x/token",)),
    }
    fields.update(overrides)
    return CapabilityProfile(**fields)  # type: ignore[arg-type]


class TestBuildAgyCommand:
    def test_the_prompt_travels_as_the_print_argument(self) -> None:
        command = agy.build_agy_command(model="m", prompt="P", timeout_seconds=42.0)
        assert command == [
            "agy",
            "--print",
            "P",
            "--output-format",
            "stream-json",
            "--print-timeout",
            "42s",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model",
            "m",
        ]

    def test_a_blank_model_adds_no_flag(self) -> None:
        assert "--model" not in agy.build_agy_command(model=" ", prompt="P")

    def test_an_oversized_prompt_is_refused_before_execve(self) -> None:
        with pytest.raises(ValueError, match="argv"):
            agy.build_agy_command(model="m", prompt="x" * (agy.MAX_PROMPT_BYTES + 1))


class TestGuardDeniesMachineTools:
    def test_a_denying_guard_passes(self, tmp_path: Path) -> None:
        assert agy.guard_denies_machine_tools(_guard(tmp_path).path) is True

    def test_a_permissive_or_absent_guard_fails(self, tmp_path: Path) -> None:
        permissive = _guard(tmp_path, '#!/usr/bin/env bash\nprintf \'{"decision":"allow"}\'\n')
        assert agy.guard_denies_machine_tools(permissive.path) is False
        assert agy.guard_denies_machine_tools(tmp_path / "absent.sh") is False


def _events(*lines: dict[str, object]) -> str:
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def _mcp_step(state: str = "DONE", tool: str = "call_mcp_tool") -> dict[str, object]:
    return {"step_update": {"step_type": "tool", "state": state, "tool_name": tool}}


class TestToolCallCompleted:
    def test_only_a_done_mcp_step_counts(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_mcp_step("DONE", "run_command")), encoding="utf-8")
        assert agy.tool_call_completed(log) is False
        log.write_text(_events(_mcp_step("ERROR")), encoding="utf-8")
        assert agy.tool_call_completed(log) is False
        log.write_text(_events(_mcp_step()), encoding="utf-8")
        assert agy.tool_call_completed(log) is True


class TestExtractReport:
    def test_reads_the_last_result_response(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        log.write_text(
            _events(
                {"event": "result", "result": {"response": "first"}},
                {"event": "result", "result": {"response": "final"}},
            ),
            encoding="utf-8",
        )
        agy.extract_report(log, report)
        assert report.read_text(encoding="utf-8") == "final"

    def test_an_empty_or_absent_response_writes_nothing(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        log.write_text(_events({"event": "other"}), encoding="utf-8")
        agy.extract_report(log, report)
        assert not report.exists()


class _FakeProcess:
    def __init__(self, *, returncode: int, events: str = "", hang: bool = False) -> None:
        self._returncode = returncode
        self._events = events
        self._hang = hang
        self.returncode: int | None = None
        self.pid = 4242

    def bind(self, stream: object) -> None:
        self._stream = stream

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        # A hang writes what it had produced so far, THEN stalls: that is
        # what the runner sees on disk when its own deadline fires.
        self._stream.write(self._events)  # type: ignore[attr-defined]
        self._stream.flush()  # type: ignore[attr-defined]
        if self._hang:
            raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout or 0)
        self.returncode = self._returncode
        return None, None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def _install(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess, *, guard_ok: bool = True
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        # The HOME is destroyed with the run: snapshot it at launch.
        home = Path(str(kwargs["cwd"]))
        captured["home_files"] = {
            str(path.relative_to(home)): path.read_text(encoding="utf-8")
            for path in sorted(home.rglob("*"))
            if path.is_file()
        }
        captured["home_symlinks"] = {
            str(path.relative_to(home)) for path in home.rglob("*") if path.is_symlink()
        }
        fake.bind(kwargs["stdout"])
        return fake

    # The guard probe runs the real script through subprocess.run, which
    # itself calls Popen: stub the probe's verdict so the Popen fake only ever
    # sees the agent launch. The probe has its own tests above.
    monkeypatch.setattr(agy, "guard_denies_machine_tools", lambda path: guard_ok)
    monkeypatch.setattr(agy.subprocess, "Popen", popen)
    monkeypatch.setattr(agy, "terminate_process_group", lambda process: process.kill())
    return captured


def _logs(tmp_path: Path) -> dict[str, Path]:
    return {
        "events_log": tmp_path / "out" / "events.jsonl",
        "report_log": tmp_path / "out" / "report.log",
        "stderr_log": tmp_path / "out" / "stderr.log",
    }


def _run(tmp_path: Path, **overrides: object) -> int:
    real_home = tmp_path / "real-home"
    (real_home / ".x").mkdir(parents=True, exist_ok=True)
    (real_home / ".x" / "token").write_text("t", encoding="utf-8")
    kwargs: dict[str, object] = {
        "prompt": "PROMPT",
        "name": "example-run",
        "model": "m",
        "timeout_seconds": 5.0,
        "profile": _profile(tmp_path),
        "real_home": real_home,
        "environment": {"PATH": "/usr/bin", "LANG": "fr_FR.UTF-8", "SECRET": "x"},
        "ephemeral_root": tmp_path / "runtime",
        **_logs(tmp_path),
    }
    kwargs.update(overrides)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    return agy.run_agy(**kwargs)  # type: ignore[arg-type]


class TestRunAgy:
    def test_runs_in_an_ephemeral_home_built_from_the_profile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        events = _events(_mcp_step(), {"event": "result", "result": {"response": "REPORT"}})
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=events))

        assert _run(tmp_path) == 0

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        home = Path(kwargs["cwd"])
        assert home.name == "example-run"
        assert kwargs["env"] == {
            "HOME": str(home),
            "PATH": "/usr/bin",
            "LANG": "fr_FR.UTF-8",
            "TERM": "dumb",
        }
        assert kwargs["stdin"] is subprocess.DEVNULL
        files = captured["home_files"]
        assert isinstance(files, dict)
        config = json.loads(files[".gemini/config/mcp_config.json"])
        assert config["mcpServers"]["example"]["headers"]["Authorization"] == "Bearer scoped-token"
        assert ".gemini/config/hooks.json" in files
        assert ".x/token" in captured["home_symlinks"]  # type: ignore[operator]
        assert (tmp_path / "out" / "report.log").read_text(encoding="utf-8") == "REPORT"

    def test_the_home_is_destroyed_after_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        _run(tmp_path)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert not Path(kwargs["cwd"]).exists()

    def test_refuses_to_start_without_a_proven_guard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert _run(tmp_path, profile=_profile(tmp_path, guard=None)) == 1
        assert "guard" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

        captured = _install(monkeypatch, _FakeProcess(returncode=0), guard_ok=False)
        assert _run(tmp_path) == 1
        assert "command" not in captured

    def test_launch_failure_and_silent_failure_are_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def popen(command: list[str], **kwargs: object) -> None:
            raise OSError("no such binary")

        monkeypatch.setattr(agy, "guard_denies_machine_tools", lambda path: True)
        monkeypatch.setattr(agy.subprocess, "Popen", popen)
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=9, events=""))
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE

    def test_a_failure_after_a_completed_call_keeps_its_own_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=9, events=_events(_mcp_step())))
        assert _run(tmp_path) == 9

    def test_an_oversized_prompt_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert (
            _run(tmp_path, prompt="x" * (agy.MAX_PROMPT_BYTES + 1)) == PROVIDER_FALLBACK_EXIT_CODE
        )
        assert "command" not in captured

    def test_a_timeout_after_a_started_mcp_step_is_never_a_switchover(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An ``ACTIVE`` ``call_mcp_tool`` step precedes the call's execution
        (measured on the live stream of 2026-09-14): whatever its final state,
        the run may have written."""
        for state in ("ACTIVE", "DONE", "ERROR"):
            events = _events(_mcp_step(state))
            _install(monkeypatch, _FakeProcess(returncode=0, events=events, hang=True))
            assert _run(tmp_path) == TIMEOUT_EXIT_CODE, state
        _install(monkeypatch, _FakeProcess(returncode=124))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        with pytest.raises(ValueError, match="timeout"):
            _run(tmp_path, timeout_seconds=0)

    def test_a_timeout_without_a_started_mcp_step_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An empty stream, or a response that never reached an MCP step: no
        call was issued, so the run may be handed to the next link. A refused
        built-in tool step is not an MCP call either -- the guard stops it
        before anything can be written."""
        _install(monkeypatch, _FakeProcess(returncode=0, hang=True))
        assert _run(tmp_path) == TIMEOUT_REPLAYABLE_EXIT_CODE
        stderr = (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "deadline" in stderr and "no MCP tool step started" in stderr

        chatter = _events(
            {"step_update": {"step_type": "agent_response", "state": "ACTIVE"}},
            _mcp_step("ERROR", "run_command"),
        )
        _install(monkeypatch, _FakeProcess(returncode=0, events=chatter, hang=True))
        assert _run(tmp_path) == TIMEOUT_REPLAYABLE_EXIT_CODE


class TestRefusalsBeforeLaunch:
    def test_a_relative_guard_path_is_refused_even_if_the_probe_would_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The probe runs the script from the parent's cwd, agy resolves the hook
        # from the ephemeral HOME: a relative path can pass the first and name
        # nothing in the second.
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        relative = _profile(tmp_path, guard=ToolGuard(path=Path("guard.sh")))
        assert _run(tmp_path, profile=relative) == 1
        assert "absolute" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_a_server_without_a_bearer_value_is_refused_with_a_line_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        server = McpServer(name="example", url=URL, bearer=None, tools=("example_search",))
        assert _run(tmp_path, profile=_profile(tmp_path, mcp=server)) == 1
        assert "bearer" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_guard_proven_skips_the_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        probes: list[Path] = []
        _install(monkeypatch, _FakeProcess(returncode=0))
        monkeypatch.setattr(
            agy, "guard_denies_machine_tools", lambda path: probes.append(path) or True
        )
        assert _run(tmp_path, guard_proven=True) == 0
        assert probes == []

    def test_the_temp_prefix_is_the_callers_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert _run(tmp_path, temp_prefix="caller-") == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert Path(str(kwargs["cwd"])).parent.name.startswith("caller-")


class TestAgyProvider:
    def test_prepare_home_and_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        real_home = tmp_path / "rh"
        real_home.mkdir()
        provider = agy.AgyProvider(real_home=real_home, ephemeral_root=tmp_path / "root")
        (tmp_path / "root").mkdir()
        spec = RunSpec(
            prompt="P", name="seat-1", model="m", profile=_profile(tmp_path), **_logs(tmp_path)
        )
        home = provider.prepare_home(spec)
        assert home == tmp_path / "root" / "seat-1"
        assert (home / ".gemini" / "config" / "hooks.json").exists()

        calls: list[dict[str, object]] = []
        monkeypatch.setattr(agy, "run_agy", lambda **kwargs: calls.append(kwargs) or 0)
        result = provider.run(spec)
        assert result.provider == "agy" and result.exit_code == 0
        assert calls[0]["name"] == "seat-1" and calls[0]["real_home"] == real_home
