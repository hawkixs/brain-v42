"""The shape ``implement`` through the engine and the CLI (spec 0.5.0 §3.6, lot 3).

The provider is a fake that edits its workspace like an agent would; the
worktree, the engine commit, the lineage state, the registry, provenance and
the report are the real code and a real git, as in test_write_flow.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import cli, engine, lineage, provenance, write_flow
from headless_agents.engine import Overrides, Request, execute, plan
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec
from headless_agents.templates import implement_prompt

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
IMPLEMENT = {"kind": "workflow", "name": "build", "shape": "implement"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


Edit = Callable[[Path], None]


@dataclass
class _Agent:
    """A provider that applies ``edit`` to its writable workspace and answers ``code``."""

    name: str
    edit: Edit | None = None
    code: int = 0
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        workspace = spec.profile.workspace
        assert workspace is not None and workspace.write
        if self.edit is not None:
            self.edit(workspace.path)
        spec = spec.with_run_dir_defaults()
        return record(
            spec,
            RunResult(
                exit_code=self.code,
                provider=self.name,
                model=spec.model,
                model_reported="served-model",
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=0.1,
                tool_call_completed=False,
                text="I added it" if self.code == 0 else None,
                run_id=run_id_of(spec),
            ),
        )


@dataclass
class World:
    home: Path
    repo: Path
    agent: _Agent
    said: list[str] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")

    def request(self, task: str | None = "Add a flag.", **fields: object) -> Request:
        request = Request(
            target="build",
            prompt=task,
            stdin_is_tty=False,
            overrides=Overrides(),
            base=None,
            repo=None,
            run_dir=None,
            cwd=self.repo,
            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            home=self.home,
        )
        return replace(request, **fields)  # type: ignore[arg-type]

    def implement(self, task: str | None = "Add a flag.", **fields: object) -> engine.Outcome:
        return execute(plan(self.request(task, **fields)), say=self.said.append)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            stdin=io.StringIO(""),
            stdout=out,
            stderr=err,
            cwd=self.repo,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    config = home / ".config" / "ha"
    config.mkdir(parents=True)
    (config / "models.toml").write_text('codex = "codex-default"\nopencode = "oc-default"\n')
    (config / "roles.toml").write_text(
        '[implementer]\nprovider = "codex"\nwrite = true\n\n'
        '[pair]\nchain = ["opencode", "codex"]\nwrite = true\n'
    )
    (config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[build-pair]\nshape = "implement"\nimplement = "pair"\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Op")
    _git(repo, "config", "user.email", "op@example.test")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    agent = _Agent("codex")
    monkeypatch.setattr(engine, "get_provider", lambda name: agent)
    monkeypatch.setattr(
        engine,
        "probe",
        lambda name, **_: Probe(available=True, detail="fake", version=f"{name} 1.0"),
    )
    state = (home / ".local" / "state" / "ha").resolve()
    for rail in CLI_RAILS:
        record_proof(
            state, rail, version=f"{rail} 1.0", isolation=True, confinement=True, today="2026-09-25"
        )
    return World(home=home, repo=repo, agent=agent)


def _flag(root: Path) -> None:
    (root / "app.py").write_text("print('v1')\nVERBOSE = False\n")


def _subjects(world: World, branch: str) -> list[str]:
    return _git(world.repo, "log", "--format=%s", f"main..{branch}").splitlines()


# ── a new implement run (plan Task 6) ──────────────────────────────────────


def test_an_implement_run_commits_on_a_new_lineage(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert outcome.exit_code == 0, world.said
    run_id = outcome.run_id
    branch = f"ha/{run_id}"
    assert _subjects(world, branch) == [f"chore(ha): {run_id} implement via codex/served-model"]
    assert lineage.load(world.state, run_id).members == {run_id: "committed"}
    entry = world.registry().resolve(run_id)
    assert entry.target == IMPLEMENT and entry.lineage == run_id and entry.continues is None
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["target"] == IMPLEMENT and report["status"] == "committed"
    assert [(s["slot"], s["dir"]) for s in report["steps"]] == [
        ("implement", "steps/01-implement-implementer")
    ]
    assert report["implement_providers"] == ["codex"] and report["continues"] is None


def test_the_task_is_kept_as_given_and_the_provider_gets_the_template(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert (outcome.run_dir / "prompt.md").read_text() == "Add a flag."
    assert world.agent.specs[0].prompt == implement_prompt("Add a flag.")


def test_the_engine_commit_is_attributed_to_every_link_of_the_role(world: World) -> None:
    """Plan P6: the vendor rule reads the implementer's providers from the engine's commit."""
    world.agent.edit = _flag
    outcome = world.implement(target="build-pair")
    tip = _git(world.repo, "rev-parse", f"ha/{outcome.run_id}").strip()
    recorded = provenance.lookup(world.state, tip)
    assert recorded is not None and recorded["made_by"] == "engine"
    assert recorded["providers"] == ["opencode", "codex"]


def test_an_implement_run_that_changes_nothing_exits_5(world: World) -> None:
    outcome = world.implement()
    assert outcome.exit_code == write_flow.NO_CHANGE_EXIT_CODE
    assert lineage.load(world.state, outcome.run_id).members == {outcome.run_id: "no_change"}


def test_a_step_code_stays_the_steps_and_the_workflow_exits_1(world: World) -> None:
    """§3.9: codes 3, 4 and 124 stay a step's -- a workflow that wrote cannot promise nothing was."""
    world.agent.edit, world.agent.code = _flag, 3
    outcome = world.implement()
    assert outcome.exit_code == 1
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["exit_code"] == 1 and report["steps"][0]["exit_code"] == 3
    run_id = outcome.run_id
    assert _subjects(world, f"ha/{run_id}") == [
        f"chore(ha): {run_id} residue via codex/served-model"
    ]


def test_the_cli_prints_the_run_id_branch_diffstat_and_patch(world: World) -> None:
    """§3.9: the run id first, so a session can pass it to --continue."""
    world.agent.edit = _flag
    code, out, _ = world.cli("run", "build", "Add a flag.")
    assert code == 0
    (run_id,) = world.registry().run_ids()
    run_dir = world.registry().resolve(run_id).run_dir
    assert out == (
        f"run: {run_id}\nbranch: ha/{run_id}\ndiffstat: +1 -0  1 file\n"
        f"patch: {run_dir / write_flow.PATCH_FILE}\n\nI added it\n"
    )


def test_ha_show_names_the_workflow_and_its_implement_step(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement()
    code, out, _ = world.cli("show", outcome.run_id)
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"{outcome.run_id}  build  exit 0  committed"
    assert lines[2].startswith(f"head    ha/{outcome.run_id} @ ")
    assert lines[3].startswith("  implement  implementer  codex  ")
