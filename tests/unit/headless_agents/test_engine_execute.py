"""engine.execute(): a one-step read-only run, its locks, its records (spec 0.5.0 §3.4, §3.8, §3.10)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import engine, locks
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
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
    raises: BaseException | None = None
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        if self.raises is not None:
            raise self.raises
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
                cost_usd=0.5,
            ),
        )


@dataclass
class World:
    home: Path
    repo: Path
    fakes: dict[str, _Fake]
    said: list[str] = field(default_factory=list)

    def roles(self, text: str) -> None:
        (self.home / ".config" / "ha" / "roles.toml").write_text(text)

    def request(self, target: str, prompt: str = "task", **kwargs: object) -> Request:
        fields: dict[str, object] = {
            "target": target,
            "prompt": prompt,
            "stdin_is_tty": False,
            "overrides": Overrides(),
            "base": None,
            "repo": None,
            "run_dir": None,
            "cwd": self.repo,
            "environ": {"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
            "home": self.home,
        }
        fields.update(kwargs)
        return Request(**fields)  # type: ignore[arg-type]

    def run(self, target: str, prompt: str = "task", **kwargs: object) -> engine.Outcome:
        return execute(plan(self.request(target, prompt, **kwargs)), say=self.said.append)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    (home / ".config" / "ha" / "models.toml").write_text(
        'codex = "codex-default"\nclaude = "claude-default"\nopencode = "oc-default"\n'
    )
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("user rules\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "CLAUDE.md").write_text("repo rules\n")
    fakes: dict[str, _Fake] = {}

    def provider(name: str) -> _Fake:
        return fakes.setdefault(name, _Fake(name))

    monkeypatch.setattr(engine, "get_provider", provider)
    return World(home=home, repo=repo, fakes=fakes)


def test_a_one_step_run_writes_its_records(world: World) -> None:
    outcome = world.run("codex")
    assert outcome.exit_code == 0
    assert outcome.final is not None and outcome.final.text == "the answer"
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["run_id"] == outcome.run_id
    assert report["status"] == "answered"
    assert report["exit_code"] == 0
    assert report["text"] == "the answer"
    assert report["target"] == {"kind": "provider", "name": "codex"}
    assert report["repository"] == str(world.repo.resolve())
    assert [s["dir"] for s in report["steps"]] == ["steps/01-run-codex"]
    assert report["cost_usd"] == 0.5
    assert (outcome.run_dir / "steps" / "01-run-codex" / "result.json").is_file()
    assert (outcome.run_dir / "prompt.md").read_text() == "task"
    assert outcome.run_dir == world.home / ".cache" / "ha" / "runs" / outcome.run_id
    assert world.registry().resolve(outcome.run_id).status == "answered"


def test_the_spec_carries_the_role_the_context_and_a_read_only_workspace(world: World) -> None:
    world.roles('[rev]\nprovider = "codex"\ncontext = "full"\ninstructions = "be terse"\n')
    world.run("rev")
    spec = world.fakes["codex"].specs[0]
    assert spec.model == "codex-default"
    assert spec.profile.workspace is not None
    assert spec.profile.workspace.path == world.repo.resolve()
    assert not spec.profile.workspace.write
    assert spec.context is not None
    preamble = spec.context.preamble()
    assert "user rules" in preamble and "repo rules" in preamble
    assert preamble.rstrip().endswith("be terse\n</instructions>".rstrip())
    assert spec.name.startswith("ha-")


def test_a_failed_step_is_a_failed_run(world: World) -> None:
    world.fakes["codex"] = _Fake("codex", code=1)
    outcome = world.run("codex")
    assert outcome.exit_code == 1
    assert world.registry().resolve(outcome.run_id).status == "failed"


def test_an_exhausted_chain_returns_its_last_links_code(world: World) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    world.fakes["codex"] = _Fake("codex", code=3)
    world.fakes["claude"] = _Fake("claude", code=4)
    outcome = world.run("r")
    assert outcome.exit_code == 4
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["steps"][0]["exit_code"] == 4
    step = outcome.run_dir / "steps" / "01-run-r"
    assert (step / "links" / "0-codex" / "result.json").is_file()
    assert (step / "links" / "1-claude" / "result.json").is_file()
    assert any("falling back to claude" in line for line in world.said)


def test_a_chain_that_falls_back_answers_with_its_second_link(world: World) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    world.fakes["codex"] = _Fake("codex", code=3)
    outcome = world.run("r")
    assert outcome.exit_code == 0
    assert outcome.final is not None and outcome.final.provider == "claude"
    assert (outcome.run_dir / "steps" / "01-run-r" / "result.json").is_file()


def test_a_custom_run_dir_inside_the_repository_is_refused(world: World) -> None:
    """Review Focus 3."""
    with pytest.raises(UsageError, match="inside the repository"):
        world.run("codex", run_dir=world.repo / "out")
    assert not (world.repo / "out").exists()


def test_a_custom_run_dir_is_used_and_registered(world: World, tmp_path: Path) -> None:
    outcome = world.run("codex", run_dir=tmp_path / "mine")
    assert outcome.run_dir == tmp_path / "mine"
    assert world.registry().resolve(outcome.run_id).run_dir == tmp_path / "mine"


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("ok")
time.sleep(60)
"""


def test_an_unconfined_write_in_progress_refuses_the_run(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.02)
        with pytest.raises(UsageError, match="an unconfined write is running"):
            world.run("codex")
    finally:
        holder.kill()
        holder.wait()
    assert "codex" not in world.fakes, "no provider may start"


def test_an_interrupted_run_releases_its_locks_and_reads_incomplete(world: World) -> None:
    """Review Focus 5, in the engine: locks released, status left non-final."""
    world.fakes["codex"] = _Fake("codex", raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        world.run("codex")
    registry = world.registry()
    entries = sorted((world.state / "runs").glob("*.json"))
    assert len(entries) == 1
    entry = registry.resolve(entries[0].stem)
    assert entry.status == "running"
    assert registry.effective_status(entry, None) == "incomplete"
    assert locks.is_free(world.state / "unconfined.lock")
    world.fakes["codex"] = _Fake("codex")
    start = time.monotonic()
    assert world.run("codex").exit_code == 0
    assert time.monotonic() - start < 2


def test_outside_a_repository_the_cwd_is_the_workspace(world: World, tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    monkey_cwd = lonely
    outcome = execute(plan(world.request("codex", cwd=monkey_cwd)), say=world.said.append)
    spec = world.fakes["codex"].specs[0]
    assert spec.profile.workspace is not None
    # /tmp may itself sit inside a repository on some hosts: the workspace is the
    # work tree discovered from the cwd, or the cwd when there is none.
    assert spec.profile.workspace.path in {lonely.resolve(), *(p for p in lonely.resolve().parents)}
    assert outcome.exit_code == 0
