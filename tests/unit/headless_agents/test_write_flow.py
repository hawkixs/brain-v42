"""A write run through the engine: spec 0.5.0 §3.8.3 on real throwaway repositories.

The provider is a fake that edits its workspace like an agent would; the
worktree, the engine commit, the repository's hooks, the tripwire, the
lineage state, provenance and quarantines are the real code and a real git.
A write plan is built here from a read-only one with ``write=True``, so each
test names its role's capabilities itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import engine, lineage, locks, provenance, quarantine, write_flow
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec
from headless_agents.state import Unknown

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


Edit = Callable[[Path], None]


@dataclass
class _Agent:
    """A provider that applies ``edit`` to its writable workspace and answers."""

    name: str
    edit: Edit | None = None
    code: int = 0
    specs: list[RunSpec] = field(default_factory=list)
    after: Callable[[], None] | None = None

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        workspace = spec.profile.workspace
        assert workspace is not None and workspace.write
        if self.edit is not None:
            self.edit(workspace.path)
        if self.after is not None:
            self.after()
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
                text="I changed things" if self.code == 0 else None,
                run_id=run_id_of(spec),
            ),
        )


@dataclass
class World:
    home: Path
    repo: Path
    agent: _Agent
    said: list[str] = field(default_factory=list)
    git_calls: list[list[str]] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")

    def write_plan(self, *, repo: Path | None = None, base: str | None = None) -> engine.Plan:
        request = Request(
            target="codex",
            prompt="improve app",
            stdin_is_tty=False,
            overrides=Overrides(),
            base=None,
            repo=repo,
            run_dir=None,
            cwd=self.repo,
            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            home=self.home,
        )
        planned = plan(request)
        return replace(
            planned, role=replace(planned.role, write=True), request=replace(request, base=base)
        )

    def write(self, **kwargs: object) -> engine.Outcome:
        return execute(self.write_plan(**kwargs), say=self.said.append)  # type: ignore[arg-type]

    def common_dir(self) -> Path:
        return (self.repo / ".git").resolve()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    (home / ".config" / "ha" / "models.toml").write_text('codex = "codex-default"\n')
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
    world = World(home=home, repo=repo, agent=agent)
    real_git = write_flow.git

    def recording_git(root: Path, args: list[str], environ: object, **kwargs: object):  # type: ignore[no-untyped-def]
        world.git_calls.append(list(args))
        return real_git(root, args, environ, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(write_flow, "git", recording_git)
    return world


def _edit_app(root: Path) -> None:
    (root / "app.py").write_text("print('v2')\n")
    (root / "new.py").write_text("x = 1\n")


def _hook(world: World, name: str, body: str) -> None:
    hook = world.repo / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)


def _subjects(world: World, branch: str) -> list[str]:
    return _git(world.repo, "log", "--format=%s", f"main..{branch}").splitlines()


# ── the committed path ─────────────────────────────────────────────────────


def test_a_committed_write(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    assert outcome.exit_code == 0, world.said
    run_id = outcome.run_id
    branch = f"ha/{run_id}"
    assert _subjects(world, branch) == [f"chore(ha): {run_id} implement via codex/served-model"]
    assert (world.repo / "app.py").read_text() == "print('v1')\n"
    state = lineage.load(world.state, run_id)
    assert state.members == {run_id: "committed"}
    assert state.pending is None and state.compromised is None
    assert state.base == _git(world.repo, "rev-parse", "main").strip()
    tip = _git(world.repo, "rev-parse", branch).strip()
    assert provenance.lookup(world.state, tip) == {
        "sha": tip,
        "run_id": run_id,
        "lineage": run_id,
        "made_by": "engine",
        "providers": [],
    }
    assert world.registry().resolve(run_id).lineage == run_id
    patch = (outcome.run_dir / write_flow.PATCH_FILE).read_text()
    assert "print('v2')" in patch
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["status"] == "committed" and report["branch"] == branch
    assert report["head"] == tip and report["lineage"] == run_id
    assert report["commits"] == [{"sha": tip, "made_by": "engine"}]


def test_the_agent_works_in_the_worktree_with_the_role_shell(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    workspace = world.agent.specs[0].profile.workspace
    assert workspace is not None
    assert workspace.path == outcome.run_dir / "wt" and workspace.write
    assert workspace.shell is False


def test_a_named_base_is_used(world: World) -> None:
    first = _git(world.repo, "rev-parse", "HEAD").strip()
    (world.repo / "later.txt").write_text("later\n")
    _git(world.repo, "add", "later.txt")
    _git(world.repo, "commit", "-q", "-m", "later")
    world.agent.edit = _edit_app
    outcome = world.write(base=first)
    assert _git(world.repo, "rev-parse", f"ha/{outcome.run_id}~1").strip() == first


def test_no_change_exits_5_and_commits_nothing(world: World) -> None:
    outcome = world.write()
    assert outcome.exit_code == 5
    branch = f"ha/{outcome.run_id}"
    assert _subjects(world, branch) == []
    assert lineage.load(world.state, outcome.run_id).members[outcome.run_id] == "no_change"


def test_a_failed_step_with_changes_commits_a_residue(world: World) -> None:
    world.agent.edit = _edit_app
    world.agent.code = 1
    outcome = world.write()
    assert outcome.exit_code == 1
    (subject,) = _subjects(world, f"ha/{outcome.run_id}")
    assert subject == f"chore(ha): {outcome.run_id} residue via codex/served-model"
    tip = _git(world.repo, "rev-parse", f"ha/{outcome.run_id}").strip()
    record_ = provenance.lookup(world.state, tip)
    assert record_ is not None and record_["made_by"] == "engine"
    assert lineage.load(world.state, outcome.run_id).members[outcome.run_id] == "failed"


def test_a_failed_step_without_changes_commits_nothing(world: World) -> None:
    world.agent.code = 1
    outcome = world.write()
    assert outcome.exit_code == 1
    assert _subjects(world, f"ha/{outcome.run_id}") == []


def test_the_worktree_creation_is_not_attributed_to_the_agent(world: World) -> None:
    """The start point is taken after preparation (§3.8.3 step 4)."""
    world.agent.edit = _edit_app
    outcome = world.write()
    commits = json.loads((outcome.run_dir / "run.json").read_text())["commits"]
    assert [c["made_by"] for c in commits] == ["engine"]


# ── what an agent or a hook did ────────────────────────────────────────────


def test_an_agent_commit_fails_the_run_and_is_recorded(world: World) -> None:
    def commit_itself(root: Path) -> None:
        _edit_app(root)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--no-verify", "-m", "agent did it")

    world.agent.edit = commit_itself
    outcome = world.write()
    assert outcome.exit_code == 1
    state = lineage.load(world.state, outcome.run_id)
    assert state.compromised == "agent_moved_head"
    tip = _git(world.repo, "rev-parse", f"ha/{outcome.run_id}").strip()
    record_ = provenance.lookup(world.state, tip)
    assert record_ is not None and record_["made_by"] == "agent"
    assert record_["providers"] == ["codex"]
    assert _subjects(world, f"ha/{outcome.run_id}") == ["agent did it"]


def test_a_refusing_hook_keeps_the_change_uncommitted(world: World) -> None:
    _hook(world, "pre-commit", "echo 'lint says no' >&2\nexit 1\n")
    world.agent.edit = _edit_app
    outcome = world.write()
    assert outcome.exit_code == 1
    step = outcome.run_dir / "steps" / "01-run-codex"
    assert "lint says no" in (step / write_flow.COMMIT_LOG).read_text()
    assert (outcome.run_dir / "wt" / "app.py").read_text() == "print('v2')\n"
    assert _subjects(world, f"ha/{outcome.run_id}") == []
    assert lineage.load(world.state, outcome.run_id).compromised == "hook_refused"


def test_a_passing_hook_runs_for_the_engine_commit(world: World) -> None:
    marker = world.home / "hook-ran"
    _hook(world, "pre-commit", f"touch {marker}\n")
    world.agent.edit = _edit_app
    assert world.write().exit_code == 0
    assert marker.exists()


def test_a_hook_that_commits_then_fails_is_attributed(world: World) -> None:
    _hook(
        world,
        "pre-commit",
        "echo hooked > hooked.txt\ngit add hooked.txt\n"
        "git commit -q --no-verify -m 'hook commit'\nexit 1\n",
    )
    world.agent.edit = _edit_app
    outcome = world.write()
    assert outcome.exit_code == 1
    assert lineage.load(world.state, outcome.run_id).compromised == "hook_committed"
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert {c["made_by"] for c in report["commits"]} == {"hook"}
    for commit in report["commits"]:
        found = provenance.lookup(world.state, commit["sha"])
        assert found is not None and found["made_by"] == "hook"


def test_a_hook_that_amends_is_attributed(world: World) -> None:
    # post-commit runs again for the amend itself: the guard stops the recursion.
    _hook(
        world,
        "post-commit",
        '[ -n "$HA_TEST_AMENDED" ] && exit 0\n'
        "HA_TEST_AMENDED=1 git commit -q --amend --no-verify -m 'amended by hook'\n",
    )
    world.agent.edit = _edit_app
    outcome = world.write()
    assert outcome.exit_code == 1
    assert lineage.load(world.state, outcome.run_id).compromised == "hook_committed"
    made_by = {
        c["made_by"] for c in json.loads((outcome.run_dir / "run.json").read_text())["commits"]
    }
    assert made_by == {"engine", "hook"}


# ── the tripwire ───────────────────────────────────────────────────────────


def _mark_step_end(world: World) -> None:
    world.git_calls.append(["<step ended>"])


def _git_after_step(world: World) -> list[list[str]]:
    index = world.git_calls.index(["<step ended>"])
    return world.git_calls[index + 1 :]


def test_a_tampered_worktree_git_file_compromises_the_lineage_without_git(world: World) -> None:
    def tamper(root: Path) -> None:
        _edit_app(root)
        (root / ".git").write_text("gitdir: /somewhere/else\n")

    world.agent.edit = tamper
    world.agent.after = lambda: _mark_step_end(world)
    outcome = world.write()
    assert outcome.exit_code == 1
    assert _git_after_step(world) == []
    state = lineage.load(world.state, outcome.run_id)
    assert state.compromised == "tripwire" and state.pending is not None
    assert quarantine.check(world.state, world.common_dir()) is None
    assert any("tripwire" in line for line in world.said)


def test_a_tampered_common_dir_quarantines_the_repository(world: World) -> None:
    def tamper(root: Path) -> None:
        _edit_app(root)
        with (world.repo / ".git" / "config").open("a") as config:
            config.write("[core]\n\tfsmonitor = /bin/true\n")

    world.agent.edit = tamper
    world.agent.after = lambda: _mark_step_end(world)
    outcome = world.write()
    assert outcome.exit_code == 1
    assert _git_after_step(world) == []
    refusal = quarantine.check(world.state, world.common_dir())
    assert refusal is not None and "repository" in refusal

    world.agent.edit, world.agent.after = _edit_app, None
    world.git_calls.clear()
    with pytest.raises(UsageError, match="repository quarantine"):
        world.write()
    assert world.git_calls == []


# ── crashes and admission ──────────────────────────────────────────────────


def test_a_crash_after_the_intent_compromises_the_lineage_at_the_next_write(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "intent":
            raise SystemExit("crashed")

    monkeypatch.setattr(write_flow, "_crash_after", crash)
    with pytest.raises(SystemExit):
        world.write()
    (crashed,) = lineage.owners(world.state)
    assert lineage.load(world.state, crashed).pending is not None

    monkeypatch.setattr(write_flow, "_crash_after", lambda step: None)
    world.git_calls.clear()
    with pytest.raises(UsageError, match="unfinished write"):
        world.write()
    assert world.git_calls == []
    assert lineage.load(world.state, crashed).compromised == "unfinalized_write"
    assert quarantine.check(world.state, world.common_dir()) is not None


def test_a_refused_write_leaves_no_entry_and_no_lineage(world: World) -> None:
    quarantine.publish(world.state, "operator", reason="x", run_id="r", paths=[], common_dir=None)
    with pytest.raises(UsageError, match="operator quarantine"):
        world.write()
    assert world.registry().run_ids() == []
    assert lineage.owners(world.state) == []


def test_an_unknown_lineage_of_the_repository_refuses(world: World) -> None:
    world.agent.edit = _edit_app
    first = world.write()
    path = lineage.lineage_path(world.state, first.run_id)
    path.write_text("{broken")
    with pytest.raises(UsageError, match="unknown"):
        world.write()


def test_a_stale_unconfined_intent_quarantines_the_operator(world: World) -> None:
    (world.state / write_flow.UNCONFINED_INTENT).write_text(json.dumps({"run_id": "dead"}))
    with pytest.raises(UsageError, match="stale unconfined intent"):
        world.write()
    refusal = quarantine.check(world.state, None)
    assert refusal is not None and refusal.startswith("operator quarantine")


def _instrument(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    real_held = locks.held

    def held(path: Path, *, rank: locks.Rank, **kwargs: object):  # type: ignore[no-untyped-def]
        mode = "ex" if kwargs.get("exclusive") else "sh"
        events.append(f"lock {rank.name} {kwargs.get('key', '')}".rstrip() + f" {mode}")
        manager = real_held(path, rank=rank, **kwargs)  # type: ignore[arg-type]

        class _Traced:
            def __enter__(self) -> None:
                manager.__enter__()

            def __exit__(self, *exc: object) -> None:
                manager.__exit__(*exc)  # type: ignore[arg-type]
                events.append(f"release {rank.name}")

        return _Traced()

    real_check = quarantine.check

    def check(state: Path, common_dir: Path | None) -> str | None:
        events.append("quarantine check")
        return real_check(state, common_dir)

    real_create = lineage.create

    def create(state: Path, lineage_state: lineage.LineageState) -> None:
        real_create(state, lineage_state)
        events.append("intent")

    real_git = write_flow.git

    def git(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        events.append("git")
        return real_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "held", held)
    monkeypatch.setattr(write_flow, "held", held)
    monkeypatch.setattr(quarantine, "check", check)
    monkeypatch.setattr(lineage, "create", create)
    monkeypatch.setattr(write_flow, "git", git)
    return events


def _other_lineage(world: World, owner: str, *, pending: bool) -> None:
    lineage.create(
        world.state,
        lineage.LineageState(
            owner=owner,
            repository=world.repo,
            common_dir=world.common_dir(),
            worktree=world.home / "elsewhere" / owner,
            branch=f"ha/{owner}",
            base="0" * 40,
            members={owner: "running" if pending else "committed"},
            pending=lineage.PendingWrite(owner, ("claude",), False, "0" * 40, None)
            if pending
            else None,
            compromised=None,
        ),
    )


def test_admission_takes_every_lock_before_reading_state_and_runs_no_git(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _other_lineage(world, "20200101T000000-00000001", pending=False)
    _other_lineage(world, "20200101T000000-00000002", pending=True)
    events = _instrument(world, monkeypatch)
    with pytest.raises(UsageError, match="unfinished write"):
        world.write()
    assert "git" not in events
    locks_taken = [e for e in events if e.startswith("lock")]
    assert [e.split()[1] for e in locks_taken] == [
        "LIFECYCLE",
        "UNCONFINED",
        "LINEAGE_REGISTRY",
        "LINEAGE",
    ]
    assert events.index("quarantine check") > events.index(locks_taken[-1])


def test_the_first_git_comes_after_the_intent_and_the_registry_release(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _other_lineage(world, "20200101T000000-00000001", pending=False)
    world.agent.edit = _edit_app
    events = _instrument(world, monkeypatch)
    assert world.write().exit_code == 0
    first_git = events.index("git")
    assert events.index("intent") < first_git
    assert events.index("release LINEAGE_REGISTRY") < first_git


def test_the_registry_lock_is_held_until_the_intent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lineage can appear while an admission decides: another process finds
    the registry lock busy at the intent, and free once the intent is published."""
    probe = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print('free')\n"
        "except BlockingIOError:\n    print('busy')\n"
    )
    seen: dict[str, str] = {}

    def at(step: str) -> None:
        if step in ("intent", "preparation"):
            seen[step] = subprocess.run(
                [sys.executable, "-c", probe, str(lineage.registry_lock(world.state))],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

    monkeypatch.setattr(write_flow, "_crash_after", at)
    world.agent.edit = _edit_app
    assert world.write().exit_code == 0
    assert seen == {"intent": "busy", "preparation": "free"}


@pytest.mark.parametrize("source_sorts", ["after", "before"])
def test_a_source_lineage_is_locked_in_ascending_order(
    world: World, monkeypatch: pytest.MonkeyPatch, source_sorts: str
) -> None:
    own, source = (
        "20260925T120000-bbbbbbbb",
        ("20260925T120000-cccccccc" if source_sorts == "after" else "20260925T120000-aaaaaaaa"),
    )
    worktree = world.home / "source-wt"
    _git(world.repo, "worktree", "add", "-q", "-b", f"ha/{source}", str(worktree), "main")
    lineage.create(
        world.state,
        lineage.LineageState(
            owner=source,
            repository=world.repo,
            common_dir=world.common_dir(),
            worktree=worktree,
            branch=f"ha/{source}",
            base=_git(world.repo, "rev-parse", "main").strip(),
            members={source: "committed"},
            pending=None,
            compromised=None,
        ),
    )
    monkeypatch.setattr(Registry, "mint", lambda self: own)
    events = _instrument(world, monkeypatch)
    world.agent.edit = _edit_app
    assert world.write(repo=worktree).exit_code == 0
    lineage_locks = [e.split()[2] for e in events if e.startswith("lock LINEAGE ")]
    assert [e.split()[3] for e in events if e.startswith("lock LINEAGE ")] == [
        "ex" if owner == own else "sh" for owner in sorted([own, source])
    ]
    assert lineage_locks == sorted([own, source])


def test_a_compromised_source_lineage_refuses(world: World) -> None:
    source = "20200101T000000-00000003"
    worktree = world.home / "source-wt"
    _git(world.repo, "worktree", "add", "-q", "-b", f"ha/{source}", str(worktree), "main")
    lineage.create(
        world.state,
        lineage.LineageState(
            owner=source,
            repository=world.repo,
            common_dir=world.common_dir(),
            worktree=worktree,
            branch=f"ha/{source}",
            base="0" * 40,
            members={source: "failed"},
            pending=None,
            compromised="tripwire",
        ),
    )
    with pytest.raises(UsageError, match="compromised"):
        world.write(repo=worktree)


def test_a_lineage_file_is_never_trusted_when_malformed(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    path = lineage.lineage_path(world.state, outcome.run_id)
    path.write_text(json.dumps({"owner": outcome.run_id}))
    with pytest.raises(Unknown):
        lineage.load(world.state, outcome.run_id)


def test_a_write_outside_a_git_repository_is_refused_before_anything(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    # Bounded at tmp_path: a stray .git above the temporary directory (seen on a
    # development machine as an empty /tmp/.git) must not make ``plain`` a repository.
    real_discover = engine.discover
    monkeypatch.setattr(engine, "discover", lambda start: real_discover(start, ceiling=tmp_path))
    with pytest.raises(UsageError, match="git repository"):
        world.write(repo=plain)
    assert world.registry().run_ids() == [] and world.git_calls == []


# ── the unconfined path (plan Task 20) ─────────────────────────────────────


@pytest.fixture
def unconfined(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(engine, "write_is_unconfined", lambda planned: True)
    return world


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "ex" else fcntl.LOCK_SH)
pathlib.Path(sys.argv[3]).write_text("ok")
time.sleep(30)
"""


@contextmanager
def _holding(world: World, mode: str):  # type: ignore[no-untyped-def]
    ready = world.home / f"ready-{mode}"
    world.state.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(world.state / "unconfined.lock"), mode, str(ready)]
    )
    try:
        while not ready.exists():
            time.sleep(0.02)
        yield
    finally:
        holder.kill()
        holder.wait()


def test_an_unconfined_write_waits_for_running_runs_then_is_refused(
    unconfined: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    with _holding(unconfined, "sh"), pytest.raises(UsageError, match="running"):
        unconfined.write()
    assert unconfined.registry().run_ids() == []


def test_a_running_unconfined_write_serialises_a_read_only_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    read_only = plan(replace(world.write_plan().request, base=None))
    with _holding(world, "ex"), pytest.raises(UsageError, match="unconfined write is running"):
        execute(read_only, say=world.said.append)


def test_an_unconfined_write_publishes_its_intent_then_removes_it(
    unconfined: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def at(step: str) -> None:
        if step == "intent":
            seen["intent"] = json.loads(
                (unconfined.state / write_flow.UNCONFINED_INTENT).read_text()
            )

    monkeypatch.setattr(write_flow, "_crash_after", at)
    unconfined.agent.edit = _edit_app
    outcome = unconfined.write()
    assert outcome.exit_code == 0
    assert seen["intent"] == {
        "run_id": outcome.run_id,
        "repository": str(unconfined.repo),
        "providers": ["codex"],
    }
    assert not (unconfined.state / write_flow.UNCONFINED_INTENT).exists()
    writers = json.loads((unconfined.state / write_flow.UNCONFINED_WRITERS).read_text())
    assert writers["writers"] == [
        {"run_id": outcome.run_id, "repository": str(unconfined.repo), "providers": ["codex"]}
    ]
    assert lineage.load(unconfined.state, outcome.run_id).members[outcome.run_id] == "committed"


def test_an_unconfined_agent_commit_hidden_by_a_reset_is_found_in_the_reflog(
    unconfined: World,
) -> None:
    def commit_then_hide(root: Path) -> None:
        _edit_app(root)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--no-verify", "-m", "hidden")
        _git(root, "reset", "-q", "--hard", "HEAD~1")

    unconfined.agent.edit = commit_then_hide
    outcome = unconfined.write()
    assert outcome.exit_code == 1
    assert lineage.load(unconfined.state, outcome.run_id).compromised == "agent_moved_head"
    commits = json.loads((outcome.run_dir / "run.json").read_text())["commits"]
    assert [c["made_by"] for c in commits] == ["agent"]
    hidden = provenance.lookup(unconfined.state, commits[0]["sha"])
    assert hidden is not None and hidden["made_by"] == "agent"
    assert _git(unconfined.repo, "log", "-1", "--format=%s", commits[0]["sha"]).strip() == "hidden"


def test_a_confined_write_does_not_read_the_reflog(world: World) -> None:
    """The reflog is the unconfined path's extra witness; a confined write
    compares the tips only (a reset back to the start is invisible to it)."""

    def commit_then_hide(root: Path) -> None:
        _edit_app(root)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--no-verify", "-m", "hidden")
        _git(root, "reset", "-q", "--hard", "HEAD~1")

    world.agent.edit = commit_then_hide
    assert world.write().exit_code == 5


def _second_repository(world: World) -> Path:
    other = world.home / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.name", "Op")
    _git(other, "config", "user.email", "op@example.test")
    (other / "a.txt").write_text("a\n")
    _git(other, "add", "a.txt")
    _git(other, "commit", "-q", "-m", "init")
    return other


def test_an_unconfined_write_killed_in_one_repository_stops_every_repository(
    unconfined: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "step":
            raise SystemExit("killed")

    monkeypatch.setattr(write_flow, "_crash_after", crash)
    unconfined.agent.edit = _edit_app
    with pytest.raises(SystemExit):
        unconfined.write()
    monkeypatch.setattr(write_flow, "_crash_after", lambda step: None)
    monkeypatch.setattr(engine, "write_is_unconfined", lambda planned: False)
    unconfined.git_calls.clear()
    with pytest.raises(UsageError, match="stale unconfined intent"):
        unconfined.write(repo=_second_repository(unconfined))
    assert unconfined.git_calls == []
    refusal = quarantine.check(unconfined.state, None)
    assert refusal is not None and refusal.startswith("operator quarantine")


def test_a_crash_between_the_lineage_rename_and_the_intent_removal_quarantines_the_operator(
    unconfined: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "lineage_published":
            raise SystemExit("killed")

    monkeypatch.setattr(write_flow, "_crash_after", crash)
    unconfined.agent.edit = _edit_app
    with pytest.raises(SystemExit):
        unconfined.write()
    (owner,) = lineage.owners(unconfined.state)
    assert lineage.load(unconfined.state, owner).members[owner] == "committed"
    monkeypatch.setattr(write_flow, "_crash_after", lambda step: None)
    with pytest.raises(UsageError, match="stale unconfined intent"):
        unconfined.write()
    assert quarantine.check(unconfined.state, None) is not None


def test_a_new_unconfined_write_finding_a_leftover_intent_is_refused(unconfined: World) -> None:
    unconfined.state.mkdir(parents=True, exist_ok=True)
    (unconfined.state / write_flow.UNCONFINED_INTENT).write_text(json.dumps({"run_id": "old"}))
    unconfined.git_calls.clear()
    with pytest.raises(UsageError, match="stale unconfined intent"):
        unconfined.write()
    assert unconfined.git_calls == []
    assert quarantine.check(unconfined.state, None) is not None


@pytest.mark.parametrize(
    ("providers", "shell", "expected"),
    [
        (("claude",), True, True),
        (("opencode",), True, True),
        (("agy",), True, True),
        (("codex", "claude"), True, True),
        (("codex",), True, False),
        (("claude",), False, False),
    ],
)
def test_a_shell_write_on_an_unsandboxed_rail_is_unconfined(
    world: World, providers: tuple[str, ...], shell: bool, expected: bool
) -> None:
    """Decision 13: codex keeps its sandboxed shell; the other rails' shell is unconfined."""
    planned = world.write_plan()
    links = tuple(replace(planned.role.links[0], provider=p) for p in providers)
    role = replace(planned.role, links=links, shell=shell)
    assert engine.write_is_unconfined(replace(planned, role=role)) is expected


# ── ha clean under the lineage rules (plan Task 21) ────────────────────────


def _clean(world: World, run_id: str) -> int:
    return engine.clean(
        run_id,
        environ={"PATH": os.environ["PATH"], "HOME": str(world.home)},
        home=world.home,
        say=world.said.append,
    )


def test_clean_removes_a_committed_write_worktree_and_keeps_the_rest(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    worktree = outcome.run_dir / "wt"
    tip = _git(world.repo, "rev-parse", f"ha/{outcome.run_id}").strip()
    assert _clean(world, outcome.run_id) == 0
    assert not outcome.run_dir.exists()
    assert str(worktree) not in _git(world.repo, "worktree", "list")
    assert _git(world.repo, "rev-parse", "--verify", f"ha/{outcome.run_id}").strip() == tip
    assert world.registry().resolve(outcome.run_id).cleaned_at is not None
    assert lineage.load(world.state, outcome.run_id).members[outcome.run_id] == "committed"
    assert provenance.lookup(world.state, tip) is not None


def test_clean_waits_for_a_lineage_in_use_then_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    ready = world.home / "ready-lineage"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD,
            str(lineage.lineage_lock(world.state, outcome.run_id)),
            "ex",
            str(ready),
        ]
    )
    try:
        while not ready.exists():
            time.sleep(0.02)
        with pytest.raises(UsageError, match="lineage lock"):
            _clean(world, outcome.run_id)
    finally:
        holder.kill()
        holder.wait()
    assert outcome.run_dir.exists()


def test_clean_of_a_compromised_lineage_runs_no_git(world: World) -> None:
    def tamper(root: Path) -> None:
        _edit_app(root)
        (root / ".git").write_text("gitdir: /somewhere/else\n")

    world.agent.edit = tamper
    outcome = world.write()
    world.git_calls.clear()
    assert _clean(world, outcome.run_id) == 1
    assert world.git_calls == []
    assert outcome.run_dir.exists()
    assert any("compromised" in line for line in world.said)


def test_clean_under_a_repository_quarantine_runs_no_git(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    quarantine.publish(
        world.state, "repository", reason="x", run_id="r", paths=[], common_dir=world.common_dir()
    )
    world.git_calls.clear()
    assert _clean(world, outcome.run_id) == 1
    assert world.git_calls == []


def test_clean_finds_a_stale_pending_write_in_the_repository(world: World) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    _other_lineage(world, "20200101T000000-00000002", pending=True)
    world.git_calls.clear()
    assert _clean(world, outcome.run_id) == 1
    assert world.git_calls == []
    stale = lineage.load(world.state, "20200101T000000-00000002")
    assert stale.compromised == "unfinalized_write"
    assert quarantine.check(world.state, world.common_dir()) is not None


def test_clean_of_a_write_that_never_started_forgets_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260925T000000-ffffffff"
    world.registry().create(
        run_id,
        run_dir=None,
        target={"kind": "role", "name": "codex"},
        repository=world.repo,
        lineage=run_id,
    )
    world.git_calls.clear()
    assert _clean(world, run_id) == 0
    assert run_id not in world.registry().run_ids()
    assert world.git_calls == []


def test_clean_takes_its_locks_in_order_and_releases_the_registry_before_git(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.agent.edit = _edit_app
    outcome = world.write()
    events = _instrument(world, monkeypatch)
    assert _clean(world, outcome.run_id) == 0
    taken = [e for e in events if e.startswith("lock")]
    assert [(e.split()[1], e.split()[-1]) for e in taken] == [
        ("LIFECYCLE", "ex"),
        ("UNCONFINED", "sh"),
        ("LINEAGE_REGISTRY", "sh"),
        ("LINEAGE", "ex"),
    ]
    assert events.index("release LINEAGE_REGISTRY") < events.index("git")
    assert events.index("quarantine check") < events.index("release LINEAGE_REGISTRY")


# ── classification by the confinement proofs (plan Task 22) ────────────────


def _unconfined_lock_mode(world: World, monkeypatch: pytest.MonkeyPatch) -> str:
    events = _instrument(world, monkeypatch)
    world.agent.edit = _edit_app
    world.write()
    (taken,) = [e for e in events if e.startswith("lock UNCONFINED")]
    return taken.split()[-1]


def test_a_confined_write_role_takes_the_confined_path(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _unconfined_lock_mode(world, monkeypatch) == "sh"


@pytest.mark.parametrize(
    "proof",
    [
        {"confinement": False},
        {"version": "codex 0.9", "confinement": True},
        {"isolation": True},
    ],
    ids=["failed", "another-version", "missing"],
)
def test_a_codex_write_without_a_confinement_proof_is_unconfined(
    world: World, monkeypatch: pytest.MonkeyPatch, proof: dict[str, object]
) -> None:
    """Although it has no shell: a write role on an unproven rail takes the
    unconfined path -- exclusive lock, intent, reflog attribution."""
    from headless_agents.proofs import proof_path

    proof_path(world.state, "codex").unlink()
    fields = {"version": "codex 1.0", "isolation": True, **proof}
    record_proof(world.state, "codex", today="2026-09-25", **fields)  # type: ignore[arg-type]
    if fields["version"] != "codex 1.0":
        record_proof(world.state, "codex", version="codex 1.0", isolation=True, today="2026-09-25")
    assert _unconfined_lock_mode(world, monkeypatch) == "ex"


def test_the_cli_prints_the_branch_and_the_patch_of_a_committed_write(world: World) -> None:
    import io as _io

    from headless_agents import cli

    world.agent.edit = _edit_app
    out, err = _io.StringIO(), _io.StringIO()
    code = cli.main(
        ["run", "codex", "--write", "go"],
        environ={"PATH": os.environ["PATH"], "HOME": str(world.home)},
        stdin=_io.StringIO(),
        stdout=out,
        stderr=err,
        cwd=world.repo,
        home=world.home,
    )
    assert code == 0, err.getvalue()
    (run_id,) = world.registry().run_ids()
    text = out.getvalue()
    assert f"branch: ha/{run_id}" in text
    assert "patch: " in text and "I changed things" in text
