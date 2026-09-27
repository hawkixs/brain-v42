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
