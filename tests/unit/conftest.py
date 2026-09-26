"""Shared fixtures for the unit tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import pytest_asyncio

# A stranger on a fresh clone, following README/CONTRIBUTING to the letter, exports
# no env var at all before `pytest tests/unit`. `Settings()` (brain_v42.config) has no
# default for postgres_url -- by design, so the *application* never boots against an
# unnamed database -- so every test that merely imports or constructs Settings/
# get_settings crashed with `pydantic_core.ValidationError: BRAIN_POSTGRES_URL Field
# required` instead of running or skipping (ticket 8dc6f0d2, measured 45 failed).
#
# This is syntactically valid and points at a loopback port nothing listens on: any
# code path that actually tried to open it gets an immediate ECONNREFUSED, never a
# hang. Tests that need a REAL database opt in through `require_test_db_url()`
# (tests/conftest.py), keyed on BRAIN_V42_TEST_DB_URL, and skip loudly without it --
# this fixture does not touch that mechanism.
_UNREACHABLE_TEST_POSTGRES_URL = (
    "postgresql+asyncpg://unit-test:unit-test@127.0.0.1:1/brain_unit_test_unreachable"
)


@pytest.fixture(autouse=True)
def _default_postgres_url_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``Settings()``/``get_settings()`` a harmless DSN when the shell has none.

    Only a FALLBACK: a developer's own ``POSTGRES_URL`` (or ``BRAIN_POSTGRES_URL``,
    which wins by alias priority -- see ``_brain_alias`` in ``brain_v42.config``) is
    never overridden, so a shell already configured against a real database keeps
    behaving exactly as before.
    """
    if not os.environ.get("BRAIN_POSTGRES_URL") and not os.environ.get("POSTGRES_URL"):
        monkeypatch.setenv("POSTGRES_URL", _UNREACHABLE_TEST_POSTGRES_URL)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _purge_unit_rows() -> None:  # type: ignore[misc]
    """Clean up, once the unit session is over, what it wrote to the database.

    ``tests/unit/`` hits the SAME ``brain_test`` database as the integration suite
    as soon as ``BRAIN_V42_TEST_DB_URL`` is set, and both CI rails set it. But
    ``cleanup_test_data`` is a fixture of ``tests/integration/conftest.py``: it only
    applies to ITS suite. Seven unit modules were therefore writing with nothing
    erasing them — 5,674 learnings measured in brain_test on 2026-08-11 for 188 real
    rows (ticket cb888186).

    The symptom is invisible in CI, which recreates its database at every pipeline;
    it only grows on local development databases, hence differently on each machine.
    That is the worst place for a defect.

    Silent when the variable is absent: most unit tests touch no database and must
    not pay for a connection because of this.
    """
    yield  # type: ignore[misc]

    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url or not url.strip():
        return

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # Imported from the integration conftest on purpose: TWO purges would diverge,
    # and the divergence would only be seen the day one of them lets something
    # through. `tests` is a package and this crossing is already a repository pattern
    # (tests/integration/test_cleanup_purge_scope.py).
    from tests.integration.conftest import purge_integration_rows

    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 — unreachable database: nothing to clean, nothing to report
        await engine.dispose()
        return
    try:
        async with engine.begin() as conn:
            await purge_integration_rows(conn)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_activity_reporter() -> Iterator[None]:
    """Reset the global activity emitter to ``None`` around each test.

    ``brain_v42.mcp.activity_reporter._reporter`` is a process global, built lazily
    at the first tool call. Two test modules inject a double into it — the emitter
    itself and the provenance middleware's wiring — and a double left in place would
    leak into every following test of the process. The reset lives here rather than
    duplicated in each module: it also protects the tests that traverse the
    middleware without knowing they touch this global.
    """
    from brain_v42.mcp import activity_reporter

    activity_reporter.set_activity_reporter(None)
    yield
    activity_reporter.set_activity_reporter(None)


@pytest.fixture(autouse=True)
def _no_real_group_watcher(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rails started with a fake ``Popen`` must never get a real group watcher.

    A watcher attached to a fake provider pid would ``SIGKILL`` whatever
    process group owns that number on this machine if the test died before
    releasing it. Tests of the watcher itself opt in with ``real_watcher``.
    """
    if request.node.get_closest_marker("real_watcher") is not None:
        return
    from headless_agents import procgroup

    monkeypatch.setattr(procgroup, "start_watcher", procgroup.Lifeline.unwatched)
