"""The ``ha`` CLI over the engine (spec 0.5.0 §3.9): a thin adapter that parses and prints.

Providers are replaced by recording fakes (a real run needs a CLI and quota);
everything else -- the engine, the registry, run.json, the context bundle, the
environment -- is the real code. The engine's own gates are tested through its
API in test_engine_plan.py; here, that the CLI reaches them and reports them.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli, engine, locks
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec


@dataclass
class _Fake:
    """A provider that records the spec it got and answers with a fixed code."""

    name: str
    code: int = 0
    answer: str = "the answer"
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        spec = spec.with_run_dir_defaults()
        return record(
            spec,
            RunResult(
                exit_code=self.code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=0.1,
                tool_call_completed=False,
                text=self.answer if self.code == 0 else None,
                run_id=run_id_of(spec),
            ),
        )


@dataclass
class _World:
    home: Path
    repo: Path
    environ: dict[str, str]
    fakes: dict[str, _Fake]

    def run(self, *argv: str, stdin: str = "", cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ=self.environ,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            cwd=cwd or self.repo,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()

    def spec(self, provider: str, index: int = -1) -> RunSpec:
        return self.fakes[provider].specs[index]

    def roles(self, text: str) -> None:
        (self.home / ".config" / "ha" / "roles.toml").write_text(text)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")


def _write_models(home: Path, models: dict[str, str]) -> Path:
    path = home / ".config" / "ha" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'"{name}" = "{model}"\n' for name, model in models.items()))
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("Everything on GitHub in English.\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "CLAUDE.md").write_text("Repository rules.\n")
    fakes: dict[str, _Fake] = {}

    def fake_provider(name: str) -> _Fake:
        return fakes.setdefault(name, _Fake(name))

    monkeypatch.setattr(engine, "get_provider", fake_provider)
    environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDECODE": "1",
        "KEEP_ME": "yes",
    }
    _write_models(home, {name: f"{name}-default" for name in cli.PROVIDER_NAMES if name != "agy"})
    return _World(home=home, repo=repo, environ=environ, fakes=fakes)


def _prime(world: _World, name: str, **kwargs: object) -> _Fake:
    fake = _Fake(name, **kwargs)  # type: ignore[arg-type]
    world.fakes[name] = fake
    return fake


def _report(world: _World, out: str) -> dict[str, object]:
    del world
    return json.loads(out)


# ── ha providers ───────────────────────────────────────────────────────────


def test_providers_json_lists_every_registry_name(world: _World, monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "probe", lambda name, **_: cli.Probe(available=name == "codex", detail=f"d-{name}")
    )
    code, out, _ = world.run("providers", "--json")
    assert code == 0
    rows = json.loads(out)
    assert [row["name"] for row in rows] == list(cli.PROVIDER_NAMES)
    codex = next(row for row in rows if row["name"] == "codex")
    assert codex == {
        "name": "codex",
        "available": True,
        "detail": "d-codex",
        "version": None,
        "max_prompt_bytes": None,
    }


def test_providers_text_names_each_provider(world: _World, monkeypatch) -> None:
    monkeypatch.setattr(cli, "probe", lambda name, **_: cli.Probe(available=False, detail="x"))
    code, out, _ = world.run("providers")
    assert code == 0
    for name in cli.PROVIDER_NAMES:
        assert name in out


# ── ha run TARGET: a provider ──────────────────────────────────────────────


def test_run_prints_the_answer_and_records_the_run(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "-m", "m1", "explain this repo")
    assert code == 0
    assert out == "the answer\n"
    spec = world.spec("codex")
    assert spec.prompt == "explain this repo"
    assert spec.model == "m1"
    assert spec.run_dir is not None
    run_dir = spec.run_dir.parent.parent
    assert spec.run_dir == run_dir / "steps" / "01-run-codex"
    assert run_dir.parent == world.home / ".cache" / "ha" / "runs"
    assert (spec.run_dir / "result.json").is_file()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "answered"


def test_a_cli_rail_reads_the_repository_root_read_only(world: _World) -> None:
    sub = world.repo / "deep" / "er"
    sub.mkdir(parents=True)
    code, *_ = world.run("run", "claude", "go", cwd=sub)
    assert code == 0
    workspace = world.spec("claude").profile.workspace
    assert workspace is not None
    assert workspace.path == world.repo.resolve()
    assert workspace.write is False and workspace.shell is False


def test_the_default_read_only_context_is_global(world: _World) -> None:
    world.run("run", "codex", "go")
    context = world.spec("codex").context
    assert context is not None and context.level == "global"
    assert [str(f.source) for f in context.files] == [str(world.home / ".claude" / "CLAUDE.md")]


def test_context_full_adds_the_repository_files(world: _World) -> None:
    world.run("run", "codex", "--context", "full", "go")
    context = world.spec("codex").context
    assert context is not None
    assert str(world.repo.resolve() / "CLAUDE.md") in [str(f.source) for f in context.files]


def test_context_none_carries_nothing(world: _World) -> None:
    world.run("run", "codex", "--context", "none", "go")
    context = world.spec("codex").context
    assert context is None or context.files == ()


def test_the_prompt_is_read_from_stdin_when_dash_or_absent(world: _World) -> None:
    world.run("run", "codex", "-", stdin="from stdin\n")
    assert world.spec("codex").prompt == "from stdin\n"
    world.run("run", "codex", stdin="also stdin")
    assert world.spec("codex").prompt == "also stdin"


def test_an_empty_prompt_is_a_usage_error(world: _World) -> None:
    code, _, err = world.run("run", "codex", stdin="   ")
    assert code == 2 and "no prompt" in err
    assert not world.fakes


def test_json_prints_run_json(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    assert code == 0
    payload = json.loads(out)
    assert payload["schema"] == 1 and payload["text"] == "the answer"
    assert payload["status"] == "answered"
    assert payload["steps"][0]["provider"] == "codex"


def test_a_failed_run_keeps_its_code_and_says_where_the_logs_are(world: _World) -> None:
    _prime(world, "codex", code=1)
    code, out, err = world.run("run", "codex", "go")
    assert code == 1
    assert out == ""
    assert "runs" in err


def test_the_parent_claude_session_markers_never_reach_the_child(world: _World) -> None:
    world.run("run", "claude", "go")
    env = world.spec("claude").environment
    assert env is not None
    assert "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDECODE" not in env
    assert env["KEEP_ME"] == "yes"


def test_run_dir_can_be_named(world: _World, tmp_path: Path) -> None:
    target = tmp_path / "mine"
    code, *_ = world.run("run", "codex", "--run-dir", str(target), "go")
    assert code == 0
    assert world.spec("codex").run_dir == target / "steps" / "01-run-codex"


def test_a_relative_run_dir_is_anchored_at_the_cwd(world: _World, tmp_path: Path) -> None:
    code, *_ = world.run("run", "codex", "--run-dir", "../out-run", "go")
    assert code == 0
    assert world.spec("codex").run_dir == tmp_path / "out-run" / "steps" / "01-run-codex"


@pytest.mark.parametrize("run_dir", ["/", ".", "sub"])
def test_a_run_dir_that_cannot_hold_a_run_is_refused(world: _World, run_dir: str) -> None:
    """No name, an existing directory, or inside the repository: refused, nothing ran."""
    code, _, err = world.run("run", "codex", "--run-dir", run_dir, "go")
    assert code == 2
    assert "run-dir" in err
    assert not world.fakes


def test_timeout_model_and_effort_are_passed(world: _World) -> None:
    world.run("run", "codex", "--timeout", "42", "--effort", "high", "go")
    spec = world.spec("codex")
    assert spec.timeout_seconds == 42.0 and spec.reasoning_effort == "high"


# ── HTTP providers ──────────────────────────────────────────────────────────


def test_an_http_provider_runs_without_a_workspace(world: _World) -> None:
    world.run("run", "mistral", "-m", "mistral-small-latest", "go")
    assert world.spec("mistral").profile.workspace is None


def test_openai_compat_takes_base_url_and_key_env(world: _World) -> None:
    code, *_ = world.run(
        "run",
        "openai-compat",
        "-m",
        "m",
        "--base-url",
        "http://10.0.0.5/v1",
        "--key-env",
        "MY_KEY",
        "go",
    )
    assert code == 0
    assert world.spec("openai-compat").extra == {
        "base_url": "http://10.0.0.5/v1",
        "key_env": "MY_KEY",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("run", "openai-compat", "-m", "m", "--base-url", "http://x/v1", "go"),
        ("run", "openai-compat", "-m", "m", "--key-env", "K", "go"),
        ("run", "codex", "--base-url", "http://x/v1", "--key-env", "K", "go"),
        ("run", "codex", "--shell", "go"),
        ("run", "mistral", "-m", "m", "--write", "go"),
        ("run", "gpt", "go"),
        ("run",),
        ("run", "mistral", "-m", "m", "--mcp", "brain-read", "go"),
        ("run", "codex", "--base", "main", "go"),
    ],
)
def test_invalid_usage_exits_2_without_running(world: _World, argv: tuple[str, ...]) -> None:
    code, _, err = world.run(*argv)
    assert code == 2
    assert err
    assert not any(fake.specs for fake in world.fakes.values())


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (("run", "-p", "codex", "go"), "-p was removed in 0.5.0"),
        (("run", "--provider", "codex", "go"), "-p was removed in 0.5.0"),
        (("run", "codex", "--chain", "codex,claude", "go"), "--chain was removed in 0.5.0"),
    ],
)
def test_the_removed_options_say_what_replaced_them(
    world: _World, argv: tuple[str, ...], needle: str
) -> None:
    code, _, err = world.run(*argv)
    assert code == 2 and needle in err
    assert "roles.toml" in err or "TARGET" in err


def test_write_runs_are_refused_in_this_build(world: _World) -> None:
    """Plan decision P5: write runs return with the spec §3.8.3 protocol."""
    code, _, err = world.run("run", "codex", "--write", "go")
    assert code == 2 and "write runs are not available" in err
    assert not world.fakes


# ── roles: declared targets, chains ─────────────────────────────────────────


def test_a_declared_role_runs_with_its_instructions(world: _World) -> None:
    world.roles('[reviewer]\nprovider = "claude"\ninstructions = "Report findings."\n')
    code, *_ = world.run("run", "reviewer", "go")
    assert code == 0
    context = world.spec("claude").context
    assert context is not None and "Report findings." in context.preamble()
    run_dir = world.spec("claude").run_dir
    assert run_dir is not None and run_dir.name == "01-run-reviewer"


def test_the_chain_of_a_role_advances_on_3_and_stops_on_an_answer(world: _World) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    _prime(world, "codex", code=3)
    _prime(world, "claude")
    code, out, err = world.run("run", "r", "go")
    assert code == 0 and out == "the answer\n"
    assert "codex" in err  # the fallback is reported


def test_the_chain_stops_on_an_ordinary_failure(world: _World) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    _prime(world, "codex", code=1)
    code, *_ = world.run("run", "r", "go")
    assert code == 1
    assert "claude" not in world.fakes


@pytest.mark.parametrize("last", [3, 4])
def test_an_exhausted_chain_returns_the_last_links_code(world: _World, last: int) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    _prime(world, "codex", code=3)
    _prime(world, "claude", code=last)
    code, *_ = world.run("run", "r", "go")
    assert code == last


def test_a_chain_link_names_its_own_model(world: _World) -> None:
    world.roles('[r]\nchain = ["codex:gpt-x", "claude:sonnet-y"]\n')
    _prime(world, "codex", code=3)
    world.run("run", "r", "go")
    assert world.spec("codex").model == "gpt-x"
    assert world.spec("claude").model == "sonnet-y"


def test_minus_m_serves_the_links_without_their_own_model(world: _World) -> None:
    world.roles('[r]\nchain = ["codex:gpt-x", "claude"]\n')
    _prime(world, "codex", code=3)
    world.run("run", "r", "-m", "m1", "go")
    assert world.spec("codex").model == "gpt-x"
    assert world.spec("claude").model == "m1"


def test_models_toml_is_the_last_default(world: _World) -> None:
    _write_models(world.home, {"codex": "declared-model"})
    world.run("run", "codex", "go")
    assert world.spec("codex").model == "declared-model"


def test_a_link_with_no_model_anywhere_is_refused_before_anything_runs(world: _World) -> None:
    _write_models(world.home, {})
    code, _, err = world.run("run", "codex", "go")
    assert code == 2
    assert "-m" in err and "models.toml" in err
    assert not world.fakes


def test_agy_needs_no_model(world: _World) -> None:
    _write_models(world.home, {})
    code, *_ = world.run("run", "agy", "go")
    assert code == 0
    assert world.spec("agy").model == ""


def test_an_invalid_roles_file_is_a_usage_error(world: _World) -> None:
    world.roles('[a]\nprovider = "codex"\nshell = true\n')
    code, _, err = world.run("run", "codex", "go")
    assert code == 2 and "roles.toml" in err and "shell requires write" in err


# ── --mcp ──────────────────────────────────────────────────────────────────


def test_mcp_profile_is_loaded_from_the_config_dir(world: _World) -> None:
    (world.home / ".config" / "ha" / "mcp.toml").write_text(
        '[brain-read]\nurl = "http://127.0.0.1:8765/mcp"\nbearer_env = "BT"\n'
        'tools = ["brain_search"]\n'
    )
    world.environ["BT"] = "tok"
    code, *_ = world.run("run", "codex", "-m", "m", "--mcp", "brain-read", "go")
    assert code == 0
    mcp = world.spec("codex").profile.mcp
    assert mcp is not None and mcp.tools == ("brain_search",)
    environment = world.spec("codex").environment
    assert environment is not None and environment["BT"] == "tok"


def test_an_unknown_mcp_profile_is_a_usage_error(world: _World) -> None:
    code, _, err = world.run("run", "codex", "-m", "m", "--mcp", "nope", "go")
    assert code == 2 and "nope" in err


# ── ha roles ───────────────────────────────────────────────────────────────


def test_roles_lists_the_declared_roles_resolved(world: _World) -> None:
    world.roles(
        '[reviewer]\nprovider = "codex"\neffort = "high"\ninstructions = "abc"\n'
        '[impl]\nchain = ["opencode:m1", "codex"]\nwrite = true\n'
    )
    code, out, _ = world.run("roles", "--json")
    assert code == 0
    rows = {row["name"]: row for row in json.loads(out)}
    assert rows["reviewer"]["links"] == [{"provider": "codex", "model": "codex-default"}]
    assert rows["reviewer"]["effort"] == "high"
    assert rows["reviewer"]["instructions_bytes"] == 3
    assert rows["impl"]["links"] == [
        {"provider": "opencode", "model": "m1"},
        {"provider": "codex", "model": "codex-default"},
    ]
    assert rows["impl"]["write"] is True and rows["impl"]["context"] == "full"
    code, out, _ = world.run("roles")
    assert code == 0 and "reviewer" in out and "impl" in out


def test_roles_on_an_invalid_file_exits_2_naming_the_problem(world: _World) -> None:
    world.roles('[a]\nprovider = "nope"\n')
    code, _, err = world.run("roles")
    assert code == 2 and "roles.toml" in err and "unknown provider 'nope'" in err


def test_roles_without_a_file_lists_nothing(world: _World) -> None:
    code, out, _ = world.run("roles", "--json")
    assert code == 0 and json.loads(out) == []


# ── ha runs / ha clean ─────────────────────────────────────────────────────


def test_runs_lists_the_newest_first(world: _World) -> None:
    world.run("run", "codex", "first")
    time.sleep(0.02)
    world.run("run", "claude", "second")
    code, out, _ = world.run("runs", "--json")
    assert code == 0
    rows = json.loads(out)
    assert [row["target"] for row in rows] == ["claude", "codex"]
    assert {"run_id", "target", "exit_code", "status", "text"} <= rows[0].keys()
    code, out, _ = world.run("runs", "--json", "--limit", "1")
    assert len(json.loads(out)) == 1


def test_runs_lists_a_legacy_run(world: _World) -> None:
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(
        json.dumps({"provider": "codex", "exit_code": 0, "text": "old answer"})
    )
    code, out, _ = world.run("runs", "--json")
    assert code == 0
    (row,) = json.loads(out)
    assert row["status"] == "legacy" and row["target"] == "codex"


def test_runs_with_no_runs_is_empty(world: _World) -> None:
    code, out, _ = world.run("runs", "--json")
    assert code == 0 and json.loads(out) == []


def test_clean_removes_a_finished_run_and_keeps_its_entry(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    run_dir = world.registry().resolve(run_id).run_dir
    code, *_ = world.run("clean", run_id)
    assert code == 0
    assert not run_dir.exists()
    assert world.registry().resolve(run_id).cleaned_at is not None


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("ok")
time.sleep(60)
"""


def test_clean_of_an_active_run_is_refused(world: _World, tmp_path: Path) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(world.registry().lifecycle_lock(run_id)), str(ready)]
    )
    try:
        while not ready.exists():
            time.sleep(0.02)
        code, _, err = world.run("clean", run_id)
    finally:
        holder.kill()
        holder.wait()
    assert code == 2 and "active" in err


@pytest.mark.parametrize(
    ("run_id", "needle"),
    [
        ("absent", "not a run id"),
        ("../etc", "not a run id"),
        ("20260925T000000-aaaaaaaa", "no run"),
    ],
)
def test_clean_refuses_what_is_not_a_run(world: _World, run_id: str, needle: str) -> None:
    code, _, err = world.run("clean", run_id)
    assert code == 2 and needle in err


# ── entry point ────────────────────────────────────────────────────────────


def test_no_subcommand_is_a_usage_error(world: _World) -> None:
    code, *_ = world.run()
    assert code == 2


def test_version_prints_the_installed_package_version(world: _World) -> None:
    """Spec 0.5.0 §3.9: ``ha --version``."""
    from importlib.metadata import version

    code, out, err = world.run("--version")
    assert code == 0
    assert out.strip() == f"ha {version('headless-agents')}"
    assert err == ""


def test_run_help_ends_with_the_exit_codes_and_three_examples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = capsys.readouterr().out
    assert "exit codes" in text.lower()
    for code in ("0", "1", "2", "3", "4", "5", "124"):
        assert f"  {code} " in text
    assert text.count("ha run ") >= 3


def test_the_package_installs_the_ha_command() -> None:
    import tomllib

    pyproject = (
        Path(__file__).resolve().parents[3] / "packages" / "headless-agents" / "pyproject.toml"
    )
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"ha": "headless_agents.cli:main"}


def test_the_readme_synopsis_uses_the_new_grammar() -> None:
    readme = (
        Path(__file__).resolve().parents[3] / "packages" / "headless-agents" / "README.md"
    ).read_text(encoding="utf-8")
    assert "ha run TARGET" in readme
    assert "ha run -p " not in readme


# ── Review Focus 5: Ctrl-C through a real ha and a real provider ─────────────

_FAKE_CLAUDE = """#!/bin/sh
echo $$ > "{pid_file}"
exec sleep 30
"""


def test_an_interrupted_ha_kills_its_provider_and_exits_130(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "claude.pid"
    fake = bin_dir / "claude"
    fake.write_text(_FAKE_CLAUDE.format(pid_file=pid_file))
    fake.chmod(0o755)
    cwd = tmp_path / "work"
    cwd.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    ha = subprocess.Popen(
        [sys.executable, "-m", "headless_agents.cli", "run", "claude", "-m", "m", "task"],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not (pid_file.exists() and pid_file.read_text().strip()):
            assert time.monotonic() < deadline, (
                ha.stderr.read() if ha.poll() is not None else "no pid"
            )
            time.sleep(0.05)
        provider = int(pid_file.read_text())
        ha.send_signal(signal.SIGINT)
        assert ha.wait(timeout=15) == 130
        deadline = time.monotonic() + 6
        while True:
            try:
                os.kill(provider, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "the provider outlived the interrupted ha"
            time.sleep(0.05)
    finally:
        if ha.poll() is None:
            ha.kill()
            ha.wait()
    state = (home / ".local" / "state" / "ha").resolve()
    registry = Registry(state, runs_root=home / ".cache" / "ha" / "runs")
    (entry_path,) = (state / "runs").glob("*.json")
    entry = registry.resolve(entry_path.stem)
    assert entry.status == "running"
    assert registry.effective_status(entry, None) == "incomplete"
    assert locks.is_free(registry.lifecycle_lock(entry.run_id))
    assert locks.is_free(state / "unconfined.lock")
