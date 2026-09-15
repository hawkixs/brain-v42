"""Contract tests for the isolated opencode Dream phase rail.

The rail is a small process boundary over :mod:`headless_agents.providers.opencode`:
it resolves the ``(project, phase)`` bearer into the child environment, declares
the phase's Brain tool allowlist, borrows the operator's opencode credentials
and runtime cache into an ephemeral HOME, and keeps the argv contract
``brain_v42.agents.phase`` invokes through ``python -m
brain_v42.agents.providers.opencode``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from brain_v42.agents.capability import PROVIDER_FALLBACK_EXIT_CODE
from brain_v42.agents.providers import opencode as rail
from brain_v42.agents.spec import RunSpec
from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS

AUTH = ".local/share/opencode/auth.json"
PHASES = tuple(DREAM_PHASE_TOOL_ALLOWLISTS)


def _capability_registry(*, project_key: str = "brain-v42") -> str:
    profiles = {
        f"{project_key}:{phase}": {
            "active": f"{phase}-active-token",
            "accepted": [f"{phase}-accepted-token"] if phase == "scan" else [],
        }
        for phase in PHASES
    }
    return json.dumps(profiles)


def _seed_real_home(tmp_path: Path, *, cache: bool = True) -> Path:
    real_home = tmp_path / "real-home"
    (real_home / ".local/share/opencode").mkdir(parents=True)
    (real_home / AUTH).write_text("{}", encoding="utf-8")
    if cache:
        (real_home / ".config/opencode/node_modules").mkdir(parents=True)
        (real_home / ".config/opencode/package.json").write_text("{}", encoding="utf-8")
        (real_home / ".config/opencode/package-lock.json").write_text("{}", encoding="utf-8")
    return real_home


def _environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enforcement: str = "true",
    cache: bool = True,
) -> Path:
    real_home = _seed_real_home(tmp_path, cache=cache)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", enforcement)
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", _capability_registry())
    monkeypatch.setenv("TOP_SECRET", "must-not-reach-opencode")
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", "http://127.0.0.1:8765/mcp")
    return real_home


def _capture_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def capture_popen(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        home = Path(str(kwargs["cwd"]))
        captured["home_symlinks"] = {
            str(path.relative_to(home)): str(path.readlink())
            for path in home.rglob("*")
            if path.is_symlink()
        }
        raise OSError("intentional test stop after Popen capture")

    monkeypatch.setattr(rail.subprocess, "Popen", capture_popen)
    return captured


def _logs(tmp_path: Path) -> dict[str, Path]:
    return {
        "events_log": tmp_path / "out" / "scan.events.jsonl",
        "report_log": tmp_path / "out" / "scan.log",
        "stderr_log": tmp_path / "out" / "scan.stderr.log",
    }


def _run(tmp_path: Path, **overrides: object) -> int:
    kwargs: dict[str, object] = {
        "prompt": "Return a scan report.",
        "phase": "scan",
        "project_key": "brain-v42",
        "model": "opencode-go/glm-5.3-flash",
        "variant": "high",
        "timeout_seconds": 1,
        **_logs(tmp_path),
    }
    kwargs.update(overrides)
    return rail.run_opencode(**kwargs)  # type: ignore[arg-type]


def test_the_rail_exposes_the_shared_phase_policy() -> None:
    assert rail.PHASE_TOOL_ALLOWLISTS is DREAM_PHASE_TOOL_ALLOWLISTS


def test_enabled_run_scopes_the_bearer_and_declares_the_phase_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = _environment(monkeypatch, tmp_path)
    captured = _capture_popen(monkeypatch)

    assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["MCP_HTTP_TOKEN"] == "scan-active-token"
    assert "admin-token" not in env.values()
    assert "scan-accepted-token" not in env.values()
    assert "MCP_HTTP_DREAM_TOKENS" not in env and "TOP_SECRET" not in env
    home = Path(str(kwargs["cwd"]))
    assert env["HOME"] == str(home) and home != real_home
    assert home.parent.parent == tmp_path / "runtime"
    assert home.name == "opencode-brain-v42-scan"

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    server = config["mcp"]["brain-v42"]
    assert server["url"] == "http://127.0.0.1:8765/mcp"
    assert server["headers"] == {
        "Authorization": "Bearer {env:MCP_HTTP_TOKEN}",
        "X-Brain-Agent": "dream-opencode-scan",
        "X-Brain-Tool-Profile": "native",
    }
    assert config["tools"] == {
        "*": False,
        **{f"brain-v42_{tool}": True for tool in DREAM_PHASE_TOOL_ALLOWLISTS["scan"]},
    }
    assert "scan-active-token" not in env["OPENCODE_CONFIG_CONTENT"]

    symlinks = captured["home_symlinks"]
    assert isinstance(symlinks, dict)
    assert symlinks[AUTH] == str(real_home / AUTH)
    assert symlinks[".config/opencode/node_modules"] == str(
        real_home / ".config/opencode/node_modules"
    )

    command = captured["args"][0]  # type: ignore[index]
    assert command[:4] == ["opencode", "run", "--dir", str(home)]
    assert command[command.index("-m") + 1] == "opencode-go/glm-5.3-flash"
    assert command[command.index("--variant") + 1] == "high"
    assert command[command.index("--title") + 1] == "opencode-brain-v42-scan"
    assert command[-1] == "Return a scan report."


def test_disabled_run_inherits_the_ambient_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path, enforcement="false")
    captured = _capture_popen(monkeypatch)
    assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["env"]["MCP_HTTP_TOKEN"] == "admin-token"


@pytest.mark.parametrize(
    "mcp_url",
    ("https://mcp.example.test/mcp", "http://127.0.0.2:8765/mcp"),
)
def test_enabled_run_rejects_a_non_loopback_url_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mcp_url: str
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", mcp_url)
    captured = _capture_popen(monkeypatch)
    assert _run(tmp_path) == 1
    assert "kwargs" not in captured
    stderr = (tmp_path / "out" / "scan.stderr.log").read_text(encoding="utf-8")
    assert "scan-active-token" not in stderr and "admin-token" not in stderr


def test_a_missing_project_profile_is_refused_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path)
    captured = _capture_popen(monkeypatch)
    assert _run(tmp_path, project_key="red-lab") == 1
    assert "kwargs" not in captured


def test_a_malformed_project_key_is_refused_even_without_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without enforcement no registry lookup validates the key; the profile
    # canonicalizes it itself so the parser never writes garbage into
    # dream_runs.project_key.
    _environment(monkeypatch, tmp_path, enforcement="false")
    captured = _capture_popen(monkeypatch)
    assert _run(tmp_path, project_key="not a key!") == 1
    assert "kwargs" not in captured
    # A known alias is accepted (validated, not rewritten: dream.sh canonicalizes
    # upstream, and the HOME name follows what it passed, as on the agy rail).
    assert _run(tmp_path, project_key="brain_v42") == PROVIDER_FALLBACK_EXIT_CODE
    assert "kwargs" in captured


def test_a_real_home_without_the_runtime_cache_is_refused_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path, cache=False)
    captured = _capture_popen(monkeypatch)
    # Nothing launched, nothing written: the chain may hand the phase on.
    assert _run(tmp_path) == PROVIDER_FALLBACK_EXIT_CODE
    assert "kwargs" not in captured
    assert "node_modules" in (tmp_path / "out" / "scan.stderr.log").read_text(encoding="utf-8")


def test_the_preflight_validates_the_registry_and_the_runtime_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path)
    assert rail.main(["--preflight-capabilities", "--project-key", "brain-v42"]) == 0
    assert capsys.readouterr() == ("", "")

    assert rail.main(["--preflight-capabilities", "--project-key", "red-lab"]) == 1
    err = capsys.readouterr().err
    assert "admin-token" not in err and "scan-active-token" not in err


def test_the_preflight_refuses_a_host_without_the_runtime_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path, cache=False)
    assert rail.main(["--preflight-capabilities", "--project-key", "brain-v42"]) == 1
    assert "node_modules" in capsys.readouterr().err


def test_main_reads_the_prompt_from_stdin_and_refuses_an_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    argv = [
        "--phase",
        "scan",
        "--project-key",
        "brain-v42",
        "--model",
        "opencode-go/m",
        "--variant",
        "",
        "--timeout-seconds",
        "1",
        "--events-log",
        str(tmp_path / "e"),
        "--report-log",
        str(tmp_path / "r"),
        "--stderr-log",
        str(tmp_path / "s"),
        "--opencode-executable",
        "opencode",
    ]
    assert rail.main(argv) == 1
    assert "empty" in capsys.readouterr().err


def test_main_forwards_every_argument_to_run_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("PROMPT"))
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(rail, "run_opencode", lambda **kwargs: calls.append(kwargs) or 0)
    argv = [
        "--phase",
        "reorg",
        "--project-key",
        "brain-v42",
        "--model",
        "opencode-go/m",
        "--variant",
        "high",
        "--timeout-seconds",
        "600",
        "--events-log",
        str(tmp_path / "e"),
        "--report-log",
        str(tmp_path / "r"),
        "--stderr-log",
        str(tmp_path / "s"),
        "--opencode-executable",
        "/opt/opencode",
    ]
    assert rail.main(argv) == 0
    assert calls[0]["prompt"] == "PROMPT"
    assert calls[0]["phase"] == "reorg" and calls[0]["project_key"] == "brain-v42"
    assert calls[0]["variant"] == "high" and calls[0]["timeout_seconds"] == 600.0
    assert calls[0]["opencode_executable"] == "/opt/opencode"


def test_the_executable_defaults_to_the_dream_bin_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_DREAM_OPENCODE_BIN", "/x/opencode")
    parser = rail._build_arg_parser()
    assert parser.parse_args([]).opencode_executable == "/x/opencode"


def test_main_refuses_a_missing_or_blank_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("PROMPT"))
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(rail, "run_opencode", lambda **kwargs: calls.append(kwargs) or 0)
    base = [
        "--phase",
        "scan",
        "--project-key",
        "brain-v42",
        "--timeout-seconds",
        "1",
        "--events-log",
        str(tmp_path / "e"),
        "--report-log",
        str(tmp_path / "r"),
        "--stderr-log",
        str(tmp_path / "s"),
    ]
    with pytest.raises(SystemExit):
        rail.main(base)
    assert "the following arguments are required: --model" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        rail.main([*base, "--model", "  "])
    assert "the following arguments are required: --model" in capsys.readouterr().err
    assert calls == []


def test_provider_adapter_delegates_to_the_rail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> int:
        calls.append(kwargs)
        events = kwargs["events_log"]
        assert isinstance(events, Path)
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "brain-v42_brain_list", "state": {"status": "completed"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(rail, "run_opencode", fake_run)
    provider = rail.OpenCodeProvider()
    spec = RunSpec(
        phase="scan",
        prompt="P",
        project_key="brain-v42",
        model="opencode-go/m",
        reasoning_effort="high",
        **_logs(tmp_path),
    )
    result = provider.run(spec)
    assert result.provider == "opencode" and result.exit_code == 0
    assert result.tool_call_completed is True
    assert calls[0]["variant"] == "high"
    assert provider.prepare_home(spec) is not None
    assert provider.build_command(spec)[:2] == ["opencode", "run"]
    # The wall (inline config, ephemeral HOME) is composed inside run_opencode:
    # a consumer pairing build_command with child_environment must not get an
    # environment that would launch opencode against the real HOME.
    assert provider.child_environment(spec, dict(__import__("os").environ)) is None
