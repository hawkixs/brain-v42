"""Global admission: exclusion first, then first come, first served (0.5.3 lot 4b).

The invariants at the top hold whatever orders the waiters -- 0.5.2's admission gate or
0.5.3's ticketed queue: the global lock excludes an unconfined write from every other
run, shared runs coexist, and a writer already waiting is not overtaken by readers
arriving after it. They are pinned first, against the gate, and stay unchanged through
the redesign. Every property here depends on ``flock``, so every test drives real
processes on a temporary state directory, and kills every child in a ``finally``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from headless_agents import locks
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


def _until(
    condition: Callable[[], bool],
    what: str,
    timeout: float = 10.0,
    children: tuple[subprocess.Popen[bytes], ...] = (),
) -> None:
    """Poll ``condition``; fail at ``timeout``, or as soon as one of ``children`` --
    each meant to be still running meanwhile -- has exited."""
    limit = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < limit, f"timed out waiting for {what}"
        for child in children:
            assert child.poll() is None, f"a child exited {child.returncode} before {what}"
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


# ── the queue: tickets, waiter files, liveness (Task 2) ────────────────────────

#: Issues one ticket and holds it until the test creates ``release``. ``os.link`` is
#: wrapped to log each publication while ``tickets.lock`` is still held, so the order
#: of the ``linked`` lines is the order in which the issuers took that lock.
_ISSUER = """
import os
import sys
import time
from pathlib import Path

from headless_agents import locks

state, label, events = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
link = os.link


def logged_link(source, target, *args, **kwargs):
    link(source, target, *args, **kwargs)
    with events.open("a") as stream:
        stream.write(f"{label} linked {Path(target).name} {time.monotonic_ns()}\\n")


os.link = logged_link
queued = locks._issue_ticket(state, exclusive=False, label=label, wait=locks.AdmissionWait(10))
with events.open("a") as stream:
    stream.write(f"{label} issued {queued.ticket} {time.monotonic_ns()}\\n")
while not events.with_name("release").exists():
    time.sleep(0.01)
queued.leave()
"""


def _issuer_lines(events: Path, kind: str) -> list[tuple[str, str, int]]:
    """``(label, value, monotonic_ns)`` of every ``kind`` line, in time order."""
    if not events.exists():
        return []
    found = []
    for line in events.read_text().splitlines():
        label, event, value, ns = line.split()
        if event == kind:
            found.append((label, value, int(ns)))
    return sorted(found, key=lambda item: item[2])


def _lock_state(path: Path) -> str:
    """How another open file description sees ``path``'s ``flock``."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "exclusive"
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "shared"
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(descriptor)


def _issue(state: Path, label: str = "w", *, exclusive: bool = False) -> Any:
    return locks._issue_ticket(  # noqa: SLF001
        state, exclusive=exclusive, label=label, wait=AdmissionWait(5)
    )


def test_tickets_are_issued_in_order_across_processes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for index in range(20):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", _ISSUER, str(state), f"W{index}", str(events)]
                )
            )
        _until(
            lambda: len(_issuer_lines(events, "issued")) == 20,
            "20 tickets issued",
            30,
            tuple(processes),
        )
        linked = _issuer_lines(events, "linked")
        assert [name for _, name, _ in linked] == [f"{t:016d}.wait" for t in range(1, 21)]
        issued = {label: int(ticket) for label, ticket, _ in _issuer_lines(events, "issued")}
        assert [issued[label] for label, _, _ in linked] == list(range(1, 21))
        listed = locks.waiters(state)
        assert [waiter.ticket for waiter in listed] == list(range(1, 21))
        assert all(waiter.alive and not waiter.exclusive for waiter in listed)
        assert {waiter.label: waiter.ticket for waiter in listed} == issued
        assert {waiter.pid for waiter in listed} == {process.pid for process in processes}
    finally:
        events.with_name("release").write_text("go")
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    assert all(process.returncode == 0 for process in processes)
    assert locks.waiters(state) == []


def test_a_waiter_file_is_visible_only_once_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    seen: list[tuple[str, list[str]]] = []
    real_link = os.link

    def checking_link(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
        visible = sorted(path.name for path in Path(target).parent.glob("*.wait"))
        seen.append((_lock_state(Path(source)), visible))
        real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(locks.os, "link", checking_link)
    queued = _issue(state, exclusive=True)
    try:
        assert seen == [("exclusive", [])]
        assert _lock_state(queued.path) == "exclusive"
        assert [path.name for path in queued.path.parent.iterdir() if path.suffix == ".tmp"] == []
    finally:
        queued.leave()


def test_a_dead_waiter_is_listed_dead_and_removed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    events = tmp_path / "events"
    child = subprocess.Popen([sys.executable, "-c", _ISSUER, str(state), "D", str(events)])
    try:
        _until(lambda: len(_issuer_lines(events, "issued")) == 1, "the ticket issued", 10, (child,))
        (path,) = (state / "admission").glob("*.wait")
        child.send_signal(signal.SIGKILL)
        child.wait()
        first = locks.waiters(state)
        assert [(waiter.ticket, waiter.alive) for waiter in first] in ([], [(1, False)])
        assert not path.exists(), "a scan removes a dead waiter's file"
        assert locks.waiters(state) == []
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_a_stale_file_with_the_counters_value_is_not_overwritten(tmp_path: Path) -> None:
    state = tmp_path / "state"
    directory = state / "admission"
    directory.mkdir(parents=True)
    stale = directory / f"{7:016d}.wait"
    stale.write_text('{"left": "by a waiter that died"}')
    before = (stale.stat().st_ino, stale.read_bytes())
    (directory / "next-ticket").write_text("7")
    queued = _issue(state)
    try:
        assert queued.ticket == 8
        assert (stale.stat().st_ino, stale.read_bytes()) == before
        assert (directory / "next-ticket").read_text() == "9"
        # A later scan removes the dead file; the live waiter stays.
        assert [(waiter.ticket, waiter.alive) for waiter in locks.waiters(state)] == [
            (7, False),
            (8, True),
        ]
        assert not stale.exists()
    finally:
        queued.leave()


def test_a_name_that_is_taken_fails_the_link_and_moves_to_the_next_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``link`` refuses to overwrite: even a file the directory scan missed keeps its
    bytes, and the ticket moves on (``rename`` would have replaced it silently)."""
    state = tmp_path / "state"
    directory = state / "admission"
    directory.mkdir(parents=True)
    taken = directory / f"{1:016d}.wait"
    taken.write_text("not a waiter's")
    monkeypatch.setattr(locks, "_highest_ticket", lambda _directory: 0)
    queued = _issue(state, "moved")
    try:
        assert queued.ticket == 2
        assert queued.path.name == f"{2:016d}.wait"
        assert taken.read_text() == "not a waiter's"
        payload = json.loads(queued.path.read_text())
        assert payload["ticket"] == 2 and payload["label"] == "moved"
    finally:
        queued.leave()


def test_a_garbage_counter_recovers_from_the_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    directory = state / "admission"
    directory.mkdir(parents=True)
    (directory / "next-ticket").write_text("x")
    held_open = []
    try:
        for ticket in (3, 5):
            descriptor = os.open(directory / f"{ticket:016d}.wait", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held_open.append(descriptor)
        queued = _issue(state)
        try:
            assert queued.ticket == 6
        finally:
            queued.leave()
    finally:
        for descriptor in held_open:
            os.close(descriptor)


def test_leave_unlinks_before_closing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    queued = _issue(state)
    path, descriptor = queued.path, queued.fd
    observed: list[bool] = []
    real_close = os.close

    def watching_close(fd: int) -> None:
        if fd == descriptor and not observed:
            observed.append(path.exists())
        real_close(fd)

    monkeypatch.setattr(locks.os, "close", watching_close)
    queued.leave()
    queued.leave()  # idempotent
    monkeypatch.undo()
    assert observed == [False], "the lock dropped while the file was still visible"
    assert not path.exists()
    assert locks.waiters(state) == []


def test_waiters_never_raises(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert locks.waiters(state) == []
    assert not (state / "admission").exists(), "listing creates nothing"
    directory = state / "admission"
    directory.mkdir(parents=True)
    (directory / f"{1:016d}.wait").mkdir()
    (directory / f"{2:016d}.wait").symlink_to(tmp_path / "elsewhere")
    (directory / f"{3:016d}.wait").write_text("{not json")
    (directory / f"{4:016d}.wait").write_text("[1, 2]")
    os.mkfifo(directory / f"{5:016d}.wait")  # opening it for reading must not hang
    (directory / f"{6:016d}.wait").write_text(
        '{"ticket": 6, "exclusive": "yes", "label": "x", "pid": 1, "since": "now"}'
    )
    (directory / f"{7:016d}.wait").write_text("[" * 20000 + "]" * 20000)  # too deep to parse
    (directory / "garbage.wait").write_text("{}")
    outcome: list[object] = []

    def scan_queue() -> None:
        try:
            outcome.append(locks.waiters(state))
        except BaseException as exc:  # handed to the test, never lost with the thread
            outcome.append(exc)

    scan = threading.Thread(target=scan_queue, daemon=True)
    scan.start()
    scan.join(timeout=5)
    assert not scan.is_alive(), "waiters() hung"
    (listed,) = outcome
    assert isinstance(listed, list), f"waiters() raised {listed!r}"
    assert {waiter.ticket for waiter in listed} <= {1, 2, 3, 4, 5, 6, 7}
    assert all(not waiter.alive and waiter.label == "<unreadable>" for waiter in listed)


# ── admission through the queue: first come, first served (Task 3) ─────────────


def _admission_order(events: Path, labels: list[str]) -> list[str]:
    logged = _log(events)
    return sorted(labels, key=lambda label: logged[(label, "admitted")])


def test_writers_are_admitted_in_ticket_order(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        holder = spawn("holder-shared", "H")
        _until_logged(events, ("H", "admitted"))
        writers = []
        labels = [f"E{index}" for index in range(1, 6)]
        for label in labels:
            writers.append(spawn("admit-exclusive", label, hold=0.1))
            _until_logged(events, (label, "queued"))
            time.sleep(0.15)  # it holds its ticket before the next one even starts
        _release(events, "H")
        assert holder.wait(timeout=10) == 0
        assert all(writer.wait(timeout=20) == 0 for writer in writers)
        assert _admission_order(events, labels) == labels


def test_a_reader_is_not_timed_out_by_later_writers(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        holder = spawn("holder-shared", "H")
        _until_logged(events, ("H", "admitted"))
        first = spawn("admit-exclusive", "E1", hold=0.3)
        _until_logged(events, ("E1", "queued"))
        time.sleep(0.15)
        reader = spawn("admit-shared", "R", wait="5")
        _until_logged(events, ("R", "queued"))
        time.sleep(0.15)
        later = []
        for index in range(2, 6):
            later.append(spawn("admit-exclusive", f"E{index}", hold=0.3))
            _until_logged(events, (f"E{index}", "queued"))
            time.sleep(0.15)
        _release(events, "H")
        assert reader.wait(timeout=10) == 0, "the reader timed out"
        assert all(process.wait(timeout=20) == 0 for process in [holder, first, *later])
        logged = _log(events)
        assert ("R", "timeout") not in logged
        assert logged[("E1", "released")] < logged[("R", "admitted")]
        assert logged[("R", "released")] < logged[("E2", "admitted")]


def test_a_writer_is_not_starved_by_overlapping_readers(tmp_path: Path) -> None:
    with _children(tmp_path) as (_, events, spawn):
        processes = []
        for index in range(1, 7):  # two waves of overlapping readers
            processes.append(spawn("admit-shared", f"R{index}", hold=0.4))
            time.sleep(0.15)
        writer = spawn("admit-exclusive", "E", wait="5", hold=0.1)
        _until_logged(events, ("E", "queued"))
        time.sleep(0.05)
        for index in range(7, 11):  # the readers arriving after it
            processes.append(spawn("admit-shared", f"R{index}", hold=0.4))
            time.sleep(0.15)
        assert writer.wait(timeout=10) == 0, "the writer timed out"
        assert all(process.wait(timeout=20) == 0 for process in processes)
        logged = _log(events)
        for index in range(7, 11):
            assert logged[(f"R{index}", "admitted")] > logged[("E", "released")], (
                f"R{index}, arriving after the writer, got in before it"
            )


def test_a_waiter_dying_ahead_stops_blocking_at_the_next_poll(tmp_path: Path) -> None:
    with _children(tmp_path) as (state, events, spawn):
        spawn("holder-shared", "H")
        _until_logged(events, ("H", "admitted"))
        writer = spawn("admit-exclusive", "E1")
        _until_a_writer_waits(state)
        spawn("admit-shared", "R")
        _until_logged(events, ("R", "queued"))
        time.sleep(0.3)
        assert ("R", "admitted") not in _log(events), "the reader got in behind a waiting writer"
        writer.kill()
        writer.wait()
        killed = time.monotonic_ns()
        _until_logged(events, ("R", "admitted"))
        assert _log(events)[("R", "admitted")] - killed < 0.5e9, "a dead waiter kept blocking"


def test_an_expired_wait_leaves_the_queue_and_names_the_phase(tmp_path: Path) -> None:
    with _children(tmp_path) as (state, events, spawn):
        spawn("holder-shared", "H")
        _until_logged(events, ("H", "admitted"))
        with pytest.raises(locks.AdmissionTimeout) as blocked_by_holders:
            with admit_global(state, exclusive=True, wait=AdmissionWait(0.3)):
                pass
        assert blocked_by_holders.value.phase == "global"
        assert blocked_by_holders.value.ahead == ()
        assert locks.waiters(state) == [], "the expired waiter left the queue"
        started = time.monotonic()
        with admit_global(state, exclusive=False, wait=AdmissionWait(0.3)):
            pass
        assert time.monotonic() - started < 0.2, "a later reader must get in at once"

        writer = spawn("admit-exclusive", "W")
        _until_a_writer_waits(state)
        with pytest.raises(locks.AdmissionTimeout) as blocked_by_the_queue:
            with admit_global(state, exclusive=False, wait=AdmissionWait(0.3)):
                pass
        assert blocked_by_the_queue.value.phase == "queue"
        (ahead,) = blocked_by_the_queue.value.ahead
        assert ahead.exclusive and ahead.alive and ahead.pid == writer.pid
        assert [waiter.pid for waiter in locks.waiters(state)] == [writer.pid]


def test_the_order_stack_is_clean_after_admission(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for exclusive in (False, True):
        with admit_global(state, exclusive=exclusive, wait=AdmissionWait(None)):
            assert [rank for rank, _ in locks._stack()] == [locks.Rank.UNCONFINED]  # noqa: SLF001
        assert locks._stack() == []  # noqa: SLF001
