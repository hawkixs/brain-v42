"""Locks of ``ha``: a fixed order, bounded waits, gone with their process (spec 0.5.0 §3.8.2).

All locks are ``flock`` on files opened with ``O_CLOEXEC``: no provider, hook or
git subprocess inherits one, and a lock dies with the ``ha`` process that
took it. The order is fixed, which excludes a deadlock:

1. the run's own lifecycle lock;
2. the writer-intent lock: an unconfined writer holds it exclusively from
   before it ever polls the gate below, until it wins the gate or times out;
   a reader takes it shared only for the instant of checking that no writer
   currently holds it;
3. the admission gate, held only for the instant of taking the lock below;
4. the global unconfined lock;
5. the lineage registry lock;
6. lineage locks, in ascending owner-id order.

:func:`held` enforces that order per thread and raises ``RuntimeError`` on a
violation -- a programming error, never a user's. A lock not obtained within
its bound raises :class:`LockTimeout`, which the engine turns into a usage
refusal (exit ``2``).

:class:`AdmissionWait` carries one optional, explicit ``--wait`` deadline
(lot 3, spec §3.3) shared by every admission lock a run takes -- the global
lock through :func:`admit_global`, and the lineage registry and lineage locks
a write or a review admits under it. Without ``--wait``, each lock keeps its
own :data:`LOCK_WAIT_SECONDS` bound, as before.

**What ``--wait`` guarantees** (real-process tests in ``test_locks.py``,
``test_engine_execute.py``, ``test_write_flow.py``, ``test_review.py``):

- ``--wait SECONDS`` bounds the *whole* admission -- the gate, the global
  lock, and, for a write or a review, the lineage registry and lineage locks
  -- with one absolute monotonic deadline, spent across every lock in turn.
- A lock granted past that deadline is refused, never accepted late (fixed
  by codex review of PR #239: :func:`held` used to check the deadline only
  on a failed attempt, so a lock released just after it expired could still
  be granted).
- An expired admission deadline starts no provider step and leaves nothing
  behind: an unstarted write's entry and run dir are forgotten, and so,
  since PR #239, are an unstarted read's and an unstarted review's --
  previously a read or a review refused at the lineage locks was left
  marked ``"failed"`` with an empty run dir, as if something had run. An
  invalid ``--wait`` value is rejected before any admission is attempted,
  so nothing is created to begin with.
- ``ha clean`` is admitted through the very same gate as every other run,
  not a lock it takes directly: it queues behind an unconfined write that
  already holds the gate, exactly like a read would (also PR #239).
- The writer-intent lock (point 2 above) makes one case exact, not
  heuristic: once an unconfined writer *holds* it, any reader that arrives
  afterward blocks on its own attempt to take it shared until the writer
  releases it -- ordinary ``flock`` mutual exclusion against a single
  exclusive holder, true regardless of timing.

**What is deliberately NOT guaranteed** (operator decision, 2026-09-27: keep
this mechanism as best-effort writer preference rather than block PR #239 on
a real fix):

- ``flock`` gives no fairness between waiters. The writer-intent lock only
  narrows the starvation window described below; it does not close it.
- A writer that has not yet won the writer-intent lock -- still polling for
  it, not holding it -- can be overtaken indefinitely by a continuous,
  overlapping stream of readers: each reader holds the intent lock shared
  only briefly, but if new ones keep arriving before the last one releases
  it, the writer's own exclusive attempt may never see a free instant.
- Symmetrically, a continuous stream of writers can make a waiting reader
  time out: a reader releases the intent lock before it ever touches the
  gate, and a new writer racing in during that gap can win the gate ahead
  of it, repeatedly.
- Neither case is exercised by the process tests here on purpose: they
  would be flaky proof of a property this mechanism does not hold. A real
  fix -- a fair FIFO admission queue -- is planned for headless-agents
  0.5.3, not this lot.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Final

#: Spec §3.8.2: every wait on a lock is bounded by ten seconds.
LOCK_WAIT_SECONDS: Final = 10.0
_POLL_SECONDS: Final = 0.05


class LockTimeout(Exception):
    """A lock not obtained within its bound; the message names which."""


class Rank(IntEnum):
    LIFECYCLE = 1
    WRITER_INTENT = 2
    ADMISSION_GATE = 3
    UNCONFINED = 4
    LINEAGE_REGISTRY = 5
    LINEAGE = 6
    # The admission queue's ranks: the gate's own, which the queue replaces.
    ADMISSION_TICKET = 2
    ADMISSION_WAITER = 3


_held = threading.local()


def _stack() -> list[tuple[Rank, str]]:
    stack: list[tuple[Rank, str]] | None = getattr(_held, "stack", None)
    if stack is None:
        stack = []
        _held.stack = stack
    return stack


def _check_order(rank: Rank, key: str) -> None:
    stack = _stack()
    if not stack:
        return
    last_rank, last_key = stack[-1]
    if rank > last_rank:
        return
    if rank == last_rank == Rank.LINEAGE and key > last_key:
        return
    raise RuntimeError(
        f"lock order violated: {rank.name}({key!r}) taken while holding "
        f"{last_rank.name}({last_key!r})"
    )


@contextmanager
def held(
    path: Path,
    *,
    rank: Rank,
    exclusive: bool,
    wait: float | None = LOCK_WAIT_SECONDS,
    what: str,
    key: str = "",
) -> Iterator[None]:
    """Hold the lock at ``path`` for the block.

    ``wait=None`` tries once. ``what`` names the lock in a refusal; ``key``
    orders locks of the same rank (the lineage owner id).

    The deadline is an absolute point in time, checked before every attempt
    and again right after a successful one, with each poll sleep capped to
    the time actually left: trying ``flock`` first and only checking the
    deadline on failure let a lock released just past the deadline be
    accepted late instead of refused (codex review of PR #239).
    """
    _check_order(rank, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise LockTimeout(f"{what}: not obtained within {wait:g} s")
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError:
                if wait is None:
                    raise LockTimeout(f"{what}: not obtained at once") from None
                remaining = deadline - time.monotonic()  # type: ignore[operator]
                time.sleep(max(min(_POLL_SECONDS, remaining), 0.0))
                continue
            else:
                if deadline is not None and time.monotonic() > deadline:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    raise LockTimeout(f"{what}: not obtained within {wait:g} s") from None
                break
        stack = _stack()
        entry = (rank, key)
        stack.append(entry)
        try:
            yield
        finally:
            # Its own entry, not the last one: §3.8.3 releases the lineage
            # registry lock while the lineage locks taken after it stay held.
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] is entry:
                    del stack[index]
                    break
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class AdmissionWait:
    """One optional monotonic admission deadline, shared by every lock a run admits under.

    ``seconds=None`` keeps the per-lock :data:`LOCK_WAIT_SECONDS` default (no
    ``--wait`` given). An explicit bound is spent across every lock
    :meth:`remaining` is asked for: time spent on one lock is not returned to
    the next.
    """

    seconds: float | None
    started: float = field(default_factory=time.monotonic)

    #: False for the budget :func:`admit_global` builds when no ``--wait`` was
    #: given: its expiry must not name a flag the caller never passed.
    explicit: bool = True

    def remaining(self, what: str) -> float:
        if self.seconds is None:
            return LOCK_WAIT_SECONDS
        left = self.started + self.seconds - time.monotonic()
        if left <= 0:
            if self.explicit:
                raise LockTimeout(f"{what}: --wait {self.seconds:g} s expired")
            raise LockTimeout(f"{what}: not obtained within {self.seconds:g} s")
        return left


@contextmanager
def admit_global(state: Path, *, exclusive: bool, wait: AdmissionWait) -> Iterator[None]:
    """Take the global unconfined lock at ``state``, gated for BEST-EFFORT writer preference.

    The admission gate (``admission-gate.lock``) is held only for the instant
    of acquiring the global lock: shared for an ordinary run, exclusive for an
    unconfined write. A writer that already holds the gate holds it
    exclusively for as long as it waits for the global lock, and every later
    run -- shared or not -- queues behind it instead of slipping in first.
    The gate is released as soon as the global lock is taken, or on a
    timeout; the global lock itself is held for the caller's block.

    A gate held nonblocking excludes a reader only once the writer already
    owns it, not while the writer is still polling for it: a reader that
    keeps arriving during that polling window could otherwise take the gate
    shared every time, starving the writer out. The writer-intent lock
    narrows that window (codex review of PR #239): an unconfined writer
    takes it exclusively *before* it ever polls the gate, and holds it for
    as long as that polling lasts; a reader takes it shared only for the
    instant of checking that no writer currently holds it, then releases it
    before it ever touches the gate itself.

    This is a heuristic mitigation, not a fairness guarantee: ``flock`` does
    not order waiters. A writer still *polling* for writer-intent (not yet
    holding it) can in principle be overtaken indefinitely by a continuous,
    overlapping stream of readers, each holding intent only briefly; and a
    continuous stream of writers can likewise make a waiting reader time out
    by winning the gate first, repeatedly, in the gap after a reader
    releases intent and before it takes the gate. What *is* guaranteed:
    once a writer holds writer-intent, a reader that arrives afterward
    blocks on that lock -- ordinary mutual exclusion, not scheduling order.
    A real fix (a fair FIFO admission queue) is planned for 0.5.3.
    """
    budget = wait if wait.seconds is not None else AdmissionWait(LOCK_WAIT_SECONDS, explicit=False)
    intent = state / "writer-intent.lock"
    gate = state / "admission-gate.lock"
    unconfined = state / "unconfined.lock"
    with ExitStack() as global_lock:
        if exclusive:
            with held(
                intent,
                rank=Rank.WRITER_INTENT,
                exclusive=True,
                wait=budget.remaining("a pending write"),
                what="a pending write",
            ):
                with held(
                    gate,
                    rank=Rank.ADMISSION_GATE,
                    exclusive=True,
                    wait=budget.remaining("the admission gate"),
                    what="the admission gate",
                ):
                    global_lock.enter_context(
                        held(
                            unconfined,
                            rank=Rank.UNCONFINED,
                            exclusive=True,
                            wait=budget.remaining("the unconfined lock"),
                            what="the unconfined lock",
                        )
                    )
        else:
            with held(
                intent,
                rank=Rank.WRITER_INTENT,
                exclusive=False,
                wait=budget.remaining("a pending write"),
                what="a pending write",
            ):
                pass
            with held(
                gate,
                rank=Rank.ADMISSION_GATE,
                exclusive=False,
                wait=budget.remaining("the admission gate"),
                what="the admission gate",
            ):
                global_lock.enter_context(
                    held(
                        unconfined,
                        rank=Rank.UNCONFINED,
                        exclusive=False,
                        wait=budget.remaining("the unconfined lock"),
                        what="the unconfined lock",
                    )
                )
        yield


# ── the admission queue ─────────────────────────────────────────────────────

#: Under ``<state>``: the ticket lock, the ticket counter and one ``.wait`` file per
#: admission waiting its turn.
ADMISSION_DIR: Final = "admission"
_TICKETS_LOCK: Final = "tickets.lock"
_COUNTER: Final = "next-ticket"
_WAIT_NAME: Final = re.compile(r"(\d{16})\.wait")
#: A waiter's payload is a few hundred bytes; anything past this is not one.
_PAYLOAD_LIMIT: Final = 65536
_UNREADABLE: Final = "<unreadable>"


@dataclass(frozen=True)
class Waiter:
    """One admission in the queue, as :func:`waiters` read it."""

    ticket: int
    exclusive: bool
    #: What is waiting -- a run id, ``clean``, ... -- for display only.
    label: str
    pid: int
    #: When the ticket was issued (UTC, ISO 8601).
    since: str
    #: Its process still holds the file's lock. A dead waiter never blocks anyone.
    alive: bool


def _unreadable(ticket: int) -> Waiter:
    return Waiter(ticket=ticket, exclusive=False, label=_UNREADABLE, pid=0, since="", alive=False)


def _payload(descriptor: int, ticket: int) -> tuple[bool, str, int, str] | None:
    """The waiter's ``(exclusive, label, pid, since)``, or ``None`` when the file is not
    one this module wrote."""
    chunks = []
    size = 0
    while size <= _PAYLOAD_LIMIT:
        chunk = os.pread(descriptor, _PAYLOAD_LIMIT + 1 - size, size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > _PAYLOAD_LIMIT:
        return None
    try:
        document = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):  # nested too deeply is not a payload either
        return None
    if not isinstance(document, dict) or document.get("ticket") != ticket:
        return None
    exclusive, label = document.get("exclusive"), document.get("label")
    pid, since = document.get("pid"), document.get("since")
    if (
        isinstance(exclusive, bool)
        and isinstance(label, str)
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and isinstance(since, str)
    ):
        return exclusive, label, pid, since
    return None


def _remove_if_same(path: Path, opened: os.stat_result) -> None:
    """Unlink ``path`` if it still names the file that was found dead. Best effort."""
    with suppress(OSError):
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
            os.unlink(path)


def _inspect(path: Path, ticket: int) -> Waiter | None:
    """``path`` as a waiter; ``None`` once it is gone (its waiter left meanwhile)."""
    try:
        # O_NONBLOCK: a FIFO named like a waiter must not hang the scan.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return _unreadable(ticket)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return _unreadable(ticket)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            alive = True
        except OSError:
            return _unreadable(ticket)
        else:
            # Nobody holds it: its process is gone, and the kernel dropped the lock.
            alive = False
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        try:
            payload = _payload(descriptor, ticket)
        except OSError:
            payload = None
        if not alive:
            _remove_if_same(path, opened)
        if payload is None:
            return _unreadable(ticket)
        exclusive, label, pid, since = payload
        return Waiter(
            ticket=ticket, exclusive=exclusive, label=label, pid=pid, since=since, alive=alive
        )
    finally:
        os.close(descriptor)


def waiters(state: Path) -> list[Waiter]:
    """The admission queue at ``state``, ascending by ticket.

    A waiter whose process died is listed ``alive=False``, never blocks anyone, and
    its file is removed on the way. Never raises and creates nothing: a file that is
    not a waiter's is listed ``alive=False`` with the label ``<unreadable>``.
    """
    try:
        with os.scandir(state / ADMISSION_DIR) as entries:
            found = sorted(
                (int(match.group(1)), entry.name)
                for entry in entries
                if (match := _WAIT_NAME.fullmatch(entry.name)) is not None
            )
    except OSError:
        return []
    listed = []
    for ticket, name in found:
        waiter = _inspect(state / ADMISSION_DIR / name, ticket)
        if waiter is not None:
            listed.append(waiter)
    return listed


def _highest_ticket(directory: Path) -> int:
    """The highest ticket any ``.wait`` name carries, live or dead; 0 for none."""
    with os.scandir(directory) as entries:
        return max(
            (
                int(match.group(1))
                for entry in entries
                if (match := _WAIT_NAME.fullmatch(entry.name)) is not None
            ),
            default=0,
        )


def _read_counter(directory: Path) -> int:
    """The persisted next ticket; 0 when missing or garbage (the directory recovers it)."""
    try:
        descriptor = os.open(directory / _COUNTER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return 0
    try:
        text = os.read(descriptor, 64).decode("ascii", errors="replace").strip()
    except OSError:
        return 0
    finally:
        os.close(descriptor)
    return int(text) if text.isdigit() else 0


def _write_counter(directory: Path, value: int) -> None:
    temporary = directory / f".{_COUNTER}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        os.write(descriptor, str(value).encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, directory / _COUNTER)


def _remove_stale_temporaries(directory: Path) -> None:
    """Remove what crashed issuers left. Called under ``tickets.lock``: every issuer
    creates its temporaries under it, so any other one found now is a dead one's."""
    with os.scandir(directory) as entries:
        names = [entry.name for entry in entries if entry.name.startswith(".")]
    for name in names:
        if name.endswith(".tmp"):
            with suppress(OSError):
                os.unlink(directory / name)


def _write_payload(descriptor: int, payload: dict[str, object]) -> None:
    data = json.dumps(payload).encode("utf-8")
    os.ftruncate(descriptor, 0)
    written = 0
    while written < len(data):
        written += os.pwrite(descriptor, data[written:], written)
    os.fsync(descriptor)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(eq=False)
class _Queued:
    """A ticket being waited on: its ``.wait`` file, held locked through ``fd``."""

    ticket: int
    fd: int
    path: Path
    _entry: tuple[Rank, str]
    _left: bool = False

    def leave(self) -> None:
        """Leave the queue: the name goes FIRST, then the lock. Closing first would
        leave, for an instant, a visible file nobody holds -- which every scanner reads
        as a dead waiter's. Idempotent."""
        if self._left:
            return
        self._left = True
        try:
            with suppress(FileNotFoundError):
                os.unlink(self.path)
        finally:
            os.close(self.fd)
            _forget(self._entry)


def _forget(entry: tuple[Rank, str]) -> None:
    stack = _stack()
    for index in range(len(stack) - 1, -1, -1):
        if stack[index] is entry:
            del stack[index]
            break


def _issue_ticket(state: Path, *, exclusive: bool, label: str, wait: AdmissionWait) -> _Queued:
    """Take a ticket and publish its waiter file, locked, under ``tickets.lock``.

    The ticket is ``max(counter, 1 + highest ticket on disk)``: the counter keeps
    tickets increasing across an empty queue; the directory makes a lost or garbage
    counter harmless. The file is created under a temporary name, locked, filled and
    ``fsync``-ed, and only then hard-linked to its ticket's name: a ``.wait`` file is
    never visible unlocked while its waiter lives, so "visible and unlocked" means
    exactly "its waiter died". ``link`` fails on a name that exists, where ``rename``
    would overwrite it: a taken name moves the ticket on instead.
    """
    directory = state / ADMISSION_DIR
    with held(
        directory / _TICKETS_LOCK,
        rank=Rank.ADMISSION_TICKET,
        exclusive=True,
        wait=wait.remaining("the admission queue"),
        what="the admission queue",
    ):
        _remove_stale_temporaries(directory)
        ticket = max(_read_counter(directory), _highest_ticket(directory) + 1)
        pid = os.getpid()
        temporary = directory / f".{ticket:016d}.{pid}.tmp"
        descriptor = os.open(
            temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        published: Path | None = None
        try:
            # A file nobody else has opened yet: the lock is granted at once.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            since = _utc_now()
            while published is None:
                _write_payload(
                    descriptor,
                    {
                        "ticket": ticket,
                        "exclusive": exclusive,
                        "label": label,
                        "pid": pid,
                        "since": since,
                    },
                )
                target = directory / f"{ticket:016d}.wait"
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    ticket += 1
                else:
                    published = target
            os.unlink(temporary)
            _write_counter(directory, ticket + 1)
            entry = (Rank.ADMISSION_WAITER, str(ticket))
            _check_order(*entry)
            _stack().append(entry)
        except BaseException:
            for leftover in (published, temporary):
                if leftover is not None:
                    with suppress(FileNotFoundError):
                        os.unlink(leftover)
            os.close(descriptor)
            raise
    return _Queued(ticket=ticket, fd=descriptor, path=published, _entry=entry)


def is_free(path: Path) -> bool:
    """No process holds ``path`` exclusively: a non-blocking shared lock succeeds.

    A writer holds its lineage lock exclusively and readers hold it shared, so
    only a live writer makes this ``False`` -- which is how a pending write whose
    writer died is told from one still running. Probing creates nothing.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


__all__ = [
    "ADMISSION_DIR",
    "LOCK_WAIT_SECONDS",
    "AdmissionWait",
    "LockTimeout",
    "Rank",
    "Waiter",
    "admit_global",
    "held",
    "is_free",
    "waiters",
]
