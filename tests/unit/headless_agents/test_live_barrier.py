"""Unit tests for the PATH-shim barrier and lock probe (0.5.2 lot 5, Task 1).

TDD: written before tests/live/headless_agents/_barrier.py exists -- every
case here must fail on collection first (ModuleNotFoundError), then on its
own assertion once a stub exists. Real subprocesses throughout, each bounded
well under a second once released.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.live.headless_agents._barrier import (
    ABORT_EXIT_CODE,
    Barrier,
    abort,
    install_shim,
    lock_state,
)

_FAKE_REAL_HEADER = "#!{python}\nCALLS = {calls!r}\nEXIT_CODE_PATH = {exit_code_path!r}\n"

_FAKE_REAL_BODY = """
import json
import os
import sys

payload = {
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "probe": os.environ.get("BARRIER_PROBE"),
}
with open(CALLS, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload) + "\\n")
try:
    with open(EXIT_CODE_PATH, encoding="utf-8") as handle:
        code = int(handle.read().strip())
except FileNotFoundError:
    code = 0
sys.exit(code)
"""


def _write_fake_real(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A 'real' binary: appends one call record per invocation to ``calls``
    (argv, stdin, the BARRIER_PROBE env var) and exits with the number in
    ``exit_code_path`` (default 0)."""
    real = tmp_path / "real"
    calls = tmp_path / "calls.jsonl"
    exit_code_path = tmp_path / "exit_code"
    header = _FAKE_REAL_HEADER.format(
        python=sys.executable, calls=str(calls), exit_code_path=str(exit_code_path)
    )
    real.write_text(header + _FAKE_REAL_BODY, encoding="utf-8")
    real.chmod(0o755)
    return real, calls, exit_code_path


def _start(
    argv: list[str], *, cwd: Path, env: dict[str, str], stdin_bytes: bytes = b""
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(stdin_bytes)
    process.stdin.close()
    return process


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


def test_a_non_matching_call_runs_the_real_binary_at_once_and_writes_nothing(
    tmp_path: Path,
) -> None:
    real, calls, _ = _write_fake_real(tmp_path)
    shims = tmp_path / "shims"
    root = tmp_path / "barrier"
    codex_barrier = Barrier(root=root, name="codex")
    codex_shim = install_shim(
        shims, codex_barrier, command="codex", real=real, match="codex-exec", hold_seconds=5.0
    )
    result = subprocess.run(
        [str(codex_shim), "--version"], capture_output=True, text=True, input="", timeout=5
    )
    assert result.returncode == 0
    assert codex_barrier.arrivals() == []
    assert json.loads(calls.read_text(encoding="utf-8").splitlines()[0])["argv"] == ["--version"]

    git_barrier = Barrier(root=root, name="git")
    git_shim = install_shim(
        shims, git_barrier, command="git", real=real, match="git-worktree-add", hold_seconds=5.0
    )
    result = subprocess.run(
        [str(git_shim), "status"], capture_output=True, text=True, input="", timeout=5
    )
    assert result.returncode == 0
    assert git_barrier.arrivals() == []
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["argv"] == ["status"]


def test_a_matching_call_waits_for_release_then_runs_the_real_binary(tmp_path: Path) -> None:
    real, calls, exit_code_path = _write_fake_real(tmp_path)
    exit_code_path.write_text("7")
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="codex")
    shim = install_shim(
        shims, barrier, command="codex", real=real, match="codex-exec", hold_seconds=5.0
    )

    process = _start([str(shim), "exec", "-C", "/x", "-"], cwd=tmp_path, env=dict(os.environ))
    try:
        _wait_until(lambda: len(barrier.arrivals()) == 1)
        assert not calls.exists(), "the real binary must not have run yet"
        barrier.release()
        returncode = process.wait(timeout=5)
    finally:
        _stop(process)
    assert returncode == 7
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["argv"] == ["exec", "-C", "/x", "-"]


def test_stdin_and_the_environment_reach_the_real_binary_untouched(tmp_path: Path) -> None:
    real, calls, _ = _write_fake_real(tmp_path)
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="codex")
    shim = install_shim(
        shims, barrier, command="codex", real=real, match="codex-exec", hold_seconds=5.0
    )

    env = {**os.environ, "BARRIER_PROBE": "1"}
    process = _start([str(shim), "exec"], cwd=tmp_path, env=env, stdin_bytes=b"prompt-bytes")
    try:
        _wait_until(lambda: len(barrier.arrivals()) == 1)
        barrier.release()
        assert process.wait(timeout=5) == 0
    finally:
        _stop(process)
    (line,) = calls.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["stdin"] == "prompt-bytes"
    assert record["probe"] == "1"


def test_abort_exits_97_and_never_runs_the_real_binary(tmp_path: Path) -> None:
    real, calls, _ = _write_fake_real(tmp_path)
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="codex")
    shim = install_shim(
        shims, barrier, command="codex", real=real, match="codex-exec", hold_seconds=30.0
    )

    process = _start([str(shim), "exec"], cwd=tmp_path, env=dict(os.environ))
    try:
        _wait_until(lambda: len(barrier.arrivals()) == 1)
        abort(root)
        returncode = process.wait(timeout=5)
    finally:
        _stop(process)
    assert returncode == ABORT_EXIT_CODE
    (failure,) = barrier.failures()
    assert failure["reason"] == "aborted"
    assert not calls.exists()


def test_an_expired_hold_exits_97_and_never_runs_the_real_binary(tmp_path: Path) -> None:
    real, calls, _ = _write_fake_real(tmp_path)
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="codex")
    shim = install_shim(
        shims, barrier, command="codex", real=real, match="codex-exec", hold_seconds=0.2
    )

    process = _start([str(shim), "exec"], cwd=tmp_path, env=dict(os.environ))
    try:
        returncode = process.wait(timeout=5)
    finally:
        _stop(process)
    assert returncode == ABORT_EXIT_CODE
    (failure,) = barrier.failures()
    assert failure["reason"] == "hold expired"
    assert not calls.exists()


def test_git_worktree_add_records_its_interval_and_exit_code(tmp_path: Path) -> None:
    real, calls, exit_code_path = _write_fake_real(tmp_path)
    exit_code_path.write_text("0")
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="git")
    shim = install_shim(
        shims, barrier, command="git", real=real, match="git-worktree-add", hold_seconds=5.0
    )

    process = _start(
        [str(shim), "-C", "r", "worktree", "add", "-q", "-b", "x", "wt", "HEAD"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    try:
        _wait_until(lambda: len(barrier.arrivals()) == 1)
        assert not calls.exists(), "the real binary must not have run yet"
        barrier.release()
        returncode = process.wait(timeout=5)
    finally:
        _stop(process)
    assert returncode == 0
    (finished,) = barrier.finished()
    assert finished["started"] <= finished["ended"]
    assert finished["returncode"] == 0

    result = subprocess.run(
        [str(shim), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        input="",
        timeout=5,
    )
    assert result.returncode == 0
    assert len(barrier.arrivals()) == 1, "git worktree list must pass straight through, unrecorded"


def test_a_shim_refuses_to_wrap_itself(tmp_path: Path) -> None:
    shims, root = tmp_path / "shims", tmp_path / "barrier"
    barrier = Barrier(root=root, name="git")
    with pytest.raises(ValueError):
        install_shim(
            shims,
            barrier,
            command="git",
            real=shims / "git",
            match="git-worktree-add",
            hold_seconds=1.0,
        )


_HOLDER = """
import fcntl, os, pathlib, sys, time
path, mode, ready = sys.argv[1], sys.argv[2], sys.argv[3]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if mode == "ex" else fcntl.LOCK_SH)
pathlib.Path(ready).write_text("ok")
time.sleep(60)
"""


@contextmanager
def _held_elsewhere(path: Path, mode: str, tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    ready = tmp_path / f"ready-{mode}-{time.monotonic_ns()}"
    process = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), mode, str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(0.02)
        yield process
    finally:
        process.kill()
        process.wait()


def test_lock_state_reads_free_shared_exclusive_and_absent(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    missing = tmp_path / "missing.lock"
    assert lock_state(missing) == "absent"
    assert not missing.exists(), "probing creates nothing"

    with _held_elsewhere(path, "sh", tmp_path):
        assert lock_state(path) == "shared"

    with _held_elsewhere(path, "ex", tmp_path) as holder:
        assert lock_state(path) == "exclusive"
        holder.kill()
        holder.wait()
        assert lock_state(path) == "free"
