"""A provider process dies with the ``ha`` process that started it (spec 0.5.0 §3.8.2)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from headless_agents import procgroup
from headless_agents.procgroup import preexec_for

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux-only")


def _wait_for(path: Path, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not (path.exists() and path.read_text().strip()):
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0): read its state.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_child_dies_when_ha_dies(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import os, pathlib, subprocess, time\n"
        "from headless_agents.procgroup import preexec_for\n"
        "c = subprocess.Popen(['sleep', '30'], preexec_fn=preexec_for(os.getpid()),"
        " start_new_session=True)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(c.pid))\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script])
    try:
        _wait_for(pid_file)
        child_pid = int(pid_file.read_text())
        assert _alive(child_pid)
        parent.kill()
        parent.wait()
        assert _gone_within(child_pid, seconds=5)
    finally:
        if parent.poll() is None:
            parent.kill()


def test_a_child_survives_while_its_spawning_thread_waits(tmp_path: Path) -> None:
    """Review Focus 1: PR_SET_PDEATHSIG follows the THREAD that forked."""
    pid_file = tmp_path / "pid"

    def spawn() -> None:
        child = subprocess.Popen(
            ["sleep", "30"], preexec_fn=preexec_for(os.getpid()), start_new_session=True
        )
        pid_file.write_text(str(child.pid))
        child.wait()

    worker = threading.Thread(target=spawn, daemon=True)
    worker.start()
    _wait_for(pid_file)
    child_pid = int(pid_file.read_text())
    time.sleep(0.5)
    assert _alive(child_pid)
    os.kill(child_pid, signal.SIGKILL)
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_a_parent_that_died_before_the_signal_was_armed_ends_the_child() -> None:
    """The fork race: ``ha`` dies between fork and prctl. The re-check of the
    parent pid after prctl ends the child before exec."""
    finished = subprocess.Popen(["true"])
    finished.wait()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the forked child
        preexec_for(finished.pid)()
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 1


def test_a_failing_prctl_is_reported_and_does_not_abort_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan closure carry-forward (Task 7): the child still execs -- a preexec
    exception would abort the spawn -- and the failure is written to its
    stderr, which the rails point at their stderr log."""
    monkeypatch.setattr(procgroup, "_arm_death_signal", lambda: -1)
    log = tmp_path / "stderr.log"
    with log.open("w") as stream:
        completed = subprocess.run(
            ["true"], preexec_fn=preexec_for(os.getpid()), stderr=stream, check=False
        )
    assert completed.returncode == 0
    assert "PR_SET_PDEATHSIG" in log.read_text()


_HA_STAND_IN = """
import os, pathlib, subprocess, sys, time
from headless_agents.procgroup import preexec_for, watch_group
out = pathlib.Path(sys.argv[1])
child = subprocess.Popen(
    ["sh", "-c", f"sleep 60 & echo $! > {out}/grandchild; wait"],
    preexec_fn=preexec_for(os.getpid()),
    start_new_session=True,
)
lifeline = watch_group(child.pid)
(out / "child").write_text(str(child.pid))
if sys.argv[2] == "release":
    while not (out / "grandchild").exists():
        time.sleep(0.02)
    child.kill()
    child.wait()
    lifeline.release()
    (out / "released").write_text("ok")
time.sleep(60)
"""


def _read_pid(path: Path) -> int:
    _wait_for(path)
    return int(path.read_text().strip())


def test_a_grandchild_dies_when_ha_is_killed(tmp_path: Path) -> None:
    """Operator decision Q75=a: PR_SET_PDEATHSIG reaches the direct child
    only; the watcher kills the provider's whole group when ha disappears,
    however it died."""
    ha = subprocess.Popen([sys.executable, "-c", _HA_STAND_IN, str(tmp_path), "kill"])
    try:
        child = _read_pid(tmp_path / "child")
        grandchild = _read_pid(tmp_path / "grandchild")
        assert _alive(child) and _alive(grandchild)
        ha.kill()
        ha.wait()
        assert _gone_within(child, seconds=5)
        assert _gone_within(grandchild, seconds=5)
    finally:
        if ha.poll() is None:
            ha.kill()


def test_a_released_lifeline_kills_nothing(tmp_path: Path) -> None:
    """A normal end writes 'done' before closing: the watcher leaves."""
    ha = subprocess.Popen([sys.executable, "-c", _HA_STAND_IN, str(tmp_path), "release"])
    grandchild: int | None = None
    try:
        grandchild = _read_pid(tmp_path / "grandchild")
        _wait_for(tmp_path / "released")
        time.sleep(1.0)
        assert _alive(grandchild), "a released watcher must not kill the group"
    finally:
        ha.kill()
        ha.wait()
        if grandchild is not None and _alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_a_group_that_does_not_exist_needs_no_watcher() -> None:
    lifeline = procgroup.watch_group(2**22 + 12345)
    lifeline.release()


def test_a_working_prctl_writes_nothing(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    with log.open("w") as stream:
        subprocess.run(["true"], preexec_fn=preexec_for(os.getpid()), stderr=stream, check=True)
    assert log.read_text() == ""
