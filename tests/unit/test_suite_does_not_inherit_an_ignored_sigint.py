"""The suite never runs with SIGINT ignored, whoever launched it.

Ticket 29e9d695. A process started as a shell background job (``&``, ``nohup``,
some CI or agent wrappers) inherits SIGINT as SIG_IGN, and every child it spawns
inherits it too: the tests that interrupt a child with a real SIGINT then wait
for a child that never stops (measured 2026-10-05:
``test_isolated_modes_clean_staging_after_signals[2-*]`` time out after 5 s
when launched that way, and pass in the foreground).
"""

from __future__ import annotations

import signal
import subprocess
import sys


def test_the_test_process_does_not_ignore_sigint() -> None:
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN


def test_a_child_process_does_not_inherit_an_ignored_sigint() -> None:
    probe = "import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)"
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=30
    )
    assert completed.stdout.strip() == "False"
