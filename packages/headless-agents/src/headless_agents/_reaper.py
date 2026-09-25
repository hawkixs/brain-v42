"""Watch one provider process group; kill it if the ``ha`` that owns it disappears.

Run as ``python -I _reaper.py <fd> <pgid>`` by :func:`headless_agents.procgroup.watch_group`,
in its own session, with ``<fd>`` the read end of a pipe whose write end only
``ha`` holds (spec 0.5.0 §3.8.2, operator decision Q75 = a):

- ``ha`` ends the run normally: it writes ``d`` and closes -- the watcher leaves,
  killing nothing;
- ``ha`` dies, however it died: the pipe reads end-of-file -- the watcher sends
  ``SIGKILL`` to the whole group, the provider's descendants included, which
  ``PR_SET_PDEATHSIG`` alone would leave running;
- the group empties on its own: the watcher leaves, so a process id reused
  later can never be hit.

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


def watch(fd: int, pgid: int) -> None:
    while _group_exists(pgid):
        readable, _, _ = select.select([fd], [], [], _POLL_SECONDS)
        if not readable:
            continue
        data = os.read(fd, 1)
        if data:
            return  # released: the run ended normally
        # End of file without a release: ha is gone.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return


if __name__ == "__main__":
    watch(int(sys.argv[1]), int(sys.argv[2]))
