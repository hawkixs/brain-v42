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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
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


_REAPER: Final = Path(__file__).with_name("_reaper.py")


class Lifeline:
    """The write end of a watcher's pipe: holding it open keeps the group alive.

    :meth:`release` tells the watcher the run ended normally. If ``ha`` dies
    instead, the kernel closes this end and the watcher kills the group.
    """

    def __init__(self, write_fd: int | None, watcher: subprocess.Popen[bytes] | None) -> None:
        self._write_fd = write_fd
        self._watcher = watcher

    def release(self) -> None:
        if self._write_fd is not None:
            try:
                os.write(self._write_fd, b"d")
            except OSError:
                pass
            os.close(self._write_fd)
            self._write_fd = None
        if self._watcher is not None:
            try:
                self._watcher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._watcher.kill()
                self._watcher.wait()
            self._watcher = None


def watch_group(pgid: int) -> Lifeline:
    """Start a watcher that kills process group ``pgid`` if this process dies.

    ``PR_SET_PDEATHSIG`` reaches a provider's direct child only; its
    descendants -- a CLI's workers, a shell command the agent started -- would
    otherwise outlive a killed ``ha`` and keep writing after its locks died
    (operator decision Q75 = a). The watcher runs in its own session, outside
    the group it guards. A group that does not exist needs none.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return Lifeline(None, None)
    read_fd, write_fd = os.pipe()
    try:
        watcher = subprocess.Popen(
            [sys.executable, "-I", str(_REAPER), str(read_fd), str(pgid)],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        os.close(write_fd)
        return Lifeline(None, None)
    finally:
        os.close(read_fd)
    return Lifeline(write_fd, watcher)


__all__ = ["PR_SET_PDEATHSIG", "Lifeline", "preexec_for", "watch_group"]
