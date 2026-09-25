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
from headless_agents.registry import max_prompt_bytes
from headless_agents.runs import Registry
from headless_agents.templates import implement_prompt


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

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def register(self, run_id: str, *, target: dict[str, str], lineage: str | None) -> None:
        Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs").create(
            run_id, run_dir=None, target=target, repository=self.cwd, lineage=lineage
        )

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
        continue_run: str | None = None,
    ) -> Request:
        return Request(
            target=target,
            prompt=prompt,
            stdin_is_tty=stdin_is_tty,
            overrides=overrides or Overrides(),
            base=base,
            repo=repo,
            run_dir=run_dir,
            continue_run=continue_run,
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


def test_a_write_role_is_planned_for_the_write_protocol(env: Env) -> None:
    """Plan Task 22 lifts the P5 refusal: a write run now reaches the write flow."""
    env.roles('[impl]\nprovider = "codex"\nwrite = true\n')
    assert plan(env.request("impl", "task")).role.write
    assert plan(env.request("codex", "task", overrides=Overrides(write=True))).role.write


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


def test_an_invalid_workflows_file_refuses_every_target(env: Env) -> None:
    """§3.2: validated before anything runs, whatever the target -- a provider's included."""
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "codex"\n'
    )
    with pytest.raises(UsageError, match=r"workflows\.toml: \[build\] the implement slot needs"):
        plan(env.request("codex", "task"))


def test_a_workflows_file_linking_outside_the_config_dir_is_a_usage_error(
    env: Env, tmp_path: Path
) -> None:
    """§3.3: a workflows.toml shipped in a repository could name a write role."""
    planted = tmp_path / "repo-workflows.toml"
    planted.write_text('[build]\nshape = "review"\nreview = "codex"\n')
    (env.config / "workflows.toml").symlink_to(planted)
    with pytest.raises(UsageError, match="outside the configuration directory"):
        plan(env.request("codex", "task"))


# ── a workflow target (lot 3) ──────────────────────────────────────────────


RUN = "20260926T100000-aaaaaaaa"
OWNER = "20260926T090000-bbbbbbbb"
IMPLEMENT = {"kind": "workflow", "name": "build", "shape": "implement"}


def _workflows(env: Env) -> None:
    env.roles(
        '[implementer]\nprovider = "codex"\nwrite = true\n\n[reviewer]\nprovider = "claude"\n'
    )
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[check]\nshape = "review"\nreview = "reviewer"\n'
    )


def test_a_workflow_target_plans_its_implement_role_on_the_template(env: Env) -> None:
    _workflows(env)
    planned = plan(env.request("build", "Add a flag."))
    assert planned.workflow is not None and planned.workflow.name == "build"
    assert planned.role.name == "implementer" and planned.role.write
    assert planned.task == "Add a flag." and planned.prompt == implement_prompt("Add a flag.")


def test_a_role_target_keeps_its_task_as_its_prompt(env: Env) -> None:
    planned = plan(env.request("codex", "Explain."))
    assert planned.workflow is None and planned.task == planned.prompt == "Explain."


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        (Overrides(model="m"), "-m"),
        (Overrides(effort="high"), "--effort"),
        (Overrides(timeout=5.0), "--timeout"),
        (Overrides(context="none"), "--context"),
        (Overrides(context_parents=True), "--context-parents"),
        (Overrides(mcp="p"), "--mcp"),
        (Overrides(write=True), "--write"),
        (Overrides(shell=True), "--shell"),
        (Overrides(base_url="http://x"), "--base-url"),
        (Overrides(key_env="K"), "--key-env"),
    ],
)
def test_a_workflow_target_refuses_every_override(
    env: Env, overrides: Overrides, flag: str
) -> None:
    """§3.3, §3.9: a workflow runs its roles as declared; none of its options is a capability."""
    _workflows(env)
    with pytest.raises(UsageError, match=f"runs its roles as declared, so {flag} is refused"):
        plan(env.request("build", "task", overrides=overrides))


def test_a_review_workflow_is_refused_until_its_shape_ships(env: Env) -> None:
    """Spec §5, lot 4: no lot exposes a review that does not enforce the vendor rule."""
    _workflows(env)
    with pytest.raises(UsageError, match=r"review shape is not available.*ha run reviewer"):
        plan(env.request("check", "task"))


def test_an_implement_workflow_needs_a_task(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("build", None, stdin_is_tty=True))


def test_the_implement_template_counts_against_the_prompt_limit(env: Env) -> None:
    """§3.4: the size checked is the prompt the provider gets -- the template around the task."""
    env.roles('[implementer]\nprovider = "opencode"\nwrite = true\n')
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n'
    )
    limit = max_prompt_bytes("opencode")
    assert limit is not None
    task = "x" * (limit - 50)
    plan(env.request("opencode", task))
    with pytest.raises(UsageError, match="opencode: the prompt with its context exceeds"):
        plan(env.request("build", task))


def test_an_unknown_target_lists_the_workflows_too(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match=r"workflows, roles and providers: .*\bbuild\b"):
        plan(env.request("nobody", "task"))


def test_continue_needs_an_implement_workflow_target(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="--continue needs an implement workflow"):
        plan(env.request("codex", "task", continue_run=RUN))


def test_continue_and_base_exclude_each_other(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="--base and --continue exclude each other"):
        plan(env.request("build", "task", base="main", continue_run=RUN))


@pytest.mark.parametrize(("run_id", "rule"), [("nope", "not a run id"), (RUN, f"no run {RUN}")])
def test_continue_refuses_what_names_no_registered_run(env: Env, run_id: str, rule: str) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match=rule):
        plan(env.request("build", "task", continue_run=run_id))


def test_continue_refuses_an_unreadable_entry(env: Env) -> None:
    _workflows(env)
    (env.state / "runs").mkdir(parents=True)
    (env.state / "runs" / f"{RUN}.json").write_text("{not json")
    with pytest.raises(UsageError, match="recover it by hand"):
        plan(env.request("build", "task", continue_run=RUN))


@pytest.mark.parametrize(
    ("target", "lineage"),
    [
        ({"kind": "provider", "name": "codex"}, None),
        ({"kind": "role", "name": "implementer"}, RUN),
        ({"kind": "workflow", "name": "check", "shape": "review"}, None),
    ],
    ids=["read-only-run", "role-write-run", "review-run"],
)
def test_continue_refuses_a_run_that_is_not_an_implement_run(
    env: Env, target: dict[str, str], lineage: str | None
) -> None:
    """§3.6: only a member of an implement lineage can be continued."""
    _workflows(env)
    env.register(RUN, target=target, lineage=lineage)
    with pytest.raises(UsageError, match=f"--continue {RUN}: not an implement run"):
        plan(env.request("build", "task", continue_run=RUN))


def test_continue_names_the_run_and_the_lineage_it_joins(
    env: Env, no_subprocess: list[object]
) -> None:
    """Read from the registry only: plan() starts no subprocess (§3.8.2)."""
    _workflows(env)
    env.register(RUN, target=IMPLEMENT, lineage=OWNER)
    planned = plan(env.request("build", "Fix it.", continue_run=RUN))
    assert planned.continues == RUN and planned.joins == OWNER
