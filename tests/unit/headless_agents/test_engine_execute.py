"""engine.execute(): a one-step read-only run, its locks, its records (spec 0.5.0 §3.4, §3.8, §3.10)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import engine, locks, proof_state
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
from headless_agents.proofs import CLI_RAILS, proof_path, record_proof
from headless_agents.registry import Probe
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
    #: The event log this provider writes, as its rail would (plan Task 5).
    events: str | None = None
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        if self.raises is not None:
            raise self.raises
        spec = spec.with_run_dir_defaults()
        if self.events is not None:
            assert spec.events_log is not None
            spec.events_log.parent.mkdir(parents=True, exist_ok=True)
            spec.events_log.write_text(self.events, encoding="utf-8")
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


def test_a_read_only_runs_entry_records_its_providers_and_no_continuation(world: World) -> None:
    """Lot 3: every entry records its role's providers; the report's copy is a write run's."""
    outcome = world.run("codex")
    entry = world.registry().resolve(outcome.run_id)
    assert entry.providers == ("codex",) and entry.continues is None
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["implement_providers"] is None and report["continues"] is None


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


def test_expired_wait_exits_2_and_runs_no_provider(world: World, tmp_path: Path) -> None:
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        limit = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        request = world.request("codex", wait_seconds=0.15)
        with pytest.raises(
            UsageError,
            match=r"^--wait 0\.15 s expired: an unconfined write holds the global lock; nothing ran$",
        ):
            execute(plan(request), say=world.said.append)
        assert "codex" not in world.fakes
    finally:
        holder.kill()
        holder.wait()


def test_expired_wait_forgets_the_unstarted_read(world: World, tmp_path: Path) -> None:
    """An explicit ``--wait`` timeout used to mark a read's entry ``failed``
    and keep its (empty) run dir, as if a run that never started had left
    something behind (codex review of PR #239). Nothing ran: forget it."""
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        limit = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        request = world.request("codex", wait_seconds=0.15)
        with pytest.raises(
            UsageError,
            match=r"^--wait 0\.15 s expired: an unconfined write holds the global lock; nothing ran$",
        ):
            execute(plan(request), say=world.said.append)
        assert "codex" not in world.fakes
        assert list((world.state / "runs").glob("*.json")) == []
    finally:
        holder.kill()
        holder.wait()


def test_without_wait_keeps_the_ten_second_default(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.20)
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        limit = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        started = time.monotonic()
        with pytest.raises(UsageError, match="an unconfined write is running"):
            world.run("codex")
        assert 0.18 <= time.monotonic() - started < 0.8
        assert "codex" not in world.fakes
        # No-flag behaviour is unchanged (codex review of PR #239): the read's
        # entry stays, marked "failed", instead of being forgotten.
        (entry,) = (world.state / "runs").glob("*.json")
        assert world.registry().resolve(entry.stem).status == "failed"
    finally:
        holder.kill()
        holder.wait()


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


# ── the executor gate: isolation proven per rail (spec §3.8.0, Task 15b) ─────


def test_a_rail_without_an_isolation_proof_is_refused(world: World) -> None:
    proof_path(world.state, "codex").unlink()
    with pytest.raises(UsageError, match=r"codex codex 1\.0 has no passing isolation proof"):
        world.run("codex")
    assert "codex" not in world.fakes
    (entry,) = (world.state / "runs").glob("*.json")
    assert world.registry().resolve(entry.stem).status == "failed"


def test_the_refusal_names_the_command_that_records_a_proof(world: World) -> None:
    proof_path(world.state, "codex").unlink()
    with pytest.raises(UsageError, match="test_proofs_live.py"):
        world.run("codex")


def test_the_refusal_names_the_reprove_command_of_proof_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source: the engine's own refusal names whatever proof_state.reprove_command
    says, so it and ``ha providers`` can never disagree (spec 0.5.2 lot 2, Task 3)."""
    monkeypatch.setattr(
        proof_state, "reprove_command", lambda rail, kinds: f"SENTINEL {rail} {list(kinds)}"
    )
    proof_path(world.state, "codex").unlink()
    with pytest.raises(UsageError, match=r"SENTINEL codex \['isolation'\]"):
        world.run("codex")


def test_a_proof_of_another_version_refuses(world: World) -> None:
    record_proof(world.state, "codex", version="codex 0.9", isolation=True, today="2026-09-01")
    with pytest.raises(UsageError, match="isolation proof"):
        world.run("codex")


def test_a_failed_proof_refuses(world: World) -> None:
    record_proof(world.state, "codex", version="codex 1.0", isolation=False, today="2026-09-25")
    with pytest.raises(UsageError, match="failed"):
        world.run("codex")


def test_a_chain_with_one_unproven_link_runs_no_link(world: World) -> None:
    """Fault tolerance must not route a run onto an unproven executor."""
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    proof_path(world.state, "claude").unlink()
    with pytest.raises(UsageError, match="claude"):
        world.run("r")
    assert not world.fakes


def test_an_http_provider_needs_no_proof(world: World) -> None:
    outcome = world.run("mistral", overrides=Overrides(model="mistral-small-latest"))
    assert outcome.exit_code == 0


def test_a_lifecycle_lock_not_obtained_leaves_no_entry_and_no_directory(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review of #207: a run that never started must not stay registered."""
    real_held = engine.held

    def busy(path, *, rank, **kwargs):  # type: ignore[no-untyped-def]
        if rank is locks.Rank.LIFECYCLE:
            raise locks.LockTimeout("the lifecycle lock: busy")
        return real_held(path, rank=rank, **kwargs)

    monkeypatch.setattr(engine, "held", busy)
    with pytest.raises(UsageError, match="lifecycle"):
        world.run("codex")
    assert not list((world.state / "runs").glob("*.json"))
    runs_root = world.home / ".cache" / "ha" / "runs"
    assert not runs_root.exists() or not any(runs_root.iterdir())
    assert "codex" not in world.fakes


def test_the_entry_is_published_under_its_lifecycle_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review of #207 (round 2): ``ha clean`` never sees a registered run whose lock is free."""
    free_at_publication: list[bool] = []
    real_create = Registry.create

    def spy(self: Registry, run_id: str, **kwargs):  # type: ignore[no-untyped-def]
        entry = real_create(self, run_id, **kwargs)
        free_at_publication.append(locks.is_free(self.lifecycle_lock(run_id)))
        return entry

    monkeypatch.setattr(Registry, "create", spy)
    assert world.run("codex").exit_code == 0
    assert free_at_publication == [False]


def test_an_id_whose_lifecycle_lock_is_held_is_minted_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process holding the minted id's lock means the id is taken: mint again."""
    ids = iter(["20260925T000000-aaaaaaaa", "20260925T000000-bbbbbbbb"])
    monkeypatch.setattr(Registry, "mint", lambda self: next(ids))
    registry = world.registry()
    (world.state / "runs").mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
            str(registry.lifecycle_lock("20260925T000000-aaaaaaaa")),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        outcome = world.run("codex")
    finally:
        holder.kill()
        holder.wait()
    assert outcome.run_id == "20260925T000000-bbbbbbbb"
    assert not (world.state / "runs" / "20260925T000000-aaaaaaaa.json").exists()


# ── tool counts in run.json (spec §3.11, plan Task 5) ──────────────────────

TOOL_FIXTURES = Path(__file__).parent / "fixtures" / "tool_counts"


def _steps(outcome: engine.Outcome) -> list[dict[str, object]]:
    report = json.loads((outcome.run_dir / "run.json").read_text())
    steps = report["steps"]
    assert isinstance(steps, list)
    return steps


def test_a_step_records_its_tool_counts(world: World) -> None:
    world.fakes["codex"] = _Fake("codex", events=(TOOL_FIXTURES / "codex.events.jsonl").read_text())
    assert _steps(world.run("codex"))[0]["tools"] == {"command_execution": 4}


def test_a_step_whose_log_is_missing_is_not_measured(world: World) -> None:
    assert _steps(world.run("codex"))[0]["tools"] is None


def test_a_claude_step_is_not_measured(world: World) -> None:
    assert _steps(world.run("claude"))[0]["tools"] is None


def test_an_http_step_has_no_tools(world: World) -> None:
    outcome = world.run(
        "openrouter",
        overrides=Overrides(model="some/model"),
        environ={"PATH": "/usr/bin:/bin", "HOME": str(world.home), "OPENROUTER_API_KEY": "k"},
    )
    assert _steps(outcome)[0]["tools"] == {}


def test_a_chain_records_the_counts_of_the_link_that_answered(world: World) -> None:
    world.roles('[pair]\nchain = ["claude:c", "codex:x"]\n')
    world.fakes["claude"] = _Fake("claude", code=3)
    world.fakes["codex"] = _Fake("codex", events=(TOOL_FIXTURES / "codex.events.jsonl").read_text())
    (step,) = _steps(world.run("pair"))
    assert step["provider"] == "codex" and step["tools"] == {"command_execution": 4}


# ── admission refusals read the timeout's fields, never its text (0.5.3 lot 4b) ─


def test_no_exception_text_is_parsed() -> None:
    """A refusal that matched words in an exception's message broke silently the day
    the message changed: the admission gate's own did, when the queue replaced it.
    The engine reads ``AdmissionTimeout.phase`` and ``.ahead`` instead."""
    tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    offending = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        for operand in (node.left, *node.comparators):
            if (
                isinstance(operand, ast.Constant)
                and isinstance(operand.value, str)
                and ("admission gate" in operand.value or "queue" in operand.value)
            ):
                offending.append(f"line {node.lineno}: {operand.value!r}")
    # Nor cut up: str(exc).split(...), .partition(...), .startswith(...) and the like.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"split", "rsplit", "partition", "startswith", "endswith", "find"}
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "str"
            and len(node.func.value.args) == 1
            and isinstance(node.func.value.args[0], ast.Name)
            and node.func.value.args[0].id == "exc"
        ):
            offending.append(f"line {node.lineno}: str(exc).{node.func.attr}(...)")
    assert offending == []


_WAITING_WRITER = """
import sys, time
from pathlib import Path
from headless_agents.locks import AdmissionWait, admit_global
with admit_global(Path(sys.argv[1]), exclusive=True, wait=AdmissionWait(30), label="W1"):
    time.sleep(30)
"""


def test_an_expired_wait_behind_a_queued_writer_names_it(world: World, tmp_path: Path) -> None:
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    writer = None
    try:
        limit = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        writer = subprocess.Popen([sys.executable, "-c", _WAITING_WRITER, str(world.state)])
        limit = time.monotonic() + 10
        while not any(w.alive and w.label == "W1" for w in locks.waiters(world.state)):
            assert time.monotonic() < limit and writer.poll() is None
            time.sleep(0.01)
        request = world.request("codex", wait_seconds=0.3)
        with pytest.raises(
            UsageError,
            match=r"^--wait 0\.3 s expired: waiting behind 1 earlier admission\(s\) \(W1\); "
            r"nothing ran$",
        ):
            execute(plan(request), say=world.said.append)
        assert "codex" not in world.fakes
    finally:
        for process in (holder, writer):
            if process is not None:
                process.kill()
                process.wait()


def test_the_default_refusal_of_a_read_is_unchanged(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.02)
        with pytest.raises(UsageError) as refused:
            world.run("codex")
        assert str(refused.value) == (
            "an unconfined write is running: nothing ran; retry once it has ended"
        )
    finally:
        holder.kill()
        holder.wait()


def test_each_admission_is_labelled_with_its_run_id(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels: list[str] = []
    real = locks.admit_global

    def recording(state: Path, *, exclusive: bool, wait: locks.AdmissionWait, label: str = ""):  # type: ignore[no-untyped-def]
        labels.append(label)
        return real(state, exclusive=exclusive, wait=wait, label=label)

    monkeypatch.setattr(locks, "admit_global", recording)
    world.run("codex")
    assert labels == world.registry().run_ids()
    assert labels and labels[0]
