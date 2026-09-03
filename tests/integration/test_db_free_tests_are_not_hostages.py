"""A test that never touches the database must not need one to run.

Ticket `b83ae9fe`. Three session-scoped autouse fixtures made the whole
`tests/integration/` tree conditional on `BRAIN_V42_TEST_DB_URL`: `run_migrations`
skipped without it, and `check_db_connection` and `cleanup_test_data` pulled
`engine`, which pulls `run_migrations`. Every test under this root therefore
skipped, database touched or not.

It cost twice, and both times the cost was a SILENT skip rather than an error:
w44's HNSW bench (`bench_docker`, spins its own containers) and w47's network
boundary probe (reads sockets and published ports, no database anywhere). The
probe had to move out of the tree entirely, because a probe that can skip itself
proves nothing -- which is the same green-by-absence its own docstring condemns.

This module is the witness. It asks for no database fixture and asserts nothing
about one; running to completion with `BRAIN_V42_TEST_DB_URL` unset IS the
assertion. If the gate returns, it skips again and this file reddens by being
absent from the report -- so it also states its own premise, loudly.
"""

from __future__ import annotations

import os

import pytest


def test_a_db_free_test_runs_without_the_integration_db_url() -> None:
    """Runs, rather than skipping, when no test database is configured.

    Meaningful only in the run where the variable is unset -- when it IS set the
    test is a tautology, and says so rather than pretending to prove something.
    """
    if os.environ.get("BRAIN_V42_TEST_DB_URL"):
        pytest.skip("URL de base configurée : ce témoin ne mesure rien dans ce run")

    assert True, "atteindre cette ligne EST la preuve : le conftest n'a pas pris ce test en otage"


def test_the_witness_asks_for_no_database_fixture(request: pytest.FixtureRequest) -> None:
    """The premise, checked rather than assumed.

    A witness that quietly acquired a database fixture would pass for the wrong
    reason and hide the very regression it exists to catch.
    """
    database_fixtures = {
        "engine",
        "session_factory",
        "db_session",
        "cleanup_test_data",
        "check_db_connection",
        "migration_downgrade_fence",
        "run_migrations",
    }

    assert database_fixtures.isdisjoint(request.fixturenames), (
        "ce témoin a acquis une fixture base : il ne prouve plus rien "
        f"({sorted(database_fixtures & set(request.fixturenames))})"
    )
