"""Locks: fixed order, bounded waits, gone with their process (spec 0.5.0 §3.8.2)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from headless_agents import locks
from headless_agents.locks import AdmissionWait, LockTimeout, Rank, held, is_free

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


def test_an_exclusive_holder_excludes_a_shared_taker(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path):
        with pytest.raises(LockTimeout, match="the test lock"):
            with held(path, rank=Rank.LIFECYCLE, exclusive=False, wait=0.3, what="the test lock"):
                pass


def test_shared_holders_coexist(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "sh", tmp_path):
        with held(path, rank=Rank.LIFECYCLE, exclusive=False, wait=0.3, what="x"):
            pass


def test_a_lock_dies_with_its_process(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path) as holder:
        holder.kill()
        holder.wait()
        start = time.monotonic()
        with held(path, rank=Rank.LIFECYCLE, exclusive=True, wait=5, what="x"):
            pass
        assert time.monotonic() - start < 1


def test_no_wait_means_one_try(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path):
        start = time.monotonic()
        with pytest.raises(LockTimeout):
            with held(path, rank=Rank.LIFECYCLE, exclusive=True, wait=None, what="x"):
                pass
        assert time.monotonic() - start < 0.5


def test_a_subprocess_does_not_inherit_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with held(path, rank=Rank.LIFECYCLE, exclusive=True, what="x"):
        child = subprocess.Popen(["sleep", "5"])
        try:
            targets = [
                os.readlink(f"/proc/{child.pid}/fd/{fd}")
                for fd in os.listdir(f"/proc/{child.pid}/fd")
            ]
        finally:
            child.kill()
            child.wait()
    assert str(path) not in targets


def test_a_lower_rank_after_a_higher_one_is_a_programming_error(tmp_path: Path) -> None:
    with held(tmp_path / "u.lock", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "r.lock", rank=Rank.LIFECYCLE, exclusive=True, what="r"):
                pass


def test_lineage_locks_go_in_ascending_owner_order(tmp_path: Path) -> None:
    with held(tmp_path / "a.lock", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
        with held(tmp_path / "b.lock", rank=Rank.LINEAGE, exclusive=False, what="b", key="b"):
            pass
    with held(tmp_path / "b.lock", rank=Rank.LINEAGE, exclusive=False, what="b", key="b"):
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "a.lock", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
                pass


def test_the_order_is_free_again_after_release(tmp_path: Path) -> None:
    with held(tmp_path / "u.lock", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        pass
    with held(tmp_path / "r.lock", rank=Rank.LIFECYCLE, exclusive=True, what="r"):
        pass


def test_is_free_tells_a_live_writer_from_readers(tmp_path: Path) -> None:
    path = tmp_path / "lineage.lock"
    assert is_free(path)
    assert not path.exists(), "probing creates nothing"
    with _held_elsewhere(path, "sh", tmp_path):
        assert is_free(path), "shared holders coexist: only a writer makes the probe fail"
    with _held_elsewhere(path, "ex", tmp_path) as writer:
        assert not is_free(path)
        writer.kill()
        writer.wait()
        assert is_free(path)


def test_the_bound_is_the_spec_value() -> None:
    assert locks.LOCK_WAIT_SECONDS == 10.0


def test_releasing_the_registry_lock_before_a_lineage_lock_keeps_the_order_true(
    tmp_path: Path,
) -> None:
    """Spec §3.8.3 releases the lineage registry lock after the intent, while the
    lineage lock stays held: the order check must then see the lineage lock, not
    the registry lock, as the last one held."""
    registry = held(tmp_path / "r", rank=Rank.LINEAGE_REGISTRY, exclusive=True, what="r")
    registry.__enter__()
    with held(tmp_path / "b", rank=Rank.LINEAGE, exclusive=True, what="b", key="b"):
        registry.__exit__(None, None, None)
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "a", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
                pass
    with held(tmp_path / "u", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        pass


# ── admission gate: writer preference, one deadline (plan lot 3, Task 1) ────

_ADMISSION_CHILD = """
import sys
import time
from pathlib import Path
from headless_agents.locks import AdmissionWait, LockTimeout, Rank, admit_global, held

state, mode, ready, events, seconds = sys.argv[1:]
state, ready, events = Path(state), Path(ready), Path(events)
if mode == "holder":
    with held(state / "unconfined.lock", rank=Rank.UNCONFINED,
              exclusive=False, wait=None, what="the unconfined lock"):
        ready.write_text("held")
        time.sleep(60)
else:
    ready.write_text("started")
    try:
        with admit_global(state, exclusive=mode == "writer",
                          wait=AdmissionWait(float(seconds))):
            with events.open("a") as stream:
                stream.write(mode + "\\n")
            if mode == "writer":
                time.sleep(0.25)
    except LockTimeout:
        with events.open("a") as stream:
            stream.write(mode + "-timeout\\n")
        sys.exit(2)
"""


def _admission_child(
    state: Path, mode: str, events: Path, seconds: float = 3.0
) -> tuple[subprocess.Popen[bytes], Path]:
    ready = state / f"{mode}-{time.monotonic_ns()}.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _ADMISSION_CHILD,
            str(state),
            mode,
            str(ready),
            str(events),
            str(seconds),
        ]
    )
    limit = time.monotonic() + 5
    while not ready.exists():
        assert process.poll() is None and time.monotonic() < limit
        time.sleep(0.01)
    return process, ready


def _gate_is_exclusive(state: Path) -> None:
    limit = time.monotonic() + 5
    while time.monotonic() < limit:
        try:
            with held(
                state / "admission-gate.lock",
                rank=Rank.ADMISSION_GATE,
                exclusive=False,
                wait=None,
                what="the admission gate",
            ):
                pass
        except LockTimeout:
            return
        time.sleep(0.01)
    pytest.fail("the waiting writer never took the admission gate")


def test_waiting_writer_precedes_a_later_shared_admission(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    holder, _ = _admission_child(state, "holder", events)
    writer = reader = None
    try:
        writer, _ = _admission_child(state, "writer", events)
        _gate_is_exclusive(state)
        reader, _ = _admission_child(state, "reader", events)
        time.sleep(0.12)
        assert not events.exists(), "a later reader bypassed the waiting writer"
        holder.kill()
        assert writer.wait(timeout=5) == 0
        assert reader.wait(timeout=5) == 0
        assert events.read_text().splitlines() == ["writer", "reader"]
    finally:
        for process in (holder, writer, reader):
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()


def test_gate_is_released_after_writer_timeout(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    holder, _ = _admission_child(state, "holder", events)
    writer = reader = None
    try:
        writer, _ = _admission_child(state, "writer", events, 0.15)
        assert writer.wait(timeout=5) == 2
        reader, _ = _admission_child(state, "reader", events)
        assert reader.wait(timeout=5) == 0
        assert events.read_text().splitlines() == ["writer-timeout", "reader"]
    finally:
        for process in (holder, writer, reader):
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()


def test_one_deadline_covers_two_contested_locks(tmp_path: Path) -> None:
    budget = AdmissionWait(0.20)
    path = tmp_path / "lineage.lock"
    with _held_elsewhere(path, "ex", tmp_path):
        with held(
            tmp_path / "registry.lock",
            rank=Rank.LINEAGE_REGISTRY,
            exclusive=False,
            wait=budget.remaining("the registry"),
            what="the registry",
        ):
            time.sleep(0.12)
            started = time.monotonic()
            with pytest.raises(LockTimeout, match="the lineage"):
                with held(
                    path,
                    rank=Rank.LINEAGE,
                    exclusive=False,
                    wait=budget.remaining("the lineage"),
                    what="the lineage",
                    key="owner",
                ):
                    pass
            assert time.monotonic() - started < 0.16


def test_new_readers_queue_behind_a_writer_still_polling_for_the_gate(tmp_path: Path) -> None:
    """The writer-preference gate must exclude a reader from the moment the
    writer starts polling for it, not only once the writer already holds it:
    ``test_waiting_writer_precedes_a_later_shared_admission`` only starts its
    reader after the writer already owns the gate, so it never exercises the
    window where the writer is still contending for it -- readers that keep
    arriving there could starve the writer out indefinitely.

    An exclusive holder on the global lock (not the ``_ADMISSION_CHILD``
    "holder" mode, which only takes it shared and would let ordinary shared
    readers straight through) forces two seed readers to stay stuck holding
    the gate shared, so the gate is already unavailable the moment the
    writer starts polling for it; a further stream of readers keeps arriving
    while the writer is still polling, not once it already holds the gate.
    """
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    writer: subprocess.Popen[bytes] | None = None
    seed_readers: list[subprocess.Popen[bytes]] = []
    late_readers: list[subprocess.Popen[bytes]] = []
    try:
        with _held_elsewhere(state / "unconfined.lock", "ex", tmp_path):
            for _ in range(2):
                process, _ = _admission_child(state, "seed", events)
                seed_readers.append(process)
            time.sleep(0.1)

            writer, _ = _admission_child(state, "writer", events, seconds=3.0)
            # Deliberately not waiting for the writer to hold the gate (unlike
            # test_waiting_writer_precedes_a_later_shared_admission): the
            # readers below arrive while it is still polling for the gate,
            # not after.
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                process, _ = _admission_child(state, "late", events)
                late_readers.append(process)
                time.sleep(0.05)

            assert not events.exists(), "an admission completed before the holder was released"
        # The ``with`` block above killed the exclusive holder on exit.

        assert writer.wait(timeout=5) == 0
        for process in seed_readers + late_readers:
            assert process.wait(timeout=5) == 0

        lines = events.read_text().splitlines()
        writer_index = lines.index("writer")
        for index, line in enumerate(lines):
            if line == "late":
                assert index > writer_index, (
                    "a reader arriving while the writer was still polling for the gate overtook it"
                )
        assert lines.count("late") == len(late_readers)
        assert lines.count("seed") == len(seed_readers)
    finally:
        for process in (writer, *seed_readers, *late_readers):
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()


# ── review round 1 (PR #239): a lock granted just after the deadline was
# still accepted, instead of refused ───────────────────────────────────────

_TIMED_HOLDER = """
import fcntl, os, pathlib, sys, time
path, ready, hold_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(ready).write_text("ok")
time.sleep(hold_seconds)
fcntl.flock(fd, fcntl.LOCK_UN)
time.sleep(5)
"""


def test_a_lock_released_just_after_the_deadline_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``held()`` tried ``flock`` before checking the deadline on the next
    iteration, so a lock released just past the deadline was granted late
    instead of refused. Deterministic timing: with the poll interval fixed at
    0.1 s and a 0.15 s wait, the fixed implementation caps its sleep to the
    remaining budget and lands its last attempt exactly at the deadline
    (0.1 s, then 0.05 s); the unfixed implementation always sleeps a full
    poll interval and lands its next attempt at 0.2 s -- after the holder's
    0.18 s release -- and would accept the lock late.
    """
    monkeypatch.setattr(locks, "_POLL_SECONDS", 0.1)
    path = tmp_path / "l.lock"
    ready = tmp_path / "ready"
    release_after = 0.18
    holder = subprocess.Popen(
        [sys.executable, "-c", _TIMED_HOLDER, str(path), str(ready), str(release_after)]
    )
    try:
        limit = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < limit
            time.sleep(0.01)
        started = time.monotonic()
        with pytest.raises(LockTimeout, match="the test lock"):
            with held(path, rank=Rank.LIFECYCLE, exclusive=True, wait=0.15, what="the test lock"):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < release_after, (
            "the deadline must expire before the lock is actually released, not after"
        )
    finally:
        holder.kill()
        holder.wait()
