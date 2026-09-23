"""The generic ``opencode`` adapter: inline config through the environment, a
fail-closed tool ALLOWLIST instead of a guard script, an ephemeral HOME that
borrows the operator's ``node_modules`` so no run ever touches npm.

Every shape asserted here was measured on ``opencode`` 1.18.30 (2026-09-15):
the ``run --format json`` event stream, the ``{env:VAR}`` substitution in
``mcp.*.headers``, the glob ``tools`` map, and the ``bun install`` a fresh
HOME triggers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from headless_agents.capability import (
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
)
from headless_agents.profile import CapabilityProfile, Credentials, McpServer, Workspace
from headless_agents.providers import opencode
from headless_agents.spec import RunSpec

URL = "http://127.0.0.1:8765/mcp"
SERVER = "example"
AUTH = ".local/share/opencode/auth.json"


def _profile(**overrides: object) -> CapabilityProfile:
    fields: dict[str, object] = {
        "mcp": McpServer(
            name=SERVER,
            url=URL,
            bearer_env_var="EXAMPLE_TOKEN",
            headers={"X-Agent": "example-run"},
            tools=("example_search", "example_get"),
        ),
        "credentials": Credentials(paths=(AUTH,)),
    }
    fields.update(overrides)
    return CapabilityProfile(**fields)  # type: ignore[arg-type]


class TestBuildOpenCodeCommand:
    def test_the_prompt_is_the_positional_message_after_every_flag(self) -> None:
        command = opencode.build_opencode_command(
            model="opencode-go/m", prompt="P", home=Path("/h"), variant="high", title="seat"
        )
        assert command == [
            "opencode",
            "run",
            "--dir",
            "/h",
            "--auto",
            "--pure",
            "--format",
            "json",
            "-m",
            "opencode-go/m",
            "--variant",
            "high",
            "--title",
            "seat",
            "P",
        ]

    def test_blank_variant_and_title_add_no_flag(self) -> None:
        command = opencode.build_opencode_command(model="m", prompt="P", home=Path("/h"))
        assert "--variant" not in command and "--title" not in command
        assert command[-1] == "P"

    def test_a_blank_model_is_refused_before_execve(self) -> None:
        # Without -m opencode picks its own default among the authenticated
        # providers -- on OpenCode Go that can be a contributor model Meta
        # trains on. The caller names the model or the run does not start.
        with pytest.raises(ValueError, match="model"):
            opencode.build_opencode_command(model=" ", prompt="P", home=Path("/h"))

    def test_an_oversized_prompt_is_refused_before_execve(self) -> None:
        with pytest.raises(ValueError, match="argv"):
            opencode.build_opencode_command(
                model="m", prompt="x" * (opencode.MAX_PROMPT_BYTES + 1), home=Path("/h")
            )

    def test_dir_is_the_workspace(self, tmp_path: Path) -> None:
        command = opencode.build_opencode_command(
            model="m", prompt="p", home=tmp_path / "home", directory=tmp_path
        )
        assert command[command.index("--dir") + 1] == str(tmp_path)


class TestOpenCodeConfig:
    def test_the_server_names_the_bearer_variable_and_never_its_value(self) -> None:
        config = opencode.opencode_config(_profile().mcp)
        server = config["mcp"][SERVER]
        assert server["type"] == "remote" and server["url"] == URL and server["enabled"] is True
        assert server["headers"] == {
            "Authorization": "Bearer {env:EXAMPLE_TOKEN}",
            "X-Agent": "example-run",
        }

    def test_a_literal_bearer_still_travels_by_reference(self) -> None:
        server = McpServer(name=SERVER, url=URL, bearer=SecretStr("scoped-token"))
        config = opencode.opencode_config(server)
        assert config["mcp"][SERVER]["headers"]["Authorization"] == "Bearer {env:MCP_HTTP_TOKEN}"
        assert "scoped-token" not in json.dumps(config)

    def test_the_tools_map_is_an_allowlist_of_the_declared_tools(self) -> None:
        config = opencode.opencode_config(_profile().mcp)
        assert config["tools"] == {
            "*": False,
            "example_example_search": True,
            "example_example_get": True,
        }

    def test_an_undeclared_tool_list_admits_the_whole_server(self) -> None:
        server = McpServer(name=SERVER, url=URL)
        assert opencode.opencode_config(server)["tools"] == {"*": False, "example_*": True}

    def test_no_server_means_no_tool_at_all(self) -> None:
        config = opencode.opencode_config(None)
        assert config["mcp"] == {} and config["tools"] == {"*": False}

    def test_every_machine_tool_is_also_denied_by_permission(self) -> None:
        permission = opencode.opencode_config(_profile().mcp)["permission"]
        for tool in ("bash", "edit", "write", "read", "webfetch", "websearch", "task", "skill"):
            assert permission[tool] == "deny"

    def test_sharing_and_updates_are_off(self) -> None:
        config = opencode.opencode_config(None)
        assert config["share"] == "disabled" and config["autoupdate"] is False


class TestOpenCodeConfigWorkspace:
    def test_config_without_workspace_is_unchanged(self) -> None:
        assert opencode.opencode_config(None, None) == opencode.opencode_config(None)

    def test_read_only_config(self, tmp_path: Path) -> None:
        config = opencode.opencode_config(None, Workspace(path=tmp_path))
        assert config["tools"] == {
            "*": False,
            "read": True,
            "glob": True,
            "grep": True,
            "list": True,
        }
        permission = config["permission"]
        assert permission["external_directory"] == "deny"
        assert {k for k, v in permission.items() if v == "allow"} == {
            "read",
            "glob",
            "grep",
            "list",
        }

    def test_write_shell_config(self, tmp_path: Path) -> None:
        config = opencode.opencode_config(None, Workspace(path=tmp_path, write=True, shell=True))
        assert {k for k, v in config["permission"].items() if v == "allow"} == {
            "read",
            "glob",
            "grep",
            "list",
            "edit",
            "write",
            "bash",
        }
        assert config["tools"]["edit"] is True and config["tools"]["bash"] is True
        assert config["permission"]["external_directory"] == "deny"

    def test_external_directory_is_denied_in_every_workspace_mode(self, tmp_path: Path) -> None:
        for workspace in (
            None,
            Workspace(path=tmp_path),
            Workspace(path=tmp_path, write=True),
            Workspace(path=tmp_path, write=True, shell=True),
        ):
            config = opencode.opencode_config(_profile().mcp, workspace)
            assert config["permission"]["external_directory"] == "deny", workspace


def _events(*lines: dict[str, object]) -> str:
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def _tool_use(tool: str = "example_example_search", status: str = "completed") -> dict[str, object]:
    return {"type": "tool_use", "part": {"tool": tool, "state": {"status": status}}}


def _step_finish(
    *,
    reason: str = "stop",
    input: int = 10,
    output: int = 4,
    reasoning: int = 1,
    cache_read: int = 6,
    cost: float = 0.001,
) -> dict[str, object]:
    return {
        "type": "step_finish",
        "part": {
            "reason": reason,
            "tokens": {
                "total": input + output + reasoning + cache_read,
                "input": input,
                "output": output,
                "reasoning": reasoning,
                "cache": {"read": cache_read, "write": 0},
            },
            "cost": cost,
        },
    }


def _text(text: str, part_id: str = "prt_1") -> dict[str, object]:
    return {"type": "text", "part": {"id": part_id, "text": text}}


ERROR_EVENT: dict[str, object] = {
    "type": "error",
    "error": {"name": "UnknownError", "data": {"message": "Unexpected server error", "ref": "e"}},
}


class TestToolCallStarted:
    """What the stream proves at the moment the runner's deadline fires.

    Measured on opencode 1.18.30 (`run --format json`): a ``tool_use`` event
    is written in its TERMINAL state only (``completed``/``error``), so a call
    in flight leaves nothing -- but the ``step_start`` of the step that issued
    it is written first. No step ever started is the only proof that no call
    could be running.
    """

    def test_an_empty_stream_proves_no_call_started(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text("", encoding="utf-8")
        assert opencode.tool_call_started(log, server="example") is False

    def test_a_started_step_may_hide_a_call_in_flight(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events({"type": "step_start", "part": {}}), encoding="utf-8")
        assert opencode.tool_call_started(log, server="example") is True

    def test_any_tool_event_on_the_server_counts_whatever_its_state(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        for status in ("pending", "running", "completed", "error"):
            log.write_text(_events(_tool_use(status=status)), encoding="utf-8")
            assert opencode.tool_call_started(log, server="example") is True, status

    def test_an_absent_or_unreadable_stream_proves_nothing(self, tmp_path: Path) -> None:
        assert opencode.tool_call_started(tmp_path / "absent.jsonl", server="example") is True
        log = tmp_path / "events.jsonl"
        log.write_text("{not json\n", encoding="utf-8")
        assert opencode.tool_call_started(log, server="example") is True


class TestToolCallCompleted:
    def test_only_a_completed_call_on_the_server_counts(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_tool_use("other_search")), encoding="utf-8")
        assert opencode.tool_call_completed(log, server=SERVER) is False
        log.write_text(_events(_tool_use(status="error")), encoding="utf-8")
        assert opencode.tool_call_completed(log, server=SERVER) is False
        log.write_text(_events(_tool_use()), encoding="utf-8")
        assert opencode.tool_call_completed(log, server=SERVER) is True

    def test_an_absent_stream_proves_nothing(self, tmp_path: Path) -> None:
        assert opencode.tool_call_completed(tmp_path / "absent", server=SERVER) is False


class TestExtractReport:
    def test_concatenates_text_parts_in_order_last_version_of_each(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        log.write_text(
            _events(
                _text("draft", "prt_a"),
                _tool_use(),
                _text("first", "prt_a"),
                _text("=== REPORT ===\nfinal", "prt_b"),
            ),
            encoding="utf-8",
        )
        opencode.extract_report(log, report)
        assert report.read_text(encoding="utf-8") == "first\n\n=== REPORT ===\nfinal"

    def test_a_fence_around_the_whole_report_is_the_envelope_not_the_report(
        self, tmp_path: Path
    ) -> None:
        # Measured on the canary of 2026-09-15: glm-5.3-flash wrapped its two
        # CONNECT lines in ``` fences and the strict validator counted four lines.
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        log.write_text(
            _events(_text("```\nSTEP_A: a=1\nSTEP_B: b=2\n```")),
            encoding="utf-8",
        )
        opencode.extract_report(log, report)
        assert report.read_text(encoding="utf-8") == "STEP_A: a=1\nSTEP_B: b=2"

        log.write_text(_events(_text("```text\nline\n```\n")), encoding="utf-8")
        opencode.extract_report(log, report)
        assert report.read_text(encoding="utf-8") == "line"

    def test_an_inner_fence_is_kept(self, tmp_path: Path) -> None:
        # Only a fence around EVERYTHING is an envelope; a fenced trailer inside
        # prose is the report's own formatting and the validators read through it.
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        text = 'Summary.\n```json\n{"updated": []}\n```'
        log.write_text(_events(_text(text)), encoding="utf-8")
        opencode.extract_report(log, report)
        assert report.read_text(encoding="utf-8") == text

    def test_an_empty_or_absent_text_writes_nothing(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        report = tmp_path / "report.log"
        log.write_text(_events(_step_finish(), _text("  ")), encoding="utf-8")
        opencode.extract_report(log, report)
        assert not report.exists()


class TestEventStreamError:
    def test_a_clean_stream_with_a_server_call_passes(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_tool_use(), _step_finish()), encoding="utf-8")
        assert opencode.event_stream_error(log, server=SERVER) is None

    def test_the_measured_error_event_is_terminal(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(ERROR_EVENT), encoding="utf-8")
        error = opencode.event_stream_error(log, server=None)
        assert error is not None and "UnknownError" in error and "Unexpected server error" in error

    def test_a_run_that_never_finished_a_step_is_an_error(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_text("hello")), encoding="utf-8")
        assert opencode.event_stream_error(log, server=None) == (
            "opencode exited 0 without a step_finish event"
        )

    def test_a_server_given_and_never_called_is_an_error_with_the_callers_words(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_step_finish()), encoding="utf-8")
        assert opencode.event_stream_error(log, server=SERVER, missing_call_message="NOPE") == (
            "NOPE"
        )
        assert opencode.event_stream_error(log, server=None) is None

    def test_malformed_and_absent_streams_are_errors(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        assert opencode.event_stream_error(log, server=None) is not None
        log.write_text('{"type": "step_finish"}\n{not json\n', encoding="utf-8")
        assert "line 2" in (opencode.event_stream_error(log, server=None) or "")


class TestTelemetry:
    def test_sums_every_step_finish_and_keeps_fresh_apart_from_cached(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(
            _events(
                _step_finish(input=10, output=4, reasoning=1, cache_read=6, cost=0.001),
                _step_finish(input=2, output=3, reasoning=0, cache_read=20, cost=0.0005),
            ),
            encoding="utf-8",
        )
        tokens, cost = opencode.telemetry(log)
        assert tokens is not None
        assert (tokens.fresh, tokens.cached, tokens.input) == (12, 26, 38)
        assert (tokens.output, tokens.thinking) == (7, 1)
        assert cost == pytest.approx(0.0015)

    def test_no_step_finish_means_no_measurement(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        log.write_text(_events(_text("x")), encoding="utf-8")
        assert opencode.telemetry(log) == (None, None)

    def test_a_step_without_a_cost_leaves_the_cost_unmeasured(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        step = _step_finish(input=1, output=1, cache_read=0)
        del step["part"]["cost"]  # type: ignore[index]
        log.write_text(_events(step), encoding="utf-8")
        tokens, cost = opencode.telemetry(log)
        assert tokens is not None and cost is None

    def test_a_re_emitted_step_finish_part_counts_once(self, tmp_path: Path) -> None:
        # opencode re-emits a part on every update; the last version wins.
        log = tmp_path / "events.jsonl"
        first = _step_finish(input=10, output=1, cache_read=0, cost=0.001)
        first["part"]["id"] = "prt_s1"  # type: ignore[index]
        again = _step_finish(input=12, output=2, cache_read=0, cost=0.002)
        again["part"]["id"] = "prt_s1"  # type: ignore[index]
        log.write_text(_events(first, again), encoding="utf-8")
        tokens, cost = opencode.telemetry(log)
        assert tokens is not None and (tokens.fresh, tokens.output) == (12, 2)
        assert cost == pytest.approx(0.002)


class TestWriteToolStarted:
    """The write-mode twin of :class:`TestToolCallStarted`, keyed on the built-in
    tool name rather than an MCP server prefix."""

    def test_opencode_write_tool_started(self, tmp_path: Path) -> None:
        log = tmp_path / "e.jsonl"
        log.write_text(_events(_tool_use(tool="edit")), encoding="utf-8")
        assert opencode.write_tool_started(log) is True
        log.write_text(_events(_tool_use(tool="read")), encoding="utf-8")
        assert opencode.write_tool_started(log) is False

    def test_every_write_tool_counts_in_any_state(self, tmp_path: Path) -> None:
        log = tmp_path / "e.jsonl"
        for tool in ("edit", "write", "patch", "bash"):
            for status in ("pending", "running", "completed", "error"):
                log.write_text(_events(_tool_use(tool=tool, status=status)), encoding="utf-8")
                assert opencode.write_tool_started(log) is True, (tool, status)

    def test_an_absent_or_unreadable_stream_fails_closed(self, tmp_path: Path) -> None:
        assert opencode.write_tool_started(tmp_path / "absent.jsonl") is True
        log = tmp_path / "e.jsonl"
        log.write_text("{not json\n", encoding="utf-8")
        assert opencode.write_tool_started(log) is True


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
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout or 0)
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
        home = Path(str(kwargs["cwd"]))
        captured["home_files"] = sorted(
            str(path.relative_to(home))
            for path in home.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        captured["home_symlinks"] = {
            str(path.relative_to(home)): str(path.readlink())
            for path in home.rglob("*")
            if path.is_symlink()
        }
        fake.bind(kwargs["stdout"])
        return fake

    monkeypatch.setattr(opencode.subprocess, "Popen", popen)
    monkeypatch.setattr(opencode, "terminate_process_group", lambda process: process.kill())
    return captured


def _logs(tmp_path: Path) -> dict[str, Path]:
    return {
        "events_log": tmp_path / "out" / "events.jsonl",
        "report_log": tmp_path / "out" / "report.log",
        "stderr_log": tmp_path / "out" / "stderr.log",
    }


def _real_home(
    tmp_path: Path, *, cache: bool = True, auth: bool = True, name: str = "real-home"
) -> Path:
    real_home = tmp_path / name
    if auth:
        (real_home / ".local/share/opencode").mkdir(parents=True, exist_ok=True)
        (real_home / AUTH).write_text("{}", encoding="utf-8")
    if cache:
        (real_home / ".config/opencode/node_modules").mkdir(parents=True, exist_ok=True)
        (real_home / ".config/opencode/package.json").write_text("{}", encoding="utf-8")
        (real_home / ".config/opencode/package-lock.json").write_text("{}", encoding="utf-8")
    real_home.mkdir(exist_ok=True)
    return real_home


GOOD_EVENTS = _events(_tool_use(), _text("REPORT"), _step_finish())


def _run(tmp_path: Path, **overrides: object) -> int:
    kwargs: dict[str, object] = {
        "prompt": "PROMPT",
        "name": "example-run",
        "model": "opencode-go/m",
        "timeout_seconds": 5.0,
        "profile": _profile(),
        "real_home": _real_home(tmp_path),
        "environment": {
            "PATH": "/usr/bin",
            "LANG": "fr_FR.UTF-8",
            "EXAMPLE_TOKEN": "scoped-token",
            "SECRET": "x",
        },
        "ephemeral_root": tmp_path / "runtime",
        **_logs(tmp_path),
    }
    kwargs.update(overrides)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    return opencode.run_opencode(**kwargs)  # type: ignore[arg-type]


class TestRunOpenCode:
    def test_runs_in_an_ephemeral_home_with_inline_config_and_borrowed_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))

        assert _run(tmp_path) == 0

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        home = Path(kwargs["cwd"])
        assert home.name == "example-run"
        assert kwargs["stdin"] is subprocess.DEVNULL
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["HOME"] == str(home) and env["TMPDIR"] == str(home)
        assert (env["PATH"], env["LANG"], env["TERM"]) == ("/usr/bin", "fr_FR.UTF-8", "dumb")
        assert env["EXAMPLE_TOKEN"] == "scoped-token"
        assert "SECRET" not in env
        for name in (
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_DISABLE_CLAUDE_CODE",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS",
            "OPENCODE_DISABLE_AUTOUPDATE",
            "OPENCODE_DISABLE_MODELS_FETCH",
            "OPENCODE_DISABLE_SHARE",
        ):
            assert env[name] == "1"
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["mcp"][SERVER]["headers"]["Authorization"] == "Bearer {env:EXAMPLE_TOKEN}"
        assert config["tools"]["*"] is False
        # Nothing is written into the HOME: the config is inline, the bearer
        # is in the environment, the cache and the credentials are borrowed.
        assert captured["home_files"] == []
        real_home = tmp_path / "real-home"
        assert captured["home_symlinks"] == {
            ".config/opencode/node_modules": str(real_home / ".config/opencode/node_modules"),
            ".config/opencode/package.json": str(real_home / ".config/opencode/package.json"),
            ".config/opencode/package-lock.json": str(
                real_home / ".config/opencode/package-lock.json"
            ),
            AUTH: str(real_home / AUTH),
        }
        assert captured["command"][:4] == ["opencode", "run", "--dir", str(home)]
        assert (tmp_path / "out" / "report.log").read_text(encoding="utf-8") == "REPORT"

    def test_the_home_is_destroyed_after_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        _run(tmp_path)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert not Path(kwargs["cwd"]).exists()

    def test_a_missing_runtime_cache_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Nothing was launched, so nothing was written: the chain may move on.
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        bare = _real_home(tmp_path, cache=False, name="bare-home")
        assert _run(tmp_path, real_home=bare) == PROVIDER_FALLBACK_EXIT_CODE
        assert "node_modules" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_refuses_to_start_without_the_bearer_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        assert _run(tmp_path, environment={"PATH": "/usr/bin"}) == 1
        assert "EXAMPLE_TOKEN" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_a_literal_bearer_in_the_profile_is_exported_under_its_variable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        literal = McpServer(name=SERVER, url=URL, bearer=SecretStr("literal-token"))
        assert _run(tmp_path, profile=_profile(mcp=literal), environment={"PATH": "/x"}) == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["env"]["MCP_HTTP_TOKEN"] == "literal-token"

    def test_launch_failure_and_silent_failure_are_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def popen(command: list[str], **kwargs: object) -> None:
            raise OSError("no such binary")

        monkeypatch.setattr(opencode.subprocess, "Popen", popen)
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=1, events=_events(ERROR_EVENT)))
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE

    def test_a_failure_after_a_completed_call_keeps_its_own_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=9, events=_events(_tool_use())))
        assert _run(tmp_path) == 9

    def test_a_childs_own_fallback_code_after_a_completed_call_never_advances_a_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """opencode exiting 3 or 4 by itself after writing must be an ordinary
        failure, or the chain would replay a run that provably wrote."""
        for code in (PROVIDER_FALLBACK_EXIT_CODE, TIMEOUT_REPLAYABLE_EXIT_CODE):
            _install(monkeypatch, _FakeProcess(returncode=code, events=_events(_tool_use())))
            assert _run(tmp_path) == 1, code

    def test_an_already_expired_deadline_is_a_timeout_and_launches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A caller's budget already spent must not read as a dead link: launching
        would kill the child at once on an empty stream and return 4, then the
        next link would do the same, and the chain would condemn every link in
        seconds. Nothing is launched; the plain 124 stops the chain."""
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        monkeypatch.setattr(opencode.time, "monotonic", lambda: 1000.0)
        assert _run(tmp_path, timeout_seconds=60.0, deadline=999.0) == TIMEOUT_EXIT_CODE
        assert "command" not in captured
        assert "deadline" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")

    def test_a_clean_exit_without_a_report_or_without_a_call_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install(monkeypatch, _FakeProcess(returncode=0, events=_events(_step_finish())))
        assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
        stderr = (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "without a final report" in stderr

        _install(
            monkeypatch,
            _FakeProcess(returncode=0, events=_events(_text("R"), _step_finish())),
        )
        assert _run(tmp_path, missing_call_message="NO CALL") == PROVIDER_FALLBACK_EXIT_CODE
        assert "NO CALL" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")

    def test_a_clean_exit_with_a_call_but_a_terminal_error_keeps_code_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        events = _events(_tool_use(), _text("R"), ERROR_EVENT, _step_finish())
        _install(monkeypatch, _FakeProcess(returncode=0, events=events))
        assert _run(tmp_path) == 1

    def test_an_oversized_prompt_or_a_blank_model_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert (
            _run(tmp_path, prompt="x" * (opencode.MAX_PROMPT_BYTES + 1))
            == PROVIDER_FALLBACK_EXIT_CODE
        )
        assert "command" not in captured
        assert _run(tmp_path, model="") == PROVIDER_FALLBACK_EXIT_CODE
        assert "model" in (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "command" not in captured

    def test_a_timeout_after_a_started_step_is_never_a_switchover(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The run may have written and then hung: the chain must stay still."""
        step = _events({"type": "step_start", "part": {}})
        _install(monkeypatch, _FakeProcess(returncode=0, events=step, hang=True))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=0, events=_events(_tool_use()), hang=True))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        # A child exiting 124 by itself is read as a timeout, empty stream or not.
        _install(monkeypatch, _FakeProcess(returncode=124))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        with pytest.raises(ValueError, match="timeout"):
            _run(tmp_path, timeout_seconds=0)

    def test_a_timeout_on_an_empty_stream_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The night of 2026-09-19: a quota-dead link blocked for the whole
        deadline without one byte of events. Nothing started, nothing could
        have been written -- the chain may hand the run to the next link, and
        stderr says why the deadline was read that way."""
        _install(monkeypatch, _FakeProcess(returncode=0, hang=True))
        assert _run(tmp_path) == TIMEOUT_REPLAYABLE_EXIT_CODE
        stderr = (tmp_path / "out" / "stderr.log").read_text(encoding="utf-8")
        assert "deadline" in stderr and "no step started" in stderr

    def test_a_timeout_without_a_server_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No server declared, no tool at all: a hang can have written nothing,
        whatever the stream says."""
        step = _events({"type": "step_start", "part": {}})
        _install(monkeypatch, _FakeProcess(returncode=0, events=step, hang=True))
        assert _run(tmp_path, profile=_profile(mcp=None)) == TIMEOUT_REPLAYABLE_EXIT_CODE

    def test_the_temp_prefix_is_the_callers_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        assert _run(tmp_path, temp_prefix="caller-") == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert Path(str(kwargs["cwd"])).parent.name.startswith("caller-")


class TestOpenCodeProvider:
    def test_prepare_home_and_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        real_home = _real_home(tmp_path)
        provider = opencode.OpenCodeProvider(real_home=real_home, ephemeral_root=tmp_path / "root")
        (tmp_path / "root").mkdir()
        spec = RunSpec(
            prompt="P",
            name="seat-1",
            model="opencode-go/m",
            profile=_profile(),
            reasoning_effort="high",
            **_logs(tmp_path),
        )
        home = provider.prepare_home(spec)
        assert home == tmp_path / "root" / "seat-1"
        assert (home / ".config/opencode/node_modules").is_symlink()
        assert provider.build_command(spec)[:4] == ["opencode", "run", "--dir", str(home)]
        assert "--variant" in provider.build_command(spec)

        calls: list[dict[str, object]] = []

        def fake_run(**kwargs: object) -> int:
            calls.append(kwargs)
            events = kwargs["events_log"]
            assert isinstance(events, Path)
            events.parent.mkdir(parents=True, exist_ok=True)
            events.write_text(GOOD_EVENTS, encoding="utf-8")
            return 0

        monkeypatch.setattr(opencode, "run_opencode", fake_run)
        result = provider.run(spec)
        assert result.provider == "opencode" and result.exit_code == 0
        assert result.tool_call_completed is True
        assert result.tokens is not None and result.tokens.cached == 6
        assert result.cost_usd == pytest.approx(0.001)
        assert calls[0]["name"] == "seat-1" and calls[0]["real_home"] == real_home
        assert calls[0]["variant"] == "high" and calls[0]["title"] == "seat-1"

    def test_child_environment_is_the_runtimes_business(self, tmp_path: Path) -> None:
        provider = opencode.OpenCodeProvider()
        spec = RunSpec(prompt="P", profile=_profile(), **_logs(tmp_path))
        assert provider.child_environment(spec, {"PATH": "/x"}) is None

    def test_run_reads_its_report_as_text_and_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run(**kwargs: object) -> int:
            report = kwargs["report_log"]
            assert isinstance(report, Path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("opencode answer", encoding="utf-8")
            return 0

        monkeypatch.setattr(opencode, "run_opencode", fake_run)
        run_dir = tmp_path / "runs" / "r1"
        result = opencode.OpenCodeProvider().run(
            RunSpec(prompt="P", model="opencode-go/m", profile=_profile(), run_dir=run_dir)
        )
        assert result.text == "opencode answer"
        assert result.run_id == "r1"
        assert result.tokens is None  # no event was written: nothing was measured
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()


class TestWorkspace:
    """``--dir`` and cwd move to the workspace; the HOME stays ephemeral."""

    def test_dir_and_cwd_move_to_the_workspace_home_stays_ephemeral(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert _run(tmp_path, workspace=Workspace(path=ws)) == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert Path(str(kwargs["cwd"])) == ws
        assert captured["command"][captured["command"].index("--dir") + 1] == str(ws)
        env = kwargs["env"]
        assert isinstance(env, dict)
        # HOME/TMPDIR keep pointing at the ephemeral directory the run's cache
        # and credentials were borrowed into -- only --dir and cwd moved.
        assert env["HOME"] != str(ws)
        assert env["HOME"].endswith("example-run")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["tools"]["read"] is True and "edit" not in config["tools"]

    def test_write_shell_workspace_widens_the_inline_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert _run(tmp_path, workspace=Workspace(path=ws, write=True, shell=True)) == 0
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["tools"]["edit"] is True and config["tools"]["bash"] is True
        assert config["permission"]["external_directory"] == "deny"

    def test_a_deadline_in_write_mode_after_a_write_step_is_a_plain_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        step = _events(_tool_use(tool="edit"))
        _install(monkeypatch, _FakeProcess(returncode=0, events=step, hang=True))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert (
            _run(tmp_path, workspace=Workspace(path=ws, write=True), profile=_profile(mcp=None))
            == TIMEOUT_EXIT_CODE
        )

    def test_a_deadline_in_read_only_mode_ignores_a_write_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The guard is the config's ``tools``/``permission`` walls, not this
        predicate: a read-only workspace never lets a write step taint the run,
        so a hang there stays replayable."""
        step = _events(_tool_use(tool="edit"))
        _install(monkeypatch, _FakeProcess(returncode=0, events=step, hang=True))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert (
            _run(tmp_path, workspace=Workspace(path=ws), profile=_profile(mcp=None))
            == TIMEOUT_REPLAYABLE_EXIT_CODE
        )

    def test_a_failure_in_write_mode_after_a_write_step_never_advances_a_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        events = _events(_tool_use(tool="edit"))
        _install(monkeypatch, _FakeProcess(returncode=3, events=events))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert (
            _run(tmp_path, workspace=Workspace(path=ws, write=True), profile=_profile(mcp=None))
            == 1
        )

    def test_a_read_only_failure_after_a_write_step_stays_replayable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        events = _events(_tool_use(tool="edit"))
        _install(monkeypatch, _FakeProcess(returncode=3, events=events))
        ws = tmp_path / "ws"
        ws.mkdir()
        assert (
            _run(tmp_path, workspace=Workspace(path=ws), profile=_profile(mcp=None))
            == PROVIDER_FALLBACK_EXIT_CODE
        )


class TestWorkspaceProvider:
    def test_preamble_over_argv_limit_exits_2_without_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("headless_agents.providers.opencode.MAX_PROMPT_BYTES", 64)
        marker = tmp_path / "spawned"
        fake = tmp_path / "opencode"
        fake.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        fake.chmod(0o755)
        ws = tmp_path / "ws"
        ws.mkdir()
        result = opencode.OpenCodeProvider().run(
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

    def test_runs_in_the_workspace_with_the_preamble(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_home = _real_home(tmp_path)
        provider = opencode.OpenCodeProvider(real_home=real_home, ephemeral_root=tmp_path / "root")
        (tmp_path / "root").mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        spec = RunSpec(
            prompt="TASK",
            model="opencode-go/m",
            profile=CapabilityProfile(workspace=Workspace(path=ws)),
            run_dir=tmp_path / "run",
        )
        assert provider.build_command(spec)[:4] == ["opencode", "run", "--dir", str(ws)]
        assert "Use read, glob, grep and list" in provider.build_command(spec)[-1]

        calls: list[dict[str, object]] = []

        def fake_run(**kwargs: object) -> int:
            calls.append(kwargs)
            events = kwargs["events_log"]
            assert isinstance(events, Path)
            events.parent.mkdir(parents=True, exist_ok=True)
            events.write_text(GOOD_EVENTS, encoding="utf-8")
            return 0

        monkeypatch.setattr(opencode, "run_opencode", fake_run)
        result = provider.run(spec)
        assert result.exit_code == 0
        assert result.workspace == {"path": str(ws), "write": False, "shell": False}
        assert calls[0]["workspace"] == Workspace(path=ws)
        assert calls[0]["prompt"].endswith("<task>\nTASK\n</task>")
