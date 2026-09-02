"""Shared fixtures for the unit tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import pytest_asyncio


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
