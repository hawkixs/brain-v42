"""The unit tests' key generator and the purge predicate must agree.

These two rules live in independent modules — `tests/unit/keys.py` and
`_INTEGRATION_PROJECT_PREDICATE` in `tests/integration/conftest.py` — and nothing
obliged them to speak of the same thing. They diverged in silence: `tests/unit/`
hits the same `brain_test` database, its keys carried prefixes invented on the spot
(`t8-`, `t9-`, `rv-`, `t-adr-`, `t-run-`), and the predicate only knows `integ-`.
Measured on 2026-08-11 in brain_test: 5,674 learnings, of which 4,241 under `t8-`
and 581 under `t9-`, against 188 real rows.

The defect is invisible in CI, which recreates its database at every pipeline. It
only shows on local development databases — the place where it diverges per machine
and where nobody looks (ticket cb888186).

This test is the point of contact: it builds a key through the path the unit tests
take, then applies the REAL purge function to it. It fails if either end moves
without the other.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import learnings
from tests.conftest import require_test_db_url
from tests.integration.conftest import purge_integration_rows
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        pytest.skip(f"PostgreSQL test database is not reachable: {exc}")
    yield eng
    await eng.dispose()


async def test_a_key_built_for_a_unit_test_is_deleted_by_the_real_purge(
    _engine: AsyncEngine,
) -> None:
    """The contract that was missing: what the unit tests write, the purge erases."""
    key = make_unit_project_key("purge-probe")

    async with _engine.begin() as conn:
        await conn.execute(
            sa.insert(learnings).values(
                topic="purge probe",
                insight="written by test_unit_project_keys_are_purged",
                project_key=key,
                source_type="experience",
                confidence="low",
            )
        )

    async with _engine.connect() as conn:
        seeded = await conn.scalar(
            sa.select(sa.func.count()).select_from(learnings).where(learnings.c.project_key == key)
        )
    # Without this assertion, a silently lost insert would make the test green on
    # nothing — the purge would then have had nothing to erase.
    assert seeded == 1, (
        "la ligne témoin n'a pas été écrite, le test suivant serait vert sur du vide"
    )

    async with _engine.begin() as conn:
        await purge_integration_rows(conn)

    async with _engine.connect() as conn:
        remaining = await conn.scalar(
            sa.select(sa.func.count()).select_from(learnings).where(learnings.c.project_key == key)
        )
    assert remaining == 0, (
        f"la clé {key!r} a survécu à la purge : le préfixe des tests unitaires "
        "n'est pas couvert par _INTEGRATION_PROJECT_PREDICATE"
    )
