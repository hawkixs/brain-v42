"""Global admission: exclusion first, then first come, first served (0.5.3 lot 4b).

The invariants at the top hold whatever orders the waiters -- 0.5.2's admission gate or
0.5.3's ticketed queue: the global lock excludes an unconfined write from every other
run, shared runs coexist, and a writer already waiting is not overtaken by readers
arriving after it. They are pinned first, against the gate, and stay unchanged through
the redesign. Every property here depends on ``flock``, so every test drives real
processes on a temporary state directory, and kills every child in a ``finally``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from headless_agents.locks import AdmissionWait, LockTimeout, admit_global

#: One admission in a child process. ``mode`` is ``holder-shared``,
#: ``holder-exclusive``, ``admit-shared`` or ``admit-exclusive``: a holder keeps the
#: global lock until the test creates its release file; an admission holds it for
#: ``hold`` seconds. Each event is one line ``label mode event monotonic_ns``: the
#: monotonic clock is system-wide, so lines of different children compare.
_CHILD = """
import sys
import time
from pathlib import Path

from headless_agents.locks import AdmissionWait, LockTimeout, admit_global

state, mode, label, events, wait, hold = sys.argv[1:]
state, events = Path(state), Path(events)
release = events.with_name(f"release-{label}")


def log(event):
    with events.open("a") as stream:
        stream.write(f"{label} {mode} {event} {time.monotonic_ns()}\\n")


kind, sharing = mode.split("-")
log("queued")
try:
    with admit_global(
        state,
        exclusive=sharing == "exclusive",
        wait=AdmissionWait(None if wait == "none" else float(wait)),
    ):
        log("admitted")
        if kind == "holder":
            while not release.exists():
                time.sleep(0.01)
        else:
            time.sleep(float(hold))
        log("released")
except LockTimeout:
    log("timeout")
    sys.exit(2)
"""

Spawn = Callable[..., subprocess.Popen[bytes]]


@contextmanager
def _children(tmp_path: Path) -> Iterator[tuple[Path, Path, Spawn]]:
    """A state directory, the shared events file, and a spawner whose children are
    all killed on the way out, whatever the test did."""
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    started: list[subprocess.Popen[bytes]] = []

    def spawn(
        mode: str, label: str, *, wait: str = "10", hold: float = 0.0
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(state), mode, label, str(events), wait, str(hold)]
        )
        started.append(process)
        return process

    try:
        yield state, events, spawn
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait()


def _log(events: Path) -> dict[tuple[str, str], int]:
    """``(label, event) -> monotonic_ns`` of every line written so far."""
    if not events.exists():
        return {}
    logged = {}
    for line in events.read_text().splitlines():
        label, _, event, ns = line.split()
        logged[(label, event)] = int(ns)
    return logged


def _until(condition: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    limit = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < limit, f"timed out waiting for {what}"
        time.sleep(0.01)


def _until_logged(events: Path, *pairs: tuple[str, str]) -> None:
    _until(lambda: all(pair in _log(events) for pair in pairs), f"{pairs} in the event log")


def _release(events: Path, label: str) -> None:
    events.with_name(f"release-{label}").write_text("go")


def _until_a_writer_waits(state: Path) -> None:
    """Until an exclusive admission is established as waiting: a shared admission
    tried now, with a 50 ms budget, no longer gets in. Only the public contract of
    :func:`admit_global` is used, so this holds for the gate and the queue alike."""
    limit = time.monotonic() + 10
    while time.monotonic() < limit:
        try:
            with admit_global(state, exclusive=False, wait=AdmissionWait(0.05)):
                pass
        except LockTimeout:
            return
        time.sleep(0.01)
    pytest.fail("the exclusive admission never started waiting")


# ── the invariants: pinned against 0.5.2's gate, unchanged by the queue ────────


def test_an_admitted_exclusive_run_excludes_every_shared_admission(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        writer = spawn("holder-exclusive", "E")
        _until_logged(events, ("E", "admitted"))
        readers = [spawn("admit-shared", f"S{index}") for index in range(1, 4)]
        _until_logged(events, *((f"S{index}", "queued") for index in range(1, 4)))
        time.sleep(0.2)
        assert not any((f"S{index}", "admitted") in _log(events) for index in range(1, 4))
        _release(events, "E")
        assert writer.wait(timeout=10) == 0
        assert all(reader.wait(timeout=10) == 0 for reader in readers)
        logged = _log(events)
        for index in range(1, 4):
            assert logged[(f"S{index}", "admitted")] > logged[("E", "released")]


def test_admitted_shared_runs_exclude_an_exclusive_admission(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        readers = [spawn("holder-shared", label) for label in ("S1", "S2")]
        _until_logged(events, ("S1", "admitted"), ("S2", "admitted"))
        writer = spawn("admit-exclusive", "E")
        _until_logged(events, ("E", "queued"))
        time.sleep(0.2)
        _release(events, "S1")
        assert readers[0].wait(timeout=10) == 0
        time.sleep(0.2)
        assert ("E", "admitted") not in _log(events), "admitted beside a shared run"
        _release(events, "S2")
        assert writer.wait(timeout=10) == 0
        assert readers[1].wait(timeout=10) == 0
        logged = _log(events)
        assert logged[("E", "admitted")] > logged[("S1", "released")]
        assert logged[("E", "admitted")] > logged[("S2", "released")]


def test_shared_runs_are_admitted_together(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        readers = [spawn("holder-shared", label) for label in ("S1", "S2")]
        # Both in at once: neither is released before the other is admitted.
        _until_logged(events, ("S1", "admitted"), ("S2", "admitted"))
        for label in ("S1", "S2"):
            _release(events, label)
        assert all(reader.wait(timeout=10) == 0 for reader in readers)
        logged = _log(events)
        assert max(logged[("S1", "admitted")], logged[("S2", "admitted")]) < min(
            logged[("S1", "released")], logged[("S2", "released")]
        )


def test_a_waiting_writer_is_not_overtaken_by_later_readers(tmp_path: Path) -> None:
    with _children(tmp_path) as (state, events, spawn):
        holder = spawn("holder-shared", "H")
        _until_logged(events, ("H", "admitted"))
        writer = spawn("admit-exclusive", "E", hold=0.2)
        _until_a_writer_waits(state)
        readers = []
        for index in range(1, 4):
            readers.append(spawn("admit-shared", f"R{index}"))
            time.sleep(0.1)
        _until_logged(events, *((f"R{index}", "queued") for index in range(1, 4)))
        time.sleep(0.2)
        assert not any((f"R{index}", "admitted") in _log(events) for index in range(1, 4)), (
            "a reader arriving after the waiting writer got in first"
        )
        _release(events, "H")
        assert holder.wait(timeout=10) == 0
        assert writer.wait(timeout=10) == 0
        assert all(reader.wait(timeout=10) == 0 for reader in readers)
        logged = _log(events)
        for index in range(1, 4):
            assert logged[(f"R{index}", "admitted")] > logged[("E", "released")]
