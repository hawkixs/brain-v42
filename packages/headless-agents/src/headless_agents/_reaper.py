"""Watch one provider process group; kill it if the ``ha`` that owns it disappears.

Run as ``python -I _reaper.py <life-fd> <ready-fd>`` by
:func:`headless_agents.procgroup.start_watcher`, in its own session. ``ha``
holds the only write end of the ``<life-fd>`` pipe (spec 0.5.0 §3.8.2,
operator decision Q75 = a):

1. the watcher writes ``r`` on ``<ready-fd>`` once it runs -- ``ha`` starts
   no provider before that;
2. ``ha`` writes the provider's process group id and a newline;
3. then ``d`` on a normal end: the watcher leaves, killing nothing -- or
   end-of-file if ``ha`` died, however it died: the watcher sends ``SIGKILL``
   to the whole group, the provider's descendants included, which
   ``PR_SET_PDEATHSIG`` alone would leave running;
4. a group that empties on its own ends the watch, so a process id reused
   later can never be hit. End-of-file before a group was named leaves
   nothing to kill.

Standard library only, and run isolated (``-I``): it must work under any
environment the rail hands its provider.
"""

from __future__ import annotations

import os
import select
import signal
import sys

_POLL_SECONDS = 0.5


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _read_group(fd: int) -> int | None:
    """The group id ``ha`` names, or ``None`` if it released or died first."""
    buffer = b""
    while not buffer.endswith(b"\n"):
        chunk = os.read(fd, 1)
        if not chunk or chunk == b"d":
            return None
        buffer += chunk
    return int(buffer)


def watch(fd: int, ready_fd: int) -> None:
    os.write(ready_fd, b"r")
    os.close(ready_fd)
    pgid = _read_group(fd)
    if pgid is None:
        return
    while _group_exists(pgid):
        readable, _, _ = select.select([fd], [], [], _POLL_SECONDS)
        if not readable:
            continue
        if os.read(fd, 1):
            return  # released: the run ended normally
        # End of file without a release: ha is gone.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return


if __name__ == "__main__":
    watch(int(sys.argv[1]), int(sys.argv[2]))
