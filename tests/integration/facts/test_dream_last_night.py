"""Real PostgreSQL evidence for the Dream latest-night fact."""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.facts import FactRegistry, FactTarget, Measured, SourceIdentity
from brain_v42.facts.probes.dream_last_night import DreamLastNightProbe
from brain_v42.facts.sources import PostgresSourceFactory
from brain_v42.repositories.pg_dream_runs import read_last_night

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _measured_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> SourceIdentity:
    """Use the source's own transaction to declare the identity this test expects."""
    async with PostgresSourceFactory(session_factory)() as source:
        return await source.identity()


async def _insert_test_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[int]:
    """Insert the one latest night and its older control row, retaining only owned identifiers."""
    rows = (
        {
            "run_date": date(2026, 9, 19),
            "phase": "factsdone",
            "status": "done",
            "phase_dry_run": False,
            "project_key": "facts-dream-last-night-a",
        },
        {
            "run_date": date(2026, 9, 19),
            "phase": "factsfail",
            "status": "fail",
            "phase_dry_run": True,
            "project_key": "facts-dream-last-night-b",
        },
        {
            "run_date": date(2026, 9, 19),
            "phase": "factstime",
            "status": "timeout",
            "phase_dry_run": None,
            "project_key": "facts-dream-last-night-a",
        },
        {
            "run_date": date(2026, 9, 18),
            "phase": "factsold",
            "status": "done",
            "phase_dry_run": False,
            "project_key": "facts-dream-last-night-old",
        },
    )
    inserted: list[int] = []
    async with session_factory() as session:
        for row in rows:
            result = await session.execute(
                sa.text(
                    "INSERT INTO dream_runs "
                    "(run_date, phase, status, phase_dry_run, project_key) "
                    "VALUES (:run_date, :phase, :status, :phase_dry_run, :project_key) "
                    "RETURNING id"
                ),
                row,
            )
            inserted.append(int(result.scalar_one()))
        await session.commit()
    return inserted


async def _delete_test_rows(
    session_factory: async_sessionmaker[AsyncSession], ids: list[int]
) -> None:
    """Remove only rows this test inserted, even when the assertion fails."""
    async with session_factory() as session:
        for row_id in ids:
            await session.execute(sa.text("DELETE FROM dream_runs WHERE id = :id"), {"id": row_id})
        await session.commit()


async def test_latest_night_aggregates_and_caches_through_the_verified_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fact counts all latest-date rows while excluding legacy NULL mode values."""
    inserted = await _insert_test_rows(session_factory)
    try:
        async with session_factory() as session:
            night = await read_last_night(session)

        assert night is not None
        assert night.run_date == date(2026, 9, 19)
        assert night.rows == 3
        assert night.done == 1
        assert night.fail == 1
        assert night.timeout == 1
        assert night.partial == 0
        assert night.other == 0
        assert night.wet == 1
        assert night.dry == 1
        assert night.projects == 2

        expected = await _measured_identity(session_factory)
        registry = FactRegistry(
            sources={FactTarget.PRODUCTION: PostgresSourceFactory(session_factory)},
            expected={FactTarget.PRODUCTION: expected},
        )
        registry.register(DreamLastNightProbe())
        registry.freeze()
        try:
            measured = await registry.measure("dream_last_night")
            cached = await registry.measure("dream_last_night")
        finally:
            await registry.aclose()

        assert isinstance(measured, Measured)
        assert measured.value["rows"] == 3
        assert cached.source_kind == "cache"
    finally:
        await _delete_test_rows(session_factory, inserted)


async def test_empty_dream_runs_table_has_no_last_night(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh test database must leave the aggregate absent instead of fabricating zeros."""
    async with session_factory() as session:
        assert await read_last_night(session) is None
