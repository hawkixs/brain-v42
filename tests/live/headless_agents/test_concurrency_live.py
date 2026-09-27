"""G1, live: two confined codex writes and a read run, in flight together (0.5.2 lot 5).

G1 (spec 2026-09-27-headless-agents-0.5.2-parallel-runs-design.md §2): two codex writes
on distinct lineages and one read-only run proceed concurrently. This is the one live
test that proves it -- not inferred from durations, but from three simultaneous barrier
arrivals plus the lock states themselves, all measured while every run is held and
nothing released yet (spec §3.5).

WHY A BARRIER COSTS NOTHING ON FAILURE. Every codex and git call this test's runs make
is intercepted by a PATH shim (``tests/live/headless_agents/_barrier.py``) before the
provider's first token or before git touches the repository: a barrier that never
arrives, or a precondition that refuses first, spends no provider quota and leaves no
trace on the operator's real state (the world below is entirely throwaway).

SKIP RULES, IN ORDER, BEFORE ANYTHING SPENDS. (1) module-level ``HA_LIVE=1``, like every
other live test; (2) the real codex must be on ``PATH``; (3) the operator's OWN passing
isolation AND confinement proof for the installed codex version must exist already --
borrowed byte-for-byte, never fabricated (the ``test_workflows_live.py`` precedent,
learning 940ab2d8) -- and (4) the ``ha`` under test must report codex's mode as
``"parallel"``. Steps (2)-(4) skip; a misconfigured ``ha`` under test, or a barrier that
turns out to be bypassed, FAILS instead (see the precondition checks below): every
uncertainty after launch is a failure, never a skip or a pass.

DEVIATION FROM THE PLAN, FLAGGED HERE (2026-09-27): the plan names
``headless_agents.prove.MODEL["codex"]`` (lot 4a) as the model source. This branch
merges lot 2 only -- lot 4a is not on it, and Part A tasks 1-2 are scoped to lot 2 by the
plan's own prerequisites table. ``_codex_model()`` below imports ``headless_agents.prove``
opportunistically and falls back to requiring ``HA_LIVE_CODEX_MODEL``, skipping with a
named reason rather than copying a model string that could drift from the real table.

WHAT STAYS THROWAWAY. ``XDG_CONFIG_HOME`` and ``XDG_STATE_HOME`` point at a fresh root
under ``~/.cache/ha-live/concurrency-<uuid8>/`` for every call in this file (never the
operator's real ``~/.config/ha`` or ``~/.local/state/ha`` -- only a copy of one proof file
is ever read from the latter). ``HOME`` stays the operator's real ``$HOME`` throughout,
which is what lets the engine find the installed codex and its real login credentials.
``HA_LIVE_KEEP=1`` keeps the whole root (repository, config, state, run directories) for
inspection instead of removing it at teardown.

OPERATIONAL NOTE FOR THE ORCHESTRATOR (2026-09-27, found while writing this): this
file's own constants (``RUN_TIMEOUT_SECONDS=900``, ``EXIT_SECONDS=960``) exceed this
repository's global ``faulthandler_timeout = 120`` / ``faulthandler_exit_on_timeout =
true`` (pyproject.toml): unlike ``test_workflows_live.py``, which keeps its
``RUN_TIMEOUT_SECONDS`` at 100 specifically to stay under that bound, G1 cannot -- a
confined codex write under a real model can legitimately take longer than 120 s. Run
this file with ``-p no:faulthandler`` (or ``-o faulthandler_timeout=0``), or the whole
pytest session will be killed by the global bound partway through a passing run.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from headless_agents import lineage as lineages
from headless_agents.config_paths import state_dir
from headless_agents.engine import executable_for
from headless_agents.proofs import read_proof
from headless_agents.registry import probe

from ._barrier import Barrier, abort, install_shim, lock_state
from .test_workflows_live import _borrowed_isolation_proof
from .test_workspace_live import LIVE_ROOT, _operator_environment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HA_LIVE") != "1", reason="live: spends real quota, set HA_LIVE=1"
    ),
]

REAL_HOME = Path.home()

CONTENTION_FILES: Final = 4000
GIT_ARRIVAL_SECONDS: Final = 60.0
PROVIDER_ARRIVAL_SECONDS: Final = 120.0
#: Longer than both arrival deadlines plus every in-flight assertion, so a shim always
#: releases or aborts before it would give up on its own.
HOLD_SECONDS: Final = 300.0
RUN_TIMEOUT_SECONDS: Final = 900.0
EXIT_SECONDS: Final = RUN_TIMEOUT_SECONDS + 60.0

READ_NONCE: Final = "ha-live-g1-read-nonce"
NONCE_A: Final = "ha-live-g1-lane-a-nonce"
NONCE_B: Final = "ha-live-g1-lane-b-nonce"
READ_PROMPT: Final = (
    "Read SEED.txt at the repository root and reply with its exact content and nothing else."
)


def _write_task(lane: str, nonce: str) -> str:
    return (
        f"Create the file lane-{lane}.txt at the repository root containing exactly the "
        f"line {nonce}. Make no other change and run no other command."
    )


def _codex_model() -> str:
    """The model every codex run in this test uses.

    The plan's authoritative source is ``headless_agents.prove.MODEL["codex"]`` (lot
    4a); this branch merges lot 2 only (see the module docstring's DEVIATION note), so
    that module may not exist. ``HA_LIVE_CODEX_MODEL`` always overrides either way, and
    nothing here copies a literal model string that could drift from the real table.
    """
    override = os.environ.get("HA_LIVE_CODEX_MODEL")
    if override:
        return override
    try:
        from headless_agents.prove import MODEL  # noqa: PLC0415
    except ModuleNotFoundError:
        pytest.skip(
            "codex model unknown: headless_agents.prove is not on this branch yet "
            "(lot 4a not merged) and HA_LIVE_CODEX_MODEL is not set"
        )
    model = MODEL.get("codex")
    if not model:
        pytest.skip("codex model unknown: headless_agents.prove.MODEL has no 'codex' entry")
    return model


def _resolve_ha() -> Path:
    """The ``ha`` under test: ``HA_LIVE_HA`` if set, else the checkout's own venv.

    A misconfigured ``ha`` here FAILS (never skips): this is the one precondition the
    plan calls a misconfiguration, not an absent live dependency.
    """
    override = os.environ.get("HA_LIVE_HA")
    ha = Path(override) if override else Path(sys.executable).with_name("ha")
    if not ha.is_file() or not os.access(ha, os.X_OK):
        pytest.fail(f"HA_LIVE_HA={ha}: not an executable file")
    result = subprocess.run([str(ha), "--version"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.fail(f"{ha} --version failed (exit {result.returncode}): {result.stderr}")
    return ha


@dataclass
class _World:
    root: Path
    repo: Path
    state: Path
    shims: Path
    barrier_root: Path
    runs: Path
    environ: dict[str, str]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=120
    )


def _seed_repository(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "ha-live")
    _git(repo, "config", "user.email", "ha-live@example.invalid")
    (repo / "SEED.txt").write_text(f"{READ_NONCE}\n", encoding="utf-8")
    for index in range(CONTENTION_FILES):
        bucket = repo / "bulk" / str(index // 100)
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / f"{index}.txt").write_text(f"file {index}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def _build_world(root: Path) -> _World:
    repo = root / "repo"
    config = root / "config" / "ha"
    state = root / "state" / "ha"
    shims = root / "shims"
    barrier_root = root / "barrier"
    runs = root / "runs"
    for directory in (config, state / "proofs", shims, barrier_root, runs):
        directory.mkdir(parents=True, exist_ok=True)
    _seed_repository(repo)
    operator = _operator_environment()
    environ = {
        **operator,
        "HOME": str(REAL_HOME),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "PATH": f"{shims}:{operator.get('PATH', '')}",
    }
    return _World(
        root=root,
        repo=repo,
        state=state,
        shims=shims,
        barrier_root=barrier_root,
        runs=runs,
        environ=environ,
    )


def _borrow_codex_proof(dest_state: Path) -> str | None:
    """Copy the operator's OWN isolation+confinement proof for codex into ``dest_state``.

    Reuses ``_borrowed_isolation_proof`` (the ``test_workflows_live.py`` precedent,
    never fabricating a proof) and additionally requires a passing confinement record
    for the same version: G1 needs confined writes, an unconfined one holds the global
    lock exclusively and the whole premise of this test collapses.
    """
    executable = executable_for("codex", REAL_HOME)
    reason = _borrowed_isolation_proof(dest_state, "codex", executable=executable)
    if reason is not None:
        return reason
    real_state = state_dir(os.environ, home=REAL_HOME)
    version = probe("codex", executable=executable).version
    record = read_proof(real_state, "codex")
    if (
        record is None
        or record.version != version
        or record.confinement is None
        or not record.confinement.passed
    ):
        status = "missing" if record is None or record.confinement is None else "failed"
        return (
            f"codex {version or '(version unknown)'}: no PASSING confinement proof recorded "
            f"({status}) in {real_state}; run ha prove codex (or the live proof tests) first"
        )
    return None


def _run_id_from_dir(run_dir: Path, *, timeout: float = 60.0) -> str:
    path = run_dir / "run.json"
    deadline = time.monotonic() + timeout
    while not path.is_file():
        assert time.monotonic() < deadline, f"{path}: run.json never appeared within {timeout}s"
        time.sleep(0.05)
    document = json.loads(path.read_text(encoding="utf-8"))
    run_id = document.get("run_id")
    assert isinstance(run_id, str) and run_id, f"{path}: malformed run_id"
    return run_id


def _dash_c_value(argv: list[object]) -> str | None:
    if "-C" in argv:
        index = argv.index("-C")
        if index + 1 < len(argv):
            return str(argv[index + 1])
    return None


@dataclass
class _Launched:
    label: str
    process: subprocess.Popen[bytes]
    run_dir: Path
    stdout_path: Path
    stderr_path: Path


def _launch(ha: Path, world: _World, *, label: str, run_dir: Path, argv: list[str]) -> _Launched:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = world.runs / f"{label}.stdout.log"
    stderr_path = world.runs / f"{label}.stderr.log"
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            [str(ha), *argv],
            cwd=world.repo,
            env=world.environ,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    return _Launched(
        label=label,
        process=process,
        run_dir=run_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _fail_on_early_exit(launched: list[_Launched], *, at: str) -> None:
    for item in launched:
        code = item.process.poll()
        if code is not None:
            stderr = item.stderr_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(
                f"{item.label} exited {code} before reaching the {at} barrier: {stderr[-2000:]}"
            )


def _wait_for_arrivals(
    barrier: Barrier, count: int, *, timeout: float, launched: list[_Launched]
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        arrivals = barrier.arrivals()
        if len(arrivals) > count:
            pytest.fail(
                f"the {barrier.name} barrier saw {len(arrivals)} arrivals, expected exactly {count}"
            )
        if len(arrivals) == count:
            return
        _fail_on_early_exit(launched, at=barrier.name)
        assert time.monotonic() < deadline, (
            f"the {barrier.name} barrier: only {len(arrivals)}/{count} arrived within {timeout}s"
        )
        time.sleep(0.1)


def _wait_for_finished(barrier: Barrier, count: int, *, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        finished = barrier.finished()
        if len(finished) >= count:
            return finished
        assert time.monotonic() < deadline, (
            f"the {barrier.name} barrier: only {len(finished)}/{count} finished within {timeout}s"
        )
        time.sleep(0.05)


def _run_ha_json(ha: Path, world: _World, *args: str, timeout: float = 30.0) -> Any:
    result = subprocess.run(
        [str(ha), *args],
        cwd=world.repo,
        env=world.environ,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"{ha} {' '.join(args)} failed: {result.stderr}"
    return json.loads(result.stdout)


def _stop(launched: list[_Launched]) -> None:
    for item in launched:
        if item.process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(item.process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    for item in launched:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            item.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(item.process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                item.process.wait(timeout=5)


def test_two_confined_codex_writes_and_a_read_are_in_flight_together() -> None:
    ha = _resolve_ha()

    real_codex = shutil.which("codex")
    if real_codex is None:
        pytest.skip("codex unavailable here: not found on PATH")

    model = _codex_model()

    root = LIVE_ROOT / f"concurrency-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    launched: list[_Launched] = []
    try:
        world = _build_world(root)

        reason = _borrow_codex_proof(world.state)
        if reason is not None:
            pytest.skip(reason)

        real_git = shutil.which("git")
        assert real_git is not None, "git must be on PATH for a live test host"
        codex_barrier = Barrier(root=world.barrier_root, name="codex")
        git_barrier = Barrier(root=world.barrier_root, name="git")
        install_shim(
            world.shims,
            codex_barrier,
            command="codex",
            real=Path(real_codex),
            match="codex-exec",
            hold_seconds=HOLD_SECONDS,
        )
        install_shim(
            world.shims,
            git_barrier,
            command="git",
            real=Path(real_git),
            match="git-worktree-add",
            hold_seconds=HOLD_SECONDS,
        )

        rows = _run_ha_json(ha, world, "providers", "--json")
        codex_row = next(row for row in rows if row["name"] == "codex")
        if "mode" not in codex_row:
            pytest.fail(
                f"{ha} providers --json has no 'mode' key for codex: this ha predates lot 2"
            )
        expected_detail = (world.shims / "codex").resolve()
        if codex_row["detail"] is None or Path(codex_row["detail"]).resolve() != expected_detail:
            pytest.fail(
                f"codex detail is {codex_row['detail']!r}, expected the shim {expected_detail}: "
                "the barrier would be bypassed"
            )
        real_codex_version = probe("codex", executable=real_codex).version
        if codex_row["version"] != real_codex_version:
            pytest.fail(
                f"codex version {codex_row['version']!r} != the real codex's "
                f"{real_codex_version!r}: the shim altered the probe"
            )
        if codex_row["mode"] != "parallel":
            pytest.skip(
                f"codex mode is {codex_row['mode']!r}, not 'parallel': isolation "
                f"{codex_row['isolation']}, confinement {codex_row['confinement']}, "
                f"reprove {codex_row['reprove']!r}"
            )

        which_git = shutil.which("git", path=world.environ["PATH"])
        expected_git = (world.shims / "git").resolve()
        if which_git is None or Path(which_git).resolve() != expected_git:
            pytest.fail(f"git resolves to {which_git!r}, not the git shim {expected_git}: refusing")

        common = [
            "-m",
            model,
            "--effort",
            "low",
            "--context",
            "none",
            "--timeout",
            str(RUN_TIMEOUT_SECONDS),
            "--json",
        ]
        read_dir, write_a_dir, write_b_dir = world.runs / "read", world.runs / "a", world.runs / "b"
        read = _launch(
            ha,
            world,
            label="read",
            run_dir=read_dir,
            argv=["run", "codex", READ_PROMPT, *common, "--run-dir", str(read_dir)],
        )
        write_a = _launch(
            ha,
            world,
            label="write-a",
            run_dir=write_a_dir,
            argv=[
                "run",
                "codex",
                "--write",
                _write_task("a", NONCE_A),
                *common,
                "--run-dir",
                str(write_a_dir),
            ],
        )
        write_b = _launch(
            ha,
            world,
            label="write-b",
            run_dir=write_b_dir,
            argv=[
                "run",
                "codex",
                "--write",
                _write_task("b", NONCE_B),
                *common,
                "--run-dir",
                str(write_b_dir),
            ],
        )
        launched = [read, write_a, write_b]

        # 1-2: both `git worktree add` calls arrive, overlap, and are released.
        _wait_for_arrivals(git_barrier, 2, timeout=GIT_ARRIVAL_SECONDS, launched=launched)
        git_barrier.release()
        git_finished = _wait_for_finished(git_barrier, 2, timeout=GIT_ARRIVAL_SECONDS)
        assert all(entry["returncode"] == 0 for entry in git_finished), git_finished
        starts = [entry["started"] for entry in git_finished]
        ends = [entry["ended"] for entry in git_finished]
        assert max(starts) < min(ends), (
            f"the two 'git worktree add' calls did not overlap: starts={starts} ends={ends}; "
            "raise CONTENTION_FILES in a follow-up"
        )

        # 3: all three codex steps arrive, at the right worktrees/repository.
        _wait_for_arrivals(codex_barrier, 3, timeout=PROVIDER_ARRIVAL_SECONDS, launched=launched)
        arrivals = codex_barrier.arrivals()
        observed = {
            Path(value).resolve()
            for value in (_dash_c_value(a["argv"]) for a in arrivals)
            if value is not None
        }
        expected_dirs = {
            (write_a_dir / "wt").resolve(),
            (write_b_dir / "wt").resolve(),
            world.repo.resolve(),
        }
        assert observed == expected_dirs, f"-C values {observed} != {expected_dirs}"

        # 4: in flight together, nothing released yet.
        write_a_id = _run_id_from_dir(write_a_dir)
        write_b_id = _run_id_from_dir(write_b_dir)
        assert lock_state(world.state / "unconfined.lock") == "shared"
        assert lock_state(lineages.lineage_lock(world.state, write_a_id)) == "exclusive"
        assert lock_state(lineages.lineage_lock(world.state, write_b_id)) == "exclusive"
        assert lock_state(lineages.registry_lock(world.state)) == "free"
        assert lock_state(world.state / "admission-gate.lock") in ("free", "absent")
        running = _run_ha_json(ha, world, "runs", "--json")
        running_ids = {row["run_id"]: row["status"] for row in running}
        for run_id in (write_a_id, write_b_id):
            assert running_ids.get(run_id) == "running", running_ids
        worktree_list = subprocess.run(
            ["git", "-C", str(world.repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        assert f"branch refs/heads/ha/{write_a_id}" in worktree_list, worktree_list
        assert f"branch refs/heads/ha/{write_b_id}" in worktree_list, worktree_list

        # 5: release, and every run completes.
        codex_barrier.release()
        for item in launched:
            code = item.process.wait(timeout=EXIT_SECONDS)
            assert code == 0, (
                f"{item.label} exited {code}: "
                f"{item.stderr_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
            )

        read_report = json.loads((read_dir / "run.json").read_text(encoding="utf-8"))
        assert read_report["status"] == "answered", read_report
        assert READ_NONCE in (read_report.get("text") or ""), read_report.get("text")

        reports = {
            "a": json.loads((write_a_dir / "run.json").read_text(encoding="utf-8")),
            "b": json.loads((write_b_dir / "run.json").read_text(encoding="utf-8")),
        }
        for label, report in reports.items():
            assert report["status"] == "committed", (label, report)
        assert reports["a"]["lineage"] != reports["b"]["lineage"]

        for label, nonce in (("a", NONCE_A), ("b", NONCE_B)):
            branch = reports[label]["branch"]
            named = subprocess.run(
                ["git", "-C", str(world.repo), "show", "--name-only", "--format=", branch],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
            assert named.split() == [f"lane-{label}.txt"], named
            content = subprocess.run(
                ["git", "-C", str(world.repo), "show", f"{branch}:lane-{label}.txt"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
            assert content.strip() == nonce, content

        for item in launched:
            stderr = item.stderr_path.read_text(encoding="utf-8", errors="replace")
            assert "unconfined write is running" not in stderr, (item.label, stderr)

        print(
            f"G1: ha={ha} {subprocess.run([str(ha), '--version'], capture_output=True, text=True).stdout.strip()}, "
            f"codex={real_codex_version}, read={read_report['run_id']}, "
            f"write_a={write_a_id} write_b={write_b_id}, "
            f"worktree_add_intervals_ms={[(e - s) / 1e6 for s, e in zip(starts, ends, strict=True)]}, "
            "locks(unconfined=shared, registry=free, admission_gate=free/absent, "
            "both lineages=exclusive)"
        )
    finally:
        abort(root / "barrier")
        _stop(launched)
        if os.environ.get("HA_LIVE_KEEP") != "1":
            for item in launched:
                if item.label != "read":
                    with suppress(Exception):
                        run_id = _run_id_from_dir(item.run_dir, timeout=1.0)
                        subprocess.run(
                            [str(ha), "clean", run_id],
                            cwd=world.repo,
                            env=world.environ,
                            capture_output=True,
                            text=True,
                            timeout=30,
                            check=False,
                        )
            shutil.rmtree(root, ignore_errors=True)
