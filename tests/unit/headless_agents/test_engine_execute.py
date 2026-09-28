"""engine.execute(): a one-step read-only run, its locks, its records (spec 0.5.0 §3.4, §3.8, §3.10)."""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli, engine, locks, proof_state
from headless_agents.context import resolve_context, role_instructions
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
from headless_agents.proofs import CLI_RAILS, proof_path, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Entry, Registry
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
    #: The text of a failed run: codex keeps an answer that is not JSON (0.5.3 lot 1).
    failure_text: str | None = None
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
                text=self.answer if self.code == 0 else self.failure_text,
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


def test_the_first_line_names_the_effective_configuration(world: World) -> None:
    outcome = world.run("codex")
    assert world.said[0] == (
        "step 1 run codex: codex/codex-default (models.toml), "
        "effort medium (default), timeout 300 s (default)"
    )
    spec = world.fakes["codex"].specs[0]
    assert (spec.model, spec.reasoning_effort, spec.timeout_seconds) == (
        "codex-default",
        "medium",
        300.0,
    )
    assert outcome.report["steps"][0]["model_source"] == "models.toml"


def test_the_flags_are_named_as_sources(world: World) -> None:
    world.run("codex", overrides=Overrides(model="flag", effort="high", timeout=42))
    assert world.said[0] == (
        "step 1 run codex: codex/flag (-m), effort high (--effort), timeout 42 s (--timeout)"
    )


def test_declared_defaults_are_named_as_role_values(world: World) -> None:
    world.roles('[rev]\nprovider = "codex"\neffort = "medium"\ntimeout = 300\n')
    world.run("rev")
    assert world.said[0].endswith("effort medium (role), timeout 300 s (role)")


def test_a_rail_that_ignores_effort_says_so(world: World) -> None:
    world.run("claude")
    assert "effort medium (default) (not used by claude)" in world.said[0]


def test_each_chain_link_prints_its_own_line(world: World) -> None:
    world.roles('[r]\nchain = ["codex:m1", "claude"]\n')
    world.fakes["codex"] = _Fake("codex", code=3)
    world.run("r")
    assert world.said[0].startswith("step 1 run r: codex/m1 (chain link),")
    fallback = next(i for i, line in enumerate(world.said) if "falling back to claude" in line)
    assert world.said[fallback + 1].startswith("step 1 run r: claude/claude-default (models.toml),")


def test_run_json_steps_carry_effort_timeout_and_model_source(world: World) -> None:
    outcome = world.run("codex")
    step = json.loads((outcome.run_dir / "run.json").read_text())["steps"][0]
    assert (step["effort"], step["timeout_seconds"], step["model_source"]) == (
        "medium",
        300.0,
        "models.toml",
    )


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


def test_the_language_line_reaches_the_bundle(world: World) -> None:
    world.roles(
        '[rev]\nprovider = "codex"\ncontext = "none"\nlanguage = "en"\n'
        'instructions = "Answer briefly."\n'
    )
    world.run("rev")
    preamble = world.fakes["codex"].specs[0].context.preamble()
    assert "<instructions" in preamble
    assert "Answer briefly.\n\nWrite your whole answer in English" in preamble


def test_a_role_without_language_has_a_byte_identical_bundle(world: World) -> None:
    world.roles('[rev]\nprovider = "codex"\ncontext = "none"\ninstructions = "Answer briefly."\n')
    world.run("rev")
    actual = world.fakes["codex"].specs[0].context.preamble()
    expected = (
        resolve_context(
            level="none",
            repository_root=world.repo,
            user_files=(world.home / ".claude" / "CLAUDE.md",),
            include_parents=False,
        )
        .with_role(role_instructions("rev", "Answer briefly."))
        .preamble()
    )
    assert actual == expected


def test_a_language_without_instructions_still_reaches_the_bundle(world: World) -> None:
    world.roles('[rev]\nprovider = "codex"\ncontext = "none"\nlanguage = "en"\n')
    world.run("rev")
    assert "Write your whole answer in English" in world.fakes["codex"].specs[0].context.preamble()


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

_HOLD_TWO = """
import fcntl, os, pathlib, sys, time
descriptors = [os.open(path, os.O_RDWR | os.O_CREAT, 0o600) for path in sys.argv[1:3]]
for descriptor in descriptors:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
pathlib.Path(sys.argv[3]).write_text("ok")
time.sleep(60)
"""

_HOLD_SHARED = """
import fcntl, os, pathlib, sys, time
life = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
global_lock = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(life, fcntl.LOCK_EX)
fcntl.flock(global_lock, fcntl.LOCK_SH)
pathlib.Path(sys.argv[3]).write_text("ok")
time.sleep(60)
"""


def test_a_refusal_names_the_run_holding_the_global_lock(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.2)
    registry = world.registry()
    entry = registry.register(
        run_dir=None,
        target={"kind": "provider", "name": "codex"},
        repository=world.repo,
        lineage=None,
    )
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD_TWO,
            str(registry.lifecycle_lock(entry.run_id)),
            str(lock),
            str(ready),
        ]
    )
    try:
        while not ready.exists():
            time.sleep(0.01)
        with pytest.raises(UsageError, match=rf"held by {entry.run_id} \(codex,"):
            world.run("codex")
    finally:
        holder.kill()
        holder.wait()


def test_a_holder_outside_the_registry_is_said_so(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.2)
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.01)
        with pytest.raises(UsageError, match="holder outside the registry"):
            world.run("codex")
    finally:
        holder.kill()
        holder.wait()


def test_a_queued_run_reads_waiting_in_runs_and_show(world: World, tmp_path: Path) -> None:
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        limit = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(world.run, "codex", wait_seconds=5)
            while True:
                waiting = [w for w in locks.waiters(world.state) if w.alive and w.label]
                if waiting:
                    break
                assert time.monotonic() < limit
                time.sleep(0.01)
            run_id = waiting[0].label
            entry = world.registry().resolve(run_id)
            assert world.registry().effective_status(entry, None, waiting_ids={run_id}) == "waiting"
            out = io.StringIO()
            assert (
                cli.main(
                    ["runs", "--json"],
                    environ={"HOME": str(world.home)},
                    stdout=out,
                    stderr=io.StringIO(),
                    home=world.home,
                    cwd=world.repo,
                )
                == 0
            )
            rows = json.loads(out.getvalue())
            assert next(row for row in rows if row["run_id"] == run_id)["status"] == "waiting"
            out = io.StringIO()
            assert (
                cli.main(
                    ["show", run_id, "--json"],
                    environ={"HOME": str(world.home)},
                    stdout=out,
                    stderr=io.StringIO(),
                    home=world.home,
                    cwd=world.repo,
                )
                == 0
            )
            assert json.loads(out.getvalue())["status"] == "waiting"
            holder.kill()
            holder.wait()
            outcome = future.result(timeout=5)
            assert world.registry().resolve(outcome.run_id).status == "answered"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_a_queued_write_without_a_lineage_yet_reads_waiting(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = world.registry()
    run_id = registry.mint()
    registry.create(
        run_id,
        run_dir=None,
        target={"kind": "role", "name": "build"},
        repository=world.repo,
        lineage=run_id,
    )
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD,
            str(registry.lifecycle_lock(run_id)),
            str(ready),
        ]
    )
    try:
        while not ready.exists():
            time.sleep(0.01)
        monkeypatch.setattr(
            locks,
            "waiters",
            lambda _: [locks.Waiter(1, True, run_id, holder.pid, "2026-09-28T00:00:00Z", True)],
        )
        for command in (["runs", "--json"], ["show", run_id, "--json"]):
            out = io.StringIO()
            assert (
                cli.main(
                    command,
                    environ={"HOME": str(world.home)},
                    stdout=out,
                    stderr=io.StringIO(),
                    home=world.home,
                    cwd=world.repo,
                )
                == 0
            )
            document = json.loads(out.getvalue())
            if isinstance(document, list):
                document = next(row for row in document if row["run_id"] == run_id)
            assert document["status"] == "waiting"
    finally:
        holder.kill()
        holder.wait()


def test_a_refusal_names_up_to_five_readers_and_excludes_itself(
    world: World, tmp_path: Path
) -> None:
    registry = world.registry()
    lock = world.state / "unconfined.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holders: list[subprocess.Popen[bytes]] = []
    entries: list[Entry] = []
    try:
        for index in range(7):
            entry = registry.register(
                run_dir=None,
                target={"kind": "provider", "name": "codex"},
                repository=world.repo,
                lineage=None,
            )
            entries.append(entry)
            ready = tmp_path / f"ready-{index}"
            holders.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _HOLD_SHARED,
                        str(registry.lifecycle_lock(entry.run_id)),
                        str(lock),
                        str(ready),
                    ]
                )
            )
            while not ready.exists():
                time.sleep(0.01)
        suffix = engine._holder_suffix(world.state, registry, own="not-a-run")
        assert suffix.count("(codex,") == 5
        assert "and 2 more" in suffix
        suffix = engine._holder_suffix(world.state, registry, own=entries[0].run_id)
        assert entries[0].run_id not in suffix
        assert suffix.count("(codex,") == 5
        assert "and 1 more" in suffix
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()


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
            match=r"^--wait 0\.15 s expired: an unconfined write holds the global lock; nothing ran; held by a holder outside the registry",
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
            match=r"^--wait 0\.15 s expired: an unconfined write holds the global lock; nothing ran; held by a holder outside the registry",
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
    with pytest.raises(UsageError, match="ha prove codex --isolation"):
        world.run("codex")
    with pytest.raises(UsageError, match="after a CLI update: ha prove --stale"):
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


# ── started_at, and ha clean deciding from it (0.5.3 lot 4a, ticket fbcda7d5) ─


def _only_entry(world: World) -> Entry:
    (run_id,) = world.registry().run_ids()
    return world.registry().resolve(run_id)


def test_a_run_records_started_at_when_its_step_starts(world: World) -> None:
    world.run("codex")
    entry = _only_entry(world)
    assert entry.started_at is not None
    assert (entry.run_dir / "run.json").is_file()


def test_a_run_refused_before_its_step_records_no_started_at(world: World) -> None:
    (world.state / "proofs" / "codex.json").unlink()
    with pytest.raises(UsageError):
        world.run("codex")
    entry = _only_entry(world)
    assert entry.started_at is None


def _clean(world: World, run_id: str) -> int:
    return engine.clean(
        run_id,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(world.home)},
        home=world.home,
        say=world.said.append,
    )


def test_clean_ignores_a_forged_report_on_a_run_that_never_started(world: World) -> None:
    (world.state / "proofs" / "codex.json").unlink()
    with pytest.raises(UsageError):
        world.run("codex")
    entry = _only_entry(world)
    entry.run_dir.mkdir(parents=True, exist_ok=True)
    (entry.run_dir / "run.json").write_text('{"schema": 1, "kind": "run"}')
    assert _clean(world, entry.run_id) == 0
    assert world.said[-1] == f"{entry.run_id} never started: forgotten"
    assert world.registry().run_ids() == []


def test_clean_ignores_a_deleted_report_on_a_run_that_started(world: World) -> None:
    world.run("codex")
    entry = _only_entry(world)
    (entry.run_dir / "run.json").unlink()
    assert _clean(world, entry.run_id) == 0
    assert world.said[-1].startswith(f"{entry.run_id} cleaned:")
    assert world.registry().resolve(entry.run_id).cleaned_at is not None


def test_clean_of_a_legacy_entry_keeps_the_report_rule(world: World) -> None:
    """An entry made before started_at existed: the report is all there is."""
    world.run("codex")
    entry = _only_entry(world)
    path = world.state / "runs" / f"{entry.run_id}.json"
    document = json.loads(path.read_text())
    del document["started_at"]
    path.write_text(json.dumps(document))
    assert _clean(world, entry.run_id) == 0
    assert world.said[-1].startswith(f"{entry.run_id} cleaned:")


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


def test_the_default_refusal_of_a_read_keeps_its_prefix(
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
        assert str(refused.value).startswith(
            "an unconfined write is running: nothing ran; retry once it has ended"
        )
        assert "holder outside the registry" in str(refused.value)
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


# ── output schema (0.5.3 lot 1) ─────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def test_output_schema_reaches_every_link_spec(world: World) -> None:
    world.roles('[r]\nchain = ["codex", "claude"]\n')
    world.fakes["codex"] = _Fake("codex", code=3)
    world.fakes["claude"] = _Fake("claude", answer='{"ok":true}')
    outcome = world.run("r", output_schema=SCHEMA)
    assert outcome.exit_code == 0, world.said
    assert [fake.specs[0].output_schema for fake in world.fakes.values()] == [SCHEMA, SCHEMA]


def test_output_schema_is_refused_for_a_workflow_target(world: World) -> None:
    world.roles('[implementer]\nprovider = "codex"\nwrite = true\n')
    (world.home / ".config" / "ha" / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n'
    )
    with pytest.raises(UsageError, match="--output-schema needs a provider or a role"):
        world.run("build", output_schema=SCHEMA)
    assert world.registry().run_ids() == []


def test_output_schema_is_refused_when_any_link_cannot_honour_it(world: World) -> None:
    world.roles('[r]\nchain = ["codex", "opencode"]\n')
    with pytest.raises(UsageError, match="opencode cannot constrain"):
        world.run("r", output_schema=SCHEMA)
    assert world.registry().run_ids() == []
    assert world.fakes == {}


def test_a_non_json_answer_is_reported_as_output_not_json(world: World) -> None:
    world.fakes["codex"] = _Fake("codex", code=1, failure_text="nope")
    outcome = world.run("codex", output_schema=SCHEMA)
    assert outcome.exit_code == 1
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["status"] == "failed" and report["failure_reason"] == "output_not_json"


def test_a_json_answer_is_answered_verbatim(world: World) -> None:
    world.fakes["codex"] = _Fake("codex", answer='{"ok":true}')
    outcome = world.run("codex", output_schema=SCHEMA)
    assert outcome.exit_code == 0
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["text"] == '{"ok":true}' and report["status"] == "answered"
    assert report["failure_reason"] is None


def test_an_answer_outside_the_schema_never_exits_0(world: World) -> None:
    """Fail-closed at the engine too: a rail that answered text that is not JSON under
    a schema -- whatever it returned -- fails the run, and the text is kept."""
    world.fakes["codex"] = _Fake("codex", answer="I think ok is true")
    outcome = world.run("codex", output_schema=SCHEMA)
    assert outcome.exit_code == 1
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["status"] == "failed" and report["failure_reason"] == "output_not_json"
    assert report["exit_code"] == 1 and report["text"] == "I think ok is true"
    assert report["steps"][0]["exit_code"] == 0
    assert world.registry().resolve(outcome.run_id).status == "failed"


def test_without_a_schema_a_text_answer_is_answered(world: World) -> None:
    outcome = world.run("codex")
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert outcome.exit_code == 0 and report["status"] == "answered"
    assert report["failure_reason"] is None
