"""engine.plan(): every gate that needs neither git nor mutable state (spec 0.5.0 §3.4, §3.8.2).

A unit test drives the engine API directly with each combination the CLI
refuses, and expects the same refusal: a gate only one entry point enforces
is the defect red-lab paid for (f9eff72c).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents.engine import Overrides, Request, UsageError, plan


@dataclass
class Env:
    home: Path
    cwd: Path
    environ: dict[str, str] = field(default_factory=dict)

    @property
    def config(self) -> Path:
        return self.home / ".config" / "ha"

    def roles(self, text: str) -> None:
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "roles.toml").write_text(text)

    def mcp(self, text: str) -> None:
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "mcp.toml").write_text(text)

    def request(
        self,
        target: str,
        prompt: str | None,
        *,
        overrides: Overrides | None = None,
        stdin_is_tty: bool = False,
        base: str | None = None,
        repo: Path | None = None,
        run_dir: Path | None = None,
    ) -> Request:
        return Request(
            target=target,
            prompt=prompt,
            stdin_is_tty=stdin_is_tty,
            overrides=overrides or Overrides(),
            base=base,
            repo=repo,
            run_dir=run_dir,
            cwd=self.cwd,
            environ={"PATH": "/usr/bin:/bin", "HOME": str(self.home), **self.environ},
            home=self.home,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    (home / ".config" / "ha" / "models.toml").write_text(
        'codex = "codex-default"\nclaude = "claude-default"\nopencode = "oc-default"\n'
        'openrouter = "or-default"\n"openai-compat" = "oc-model"\n'
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return Env(home=home, cwd=cwd)


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """plan() runs no git -- and no subprocess at all (spec §3.8.2)."""
    calls: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError(f"plan() started a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    yield calls
    assert calls == []


@pytest.mark.parametrize(
    ("target", "overrides", "rule"),
    [
        ("codex", Overrides(shell=True), "shell requires write"),
        ("openrouter", Overrides(write=True), "write needs a CLI rail"),
        ("openrouter", Overrides(mcp="brain-read"), "mcp needs a CLI rail"),
        ("openai-compat", Overrides(), "openai-compat needs base_url and key_env"),
        ("codex", Overrides(base_url="http://x"), "apply to openai-compat only"),
        ("codex", Overrides(mcp="missing"), "mcp profile 'missing' is not in mcp.toml"),
        ("nobody", Overrides(), "unknown target 'nobody'"),
    ],
)
def test_the_engine_refuses_what_the_cli_refuses(
    env: Env, target: str, overrides: Overrides, rule: str
) -> None:
    env.mcp('[brain-read]\nurl = "http://127.0.0.1:8765/mcp"\nbearer_env = "T"\n')
    with pytest.raises(UsageError, match=rule):
        plan(env.request(target, "task", overrides=overrides))


def test_base_needs_a_write_run(env: Env) -> None:
    with pytest.raises(UsageError, match="--base needs a write run"):
        plan(env.request("codex", "task", base="main"))


def test_write_runs_are_refused_until_the_write_protocol_lands(env: Env) -> None:
    """Plan decision P5: PR B refuses every write run."""
    env.roles('[impl]\nprovider = "codex"\nwrite = true\n')
    with pytest.raises(UsageError, match="write runs are not available in this build"):
        plan(env.request("impl", "task"))
    with pytest.raises(UsageError, match="write runs are not available in this build"):
        plan(env.request("codex", "task", overrides=Overrides(write=True)))


def test_no_prompt_on_a_terminal_is_refused(env: Env) -> None:
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("codex", None, stdin_is_tty=True))


@pytest.mark.parametrize("prompt", ["", "  \n\t"])
def test_an_empty_prompt_from_a_closed_pipe_is_refused(env: Env, prompt: str) -> None:
    """Review Focus 2: the CLI hands the engine what a closed or empty stdin gave."""
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("codex", prompt))


def test_every_link_is_checked_for_prompt_size(env: Env) -> None:
    env.roles('[r]\nchain = ["codex:m", "agy"]\n')
    with pytest.raises(UsageError, match=r"agy.*120000.*bytes"):
        plan(env.request("r", "x" * 130_000))


def test_role_instructions_count_against_the_claude_bound(env: Env) -> None:
    env.roles(f'[r]\nprovider = "claude"\ninstructions = """{"i" * 131_100}"""\n')
    with pytest.raises(UsageError, match="131071"):
        plan(env.request("r", "short"))


def test_a_model_is_required_before_anything_runs(env: Env) -> None:
    (env.config / "models.toml").write_text("")
    with pytest.raises(UsageError, match="codex needs a model"):
        plan(env.request("codex", "task"))


def test_the_overrides_reach_the_plan(env: Env, no_subprocess: list[object]) -> None:
    env.roles('[rev]\nprovider = "codex"\neffort = "high"\ninstructions = "be terse"\n')
    planned = plan(
        env.request("rev", "task", overrides=Overrides(model="m1", timeout=42.0, context="none"))
    )
    assert planned.role.name == "rev"
    assert (planned.role.effort, planned.role.timeout, planned.role.context) == (
        "high",
        42.0,
        "none",
    )
    assert planned.models == {"codex": "m1"}
    assert planned.prompt == "task"
    assert planned.run_dir is None
    assert planned.state == (env.home / ".local" / "state" / "ha").resolve()


def test_the_role_model_is_used_without_m(env: Env) -> None:
    env.roles('[rev]\nprovider = "codex"\nmodel = "from-role"\n')
    assert plan(env.request("rev", "task")).models == {"codex": "from-role"}


def test_an_mcp_role_resolves_its_profile(env: Env) -> None:
    env.mcp('[brain-read]\nurl = "http://127.0.0.1:8765/mcp"\nbearer_env = "T"\n')
    env.roles('[r]\nprovider = "codex"\nmcp = "brain-read"\n')
    planned = plan(env.request("r", "task"))
    assert planned.mcp is not None and planned.mcp.bearer_env_var == "T"


def test_a_relative_run_dir_is_anchored_at_the_cwd(env: Env) -> None:
    planned = plan(env.request("codex", "task", run_dir=Path("out/run")))
    assert planned.run_dir == env.cwd / "out" / "run"


def test_a_run_dir_with_no_name_is_refused(env: Env) -> None:
    with pytest.raises(UsageError, match="has no name"):
        plan(env.request("codex", "task", run_dir=Path("/")))


def test_the_parent_session_does_not_leak_into_the_child(env: Env) -> None:
    env.environ.update({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "KEEP_ME": "yes"})
    planned = plan(env.request("codex", "task"))
    assert "CLAUDECODE" not in planned.environment
    assert "CLAUDE_CODE_ENTRYPOINT" not in planned.environment
    assert planned.environment["KEEP_ME"] == "yes"


def test_an_invalid_roles_file_is_a_usage_error(env: Env) -> None:
    env.roles('[a]\nprovider = "codex"\nshell = true\n')
    with pytest.raises(UsageError, match=r"roles\.toml: \[a\] shell requires write"):
        plan(env.request("codex", "task"))


def test_a_roles_file_linking_outside_the_config_dir_is_a_usage_error(
    env: Env, tmp_path: Path
) -> None:
    planted = tmp_path / "repo-roles.toml"
    planted.write_text('[x]\nprovider = "codex"\n')
    (env.config / "roles.toml").symlink_to(planted)
    with pytest.raises(UsageError, match="outside the configuration directory"):
        plan(env.request("codex", "task"))
