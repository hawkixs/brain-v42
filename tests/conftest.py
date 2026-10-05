"""Shared test fixtures and constants for brain_v42 test suite."""

from __future__ import annotations

import os
import signal

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Restore SIGINT when the suite was launched with it ignored (ticket 29e9d695).

    A shell background job (``&``, ``nohup``, some CI or agent wrappers) starts
    with SIGINT set to SIG_IGN, and every child inherits it: the tests that stop
    a child with a real SIGINT then wait for a child that never stops. Python's
    own handler is restored here, so this process is interruptible again and its
    children start with the default disposition.
    """
    if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)


FAKE_EMBEDDING: list[float] = [0.1] * 1536


def require_test_db_url() -> str:
    """Return BRAIN_V42_TEST_DB_URL, or skip the test.

    Unit tests that touch a real PostgreSQL used to fall back to the default
    POSTGRES_URL (prod DB on localhost:5433). Every `pytest tests/unit` run
    then silently polluted production with e.g. bogus dream_runs rows carrying
    the literal "something went wrong" from test_promote_validate.py.

    The defensive fix: DB-backed tests must opt in explicitly via
    BRAIN_V42_TEST_DB_URL pointing at an isolated test database. In CI this
    is set per-job; locally, set it in your shell when you want to run the
    DB-touching tests. If it's missing, skip loudly rather than corrupt prod.
    """
    url = os.getenv("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip(
            "BRAIN_V42_TEST_DB_URL not set — skipping DB-backed tests to avoid "
            "polluting prod (POSTGRES_URL fallback removed)"
        )
    return url
