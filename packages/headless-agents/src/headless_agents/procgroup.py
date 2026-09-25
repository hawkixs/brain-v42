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
import select
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

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
#: How long a new watcher may take to say it is running before the spawn fails.
WATCHER_START_SECONDS: Final = 5.0
# The real Popen, taken at import: a test that fakes ``subprocess.Popen`` for a
# rail must not also fake the watcher (tests/unit/conftest.py keeps real
# watchers out of such tests altogether).
_POPEN: Final = subprocess.Popen


class Lifeline:
    """The write end of a watcher's pipe: while it is open, the group lives.

    :meth:`child_attach` has the provider's child name its group before exec;
    :meth:`release` tells the watcher the run ended normally. If ``ha`` dies
    instead, the kernel closes this end and the watcher kills the group.
    """

    def __init__(self, write_fd: int | None, watcher: subprocess.Popen[bytes] | None) -> None:
        self._write_fd = write_fd
        self._watcher = watcher

    @classmethod
    def unwatched(cls) -> Lifeline:
        """A lifeline with no watcher behind it (tests with a fake provider)."""
        return cls(None, None)

    def child_attach(self, preexec_fn: Callable[[], None] | None) -> Callable[[], None]:
        """A ``preexec_fn`` that runs ``preexec_fn``, then names the child's own group.

        It runs in the child after ``setsid`` and before ``exec``: the watcher
        knows the group before the provider can start any descendant, so ``ha``
        dying at any point after the fork leaves nothing unwatched (closure
        round of PR #206). A write that fails -- the watcher is gone -- ends the
        child before exec: no watcher, no provider.
        """
        write_fd = self._write_fd

        def preexec() -> None:
            if preexec_fn is not None:
                preexec_fn()
            if write_fd is None:
                return
            try:
                os.write(write_fd, b"%d\n" % os.getpgid(0))
            except OSError:
                os._exit(1)

        return preexec

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


def start_watcher() -> Lifeline:
    """Start a group watcher and wait until it runs; :class:`OSError` otherwise.

    The watcher runs in its own session, outside the group it will guard, and
    holds the read end of a pipe only this process writes. It reports ``r`` on
    a second pipe once it runs: a provider is never started before its watcher
    is (codex review of PR #206, round 3).
    """
    life_read, life_write = os.pipe()
    ready_read, ready_write = os.pipe()
    try:
        watcher = _POPEN(
            [sys.executable, "-I", str(_REAPER), str(life_read), str(ready_write)],
            pass_fds=(life_read, ready_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        for fd in (life_read, life_write, ready_read, ready_write):
            os.close(fd)
        raise
    os.close(life_read)
    os.close(ready_write)
    try:
        readable, _, _ = select.select([ready_read], [], [], WATCHER_START_SECONDS)
        ready = os.read(ready_read, 1) if readable else b""
    finally:
        os.close(ready_read)
    if ready != b"r":
        os.close(life_write)
        watcher.kill()
        watcher.wait()
        raise OSError(f"the process-group watcher did not start ({_REAPER})")
    return Lifeline(life_write, watcher)


def spawn_watched(
    command: list[str], **popen_kwargs: Any
) -> tuple[subprocess.Popen[Any], Lifeline]:
    """Start ``command`` under a group watcher; fail closed without one.

    ``PR_SET_PDEATHSIG`` reaches a provider's direct child only; its
    descendants -- a CLI's workers, a shell command the agent started -- would
    otherwise outlive a killed ``ha`` and keep writing after its locks died
    (operator decision Q75 = a). The watcher starts first; the provider stays
    this process's direct child, in its own session, so ``Popen``'s semantics
    are unchanged. The child names its own group to the watcher before exec
    (:meth:`Lifeline.child_attach`), so no instant after the fork leaves a
    descendant the watcher does not know.
    """
    lifeline = start_watcher()
    popen_kwargs["preexec_fn"] = lifeline.child_attach(popen_kwargs.get("preexec_fn"))
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except BaseException:
        lifeline.release()
        raise
    return process, lifeline


__all__ = ["PR_SET_PDEATHSIG", "Lifeline", "preexec_for", "spawn_watched", "start_watcher"]
