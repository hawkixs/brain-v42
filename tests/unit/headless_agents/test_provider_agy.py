"""The generic ``agy`` adapter: ephemeral HOME from a profile, guard proven
before launch, report extracted from the event stream.
"""

from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from headless_agents import procgroup, sandbox
from headless_agents.capability import (
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
)
from headless_agents.profile import (
    CapabilityProfile,
    Credentials,
    McpServer,
    ToolGuard,
    Workspace,
)
from headless_agents.providers import agy
from headless_agents.result import RunResult
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


class TestToolCallStarted:
    """What the stream proves at the moment the runner's deadline fires.

    Fail-closed the other way round from ``tool_call_completed``: whatever
    cannot be read answers "a call may have started".
    """

    def test_an_empty_stream_or_a_response_without_an_mcp_step_proves_no_call(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text("", encoding="utf-8")
        assert agy.tool_call_started(log) is False
        chatter = _events(
            {"step_update": {"step_type": "agent_response", "state": "ACTIVE"}},
            _mcp_step("ERROR", "run_command"),
        )
        log.write_text(chatter, encoding="utf-8")
        assert agy.tool_call_started(log) is False

    def test_an_mcp_step_counts_in_any_state(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        for state in ("ACTIVE", "DONE", "ERROR"):
            log.write_text(_events(_mcp_step(state)), encoding="utf-8")
            assert agy.tool_call_started(log) is True, state

    def test_an_absent_or_truncated_stream_proves_nothing(self, tmp_path: Path) -> None:
        """A line cut by the kill is exactly the moment a call may be leaving:
        it must read as "started", never as empty."""
        assert agy.tool_call_started(tmp_path / "absent.jsonl") is True
        log = tmp_path / "events.jsonl"
        log.write_text(
            '{"step_update": {"step_type": "agent_response"}}\n{"step_up', encoding="utf-8"
        )
        assert agy.tool_call_started(log) is True


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

    def test_a_childs_own_fallback_code_after_a_completed_call_never_advances_a_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """agy exiting 3 or 4 by itself after writing must be an ordinary
        failure, or the chain would replay a run that provably wrote."""
        for code in (PROVIDER_FALLBACK_EXIT_CODE, TIMEOUT_REPLAYABLE_EXIT_CODE):
            _install(monkeypatch, _FakeProcess(returncode=code, events=_events(_mcp_step("DONE"))))
            assert _run(tmp_path) == 1, code

    def test_an_already_expired_deadline_is_a_timeout_and_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """See the opencode twin: an exhausted budget is not a dead link."""
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=_events(_mcp_step())))
        monkeypatch.setattr(agy.time, "monotonic", lambda: 1000.0)
        assert _run(tmp_path, timeout_seconds=60.0, deadline=999.0) == TIMEOUT_EXIT_CODE
        assert "command" not in captured
        assert "deadline" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")

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

    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run_agy(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("agy answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(agy, "run_agy", fake_run_agy)
        run_dir = tmp_path / "runs" / "r1"
        result = agy.AgyProvider().run(
            RunSpec(prompt="P", model="m", profile=_profile(tmp_path), run_dir=run_dir)
        )
        assert result.text == "agy answer"
        assert result.run_id == "r1"
        assert result.stderr_log == run_dir / "stderr.log"
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()

    def test_a_failed_run_has_no_text_even_when_the_stream_left_a_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """extract_report runs before the exit code is judged, so a failed run can
        leave a report behind; that is not an answer."""

        def fake_run_agy(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("half an answer", encoding="utf-8")
            return 1

        monkeypatch.setattr(agy, "run_agy", fake_run_agy)
        result = agy.AgyProvider().run(
            RunSpec(prompt="P", model="m", profile=_profile(tmp_path), **_logs(tmp_path))
        )
        assert result.exit_code == 1
        assert result.text is None


def _workspace_run(
    tmp_path: Path,
    script: str,
    *,
    write: bool = False,
    timeout_seconds: float = 30.0,
) -> tuple[RunResult, Path]:
    """Run AgyProvider over a real fake ``agy`` and the real copied guard."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")
    fake = tmp_path / "agy"
    fake.write_text(f"#!/usr/bin/env bash\n{script}\n", encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "root").mkdir(exist_ok=True)
    provider = agy.AgyProvider(real_home=tmp_path, ephemeral_root=tmp_path / "root")
    spec = RunSpec(
        prompt="TASK",
        executable=str(fake),
        timeout_seconds=timeout_seconds,
        profile=CapabilityProfile(workspace=Workspace(path=ws, write=write)),
        run_dir=tmp_path / "run",
    )
    return provider.run(spec), ws


_WRITE_STEP = json.dumps(
    {"step_update": {"step_type": "tool", "tool_name": "write_to_file", "state": "ACTIVE"}}
)


class TestWorkspace:
    def test_workspace_and_caller_guard_are_refused(self, tmp_path: Path) -> None:
        profile = CapabilityProfile(
            guard=ToolGuard(path=tmp_path / "g.sh"), workspace=Workspace(path=tmp_path)
        )
        with pytest.raises(ValueError, match="tool_guard"):
            agy.AgyProvider().run(RunSpec(prompt="p", profile=profile, run_dir=tmp_path / "run"))
        with pytest.raises(ValueError, match="tool_guard"):
            _run(tmp_path, profile=profile)
        assert not (tmp_path / "run").exists() and not (tmp_path / "out").exists()

    def test_preamble_over_argv_limit_exits_2_without_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("headless_agents.providers.agy.MAX_PROMPT_BYTES", 64)
        marker = tmp_path / "spawned"
        fake = tmp_path / "agy"
        fake.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        fake.chmod(0o755)
        ws = tmp_path / "ws"
        ws.mkdir()
        result = agy.AgyProvider().run(
            RunSpec(
                prompt="p" * 10,
                executable=str(fake),
                profile=CapabilityProfile(workspace=Workspace(path=ws)),
                run_dir=tmp_path / "run",
            )
        )
        assert result.exit_code == INVALID_USAGE_EXIT_CODE == 2 and not marker.exists()
        assert result.text is None and result.duration_seconds == 0.0
        assert "too long for argv" in (tmp_path / "run" / "stderr.log").read_text()

    def test_runs_in_the_workspace_with_the_preamble_and_file_list(self, tmp_path: Path) -> None:
        script = f'pwd > {tmp_path}/cwd\nprintf %s "$2" > {tmp_path}/prompt'
        result, ws = _workspace_run(tmp_path, script)
        assert result.exit_code == 0
        assert result.workspace == {"path": str(ws), "write": False, "shell": False}
        assert (tmp_path / "cwd").read_text().strip() == str(ws)
        prompt = (tmp_path / "prompt").read_text()
        assert "Your only read tool is view_file" in prompt
        assert f'<files root="{ws}">\n(not a git repository: no file list)\n</files>' in prompt
        assert prompt.endswith("<task>\nTASK\n</task>")

    def test_a_guard_failing_its_probe_refuses_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = functools.partial(sandbox.build_ephemeral_home, guard_python="/nonexistent")
        monkeypatch.setattr(agy, "build_ephemeral_home", broken)
        result, _ = _workspace_run(tmp_path, f"touch {tmp_path}/spawned")
        assert result.exit_code == 1 and not (tmp_path / "spawned").exists()
        assert (tmp_path / "run" / "stderr.log").read_text() == (
            "agy workspace guard failed its probe: run refused\n"
        )

    def test_a_failure_after_a_write_step_never_advances_a_chain(self, tmp_path: Path) -> None:
        result, _ = _workspace_run(tmp_path, f"echo '{_WRITE_STEP}'\nexit 3", write=True)
        assert result.exit_code == 1

    def test_a_read_only_failure_after_a_write_step_stays_replayable(self, tmp_path: Path) -> None:
        """The guard denies writes in read mode: the step taints nothing."""
        result, _ = _workspace_run(tmp_path, f"echo '{_WRITE_STEP}'\nexit 3")
        assert result.exit_code == PROVIDER_FALLBACK_EXIT_CODE

    def test_an_ephemeral_root_inside_the_workspace_is_refused(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        root = ws / "tmp"
        root.mkdir(parents=True)
        fake = tmp_path / "agy"
        fake.write_text(f"#!/usr/bin/env bash\ntouch {tmp_path}/spawned\n", encoding="utf-8")
        fake.chmod(0o755)
        spec = RunSpec(
            prompt="p",
            executable=str(fake),
            profile=CapabilityProfile(workspace=Workspace(path=ws)),
            run_dir=tmp_path / "run",
        )
        with pytest.raises(ValueError, match="overlap"):
            agy.AgyProvider(real_home=tmp_path, ephemeral_root=root).run(spec)
        assert not (tmp_path / "spawned").exists() and not (tmp_path / "run").exists()
        assert list(root.iterdir()) == []

    def test_the_probe_refuses_a_guard_that_can_read_its_own_config(self, tmp_path: Path) -> None:
        """Whatever put the HOME under the guard's root, the probe catches it."""
        ws = tmp_path / "ws"
        ws.mkdir()
        home = sandbox.build_ephemeral_home(
            root=tmp_path / "r",
            name="h",
            profile=CapabilityProfile(),
            real_home=tmp_path,
            workspace=Workspace(path=ws),
        )
        assert agy.workspace_guard_holds(home, Workspace(path=ws)) is True
        config = home / ".gemini" / "config" / "workspace-guard.json"
        config.write_text(json.dumps({"root": str(tmp_path), "write": False, "shell": False}))
        assert agy.workspace_guard_holds(home, Workspace(path=tmp_path)) is False

    def test_a_legacy_workspace_next_to_a_profile_one_is_refused_everywhere(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        spec = RunSpec(
            prompt="p",
            profile=CapabilityProfile(workspace=Workspace(path=ws)),
            workspace=tmp_path,
        )
        provider = agy.AgyProvider(real_home=tmp_path, ephemeral_root=tmp_path / "r")
        for method in (provider.build_command, provider.prepare_home, provider.run):
            with pytest.raises(ValueError, match="pick one"):
                method(spec)
        assert not (tmp_path / "r").exists()

    def test_the_probe_refuses_a_guard_that_lets_a_write_reach_git(self, tmp_path: Path) -> None:
        """A guard from before the ``.git`` rule passes the other probes."""
        ws = tmp_path / "ws"
        ws.mkdir()
        workspace = Workspace(path=ws, write=True)
        home = sandbox.build_ephemeral_home(
            root=tmp_path / "r",
            name="h",
            profile=CapabilityProfile(),
            real_home=tmp_path,
            workspace=workspace,
        )
        assert agy.workspace_guard_holds(home, workspace) is True
        guard = home / ".gemini" / "config" / sandbox.WORKSPACE_GUARD_NAME
        lax = guard.read_text(encoding="utf-8").replace(
            "if _names_git(cast", "if False and _names_git(cast"
        )
        assert lax != guard.read_text(encoding="utf-8")
        guard.write_text(lax, encoding="utf-8")
        assert agy.workspace_guard_holds(home, workspace) is False

    def test_the_files_root_attribute_is_escaped(self, tmp_path: Path) -> None:
        ws = tmp_path / 'w"<&>'
        ws.mkdir()
        workspace = Workspace(path=ws)
        spec = RunSpec(prompt="p", profile=CapabilityProfile(workspace=workspace))
        preamble = agy._preamble_for(spec, workspace)
        assert f'<files root="{tmp_path}/w&quot;&lt;&amp;&gt;">' in preamble

    def test_a_deadline_after_a_write_step_is_a_plain_timeout(self, tmp_path: Path) -> None:
        script = f"echo '{_WRITE_STEP}'\nexec sleep 30"
        result, _ = _workspace_run(tmp_path, script, write=True, timeout_seconds=1.0)
        assert result.exit_code == TIMEOUT_EXIT_CODE


def test_listing_of_a_git_repo_and_its_cap(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    listing = agy.workspace_listing(tmp_path, max_entries=3)
    assert listing.splitlines()[:3] == ["f0.txt", "f1.txt", "f2.txt"]
    assert listing.splitlines()[-1] == "… 2 more entries not listed"
    assert agy.workspace_listing(tmp_path, max_bytes=13).splitlines() == [
        "f0.txt",
        "f1.txt",
        "… 3 more entries not listed",
    ]


def test_listing_is_pinned_to_the_workspace(tmp_path: Path) -> None:
    """A ``core.worktree`` a write run left in ``.git/config`` must not point
    the listing at another directory."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "core.worktree", str(outside)], check=True)
    (root / "in.txt").write_text("x")
    (outside / "out.txt").write_text("x")
    assert agy.workspace_listing(root) == "in.txt"


def test_listing_of_a_linked_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    (main / "tracked.txt").write_text("x")
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "tracked.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "c"], check=True)
    linked = tmp_path / "linked"
    subprocess.run([*git, "worktree", "add", "-q", str(linked)], check=True)
    (linked / "new.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "out.txt").write_text("x")
    # A per-worktree config is the one a linked worktree honours.
    subprocess.run([*git, "config", "extensions.worktreeConfig", "true"], check=True)
    per_worktree = ["git", "-C", str(linked), "config", "--worktree", "core.worktree"]
    subprocess.run([*per_worktree, str(outside)], check=True)
    assert agy.workspace_listing(linked).splitlines() == ["new.txt", "tracked.txt"]


def test_listing_drops_entries_that_could_break_the_block(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("ok.txt", "a<b", "c>d", "e\nf", "g\rh"):
        (tmp_path / name).write_text("x")
    assert agy.workspace_listing(tmp_path) == "ok.txt\n… 4 more entries not listed"


def test_listing_outside_git(tmp_path: Path) -> None:
    assert agy.workspace_listing(tmp_path) == "(not a git repository: no file list)"


def test_agy_write_tool_started(tmp_path: Path) -> None:
    log = tmp_path / "e.jsonl"
    log.write_text(_WRITE_STEP + "\n")
    assert agy.write_tool_started(log) is True
    view = {"step_update": {"step_type": "tool", "tool_name": "view_file", "state": "DONE"}}
    log.write_text(json.dumps(view) + "\n")
    assert agy.write_tool_started(log) is False
    assert agy.write_tool_started(tmp_path / "absent.jsonl") is True


class TestTheProviderDiesWithHa:
    """Spec 0.5.0 §3.8.2 (see the claude rail's test of the same name)."""

    def test_the_child_is_started_with_the_death_signal_preexec(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        _run(tmp_path)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert callable(kwargs["preexec_fn"])

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

    def test_the_watcher_is_started_and_released(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        log: list[object] = []
        monkeypatch.setattr(procgroup, "start_watcher", lambda: _RecordedLifeline(log))
        fake = _FakeProcess(returncode=0)
        _install(monkeypatch, fake)
        _run(tmp_path)
        assert log == ["start", "child_attach", "release"]

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
        assert log == ["start", "child_attach", "release"]
