"""The ``ha`` CLI (spec 3.4): providers, read-only run, chain, MCP profiles, runs, clean.

Providers are replaced by recording fakes (a real run needs a CLI and quota);
everything else -- argument validation, exit codes, run directories, the
repository root, the context bundle, the environment -- is the real code.
The ``--write`` flow has its own file.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
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

    def run(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ=self.environ,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            cwd=self.repo,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()

    def spec(self, provider: str, index: int = -1) -> RunSpec:
        return self.fakes[provider].specs[index]


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
        if name not in cli.PROVIDER_NAMES:
            raise cli.UnknownProvider(name)
        fakes.setdefault(name, _Fake(name))
        return fakes[name]

    monkeypatch.setattr(cli, "get_provider", fake_provider)
    environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDECODE": "1",
        "KEEP_ME": "yes",
    }
    # The operator's declared defaults: every rail that needs a model has one,
    # as on a configured machine; the tests about models override or empty it.
    _write_models(home, {name: f"{name}-default" for name in cli.PROVIDER_NAMES if name != "agy"})
    return _World(home=home, repo=repo, environ=environ, fakes=fakes)


def _write_models(home: Path, models: dict[str, str]) -> Path:
    path = home / ".config" / "ha" / "models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'"{name}" = "{model}"\n' for name, model in models.items()))
    return path


def _prime(world: _World, name: str, **kwargs: object) -> _Fake:
    fake = _Fake(name, **kwargs)  # type: ignore[arg-type]
    world.fakes[name] = fake
    return fake


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


# ── ha run: the read-only flow ──────────────────────────────────────────────


def test_run_prints_the_answer_and_records_the_run(world: _World) -> None:
    code, out, _ = world.run("run", "-p", "codex", "-m", "m1", "explain this repo")
    assert code == 0
    assert out == "the answer\n"
    spec = world.spec("codex")
    assert spec.prompt == "explain this repo"
    assert spec.model == "m1"
    assert spec.run_dir is not None
    assert spec.run_dir.parent == world.home / ".cache" / "ha" / "runs"
    assert (spec.run_dir / "result.json").is_file()


def test_a_cli_rail_reads_the_repository_root_read_only(world: _World) -> None:
    sub = world.repo / "deep" / "er"
    sub.mkdir(parents=True)
    code = cli.main(
        ["run", "-p", "claude", "go"],
        environ=world.environ,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        cwd=sub,
        home=world.home,
    )
    assert code == 0
    workspace = world.spec("claude").profile.workspace
    assert workspace is not None
    assert workspace.path == world.repo
    assert workspace.write is False and workspace.shell is False


def test_the_default_read_only_context_is_global(world: _World) -> None:
    world.run("run", "-p", "codex", "go")
    context = world.spec("codex").context
    assert context is not None and context.level == "global"
    assert [str(f.source) for f in context.files] == [str(world.home / ".claude" / "CLAUDE.md")]


def test_context_full_adds_the_repository_files(world: _World) -> None:
    world.run("run", "-p", "codex", "--context", "full", "go")
    context = world.spec("codex").context
    assert context is not None
    assert str(world.repo / "CLAUDE.md") in [str(f.source) for f in context.files]


def test_context_none_carries_nothing(world: _World) -> None:
    world.run("run", "-p", "codex", "--context", "none", "go")
    context = world.spec("codex").context
    assert context is None or context.files == ()


def test_the_prompt_is_read_from_stdin_when_dash_or_absent(world: _World) -> None:
    world.run("run", "-p", "codex", "-", stdin="from stdin\n")
    assert world.spec("codex").prompt == "from stdin\n"
    world.run("run", "-p", "codex", stdin="also stdin")
    assert world.spec("codex").prompt == "also stdin"


def test_an_empty_prompt_is_a_usage_error(world: _World) -> None:
    code, _, err = world.run("run", "-p", "codex", stdin="   ")
    assert code == 2 and "prompt" in err


def test_json_prints_the_schema_1_result(world: _World) -> None:
    code, out, _ = world.run("run", "-p", "codex", "--json", "go")
    assert code == 0
    payload = json.loads(out)
    assert payload["schema"] == 1 and payload["text"] == "the answer"


def test_a_failed_run_keeps_its_code_and_says_where_the_logs_are(world: _World) -> None:
    _prime(world, "codex", code=1)
    code, out, err = world.run("run", "-p", "codex", "go")
    assert code == 1
    assert out == ""
    assert "runs" in err


def test_the_parent_claude_session_markers_never_reach_the_child(world: _World) -> None:
    world.run("run", "-p", "claude", "go")
    env = world.spec("claude").environment
    assert env is not None
    assert "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDECODE" not in env
    assert env["KEEP_ME"] == "yes"


def test_run_dir_can_be_named(world: _World, tmp_path: Path) -> None:
    target = tmp_path / "mine"
    world.run("run", "-p", "codex", "--run-dir", str(target), "go")
    assert world.spec("codex").run_dir == target


def test_a_relative_run_dir_is_anchored_at_the_cwd_and_named(world: _World) -> None:
    # ``--run-dir .`` has an empty raw ``.name``: the run id, the ``ha-<id>``
    # prefix and the ``ha/<id>`` branch of a write run all read it.
    world.run("run", "-p", "codex", "--run-dir", ".", "go")
    run_dir = world.spec("codex").run_dir
    assert run_dir == world.repo
    assert run_dir is not None and run_dir.name == world.repo.name


@pytest.mark.parametrize(
    "argv",
    [
        ("-p", "codex"),
        ("--chain", "codex,claude"),
        ("-p", "claude", "--write"),
    ],
)
def test_a_run_dir_with_no_name_is_refused_before_anything_runs(
    world: _World, argv: tuple[str, ...]
) -> None:
    # Independent review of PR #197, finding 3: a chain derives
    # ``/links/0-codex`` and a write run ``/wt`` and ``ha/`` BEFORE any RunSpec
    # checks the name, so the root must be refused where it is planned.
    code, _, err = world.run("run", *argv, "--run-dir", "/", "go")
    assert code == 2
    assert "run-dir" in err
    assert not any(fake.specs for fake in world.fakes.values())


def test_timeout_model_and_effort_are_passed(world: _World) -> None:
    world.run("run", "-p", "codex", "--timeout", "42", "--effort", "high", "go")
    spec = world.spec("codex")
    assert spec.timeout_seconds == 42.0 and spec.reasoning_effort == "high"


# ── HTTP providers ──────────────────────────────────────────────────────────


def test_an_http_provider_runs_without_a_workspace(world: _World) -> None:
    world.run("run", "-p", "mistral", "-m", "mistral-small-latest", "go")
    assert world.spec("mistral").profile.workspace is None


def test_openai_compat_takes_base_url_and_key_env(world: _World) -> None:
    code, *_ = world.run(
        "run",
        "-p",
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
    extra = world.spec("openai-compat").extra
    assert extra == {"base_url": "http://10.0.0.5/v1", "key_env": "MY_KEY"}


@pytest.mark.parametrize(
    "argv",
    [
        ("run", "-p", "openai-compat", "-m", "m", "--base-url", "http://x/v1", "go"),
        ("run", "-p", "openai-compat", "-m", "m", "--key-env", "K", "go"),
        ("run", "-p", "codex", "--base-url", "http://x/v1", "--key-env", "K", "go"),
        ("run", "-p", "mistral", "-m", "m", "--key-env", "K", "--base-url", "http://x", "go"),
        ("run", "-p", "codex", "--shell", "go"),
        ("run", "-p", "mistral", "-m", "m", "--write", "go"),
        ("run", "-p", "gpt", "go"),
        ("run", "go"),
        ("run", "-p", "codex", "--chain", "codex,gpt", "go"),
        ("run", "-p", "mistral", "-m", "m", "--mcp", "brain-read", "go"),
    ],
)
def test_invalid_usage_exits_2_without_running(world: _World, argv: tuple[str, ...]) -> None:
    code, _, err = world.run(*argv)
    assert code == 2
    assert err
    assert not any(fake.specs for fake in world.fakes.values())


# ── --chain ────────────────────────────────────────────────────────────────


def test_the_chain_advances_on_3_and_stops_on_an_answer(world: _World) -> None:
    _prime(world, "codex", code=3)
    _prime(world, "claude")
    code, out, err = world.run("run", "--chain", "codex,claude", "go")
    assert code == 0 and out == "the answer\n"
    assert len(world.fakes["codex"].specs) == 1 and len(world.fakes["claude"].specs) == 1
    assert "codex" in err  # the fallback is reported


def test_the_chain_stops_on_an_ordinary_failure(world: _World) -> None:
    _prime(world, "codex", code=1)
    _prime(world, "claude")
    code, *_ = world.run("run", "--chain", "codex,claude", "go")
    assert code == 1
    assert world.fakes["claude"].specs == []


@pytest.mark.parametrize("last", [3, 4])
def test_an_exhausted_chain_returns_the_last_links_code(world: _World, last: int) -> None:
    _prime(world, "codex", code=3)
    _prime(world, "claude", code=last)
    code, *_ = world.run("run", "--chain", "codex,claude", "go")
    assert code == last


def test_each_link_gets_its_own_directory_and_the_run_keeps_the_final_result(world: _World) -> None:
    _prime(world, "codex", code=3)
    _prime(world, "claude")
    world.run("run", "--chain", "codex,claude", "--json", "go")
    first, second = world.spec("codex").run_dir, world.spec("claude").run_dir
    assert first is not None and second is not None and first != second
    assert first.parent == second.parent
    top = second.parent.parent
    assert json.loads((top / "result.json").read_text())["provider"] == "claude"


# ── models: per link, -m, models.toml ─────────────────────────────────────


def test_a_chain_link_names_its_own_model(world: _World) -> None:
    # e2e 2026-09-24: one -m shared by every link made `--chain codex,claude`
    # unusable -- no model name is valid on both rails.
    _prime(world, "codex", code=3)
    _prime(world, "claude")
    code, *_ = world.run("run", "--chain", "codex:gpt-x,claude:sonnet-y", "go")
    assert code == 0
    assert world.spec("codex").model == "gpt-x"
    assert world.spec("claude").model == "sonnet-y"


def test_a_model_keeps_everything_after_the_first_colon(world: _World) -> None:
    world.run("run", "--chain", "openrouter:meta/llama:free", "go")
    assert world.spec("openrouter").model == "meta/llama:free"


def test_minus_m_serves_the_links_without_their_own_model(world: _World) -> None:
    _prime(world, "codex", code=3)
    _prime(world, "claude")
    world.run("run", "--chain", "codex:gpt-x,claude", "-m", "m1", "go")
    assert world.spec("codex").model == "gpt-x"
    assert world.spec("claude").model == "m1"


def test_models_toml_is_the_last_default(world: _World) -> None:
    _write_models(world.home, {"codex": "declared-model"})
    world.run("run", "-p", "codex", "go")
    assert world.spec("codex").model == "declared-model"


def test_xdg_config_home_locates_models_toml(world: _World, tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "ha").mkdir(parents=True)
    (xdg / "ha" / "models.toml").write_text('codex = "from-xdg"\n')
    world.environ["XDG_CONFIG_HOME"] = str(xdg)
    world.run("run", "-p", "codex", "go")
    assert world.spec("codex").model == "from-xdg"


@pytest.mark.parametrize(
    "argv", [("-p", "codex"), ("--chain", "codex:gpt-x,claude"), ("-p", "opencode", "--write")]
)
def test_a_link_with_no_model_anywhere_is_refused_before_anything_runs(
    world: _World, argv: tuple[str, ...]
) -> None:
    # e2e 2026-09-24: without -m the rails refused ("model must not be
    # empty") -- and opencode's refusal exited 3, which a chain reads as
    # "unavailable, try the next link": a configuration error fell through.
    _write_models(world.home, {})
    code, _, err = world.run("run", *argv, "go")
    assert code == 2
    assert "-m" in err and "models.toml" in err
    assert not any(fake.specs for fake in world.fakes.values())


def test_agy_needs_no_model(world: _World) -> None:
    _write_models(world.home, {})
    code, *_ = world.run("run", "-p", "agy", "go")
    assert code == 0
    assert world.spec("agy").model == ""


def test_a_provider_twice_in_a_chain_is_refused(world: _World) -> None:
    code, _, err = world.run("run", "--chain", "codex:a,codex:b", "go")
    assert code == 2 and "codex" in err and "more than once" in err
    assert not any(fake.specs for fake in world.fakes.values())


@pytest.mark.parametrize(
    ("content", "needle"),
    [('nope = "m"\n', "nope"), ("codex = 3\n", "codex"), ("codex = [\n", "models.toml")],
)
def test_a_broken_models_toml_is_a_usage_error(world: _World, content: str, needle: str) -> None:
    path = world.home / ".config" / "ha" / "models.toml"
    path.write_text(content)
    code, _, err = world.run("run", "-p", "codex", "go")
    assert code == 2 and needle in err


# ── --mcp ──────────────────────────────────────────────────────────────────


def test_mcp_profile_is_loaded_from_the_xdg_config(world: _World, tmp_path: Path) -> None:
    config = tmp_path / "xdg"
    (config / "ha").mkdir(parents=True)
    (config / "ha" / "mcp.toml").write_text(
        '[brain-read]\nurl = "http://127.0.0.1:8765/mcp"\nbearer_env = "BT"\ntools = ["brain_search"]\n'
    )
    world.environ["XDG_CONFIG_HOME"] = str(config)
    world.environ["BT"] = "tok"
    code, *_ = world.run("run", "-p", "codex", "-m", "m", "--mcp", "brain-read", "go")
    assert code == 0
    mcp = world.spec("codex").profile.mcp
    assert mcp is not None and mcp.tools == ("brain_search",)
    assert world.spec("codex").environment is not None
    assert world.spec("codex").environment["BT"] == "tok"


def test_an_unknown_mcp_profile_is_a_usage_error(world: _World, tmp_path: Path) -> None:
    world.environ["XDG_CONFIG_HOME"] = str(tmp_path / "nothing")
    code, _, err = world.run("run", "-p", "codex", "-m", "m", "--mcp", "nope", "go")
    assert code == 2 and "MCP profile" in err


# ── ha runs / ha clean ─────────────────────────────────────────────────────


def _runs_root(world: _World) -> Path:
    return world.home / ".cache" / "ha" / "runs"


def test_runs_lists_the_newest_first(world: _World) -> None:
    world.run("run", "-p", "codex", "first")
    world.run("run", "-p", "claude", "second")
    code, out, _ = world.run("runs", "--json")
    assert code == 0
    rows = json.loads(out)
    assert [row["provider"] for row in rows] == ["claude", "codex"]
    assert {"run_id", "provider", "exit_code", "text"} <= rows[0].keys()
    code, out, _ = world.run("runs", "--json", "--limit", "1")
    assert len(json.loads(out)) == 1


def test_runs_with_no_runs_is_empty(world: _World) -> None:
    code, out, _ = world.run("runs", "--json")
    assert code == 0 and json.loads(out) == []


def test_clean_removes_one_run(world: _World) -> None:
    world.run("run", "-p", "codex", "go")
    (run_dir,) = list(_runs_root(world).iterdir())
    code, *_ = world.run("clean", run_dir.name)
    assert code == 0
    assert not run_dir.exists()


@pytest.mark.parametrize(
    ("run_id", "expected"), [("absent", 1), ("../etc", 2), ("a/b", 2), (".", 2)]
)
def test_clean_refuses_what_is_not_a_run(world: _World, run_id: str, expected: int) -> None:
    code, _, err = world.run("clean", run_id)
    assert code == expected and err


def test_no_subcommand_is_a_usage_error(world: _World) -> None:
    code, *_ = world.run()
    assert code == 2


def test_the_package_installs_the_ha_command() -> None:
    import tomllib

    pyproject = (
        Path(__file__).resolve().parents[3] / "packages" / "headless-agents" / "pyproject.toml"
    )
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"ha": "headless_agents.cli:main"}


Runner = Callable[..., tuple[int, str, str]]
