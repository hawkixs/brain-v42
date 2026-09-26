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
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli, engine, locks, quarantine
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.report import RUN_KEYS
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

    # The executor gate (spec §3.8.0, Task 15b): every fake CLI rail has a
    # passing isolation proof for the version the faked probe reports.
    monkeypatch.setattr(
        engine,
        "probe",
        lambda name, **_: Probe(available=True, detail="fake", version=f"{name} 1.0"),
    )
    state = (home / ".local" / "state" / "ha").resolve()
    for rail in CLI_RAILS:
        record_proof(state, rail, version=f"{rail} 1.0", isolation=True, today="2026-09-25")
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


def test_a_write_run_reaches_the_write_protocol(world: _World) -> None:
    """Plan Task 22: no more P5 refusal; the write flow runs (here a repository
    with no commit, so preparation cannot resolve the base)."""
    code, _, err = world.run("run", "codex", "--write", "go")
    assert "not available" not in err
    assert code == 1 and "cannot resolve --base" in err


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
    assert rows["reviewer"]["links"] == [
        {
            "provider": "codex",
            "model": "codex-default",
            "isolation": "isolated (2026-09-25)",
            "confinement": None,
        }
    ]
    assert rows["reviewer"]["effort"] == "high"
    assert rows["reviewer"]["instructions_bytes"] == 3
    assert [(link["provider"], link["model"]) for link in rows["impl"]["links"]] == [
        ("opencode", "m1"),
        ("codex", "codex-default"),
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


# ── ha workflows (lot 3) ───────────────────────────────────────────────────

_WORKFLOW_ROLES = (
    '[implementer]\nchain = ["opencode", "codex"]\nwrite = true\n\n[judge]\nprovider = "claude"\n'
)
_WORKFLOWS = (
    '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
    '[duo]\nshape = "review"\nreview = ["codex", "agy"]\njudge = "judge"\n'
)


def _workflows(world: _World, text: str) -> None:
    (world.home / ".config" / "ha" / "workflows.toml").write_text(text)


def test_workflows_json_lists_each_slot_with_its_roles_providers(world: _World) -> None:
    world.roles(_WORKFLOW_ROLES)
    _workflows(world, _WORKFLOWS)
    code, out, _ = world.run("workflows", "--json")
    assert code == 0
    assert json.loads(out) == [
        {
            "name": "build",
            "shape": "implement",
            "slots": [
                {"slot": "implement", "role": "implementer", "providers": ["opencode", "codex"]}
            ],
        },
        {
            "name": "duo",
            "shape": "review",
            "slots": [
                {"slot": "review", "role": "codex", "providers": ["codex"]},
                {"slot": "review", "role": "agy", "providers": ["agy"]},
                {"slot": "judge", "role": "judge", "providers": ["claude"]},
            ],
        },
    ]


def test_workflows_prints_one_line_per_workflow(world: _World) -> None:
    world.roles(_WORKFLOW_ROLES)
    _workflows(world, _WORKFLOWS)
    code, out, _ = world.run("workflows")
    assert code == 0
    assert out.splitlines() == [
        "build                implement  implement: implementer (opencode, codex)",
        "duo                  review     review: codex (codex), agy (agy); judge: judge (claude)",
    ]


def test_workflows_without_a_file_lists_nothing(world: _World) -> None:
    code, out, _ = world.run("workflows", "--json")
    assert code == 0 and json.loads(out) == []


def test_workflows_on_an_invalid_file_exits_2_naming_the_problem(world: _World) -> None:
    _workflows(world, '[build]\nshape = "implement"\nimplement = "codex"\n')
    code, _, err = world.run("workflows")
    assert code == 2
    assert "workflows.toml: [build] the implement slot needs a role with write = true" in err


def test_an_invalid_workflows_file_refuses_a_provider_run(world: _World) -> None:
    """§3.2: validation happens before anything runs, whatever the target."""
    _workflows(world, '[codex]\nshape = "review"\nreview = "claude"\n')
    code, _, err = world.run("run", "codex", "task")
    assert code == 2 and "collides with a provider" in err
    assert "codex" not in world.fakes


def test_the_readme_synopsis_lists_ha_workflows() -> None:
    readme = (
        Path(__file__).resolve().parents[3] / "packages" / "headless-agents" / "README.md"
    ).read_text(encoding="utf-8")
    assert "ha workflows [--json]" in readme


# ── a review and --findings through the CLI (lot 4) ─────────────────────────


class _UnreadableStdin(io.StringIO):
    """A piped stdin a target whose prompt is optional must never read (§3.9)."""

    def read(self, *args: object) -> str:  # type: ignore[override]
        raise AssertionError("stdin was read")


def _review_workflows(world: _World) -> None:
    world.roles(
        '[reviewer]\nprovider = "claude"\n\n[implementer]\nprovider = "codex"\nwrite = true\n'
    )
    _workflows(
        world,
        '[check]\nshape = "review"\nreview = "reviewer"\n\n'
        '[build]\nshape = "implement"\nimplement = "implementer"\n',
    )


def _run_with_stdin(world: _World, stdin: io.StringIO, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        list(argv),
        environ=world.environ,
        stdin=stdin,
        stdout=out,
        stderr=err,
        cwd=world.repo,
        home=world.home,
    )
    return code, out.getvalue(), err.getvalue()


def test_a_review_without_a_prompt_never_reads_a_piped_stdin(world: _World) -> None:
    _review_workflows(world)
    code, _, err = _run_with_stdin(world, _UnreadableStdin("piped"), "run", "check")
    # The engine goes on to the review, which the test repository cannot give a base to.
    assert code == 2 and "stdin was read" not in err


def test_findings_without_a_prompt_never_read_a_piped_stdin(world: _World) -> None:
    _review_workflows(world)
    run_id = "20260926T000000-aaaaaaaa"
    code, _, err = _run_with_stdin(
        world, _UnreadableStdin("piped"), "run", "build", "--findings", run_id
    )
    assert code == 2 and f"no run {run_id}" in err


@pytest.mark.parametrize("target", ["codex", "not-declared"])
def test_findings_on_any_target_never_read_a_piped_stdin(world: _World, target: str) -> None:
    """Codex review of PR C: on a provider or role target, --findings read stdin before the
    engine refused the option -- --findings never reads it unless given ``-`` (§3.9)."""
    _review_workflows(world)
    code, _, err = _run_with_stdin(
        world, _UnreadableStdin("piped"), "run", target, "--findings", "20260926T000000-aaaaaaaa"
    )
    assert code == 2 and "stdin was read" not in err


def test_a_dash_still_reads_stdin_for_a_review(world: _World) -> None:
    _review_workflows(world)
    code, _, err = _run_with_stdin(world, io.StringIO("Mind the errors."), "run", "check", "-")
    assert code == 2 and "stdin was read" not in err


@pytest.mark.parametrize(
    ("argv", "rule"),
    [
        (("run", "check", "--run", "20260926T000000-aaaaaaaa", "--head", "HEAD"), "--run excludes"),
        (("run", "build", "--head", "HEAD", "task"), "--head needs a review workflow"),
        (
            ("run", "check", "--findings", "20260926T000000-aaaaaaaa"),
            "--findings needs an implement",
        ),
    ],
)
def test_the_cli_passes_head_run_and_findings_to_the_engine(
    world: _World, argv: tuple[str, ...], rule: str
) -> None:
    _review_workflows(world)
    code, _, err = world.run(*argv)
    assert code == 2 and rule in err


def test_run_help_names_the_review_exit_codes_and_a_review_example(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = capsys.readouterr().out
    assert "  6 changes requested" in text
    assert "--run RUN_ID" in text and "--findings RUN_ID" in text and "--head REF" in text
    assert "ha run multi-review --run" in text


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
if [ "$1" = "--version" ]; then echo "fake-claude 9.9"; exit 0; fi
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
    record_proof(
        (home / ".local" / "state" / "ha").resolve(),
        "claude",
        version="fake-claude 9.9",
        isolation=True,
        today="2026-09-25",
    )
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


def test_roles_shows_each_write_links_confinement(world: _World) -> None:
    from headless_agents.proofs import record_proof

    record_proof(world.state, "codex", version="codex 1.0", confinement=True, today="2026-09-25")
    world.roles('[w]\nchain = ["codex", "claude"]\nwrite = true\n[r]\nprovider = "codex"\n')
    code, out, _ = world.run("roles", "--json")
    assert code == 0
    rows = {row["name"]: row for row in json.loads(out)}
    assert [link["confinement"] for link in rows["w"]["links"]] == [
        "confined (2026-09-25)",
        "unconfined",
    ]
    assert [link["confinement"] for link in rows["r"]["links"]] == [None]


def test_roles_shows_each_links_isolation(world: _World) -> None:
    world.roles('[r]\nchain = ["codex", "mistral"]\n')
    code, out, _ = world.run("roles", "--json")
    assert code == 0
    (row,) = json.loads(out)
    assert [link["isolation"] for link in row["links"]] == [
        "isolated (2026-09-25)",
        "not needed",
    ]


def test_runs_takes_identity_from_the_registry_not_the_report(world: _World) -> None:
    """Codex review of #207 (round 3): run.json is a report, never an authority (§3.8.1)."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    run_json = world.registry().resolve(run_id).run_dir / "run.json"
    report = json.loads(run_json.read_text())
    report["target"] = {"kind": "role", "name": "forged"}
    run_json.write_text(json.dumps(report))
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["run_id"] == run_id and row["target"] == "codex"


def test_runs_ignores_a_report_whose_run_id_is_not_the_entry(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    run_json = world.registry().resolve(run_id).run_dir / "run.json"
    report = json.loads(run_json.read_text())
    report.update(run_id="20200101T000000-ffffffff", text="forged text", exit_code=7)
    run_json.write_text(json.dumps(report))
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["run_id"] == run_id
    assert row["text"] is None and row["exit_code"] is None


def test_runs_lists_a_cleaned_run_from_its_entry(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    world.run("clean", run_id)
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["run_id"] == run_id and row["target"] == "codex"
    assert row["status"] == "answered" and row["cleaned"] is True


def test_runs_lists_a_run_with_a_custom_run_dir(world: _World, tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere" / "my-run"
    code, out, _ = world.run("run", "codex", "--json", "--run-dir", str(custom), "go")
    assert code == 0
    run_id = json.loads(out)["run_id"]
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["run_id"] == run_id and row["text"] == "the answer"


def test_runs_reads_a_write_run_status_from_its_lineage(world: _World) -> None:
    """A write run's status lives in its lineage state only (§3.8.1)."""
    from headless_agents import lineage as lineages

    run_id = "20260925T000000-eeeeeeee"
    registry = world.registry()
    registry.create(
        run_id, run_dir=None, target={"kind": "role", "name": "w"}, repository=None, lineage=run_id
    )
    lineages.create(
        world.state,
        lineages.LineageState(
            owner=run_id,
            repository=world.home,
            common_dir=world.home / ".git",
            worktree=world.home / "wt",
            branch=f"ha/{run_id}",
            base=None,
            members={run_id: "committed"},
            pending=None,
            compromised=None,
        ),
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["status"] == "committed"


def test_runs_and_clean_survive_a_malformed_registry_entry(world: _World) -> None:
    """Codex review of the lot 2 plan (round 3): lot 1 crashed here with a KeyError."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    path = world.state / "runs" / f"{run_id}.json"
    document = json.loads(path.read_text())
    del document["run_dir"]
    path.write_text(json.dumps(document))
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert code == 0 and row["status"] == "unknown"
    code, _, err = world.run("clean", run_id)
    assert code == 1 and "recover it by hand" in err


# ── the complete ha runs (plan Task 8) ─────────────────────────────────────


def test_runs_rows_carry_the_task_and_the_cost(world: _World) -> None:
    world.run("run", "codex", "Explain the layout.\nDetails.")
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["task"] == "Explain the layout."
    assert {"cost_usd", "cost_complete", "duration_seconds", "text"} <= row.keys()


def test_runs_prints_duration_cost_and_the_task(world: _World) -> None:
    world.run("run", "codex", "Explain the layout.\nDetails.")
    code, out, _ = world.run("runs")
    (line,) = out.splitlines()
    assert code == 0 and "codex" in line and "answered" in line and "exit 0" in line
    assert line.endswith("  Explain the layout.")


def test_runs_cuts_a_long_task(world: _World) -> None:
    world.run("run", "codex", "x" * 80)
    code, out, _ = world.run("runs")
    assert out.rstrip("\n").endswith("x" * 59 + "…")


def test_runs_lists_a_legacy_runs_cost(world: _World) -> None:
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(
        json.dumps(
            {"provider": "codex", "exit_code": 0, "duration_seconds": 30.0, "cost_usd": 0.12}
        )
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["status"] == "legacy" and row["cost_usd"] == 0.12 and row["task"] is None


def test_runs_lists_active_quarantines_first(world: _World) -> None:
    world.run("run", "codex", "go")
    quarantine.publish(
        world.state,
        "operator",
        reason="tripwire",
        run_id="20260925T000000-aaaaaaaa",
        paths=["/x"],
        common_dir=None,
    )
    code, out, _ = world.run("runs")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 2
    assert lines[0].startswith("QUARANTINE operator: tripwire in run 20260925T000000-aaaaaaaa")
    assert lines[0].endswith("lift it by hand after inspection")


def test_runs_json_stays_a_list_and_names_quarantines_on_stderr(world: _World) -> None:
    quarantine.publish(
        world.state,
        "operator",
        reason="tripwire",
        run_id="20260925T000000-aaaaaaaa",
        paths=[],
        common_dir=None,
    )
    code, out, err = world.run("runs", "--json")
    assert code == 0 and json.loads(out) == []
    assert "ha: quarantine operator: tripwire" in err


def test_runs_reads_a_write_run_its_lineage_does_not_list_as_unknown(world: _World) -> None:
    """Same rule as ha show (plan P6): a silent authority is no status."""
    from headless_agents import lineage as lineages

    run_id = "20260925T000000-eeeeeeee"
    world.registry().create(
        run_id, run_dir=None, target={"kind": "role", "name": "w"}, repository=None, lineage=run_id
    )
    lineages.create(
        world.state,
        lineages.LineageState(
            owner=run_id,
            repository=world.home,
            common_dir=world.home / ".git",
            worktree=world.home / "wt",
            branch=f"ha/{run_id}",
            base=None,
            members={},
            pending=None,
            compromised=None,
        ),
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert row["status"] == "unknown"


def test_runs_reads_a_lineage_status_ha_never_writes_as_unknown(world: _World) -> None:
    """Codex review of PR B (round 4): the row read incomplete, from the free lock."""
    from headless_agents import lineage as lineages

    run_id = "20260925T000000-eeeeeeee"
    world.registry().create(
        run_id, run_dir=None, target={"kind": "role", "name": "w"}, repository=None, lineage=run_id
    )
    lineages.create(
        world.state,
        lineages.LineageState(
            owner=run_id,
            repository=world.home,
            common_dir=world.home / ".git",
            worktree=world.home / "wt",
            branch=f"ha/{run_id}",
            base=None,
            members={run_id: "bogus"},
            pending=None,
            compromised=None,
        ),
    )
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert code == 0 and row["status"] == "unknown"


def test_runs_survives_an_unreadable_quarantine_and_entry(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    (world.state / "runs" / f"{run_id}.json").write_text("{not json")
    (world.state / "quarantine").mkdir(parents=True, exist_ok=True)
    (world.state / "quarantine" / "operator.json").write_text("{not json")
    code, out, _ = world.run("runs")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("QUARANTINE unreadable:")
    assert lines[1].startswith(run_id) and "unknown" in lines[1]


def _drop_status(world: _World) -> str:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id: str = json.loads(out)["run_id"]
    path = world.state / "runs" / f"{run_id}.json"
    document = json.loads(path.read_text())
    del document["status"]
    path.write_text(json.dumps(document))
    return run_id


def test_runs_reads_an_entry_without_its_status_as_unknown(world: _World) -> None:
    """Codex review of PR B (round 3): the row read incomplete, from the free lock."""
    _drop_status(world)
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert code == 0 and row["status"] == "unknown"


def test_show_exits_1_on_an_entry_without_its_status(world: _World) -> None:
    """Codex review of PR B (round 3): ha show exited 0 on a status its entry never gave."""
    run_id = _drop_status(world)
    code, out, err = world.run("show", run_id)
    assert code == 1 and "status is malformed" in err
    assert out.splitlines()[0] == f"{run_id}  -  exit -  unknown"


def test_runs_reads_a_target_that_is_not_text_as_unknown(world: _World) -> None:
    """Codex review of PR B (round 1): the row named the run's target "None"."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    path = world.state / "runs" / f"{run_id}.json"
    document = json.loads(path.read_text())
    document["target"] = {"kind": [], "name": None}
    path.write_text(json.dumps(document))
    code, out, _ = world.run("runs", "--json")
    (row,) = json.loads(out)
    assert code == 0 and row["status"] == "unknown" and row["target"] is None


def test_runs_takes_no_task_from_a_directory_a_later_run_reused(
    world: _World, tmp_path: Path
) -> None:
    """Final review of PR B: the registry refuses only an existing --run-dir, so the same
    one is allowed once ha clean removed it -- the cleaned run must not show the new task."""
    custom = tmp_path / "out" / "review"
    code, out, _ = world.run("run", "codex", "--json", "--run-dir", str(custom), "First task.")
    first = json.loads(out)["run_id"]
    world.run("clean", first)
    code, out, _ = world.run("run", "codex", "--json", "--run-dir", str(custom), "Second task.")
    assert code == 0
    code, out, _ = world.run("runs", "--json")
    tasks = {row["run_id"]: row["task"] for row in json.loads(out)}
    assert tasks[first] is None and "Second task." in tasks.values()


def test_runs_survives_documents_nested_too_deep_to_parse(world: _World) -> None:
    """Codex review of PR B (round 2), the same input class: RecursionError, not
    ValueError, escaped the readers -- one such file broke the whole listing."""
    deep = "[" * 100_000 + "]" * 100_000
    code, out, _ = world.run("run", "codex", "--json", "go")
    first = json.loads(out)["run_id"]
    code, out, _ = world.run("run", "codex", "--json", "go")
    second = json.loads(out)["run_id"]
    (world.registry().resolve(second).run_dir / "run.json").write_text(deep)
    (world.state / "runs" / f"{first}.json").write_text(deep)
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(deep)
    code, out, _ = world.run("runs", "--json")
    rows = {row["run_id"]: row for row in json.loads(out)}
    assert code == 0 and rows[first]["status"] == "unknown"
    assert rows[second]["status"] == "answered" and rows[second]["exit_code"] is None


def _strict(name: str) -> object:
    raise AssertionError(f"{name} is not JSON")


def test_runs_survives_a_number_that_is_not_finite(world: _World) -> None:
    """Final review of PR B: json.loads accepts NaN and Infinity -- one such number broke
    the whole listing (round() raised) and made ``--json`` unreadable to a strict parser."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    run_json = world.registry().resolve(run_id).run_dir / "run.json"
    report = json.loads(run_json.read_text())
    report["duration_seconds"] = float("nan")
    run_json.write_text(json.dumps(report))
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(
        '{"provider": "codex", "exit_code": 0, "duration_seconds": Infinity, "cost_usd": 1e999}'
    )
    code, out, _ = world.run("runs")
    assert code == 0 and len(out.splitlines()) == 2
    code, out, _ = world.run("runs", "--json")
    rows = json.loads(out, parse_constant=_strict)
    assert code == 0 and [row["duration_seconds"] for row in rows] == [None, None]


# ── ha show ─────────────────────────────────────────────────────────────────


def test_show_renders_a_finished_run(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "Explain the layout.\nDetails.")
    run_id = json.loads(out)["run_id"]
    code, out, err = world.run("show", run_id)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == f"{run_id}  codex  exit 0  answered"
    assert lines[1] == "task    Explain the layout."
    assert lines[-2:] == ["--- run ---", "the answer"]


def test_show_json_prints_the_rebuilt_report(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    code, out, _ = world.run("show", run_id, "--json")
    report = json.loads(out)
    assert code == 0 and list(report) == list(RUN_KEYS) and report["run_id"] == run_id


def test_show_of_a_cleaned_run_says_so_and_exits_0(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    world.run("clean", run_id)
    code, out, err = world.run("show", run_id)
    assert code == 0 and "removed by ha clean" in err
    assert out.splitlines()[0] == f"{run_id}  codex  exit -  answered"


def test_show_dir_renders_a_run_directory_for_display_only(world: _World) -> None:
    """Spec §3.8.6: readable even after the state directory is lost."""
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_dir = world.home / ".cache" / "ha" / "runs" / json.loads(out)["run_id"]
    shutil.rmtree(world.home / ".local" / "state" / "ha")
    code, out, err = world.run("show", "--dir", str(run_dir))
    assert code == 0 and "display only" in err
    assert out.splitlines()[0].endswith("codex  exit 0  answered")
    code, out, _ = world.run("show", "--dir", str(run_dir), "--json")
    assert code == 0 and json.loads(out)["status"] == "answered"


@pytest.mark.parametrize("argv", [("show",), ("show", "20260925T000000-00000000", "--dir", "/tmp")])
def test_show_takes_a_run_id_or_a_dir_exactly(world: _World, argv: tuple[str, ...]) -> None:
    code, _, err = world.run(*argv)
    assert code == 2 and "a run id or --dir" in err


def test_show_dir_refuses_a_directory_that_holds_no_report(world: _World, tmp_path: Path) -> None:
    code, _, err = world.run("show", "--dir", str(tmp_path))
    assert code == 2 and "run.json" in err


def test_show_refuses_what_is_not_a_registered_run(world: _World) -> None:
    code, _, err = world.run("show", "20260925T000000-00000000")
    assert code == 2 and "no run 20260925T000000-00000000" in err


def test_show_names_a_legacy_run(world: _World) -> None:
    legacy = world.home / ".cache" / "ha" / "runs" / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(json.dumps({"provider": "codex", "exit_code": 0}))
    code, _, err = world.run("show", "20260920T000000-aaaaaaaa")
    assert code == 2 and "0.4.0 run" in err


def test_show_exits_1_when_the_state_cannot_be_read(world: _World) -> None:
    code, out, _ = world.run("run", "codex", "--json", "go")
    run_id = json.loads(out)["run_id"]
    (world.state / "runs" / f"{run_id}.json").write_text("{not json")
    code, out, err = world.run("show", run_id)
    assert code == 1 and "recover it by hand" in err
    assert out.splitlines()[0] == f"{run_id}  -  exit -  unknown"
