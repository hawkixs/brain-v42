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
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import cli, engine, lineage, locks, provenance, quarantine, write_flow
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
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


# ── --continue (plan Tasks 7 and 8) ────────────────────────────────────────


def _fix(root: Path) -> None:
    (root / "app.py").write_text("print('v1')\nVERBOSE = True\n")


def _first(world: World) -> engine.Outcome:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert outcome.exit_code == 0, world.said
    return outcome


def _continue(
    world: World, run_id: str, task: str = "Turn it on.", **fields: object
) -> engine.Outcome:
    world.agent.edit = _fix
    return world.implement(task, continue_run=run_id, **fields)


def _run_dirs(world: World) -> list[str]:
    return sorted(path.name for path in (world.home / ".cache" / "ha" / "runs").iterdir())


def _assert_no_trace(world: World, first: str) -> None:
    """A refused continuation leaves no run behind: no entry, no directory, no agent run."""
    assert world.registry().run_ids() == [first]
    assert _run_dirs(world) == [first]
    assert len(world.agent.specs) == 1


def test_a_continuation_commits_on_the_lineages_branch(world: World) -> None:
    first = _first(world)
    second = _continue(world, first.run_id)
    assert second.exit_code == 0, world.said
    owner, run_id = first.run_id, second.run_id
    branch = f"ha/{owner}"
    assert _subjects(world, branch) == [
        f"chore(ha): {run_id} implement via codex/served-model",
        f"chore(ha): {owner} implement via codex/served-model",
    ]
    assert lineage.load(world.state, owner).members == {owner: "committed", run_id: "committed"}
    workspace = world.agent.specs[-1].profile.workspace
    assert workspace is not None and workspace.path == first.run_dir / "wt"
    entry = world.registry().resolve(run_id)
    assert entry.lineage == owner and entry.continues == owner
    report = json.loads((second.run_dir / "run.json").read_text())
    assert report["lineage"] == owner and report["continues"] == owner
    assert report["branch"] == branch
    tip = _git(world.repo, "rev-parse", branch).strip()
    assert provenance.lookup(world.state, tip) == {
        "sha": tip,
        "run_id": run_id,
        "lineage": owner,
        "made_by": "engine",
        "providers": ["codex"],
    }
    patch = (second.run_dir / write_flow.PATCH_FILE).read_text()
    assert "+VERBOSE = True" in patch and "VERBOSE = False" not in patch


def test_a_continuation_may_name_any_member_of_the_lineage(world: World) -> None:
    first = _first(world)
    second = _continue(world, first.run_id)
    world.agent.edit = lambda root: (root / "extra.py").write_text("x = 1\n")
    third = world.implement("And more.", continue_run=second.run_id)
    assert third.exit_code == 0, world.said
    assert world.registry().resolve(third.run_id).lineage == first.run_id
    members = lineage.load(world.state, first.run_id).members
    assert set(members) == {first.run_id, second.run_id, third.run_id}


def test_a_commit_made_by_hand_between_runs_is_kept(world: World) -> None:
    """Review Focus 1, §3.6: kept, unattributed, and in the cumulative patch."""
    first = _first(world)
    worktree = first.run_dir / "wt"
    (worktree / "notes.md").write_text("by hand\n")
    _git(worktree, "add", "notes.md")
    _git(worktree, "commit", "-q", "-m", "docs: notes by hand")
    hand = _git(worktree, "rev-parse", "HEAD").strip()
    second = _continue(world, first.run_id)
    assert second.exit_code == 0, world.said
    assert _subjects(world, f"ha/{first.run_id}")[1] == "docs: notes by hand"
    assert provenance.lookup(world.state, hand) is None
    assert "notes.md" in (second.run_dir / write_flow.PATCH_FILE).read_text()


def test_a_dirty_worktree_refuses_the_continuation_and_leaves_no_trace(world: World) -> None:
    first = _first(world)
    (first.run_dir / "wt" / "app.py").write_text("edited by hand, not committed\n")
    before = lineage.load(world.state, first.run_id)
    with pytest.raises(UsageError, match="has uncommitted changes"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id) == before
    assert world.registry().run_ids() == [first.run_id]
    assert _run_dirs(world) == [first.run_id]
    assert len(world.agent.specs) == 1


def test_a_worktree_off_its_branch_refuses_the_continuation(world: World) -> None:
    """Review Focus 2: a worktree detached or switched by hand would commit elsewhere."""
    first = _first(world)
    _git(first.run_dir / "wt", "checkout", "-q", "--detach")
    with pytest.raises(UsageError, match="is not on its branch"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).pending is None
    _assert_no_trace(world, first.run_id)


def test_a_continuation_from_inside_its_worktree_is_admitted(world: World) -> None:
    """Review Focus 3: its lineage is both the one it joins and the source of --repo."""
    first = _first(world)
    second = _continue(world, first.run_id, cwd=first.run_dir / "wt")
    assert second.exit_code == 0, world.said


def test_a_continuation_after_ha_clean_is_refused(world: World) -> None:
    first = _first(world)
    code, _, err = world.cli("clean", first.run_id)
    assert code == 0, err
    with pytest.raises(UsageError, match="it was cleaned"):
        _continue(world, first.run_id)


def test_a_continuation_from_another_repository_is_refused(world: World, tmp_path: Path) -> None:
    first = _first(world)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    with pytest.raises(UsageError, match="belongs to"):
        _continue(world, first.run_id, cwd=other)
    _assert_no_trace(world, first.run_id)


def test_a_continuation_refused_before_admission_creates_no_run_dir(
    world: World, tmp_path: Path
) -> None:
    """§3.8.2: what plan() read from the registry is read again before anything is created."""
    first = _first(world)
    custom = tmp_path / "custom"
    planned = plan(world.request("Turn it on.", continue_run=first.run_id, run_dir=custom))
    (world.state / "runs" / f"{first.run_id}.json").write_text("{not json")
    with pytest.raises(UsageError, match="recover it by hand"):
        execute(planned, say=world.said.append)
    assert not custom.exists()


def test_a_compromised_lineage_refuses_the_continuation(world: World) -> None:
    first = _first(world)
    current = lineage.load(world.state, first.run_id)
    lineage.save(world.state, replace(current, compromised="agent_moved_head"))
    with pytest.raises(UsageError, match=r"compromised \(agent_moved_head\)"):
        _continue(world, first.run_id)
    _assert_no_trace(world, first.run_id)


def test_an_unreadable_lineage_refuses_the_continuation(world: World) -> None:
    first = _first(world)
    lineage.lineage_path(world.state, first.run_id).write_text("{not json")
    with pytest.raises(UsageError, match=f"lineage {first.run_id} is unknown"):
        _continue(world, first.run_id)


def test_the_named_run_must_be_a_member_of_its_lineage(world: World) -> None:
    first = _first(world)
    stranger = "20260926T120000-cccccccc"
    world.registry().create(
        stranger,
        run_dir=None,
        target=IMPLEMENT,
        repository=world.repo,
        lineage=first.run_id,
        providers=("codex",),
    )
    with pytest.raises(UsageError, match=f"{stranger} is not a member of lineage {first.run_id}"):
        _continue(world, stranger)


def test_a_tripwire_in_a_continuation_compromises_the_whole_lineage(world: World) -> None:
    """§4: a tripwire fired in B, then --continue A refused and ha clean A running no git."""
    first = _first(world)
    world.agent.edit = lambda root: (root / ".git").write_text("gitdir: /somewhere/else\n")
    second = world.implement("Tamper.", continue_run=first.run_id)
    assert second.exit_code == 1
    assert lineage.load(world.state, first.run_id).compromised == "tripwire"
    with pytest.raises(UsageError, match="unfinished write"):
        _continue(world, first.run_id)
    code, _, err = world.cli("clean", first.run_id)
    assert code == 1 and "no git command" in err


def test_a_crash_after_a_continuations_intent_is_found_by_the_next(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4: the next --continue marks the lineage unfinalized_write and is refused."""
    first = _first(world)

    def crash(step: str) -> None:
        if step == "intent":
            raise SystemExit("crashed")

    monkeypatch.setattr(write_flow, "_crash_after", crash)
    with pytest.raises(SystemExit):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).pending is not None
    monkeypatch.setattr(write_flow, "_crash_after", lambda step: None)
    with pytest.raises(UsageError, match="unfinished write"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).compromised == "unfinalized_write"
    assert quarantine.check(world.state, (world.repo / ".git").resolve()) is not None


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "ex" else fcntl.LOCK_SH)
pathlib.Path(sys.argv[3]).write_text("ok")
time.sleep(30)
"""


@contextmanager
def _holding(world: World, lock: Path, mode: str) -> Iterator[None]:
    """``lock`` held by another process, as a running continuation holds its lineage's."""
    ready = world.home / f"ready-{lock.name}-{mode}"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), mode, str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.02)
        yield
    finally:
        holder.kill()
        holder.wait()


def test_two_continuations_of_one_lineage_never_run_at_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.6: the second waits for the lineage lock at most the bound, then is refused."""
    first = _first(world)
    second = _continue(world, first.run_id)
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    lock = lineage.lineage_lock(world.state, first.run_id)
    with (
        _holding(world, lock, "ex"),
        pytest.raises(UsageError, match=f"the lineage lock of {first.run_id}: not obtained"),
    ):
        world.implement("Meanwhile.", continue_run=second.run_id)
    assert sorted(world.registry().run_ids()) == sorted([first.run_id, second.run_id])


def test_ha_clean_waits_for_a_running_continuation_then_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4: ha clean A while B runs -- it waits for the lineage lock, then is refused."""
    first = _first(world)
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    with _holding(world, lineage.lineage_lock(world.state, first.run_id), "ex"):
        code, _, err = world.cli("clean", first.run_id)
    assert code == 2 and "the lineage is in use" in err
    assert (first.run_dir / "wt").is_dir()


def test_a_refused_unconfined_continuation_withdraws_its_intent(world: World) -> None:
    first = _first(world)
    record_proof(
        world.state,
        "codex",
        version="codex 1.0",
        isolation=True,
        confinement=False,
        today="2026-09-26",
    )
    (first.run_dir / "wt" / "app.py").write_text("dirty\n")
    with pytest.raises(UsageError, match="uncommitted changes"):
        _continue(world, first.run_id)
    assert not (world.state / write_flow.UNCONFINED_INTENT).exists()
