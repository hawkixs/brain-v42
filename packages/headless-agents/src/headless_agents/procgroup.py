"""Provider processes die with the ``ha`` process that started them (spec 0.5.0 §3.8.2).

Every rail starts its CLI in its own session (``start_new_session=True``), so
a terminal's Ctrl-C reaches ``ha`` only, and a killed ``ha`` would leave the
provider running -- still writing a worktree whose lock just died with ``ha``.
:func:`preexec_for` closes the second case: on Linux the child asks the
kernel for ``SIGKILL`` when its parent dies (``PR_SET_PDEATHSIG``). The first
case, an interrupted ``ha`` that lives on, is each rail's own ``except
BaseException`` around its wait.

Two properties of ``PR_SET_PDEATHSIG`` shape the code:

- It follows the **thread** that forked, not the process: a provider must be
  started by the thread that waits on it (the rails do), never by a
  short-lived helper thread.
- It is armed only once ``prctl`` runs in the child, after the fork: if ``ha``
  died in between, the signal will never come. The child therefore compares
  its parent pid with the one captured before the fork, and exits before exec
  when they differ.

The ``libc`` handle is resolved at import, in the parent: the child calls a
function pointer and allocates nothing it can avoid (``preexec_fn`` runs
between fork and exec). A ``prctl`` that fails is written to the child's
stderr -- the rail's stderr log -- and the spawn goes on: an exception in a
``preexec_fn`` would abort the whole run instead.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from collections.abc import Callable
from typing import Final

PR_SET_PDEATHSIG: Final = 1
_FAILURE_NOTE: Final = (
    b"ha: prctl(PR_SET_PDEATHSIG) failed; this provider may outlive the ha process\n"
)

_prctl: Callable[..., int] | None = None
if sys.platform == "linux":
    try:
        _prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        _prctl = None


def _arm_death_signal() -> int:
    """``0`` when the kernel will send SIGKILL at the parent's death, else ``-1``."""
    if _prctl is None:
        return -1
    return int(_prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0))


def _noop() -> None:
    return None


def preexec_for(parent_pid: int) -> Callable[[], None]:
    """The ``preexec_fn`` for a provider started by the process ``parent_pid``.

    Pass ``os.getpid()`` captured in the parent, before the fork. A no-op
    outside Linux.
    """
    if sys.platform != "linux":
        return _noop

    def preexec() -> None:
        if _arm_death_signal() != 0:
            try:
                os.write(2, _FAILURE_NOTE)
            except OSError:
                pass
        if os.getppid() != parent_pid:
            # The parent died before the death signal was armed: nothing will
            # ever kill this child for it. Stop before exec.
            os._exit(1)

    return preexec


__all__ = ["PR_SET_PDEATHSIG", "preexec_for"]
