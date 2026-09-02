"""A skipped integration suite must SAY it was skipped, and why.

Ticket `634203e0`, measured on this machine on 2026-09-02 while playing the lot-4
gates: `BRAIN_V42_TEST_DB_URL` is set neither in the environment nor in `.env`, so
`pytest tests/integration` returns **423 skipped in 1.04s** and exit code 0. A
reader sees green. Nobody is told that 423 tests measured nothing.

It is the project's own thread — "green that proves nothing" — in its local form.
CI sets the variable per job, so CI keeps its authority; the harm is entirely
local, and it lands on whoever reads a run summary and believes it.

**WHY A LOUD LINE AND NOT A HARD FAILURE.** The mandate offered both. Failing
closed when `tests/integration` is requested is defensible, and it was refused for
two measured reasons:

* the trigger cannot be read reliably. pytest sees `config.args`, and telling
  "the developer asked for the integration suite" apart from "a wider run swept it
  up" means guessing at paths, node ids, `-k`, `-m` and rootdir-relative forms.
  A guard whose condition is a guess fires on the wrong runs;
* a red that appears on every machine without an env var teaches people to set the
  variable to whatever silences it. The `brain` guard would catch the worst case,
  but the pressure would be real, and it points the wrong way.

The measured harm is not that the tests do not run — it is that nobody is told.
A summary line fixes exactly that, breaks no workflow, and reads the same whether
the suite was asked for or collected along the way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestTheSummaryLineIsBuiltHonestly:
    """The message itself, tested without spending a subprocess."""

    def test_it_names_the_variable_the_count_and_the_expected_value(self) -> None:
        """Three facts, because a line missing any of them leaves the reader stuck.

        Without the variable a reader does not know what to set; without the count
        they cannot tell one skipped test from four hundred; without the expected
        value they are one keystroke from pointing it at `brain`.
        """
        from tests.integration.conftest import format_missing_db_url_summary

        line = format_missing_db_url_summary(423)

        assert "BRAIN_V42_TEST_DB_URL" in line
        assert "423" in line
        assert "brain_test" in line

    def test_it_never_suggests_the_production_database(self) -> None:
        """A summary that suggested `brain` would turn a diagnostic into an incident."""
        from tests.integration.conftest import format_missing_db_url_summary

        line = format_missing_db_url_summary(1)

        assert "brain_test" in line
        assert not any(
            token == "brain" for token in line.replace("=", " ").replace("/", " ").split()
        )

    def test_a_run_that_skipped_nothing_gets_no_line(self) -> None:
        """Negative witness: the line must be a signal, not furniture.

        Printed on every run it would be scrolled past, and the day it matters it
        would be invisible — the failure mode this repository names for its own
        alarms.
        """
        from tests.integration.conftest import format_missing_db_url_summary

        assert format_missing_db_url_summary(0) is None


class TestTheLineReachesARealTerminal:
    """The anti-tautology proof: a formatter nobody wires is a formatter that never fires."""

    def test_a_real_run_without_the_variable_says_so(self) -> None:
        """Runs the real path, in a subprocess, with the variable removed.

        Asserting the hook exists would prove it was written. Only a run proves it
        is registered, reached, and rendered — and this is the exact invocation of
        the ticket.
        """
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/integration", "-q", "-p", "no:randomly"],
            cwd=REPO_ROOT,
            env={key: value for key, value in os.environ.items() if key != "BRAIN_V42_TEST_DB_URL"},
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert "BRAIN_V42_TEST_DB_URL" in completed.stdout, completed.stdout[-2000:]
        assert "brain_test" in completed.stdout, completed.stdout[-2000:]
