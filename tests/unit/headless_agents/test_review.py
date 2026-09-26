"""The shape ``review`` through the engine: the vendor rule, the panel, the verdict, the
records and the cleanup (spec 0.5.0 §3.5, §3.8.4, §3.8.6, §3.10; lot 4).

The providers are fakes -- a writer edits its workspace, a reader answers a text
-- over a real git repository and the real engine, state directory and reports.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import engine, lineage, locks, provenance, quarantine, review_flow, reviews
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec
from headless_agents.state import publish

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
APPROVE = "Looks right.\nVERDICT: APPROVE"
CHANGES = "src/app.py:2 high: the flag is never read.\nVERDICT: CHANGES"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


@dataclass
class _Agent:
    """A fake rail: a writer applies ``edit`` to its writable workspace; a reader answers
    ``answer`` -- a text, or a function of the spec -- with exit ``code``."""

    name: str
    edit: Callable[[Path], None] | None = None
    answer: str | Callable[[RunSpec], str] | None = APPROVE
    code: int = 0
    barrier: threading.Barrier | None = None
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        workspace = spec.profile.workspace
        assert workspace is not None
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        text: str | None
        if workspace.write:
            assert self.edit is not None
            self.edit(workspace.path)
            text = "I did it"
        else:
            text = self.answer(spec) if callable(self.answer) else self.answer
        spec = spec.with_run_dir_defaults()
        return record(
            spec,
            RunResult(
                exit_code=self.code,
                provider=self.name,
                model=spec.model,
                model_reported=f"{self.name}-served",
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=0.1,
                tool_call_completed=False,
                text=text if self.code == 0 else None,
                run_id=run_id_of(spec),
            ),
        )


ROLES = """\
[implementer]
provider = "codex"
write = true

[reviewer]
provider = "claude"

[reviewer-agy]
provider = "agy"

[reviewer-oc]
provider = "opencode"

[reviewer-codex]
provider = "codex"

[judge]
provider = "claude"
"""

WORKFLOWS = """\
[build]
shape = "implement"
implement = "implementer"

[check]
shape = "review"
review = "reviewer"

[self-check]
shape = "review"
review = "reviewer-codex"

[panel]
shape = "review"
review = ["reviewer", "reviewer-agy", "reviewer-oc"]
judge = "judge"
"""


@dataclass
class World:
    home: Path
    repo: Path
    agents: dict[str, _Agent]
    said: list[str] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")

    def request(self, target: str, prompt: str | None, **fields: object) -> Request:
        request = Request(
            target=target,
            prompt=prompt,
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

    def review(
        self, target: str = "check", prompt: str | None = None, **fields: object
    ) -> engine.Outcome:
        return execute(plan(self.request(target, prompt, **fields)), say=self.said.append)

    def implement(self, task: str = "Add a flag.", **fields: object) -> engine.Outcome:
        outcome = execute(plan(self.request("build", task, **fields)), say=self.said.append)
        assert outcome.exit_code == 0, self.said
        return outcome

    def commit_by_hand(self, text: str = "print('v2')\n", subject: str = "feat: by hand") -> str:
        (self.repo / "app.py").write_text(text)
        _git(self.repo, "commit", "-qam", subject)
        return _git(self.repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    config = home / ".config" / "ha"
    config.mkdir(parents=True)
    (config / "models.toml").write_text(
        'codex = "codex-m"\nclaude = "claude-m"\nopencode = "oc-m"\nagy = "agy-m"\n'
    )
    (config / "roles.toml").write_text(ROLES)
    (config / "workflows.toml").write_text(WORKFLOWS)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Op")
    _git(repo, "config", "user.email", "op@example.test")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    # origin/HEAD, the default --base (§3.5), at the first commit.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    agents = {name: _Agent(name) for name in ("codex", "claude", "agy", "opencode")}
    agents["codex"].edit = lambda root: (root / "app.py").write_text("print('v1')\nFLAG = 1\n")
    monkeypatch.setattr(engine, "get_provider", lambda name: agents[name])
    monkeypatch.setattr(
        engine,
        "probe",
        lambda name, **_: Probe(available=True, detail="fake", version=f"{name} 1.0"),
    )
    state = (home / ".local" / "state" / "ha").resolve()
    for rail in CLI_RAILS:
        record_proof(
            state, rail, version=f"{rail} 1.0", isolation=True, confinement=True, today="2026-09-26"
        )
    return World(home=home, repo=repo, agents=agents)


def _report(outcome: engine.Outcome) -> dict[str, object]:
    return json.loads((outcome.run_dir / "run.json").read_text())


# ── one reviewer ────────────────────────────────────────────────────────────


def test_an_approving_review_records_its_head_verdict_check_and_cleanup(world: World) -> None:
    head = world.commit_by_hand()
    outcome = world.review()
    assert outcome.exit_code == 0, world.said
    run_id = outcome.run_id
    result = reviews.load_result(world.state, run_id)
    assert (result.head, result.verdict, result.text) == (head, "approve", APPROVE)
    assert result.check == reviews.load_check(world.state, run_id)
    report = _report(outcome)
    assert report["status"] == "approved" and report["verdict"] == "approve"
    assert report["head"] == head and report["text"] == APPROVE
    assert report["vendor_check"] == result.check.to_document()
    assert report["cleanup"] == {"status": "done"}
    assert report["branch"] is None and report["commits"] is None
    assert not (outcome.run_dir / "wt").exists()
    assert "print('v2')" in (outcome.run_dir / "change.patch").read_text()
    assert world.registry().resolve(run_id).status == "approved"
    (step,) = report["steps"]  # type: ignore[misc]
    assert (step["slot"], step["role"], step["verdict"]) == ("review", "reviewer", "approve")
    assert step["dir"] == "steps/01-review-reviewer"


def test_a_review_asking_for_changes_exits_6(world: World) -> None:
    world.commit_by_hand()
    world.agents["claude"].answer = CHANGES
    outcome = world.review()
    assert outcome.exit_code == 6
    assert _report(outcome)["status"] == "changes"
    assert reviews.load_result(world.state, outcome.run_id).verdict == "changes"


def test_the_reviewer_reads_the_pinned_commit_read_only_with_the_diff(world: World) -> None:
    head = world.commit_by_hand()
    world.review(prompt="Mind the flag.")
    (spec,) = world.agents["claude"].specs
    workspace = spec.profile.workspace
    assert workspace is not None and not workspace.write
    assert "<task>\nMind the flag.\n</task>" in spec.prompt
    assert "+print('v2')" in spec.prompt and spec.prompt.endswith("anything must change first.\n")
    assert workspace.path.name == "wt"
    assert head  # the worktree was detached at it; it is gone once the review ends


def test_an_unreadable_verdict_fails_and_writes_no_result(world: World) -> None:
    world.commit_by_hand()
    world.agents["claude"].answer = "I think it is fine."
    outcome = world.review()
    assert outcome.exit_code == 1
    report = _report(outcome)
    assert report["failure_reason"] == "unreadable_verdict" and report["status"] == "failed"
    assert not reviews.result_path(world.state, outcome.run_id).exists()
    assert report["cleanup"] == {"status": "done"}


def test_a_failing_reviewer_fails_the_review(world: World) -> None:
    world.commit_by_hand()
    world.agents["claude"].code = 3
    outcome = world.review()
    assert outcome.exit_code == 1
    report = _report(outcome)
    assert report["failure_reason"] == "step_failed"
    assert report["steps"][0]["exit_code"] == 3  # type: ignore[index]


def test_a_reviewer_answering_nothing_fails_the_review(world: World) -> None:
    world.commit_by_hand()
    world.agents["claude"].answer = "  \n"
    outcome = world.review()
    assert outcome.exit_code == 1 and _report(outcome)["failure_reason"] == "step_failed"


def test_an_empty_diff_is_refused_nothing_to_review(world: World) -> None:
    with pytest.raises(UsageError, match="nothing to review"):
        world.review()
    assert world.agents["claude"].specs == []


def test_a_base_that_does_not_resolve_is_refused(world: World) -> None:
    world.commit_by_hand()
    with pytest.raises(UsageError, match="--base no-such-ref does not resolve"):
        world.review(base="no-such-ref")


def test_head_and_base_name_the_reviewed_range(world: World) -> None:
    first = world.commit_by_hand("print('v2')\n", "feat: one")
    world.commit_by_hand("print('v3')\n", "feat: two")
    outcome = world.review(head=first, base="main~2")
    report = _report(outcome)
    assert report["head"] == first
    assert "print('v3')" not in (outcome.run_dir / "change.patch").read_text()


# ── a panel ─────────────────────────────────────────────────────────────────


def test_a_panel_runs_its_reviewers_concurrently_then_the_judge_decides(world: World) -> None:
    world.commit_by_hand()
    barrier = threading.Barrier(3)
    for name in ("agy", "opencode"):
        world.agents[name].barrier = barrier
    world.agents["agy"].answer = CHANGES
    judged: list[str] = []

    def judge_or_review(spec: RunSpec) -> str:
        if "Judge the reviews below" in spec.prompt:
            judged.append(spec.prompt)
            return "Only the flag finding stands.\n**VERDICT: CHANGES**"
        barrier.wait(timeout=5)
        return APPROVE

    world.agents["claude"].answer = judge_or_review
    outcome = world.review("panel")
    assert outcome.exit_code == 6, world.said
    report = _report(outcome)
    steps = report["steps"]
    assert [(s["index"], s["slot"], s["role"], s["verdict"]) for s in steps] == [  # type: ignore[union-attr]
        (1, "review", "reviewer", "approve"),
        (2, "review", "reviewer-agy", "changes"),
        (3, "review", "reviewer-oc", "approve"),
        (4, "judge", "judge", "changes"),
    ]
    (prompt,) = judged
    assert '<review role="reviewer-agy" provider="agy" model="agy-served">' in prompt
    assert report["text"] == "Only the flag finding stands.\n**VERDICT: CHANGES**"


def test_one_failing_reviewer_stops_the_panel_before_the_judge(world: World) -> None:
    world.commit_by_hand()
    world.agents["opencode"].code = 1
    outcome = world.review("panel")
    assert outcome.exit_code == 1
    judged = [s for s in world.agents["claude"].specs if "Judge the reviews" in s.prompt]
    assert judged == []
    assert [s["slot"] for s in _report(outcome)["steps"]] == ["review"] * 3  # type: ignore[union-attr]


# ── the vendor rule (§3.8.4) ────────────────────────────────────────────────


def test_a_reviewer_sharing_a_vendor_with_the_implementer_is_refused(world: World) -> None:
    built = world.implement()
    with pytest.raises(UsageError, match=r"reviewer reviewer-codex runs codex.*wrote commit"):
        world.review("self-check", review_run=built.run_id)
    assert world.agents["codex"].specs[1:] == []


def test_an_independent_reviewer_reviews_an_implement_run(world: World) -> None:
    built = world.implement()
    outcome = world.review("check", review_run=built.run_id)
    assert outcome.exit_code == 0, world.said
    check = _report(outcome)["vendor_check"]
    assert check["authors"] == ["codex"]  # type: ignore[index]
    assert check["reviewers"] == {"reviewer": ["claude"]}  # type: ignore[index]
    assert check["commits"][0]["run_id"] == built.run_id  # type: ignore[index]


def test_a_hand_written_commit_constrains_nothing(world: World) -> None:
    world.commit_by_hand()
    outcome = world.review("self-check")
    assert outcome.exit_code == 0
    assert _report(outcome)["vendor_check"]["commits"][0]["made_by"] == "hand"  # type: ignore[index]


def test_a_chore_ha_commit_without_provenance_is_refused(world: World) -> None:
    world.commit_by_hand(subject="chore(ha): 20260101T000000-deadbeef implement via codex/m")
    with pytest.raises(UsageError, match=r"chore\(ha\).*no provenance"):
        world.review()


def test_after_an_unconfined_write_a_commit_without_provenance_is_its_writers(
    world: World,
) -> None:
    publish(
        world.state / "unconfined-writers.json",
        {"writers": [{"run_id": "x", "repository": str(world.repo), "providers": ["claude"]}]},
    )
    world.commit_by_hand()
    with pytest.raises(UsageError, match="reviewer reviewer runs claude"):
        world.review()


def test_run_reviews_the_current_tip_after_the_branch_advanced(world: World) -> None:
    """§3.5: --run stands for the lineage's current tip, later continuations included."""
    built = world.implement()
    world.agents["codex"].edit = lambda root: (root / "app.py").write_text(
        "print('v1')\nFLAG = 2\n"
    )
    world.implement("More.", continue_run=built.run_id)
    tip = _git(world.repo, "rev-parse", f"ha/{built.run_id}").strip()
    outcome = world.review("check", review_run=built.run_id)
    report = _report(outcome)
    assert report["head"] == tip
    assert "FLAG = 2" in (outcome.run_dir / "change.patch").read_text()
    assert len(report["vendor_check"]["commits"]) == 2  # type: ignore[index]


def test_run_of_a_lineage_whose_branch_is_gone_is_refused(world: World) -> None:
    built = world.implement()
    _git(world.repo, "worktree", "remove", "--force", str(built.run_dir / "wt"))
    _git(world.repo, "branch", "-D", f"ha/{built.run_id}")
    with pytest.raises(UsageError, match="no longer exists"):
        world.review("check", review_run=built.run_id)


def test_every_lineage_of_the_repository_is_locked_shared_ascending_before_the_first_git(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4: a review's lock order -- the lineage registry lock shared, then every lineage of
    its repository shared, in ascending owner order, before its first git command."""
    from headless_agents import review_flow

    first = world.implement()
    world.agents["codex"].edit = lambda root: (root / "other.py").write_text("x = 1\n")
    second = world.implement("Another.")
    world.commit_by_hand()
    events: list[str] = []
    real_held, real_git = locks.held, review_flow.git

    def held(path: Path, *, rank: locks.Rank, **kwargs: object):  # type: ignore[no-untyped-def]
        mode = "ex" if kwargs.get("exclusive") else "sh"
        events.append(f"lock {rank.name} {path.name} {mode}")
        return real_held(path, rank=rank, **kwargs)  # type: ignore[arg-type]

    def git(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        events.append("git")
        return real_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(review_flow, "held", held)
    monkeypatch.setattr(review_flow, "git", git)
    assert world.review().exit_code == 0
    before_git = events[: events.index("git")]
    owners = sorted([first.run_id, second.run_id])
    assert before_git == [
        "lock LINEAGE_REGISTRY lineages.lock sh",
        *(f"lock LINEAGE {owner}.lock sh" for owner in owners),
    ]


# ── uncertainty refuses (§3.8.4 step 2) ─────────────────────────────────────


def test_a_compromised_lineage_of_the_repository_refuses_any_review(world: World) -> None:
    built = world.implement()
    current = lineage.load(world.state, built.run_id)
    lineage.save(world.state, replace(current, compromised="tripwire"))
    world.commit_by_hand()
    with pytest.raises(UsageError, match=rf"lineage {built.run_id} is compromised \(tripwire\)"):
        world.review()


def test_a_stale_pending_write_refuses_and_quarantines_the_repository(world: World) -> None:
    built = world.implement()
    current = lineage.load(world.state, built.run_id)
    pending = lineage.PendingWrite(
        run_id="20260926T130000-eeeeeeee",
        providers=("codex",),
        unconfined=False,
        start_tip=None,
        start_reflog=None,
    )
    lineage.save(world.state, replace(current, pending=pending))
    world.commit_by_hand()
    with pytest.raises(UsageError, match="unfinished write"):
        world.review()
    assert lineage.load(world.state, built.run_id).compromised == "unfinalized_write"
    assert quarantine.check(world.state, (world.repo / ".git").resolve()) is not None


def test_an_unknown_lineage_of_the_repository_refuses(world: World) -> None:
    built = world.implement()
    lineage.lineage_path(world.state, built.run_id).write_text("{not json")
    world.commit_by_hand()
    with pytest.raises(UsageError, match=f"lineage {built.run_id} is unknown"):
        world.review()


def test_a_repository_quarantine_refuses_before_any_git(world: World) -> None:
    world.commit_by_hand()
    quarantine.publish(
        world.state,
        "repository",
        reason="tripwire",
        run_id="x",
        paths=[],
        common_dir=(world.repo / ".git").resolve(),
    )
    with pytest.raises(UsageError, match="quarantine"):
        world.review()


def test_a_quarantine_published_while_the_review_waits_for_its_locks_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.8.4 step 1: the quarantines are checked under the locks, not only before them."""
    built = world.implement()
    world.commit_by_hand()
    real_held = review_flow.held

    def held(lock: Path, **kwargs: object) -> object:
        if kwargs.get("rank") is locks.Rank.LINEAGE:
            # A write finishing while the review waits on its lineage lock quarantines.
            quarantine.publish(
                world.state,
                "repository",
                reason="tripwire",
                run_id=built.run_id,
                paths=[],
                common_dir=(world.repo / ".git").resolve(),
            )
        return real_held(lock, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(review_flow, "held", held)
    reviewers_before = len(world.agents["claude"].specs)
    with pytest.raises(UsageError, match="quarantine"):
        world.review()
    assert len(world.agents["claude"].specs) == reviewers_before


def test_a_review_waits_for_a_write_holding_its_lineage_then_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = world.implement()
    world.commit_by_hand()
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.2)
    holder = subprocess.Popen(  # noqa: S603 - a fixed argv holding the lock
        [
            "python3",
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(30)\n",
            str(lineage.lineage_lock(world.state, built.run_id)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(UsageError, match=f"the lineage lock of {built.run_id}"):
            world.review()
    finally:
        holder.kill()
        holder.wait()


# ── records ─────────────────────────────────────────────────────────────────


def test_a_failed_cleanup_keeps_the_worktree_and_the_verdicts_exit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from headless_agents import review_flow

    world.commit_by_hand()
    real = review_flow.remove_worktree

    def failing(*args: object, **kwargs: object) -> str | None:
        return "fatal: simulated"

    monkeypatch.setattr(review_flow, "remove_worktree", failing)
    outcome = world.review()
    assert outcome.exit_code == 0
    report = _report(outcome)
    assert report["cleanup"] == {"status": "failed", "reason": "fatal: simulated"}
    assert (outcome.run_dir / "wt").is_dir()
    assert reviews.load_result(world.state, outcome.run_id).verdict == "approve"
    assert real is not failing


def test_a_prompt_too_large_for_a_reviewer_stops_the_phase_with_the_step_at_2(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.4: known only once the diff is -- the phase is refused, the workflow exits 1."""
    world.commit_by_hand("x = 1\n" * 3000)
    monkeypatch.setattr(
        engine,
        "max_prompt_bytes",
        lambda provider: 1000 if provider in ("agy", "opencode") else None,
    )
    outcome = world.review("panel")
    assert outcome.exit_code == 1
    report = _report(outcome)
    assert report["failure_reason"] == "prompt_too_large"
    (step,) = report["steps"]  # type: ignore[misc]
    assert (step["role"], step["exit_code"]) == ("reviewer-agy", 2)
    assert all(agent.specs == [] for agent in world.agents.values())


def test_a_review_registers_no_lineage_and_leaves_implement_records_alone(world: World) -> None:
    built = world.implement()
    before = lineage.load(world.state, built.run_id)
    outcome = world.review("check", review_run=built.run_id)
    entry = world.registry().resolve(outcome.run_id)
    assert entry.lineage is None and entry.target == {
        "kind": "workflow",
        "name": "check",
        "shape": "review",
    }
    assert lineage.load(world.state, built.run_id) == before
    tip = _git(world.repo, "rev-parse", f"ha/{built.run_id}").strip()
    assert provenance.lookup(world.state, tip) is not None


def test_every_role_of_the_panel_needs_its_isolation_proof(world: World) -> None:
    """§3.8.0: a rail that may load the operator's configuration is refused as an executor,
    whichever slot it fills -- not only the first reviewer's."""
    world.commit_by_hand()
    record_proof(
        world.state, "agy", version="agy 1.0", isolation=False, confinement=True, today="2026-09-26"
    )
    with pytest.raises(UsageError, match="agy agy 1.0 has no passing isolation proof"):
        world.review("panel")
    assert all(agent.specs == [] for agent in world.agents.values())


# ── ha clean of a review (§3.9) ─────────────────────────────────────────────


def _kept_worktree(world: World, monkeypatch: pytest.MonkeyPatch) -> engine.Outcome:
    from headless_agents import review_flow

    world.commit_by_hand()
    monkeypatch.setattr(review_flow, "remove_worktree", lambda *a, **k: "fatal: simulated")
    outcome = world.review()
    monkeypatch.undo()
    assert (outcome.run_dir / "wt").is_dir()
    return outcome


def _clean(world: World, run_id: str) -> int:
    return engine.clean(
        run_id,
        environ={"PATH": os.environ["PATH"], "HOME": str(world.home)},
        home=world.home,
        say=world.said.append,
    )


def test_ha_clean_removes_a_kept_review_worktree_through_git(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _kept_worktree(world, monkeypatch)
    assert _clean(world, outcome.run_id) == 0, world.said
    assert not outcome.run_dir.exists()
    assert str(outcome.run_dir / "wt") not in _git(world.repo, "worktree", "list")
    entry = world.registry().resolve(outcome.run_id)
    assert entry.cleaned_at is not None and entry.status == "approved"
    assert reviews.load_result(world.state, outcome.run_id).verdict == "approve"


def test_ha_clean_of_a_review_runs_no_git_under_a_quarantine(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _kept_worktree(world, monkeypatch)
    quarantine.publish(
        world.state,
        "repository",
        reason="tripwire",
        run_id="x",
        paths=[],
        common_dir=(world.repo / ".git").resolve(),
    )
    assert _clean(world, outcome.run_id) == 1
    assert (outcome.run_dir / "wt").is_dir()
    assert any("quarantine" in line for line in world.said)
    assert world.registry().resolve(outcome.run_id).cleaned_at is None


def test_ha_clean_of_a_finished_review_removes_its_directory(world: World) -> None:
    world.commit_by_hand()
    outcome = world.review()
    assert _clean(world, outcome.run_id) == 0
    assert not outcome.run_dir.exists()
