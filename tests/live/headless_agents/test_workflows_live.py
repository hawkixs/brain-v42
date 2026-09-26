"""Replay the 0.5.0 ``ha`` workflows against the real CLIs (spec 0.5.0 §5, lot 5).

WHY IT IS OPT-IN. Every test here spends real provider quota and needs the
operator's logged-in CLIs: it carries the ``live`` marker, excluded by
``addopts``, and skips unless ``HA_LIVE=1``. Run it deliberately:

    HA_LIVE=1 .venv/bin/pytest -m live tests/live/headless_agents/test_workflows_live.py -v -rA

WHAT THIS FILE COVERS, AND HOW IT STAYS CHEAP. Three behaviours of §3-§3.8.4,
reusing a single module-scoped implement run (:func:`implement_run`) across
the write-flow and both review tests, so the whole file spends at most three
provider runs per pass:

1. a one-step read-only ``ha run <provider> ... --json`` (an ``openai-compat``
   preset: no CLI rail, so no isolation proof to borrow, no role or workflow
   declaration needed);
2. an ``implement`` write run on a throwaway repository: the registry, the
   commit's provenance, ``ha show --json`` and ``ha clean``;
3. a ``review`` run under the vendor rule -- a passing panel of another
   vendor, and a same-vendor reviewer refused before any provider runs.

ISOLATION PROOFS ARE BORROWED, NEVER FABRICATED. A CLI rail needs a passing
isolation proof for its installed version before the engine lets it execute
(§3.8.0); a proof file is measured by ``test_proofs_live.py``, never edited or
invented (learning 940ab2d8). So this file's throwaway state directory gets a
byte-for-byte copy of the operator's OWN, already-recorded proof for the rail
version installed here (:func:`_borrowed_isolation_proof`); a rail without one
is skipped, never granted one on the spot.

WHAT STAYS UNTOUCHED. ``XDG_CONFIG_HOME`` and ``XDG_STATE_HOME`` are pointed
at a throwaway root for every call in this file: no run here reads or writes
the operator's real ``~/.config/ha`` or ``~/.local/state/ha`` (only READS a
copy of one proof file out of the latter). Every run also takes an explicit
``--run-dir`` under that same root, so nothing lands in the operator's real
``~/.cache/ha/runs`` either. ``home=`` stays the operator's real ``$HOME``
throughout: that is what lets the engine find the installed CLI rails
(``executable_for``) and their real login credentials, exactly as a genuine
``ha`` invocation would.

HA_LIVE_KEEP=1 keeps every throwaway root (repository, config, state, run
directories) for inspection instead of removing it at teardown.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from headless_agents import cli, provenance
from headless_agents import lineage as lineages
from headless_agents.config_paths import state_dir
from headless_agents.proofs import proof_path, read_proof
from headless_agents.registry import probe
from headless_agents.runs import FINAL_STATUSES, Registry

from .test_openai_compat_live import DEFAULT_MODELS, KEY_ENV
from .test_workspace_live import EXECUTABLE, LIVE_ROOT, _operator_environment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HA_LIVE") != "1", reason="live: spends real quota, set HA_LIVE=1"
    ),
]

REAL_HOME = Path.home()

# The write role's rail and its independent reviewer: both have a real, passing
# isolation proof recorded by test_proofs_live.py on this machine (measured
# 2026-09-25). opencode also has a passing confinement proof, but this file
# does not borrow it -- an unclassified write is still a fully functional
# (conservative, "unconfined") write, and this suite does not test the
# confined-vs-unconfined classification itself (spec 3.8.0).
IMPLEMENTER_RAIL = "opencode"
IMPLEMENTER_MODEL = "opencode-go/glm-5.3-flash"
REVIEWER_RAIL = "codex"
REVIEWER_MODEL = "gpt-6-luna"
RUN_TIMEOUT_SECONDS = 100.0

# context = "none" on every role: none of this file's assertions need the
# context bundle, and "global" or "full" would otherwise read the OPERATOR'S
# REAL ~/.claude/CLAUDE.md (home= stays the real $HOME, see the module
# docstring) and send it to a real third-party provider -- a live test must
# not do that just to exercise the write and review flows.
_ROLES = f"""\
[implementer]
provider = "{IMPLEMENTER_RAIL}"
model = "{IMPLEMENTER_MODEL}"
write = true
context = "none"
timeout = {RUN_TIMEOUT_SECONDS:g}

[reviewer-other-vendor]
provider = "{REVIEWER_RAIL}"
model = "{REVIEWER_MODEL}"
effort = "low"
context = "none"
timeout = {RUN_TIMEOUT_SECONDS:g}

[reviewer-same-vendor]
provider = "{IMPLEMENTER_RAIL}"
model = "{IMPLEMENTER_MODEL}"
context = "none"
timeout = {RUN_TIMEOUT_SECONDS:g}
"""

_WORKFLOWS = """\
[build]
shape     = "implement"
implement = "implementer"

[check]
shape  = "review"
review = "reviewer-other-vendor"

[check-same-vendor]
shape  = "review"
review = "reviewer-same-vendor"
"""

_IMPLEMENT_TASK = (
    "Append exactly one line to the end of NOTES.md containing exactly the text "
    "ha-live-lot5-ok and nothing else. Make no other change."
)


def _borrowed_isolation_proof(dest_state: Path, rail: str, *, executable: str | None) -> str | None:
    """Copy the operator's OWN, already-recorded isolation proof for ``rail``'s
    installed version into ``dest_state`` (a throwaway ``<state>/proofs/<rail>.json``).

    Never invented: the engine's gate (§3.8.0) must read a genuine measurement
    made by ``test_proofs_live.py``, or refuse to run the rail -- a proof file
    is never edited or fabricated (learning 940ab2d8). Returns the reason to
    skip when the rail is unavailable, or no PASSING proof covers the version
    installed right now.
    """
    probed = probe(rail, executable=executable)
    if not probed.available:
        return f"{rail} unavailable here: {probed.detail}"
    real_state = state_dir(os.environ, home=REAL_HOME)
    record = read_proof(real_state, rail)
    if record is None or record.version != probed.version:
        return (
            f"{rail} {probed.version or '(version unknown)'}: no isolation proof recorded for "
            f"this version in {real_state}; run test_proofs_live.py first"
        )
    if record.isolation is None or not record.isolation.passed:
        return f"{rail} {probed.version}: no PASSING isolation proof recorded"
    source = proof_path(real_state, rail)
    target = proof_path(dest_state, rail)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@dataclass
class World:
    """One throwaway ``ha`` world: a real ``$HOME`` for rails and credentials, an
    isolated config/state pair for everything else (never the operator's real
    ones), and a repository to work in."""

    root: Path
    repo: Path
    environ: dict[str, str]

    def run(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ=self.environ,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            cwd=self.repo,
            home=REAL_HOME,
        )
        return code, out.getvalue(), err.getvalue()

    @property
    def state(self) -> Path:
        return state_dir(self.environ, home=REAL_HOME)


def _new_world(root: Path, *, roles: str = "", workflows: str = "") -> World:
    config = root / "config" / "ha"
    config.mkdir(parents=True)
    if roles:
        (config / "roles.toml").write_text(roles, encoding="utf-8")
    if workflows:
        (config / "workflows.toml").write_text(workflows, encoding="utf-8")
    (root / "state" / "ha").mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    environ = {
        **_operator_environment(),
        "HOME": str(REAL_HOME),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    return World(root=root, repo=repo, environ=environ)


# ── 1. a one-step read-only run, via the CLI (§3.9, §3.10) ──────────────────


def test_a_one_step_provider_run_writes_the_run_json_and_steps_layout(tmp_path: Path) -> None:
    """``ha run <provider> ... --json``: schema 1, a terminal status, one step with a
    non-empty provider and model, and the ``run.json`` / ``steps/<n>/result.json``
    layout of §3.10 on disk.

    An ``openai-compat`` preset needs no CLI rail (so no isolation proof to
    borrow): the cheapest possible exercise of the 0.5.0 engine's one-step path.

    NOTE ON THE TASK'S "kind": "run": ``run.json``'s pinned key set
    (``headless_agents.report.RUN_KEYS``, read in this worktree) carries no
    top-level ``kind`` field -- only ``target.kind`` (``"provider"``, ``"role"``
    or ``"workflow"``). This asserts ``target.kind == "provider"``, the real
    field that exists today, rather than inventing the one the task named.
    """
    preset = "mistral"
    if not probe(preset).available:
        pytest.skip(f"{KEY_ENV[preset]} is not set")
    world = _new_world(tmp_path)
    run_dir = tmp_path / "run"
    code, out, err = world.run(
        "run",
        preset,
        "-m",
        DEFAULT_MODELS[preset],
        "Reply with exactly the word OK and nothing else.",
        "--run-dir",
        str(run_dir),
        "--timeout",
        "60",
        # never send the operator's real ~/.claude/CLAUDE.md to a real
        # third-party API just to exercise the one-step run path.
        "--context",
        "none",
        "--json",
    )
    assert code == 0, err
    report = json.loads(out)
    assert report["schema"] == 1
    assert report["target"] == {"kind": "provider", "name": preset}
    assert report["status"] in FINAL_STATUSES
    assert report["exit_code"] == 0
    (step,) = report["steps"]
    assert step["provider"] == preset and step["model"]
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "steps" / f"01-run-{preset}" / "result.json").is_file()


# ── 2 & 3: one implement run, shared by the write-flow and the review tests ──


@dataclass
class ImplementRun:
    world: World
    run_id: str
    report: dict[str, object]
    run_dir: Path


@pytest.fixture(scope="module")
def implement_run() -> Iterator[ImplementRun]:
    """One real ``implement`` run on a throwaway repository (§3.6), reused -- never
    re-run -- by the write-flow test and both review tests below."""
    root = LIVE_ROOT / f"workflows-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        world = _new_world(root, roles=_ROLES, workflows=_WORKFLOWS)
        state = world.state
        for rail, executable in (
            (IMPLEMENTER_RAIL, EXECUTABLE[IMPLEMENTER_RAIL]),
            (REVIEWER_RAIL, EXECUTABLE[REVIEWER_RAIL]),
        ):
            reason = _borrowed_isolation_proof(state, rail, executable=executable)
            if reason is not None:
                pytest.skip(reason)
        (world.repo / "NOTES.md").write_text("seed\n", encoding="utf-8")
        _git(world.repo, "add", "NOTES.md")
        _git(
            world.repo,
            "-c",
            "user.name=ha-live",
            "-c",
            "user.email=ha-live@example.invalid",
            "commit",
            "-q",
            "-m",
            "seed",
        )
        run_dir = root / "run-build"
        code, out, err = world.run(
            "run", "build", _IMPLEMENT_TASK, "--run-dir", str(run_dir), "--json"
        )
        assert code == 0, f"the implement run failed with exit {code}: {err}"
        report = json.loads(out)
        yield ImplementRun(world=world, run_id=report["run_id"], report=report, run_dir=run_dir)
    finally:
        if os.environ.get("HA_LIVE_KEEP") != "1":
            shutil.rmtree(root, ignore_errors=True)


# ── 2. an implement write run: registry, provenance, show, clean (§3.6, §3.8) ─


def test_an_implement_run_is_recorded_shown_and_cleaned(implement_run: ImplementRun) -> None:
    report = implement_run.report
    assert report["status"] == "committed", report
    commits = report["commits"]
    assert isinstance(commits, list) and commits, "no commit was recorded for the write"
    for commit in commits:
        assert commit["made_by"] in ("engine", "agent"), commit

    state = implement_run.world.state
    engine_commits = [c for c in commits if c["made_by"] == "engine"]
    assert engine_commits, "the engine never committed the agent's change"
    record = provenance.lookup(state, engine_commits[0]["sha"])
    assert record is not None
    assert record["run_id"] == implement_run.run_id
    assert record["made_by"] == "engine"
    assert IMPLEMENTER_RAIL in record["providers"]

    lineage = lineages.load(state, report["lineage"])
    assert lineage.members[implement_run.run_id] == "committed"

    code, out, err = implement_run.world.run("show", implement_run.run_id, "--json")
    assert code == 0, err
    shown = json.loads(out)
    assert shown["run_id"] == implement_run.run_id
    assert shown["commits"] == report["commits"]
    assert shown["status"] == "committed"

    registry = Registry(state, runs_root=state.parent / "unused-runs-root")
    run_dir = implement_run.run_dir
    assert run_dir.is_dir()
    code, _, err = implement_run.world.run("clean", implement_run.run_id)
    assert code == 0, err
    assert not run_dir.exists()
    entry = registry.resolve(implement_run.run_id)
    assert entry.cleaned_at is not None
    # ha clean removes the run directory and marks cleaned_at; it never touches
    # the lineage state, whose branch the review tests below still read.
    assert lineages.load(state, report["lineage"]).members[implement_run.run_id] == "committed"


# ── 3. a review under the vendor rule (§3.5, §3.8.4) ─────────────────────────


def test_a_review_of_another_vendor_reads_a_verdict_and_a_passing_vendor_check(
    implement_run: ImplementRun,
) -> None:
    world = implement_run.world
    run_dir = world.root / "run-check"
    code, out, err = world.run(
        "run", "check", "--run", implement_run.run_id, "--run-dir", str(run_dir), "--json"
    )
    assert code in (0, 6), f"unexpected exit {code}: {err}"
    report = json.loads(out)
    assert report["verdict"] in ("approve", "changes")
    check = report["vendor_check"]
    assert check is not None
    assert IMPLEMENTER_RAIL in check["authors"]
    reviewer_providers = check["reviewers"]["reviewer-other-vendor"]
    assert reviewer_providers == [REVIEWER_RAIL]
    # the vendor rule itself: the reviewer's vendor and the code's authors are disjoint.
    assert not (set(reviewer_providers) & set(check["authors"]))


def test_a_same_vendor_reviewer_is_refused_before_any_provider_runs(
    implement_run: ImplementRun,
) -> None:
    """§3.8.4 step 5: independence is checked before ``change.patch`` is written and
    before the review's worktree is created (:mod:`headless_agents.review_flow`), so a
    refusal here spends no provider quota at all -- unlike a fake-provider unit test,
    this is shown by elapsed time and by the absence of any step or worktree, never by
    a recorded call count."""
    world = implement_run.world
    run_dir = world.root / "run-check-same-vendor"
    started = time.monotonic()
    code, out, err = world.run(
        "run", "check-same-vendor", "--run", implement_run.run_id, "--run-dir", str(run_dir)
    )
    elapsed = time.monotonic() - started
    assert code == 2, f"unexpected exit {code}: {err}"
    assert out == ""
    assert "must not share a vendor" in err
    assert IMPLEMENTER_RAIL in err
    # No provider process was spent: no worktree, no step directory, no patch --
    # the refusal happens strictly before §3.5 step 1 ever runs (review_flow.prepare).
    assert not (run_dir / "steps").exists()
    assert not (run_dir / "wt").exists()
    assert not (run_dir / "change.patch").exists()
    # A real provider call would take many seconds; a pure state/git refusal does not.
    assert elapsed < 20.0, f"took {elapsed:.1f}s: a provider may have run"
