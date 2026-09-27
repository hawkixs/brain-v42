"""Locks of ``ha``: a fixed order, bounded waits, gone with their process (spec 0.5.0 §3.8.2).

All locks are ``flock`` on files opened with ``O_CLOEXEC``: no provider, hook or
git subprocess inherits one, and a lock dies with the ``ha`` process that
took it. The order is fixed, which excludes a deadlock:

1. the run's own lifecycle lock;
2. the writer-intent lock: a waiting unconfined writer holds it exclusively
   from before it ever polls the gate below, so a reader that arrives while
   it is still polling -- not only once it already holds the gate -- queues
   behind it too (codex review of PR #239); a reader takes it shared only
   for the instant of checking that no writer is queued;
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
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
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
    """Take the global unconfined lock at ``state``, gated for writer preference.

    The admission gate (``admission-gate.lock``) is held only for the instant
    of acquiring the global lock: shared for an ordinary run, exclusive for an
    unconfined write. A waiting unconfined writer therefore holds the gate
    exclusively for as long as it waits for the global lock, and every later
    run -- shared or not -- queues behind it instead of slipping in first. The
    gate is released as soon as the global lock is taken, or on a timeout; the
    global lock itself is held for the caller's block.

    A gate held nonblocking excludes a reader only once the writer already
    owns it: a reader that keeps arriving while the writer is still polling
    for the (currently unavailable) gate could otherwise take it shared
    every time, starving the writer out (codex review of PR #239). The
    writer-intent lock closes that window: an unconfined writer takes it
    exclusively *before* it ever polls the gate, and holds it for as long as
    that polling lasts; a reader takes it shared only for the instant of
    checking that no writer is queued, then releases it before it ever
    touches the gate itself.
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
    "LOCK_WAIT_SECONDS",
    "AdmissionWait",
    "LockTimeout",
    "Rank",
    "admit_global",
    "held",
    "is_free",
]
