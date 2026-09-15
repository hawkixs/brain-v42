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

from headless_agents.capability import PROVIDER_FALLBACK_EXIT_CODE, TIMEOUT_EXIT_CODE
from headless_agents.profile import CapabilityProfile, Credentials, McpServer
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

    def test_blank_model_variant_and_title_add_no_flag(self) -> None:
        command = opencode.build_opencode_command(model=" ", prompt="P", home=Path("/h"))
        assert "-m" not in command and "--variant" not in command and "--title" not in command
        assert command[-1] == "P"

    def test_an_oversized_prompt_is_refused_before_execve(self) -> None:
        with pytest.raises(ValueError, match="argv"):
            opencode.build_opencode_command(
                model="m", prompt="x" * (opencode.MAX_PROMPT_BYTES + 1), home=Path("/h")
            )


class TestOpenCodeConfig:
    def test_the_server_names_the_bearer_variable_and_never_its_value(self) -> None:
        config = opencode.opencode_config(_profile().mcp)
        server = config["mcp"][SERVER]
        assert server["type"] == "remote" and server["url"] == URL and server["enabled"] is True
        assert server["headers"] == {
            "Authorization": "Bearer {env:EXAMPLE_TOKEN}",
            "X-Agent": "example-run",
        }
        assert "scoped" not in json.dumps(config)

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
        if self._hang:
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout or 0)
        self._stream.write(self._events)  # type: ignore[attr-defined]
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

    def test_refuses_to_start_without_the_borrowed_runtime_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0, events=GOOD_EVENTS))
        bare = _real_home(tmp_path, cache=False, name="bare-home")
        assert _run(tmp_path, real_home=bare) == 1
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

    def test_an_oversized_prompt_is_replayable_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured = _install(monkeypatch, _FakeProcess(returncode=0))
        assert (
            _run(tmp_path, prompt="x" * (opencode.MAX_PROMPT_BYTES + 1))
            == PROVIDER_FALLBACK_EXIT_CODE
        )
        assert "command" not in captured

    def test_timeouts(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _install(monkeypatch, _FakeProcess(returncode=0, hang=True))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        _install(monkeypatch, _FakeProcess(returncode=124))
        assert _run(tmp_path) == TIMEOUT_EXIT_CODE
        with pytest.raises(ValueError, match="timeout"):
            _run(tmp_path, timeout_seconds=0)

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
