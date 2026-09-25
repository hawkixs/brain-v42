"""Locks of ``ha``: a fixed order, bounded waits, gone with their process (spec 0.5.0 §3.8.2).

All locks are ``flock`` on files opened with ``O_CLOEXEC``: no provider, hook or
git subprocess inherits one, and a lock dies with the ``ha`` process that
took it. The order is fixed, which excludes a deadlock:

1. the run's own lifecycle lock;
2. the global unconfined lock;
3. the lineage registry lock;
4. lineage locks, in ascending owner-id order.

:func:`held` enforces that order per thread and raises ``RuntimeError`` on a
violation -- a programming error, never a user's. A lock not obtained within
its bound raises :class:`LockTimeout`, which the engine turns into a usage
refusal (exit ``2``).
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    UNCONFINED = 2
    LINEAGE_REGISTRY = 3
    LINEAGE = 4


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
    """
    _check_order(rank, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + (wait or 0.0)
        while True:
            try:
                fcntl.flock(descriptor, operation)
                break
            except BlockingIOError:
                if wait is None or time.monotonic() >= deadline:
                    bound = "at once" if wait is None else f"within {wait:g} s"
                    raise LockTimeout(f"{what}: not obtained {bound}") from None
                time.sleep(_POLL_SECONDS)
        stack = _stack()
        stack.append((rank, key))
        try:
            yield
        finally:
            stack.pop()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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


__all__ = ["LOCK_WAIT_SECONDS", "LockTimeout", "Rank", "held", "is_free"]
