"""The PostgreSQL source: one read-only REPEATABLE READ transaction for value and identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.sources import PostgresSourceFactory, PostgresSourceSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _measured_identity(session_factory) -> SourceIdentity:
    async with PostgresSourceFactory(session_factory)() as source:
        return await source.identity()


async def test_identity_is_read_from_the_server_not_declared(session_factory) -> None:
    """The four fields come from PostgreSQL itself, inside the source's transaction."""
    identity = await _measured_identity(session_factory)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT (SELECT system_identifier FROM pg_control_system())::text, "
                    "current_database(), inet_server_addr()::text, inet_server_port()"
                )
            )
        ).one()
    assert identity.system_identifier == row[0]
    assert identity.database == row[1]
    assert identity.server_port == row[3]
    assert row[2] is not None, "the test connection must be TCP, not a Unix socket"
    assert identity.server_addr == row[2].split("/", 1)[0]


async def test_an_insert_is_refused_by_postgresql_inside_the_source(session_factory) -> None:
    """Read-only is enforced by the database, not by a code review."""
    async with PostgresSourceFactory(session_factory)() as source:
        assert isinstance(source, PostgresSourceSession)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await source.session.execute(
                sa.text("INSERT INTO projects (project_key) VALUES ('facts-probe-write')")
            )


async def test_the_source_sees_one_snapshot_for_value_and_identity(session_factory) -> None:
    """REPEATABLE READ: a row committed after the transaction began stays invisible."""
    key = f"facts-snapshot-{datetime.now(UTC).strftime('%H%M%S%f')}"
    try:
        async with PostgresSourceFactory(session_factory)() as source:
            before = await source.session.execute(
                sa.text("SELECT count(*) FROM projects WHERE project_key = :k"), {"k": key}
            )
            assert before.scalar_one() == 0
            async with session_factory() as writer:
                await writer.execute(
                    sa.text("INSERT INTO projects (project_key) VALUES (:k)"), {"k": key}
                )
                await writer.commit()
            after = await source.session.execute(
                sa.text("SELECT count(*) FROM projects WHERE project_key = :k"), {"k": key}
            )
            assert after.scalar_one() == 0
            await source.identity()  # still inside the same transaction
    finally:
        async with session_factory() as cleaner:
            await cleaner.execute(
                sa.text("DELETE FROM projects WHERE project_key = :k"), {"k": key}
            )
            await cleaner.commit()


class _OneProbe:
    name = "one"
    definition_version = 1
    target = FactTarget.PRODUCTION
    ttl = timedelta(seconds=15)
    timeout = timedelta(seconds=3)
    briefing = True
    policies: dict[str, int] = {}
    value_schema = {"one": "int"}

    async def measure(self, source: PostgresSourceSession) -> dict[str, object]:
        return {"one": (await source.session.execute(sa.text("SELECT 1"))).scalar_one()}


def _registry(session_factory, expected: SourceIdentity) -> FactRegistry:
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: PostgresSourceFactory(session_factory)},
        expected={FactTarget.PRODUCTION: expected},
    )
    registry.register(_OneProbe())
    registry.freeze()
    return registry


async def test_a_declared_identity_that_matches_yields_a_measured_value(session_factory) -> None:
    identity = await _measured_identity(session_factory)
    registry = _registry(session_factory, identity)
    try:
        result = await registry.measure("one")
    finally:
        await registry.aclose()
    assert isinstance(result, Measured)
    assert result.value == {"one": 1}
    assert result.source == identity


@pytest.mark.parametrize(
    "field,other",
    [
        ("database", "brain_test_optout"),
        ("server_port", 5433),
        ("system_identifier", "1"),
        ("server_addr", "10.0.0.1"),
    ],
)
async def test_a_declared_identity_that_differs_on_one_field_is_a_target_mismatch(
    session_factory, field: str, other: object
) -> None:
    """The 2026-09-12 failure mode: the wrong database can never be measured as production."""
    identity = await _measured_identity(session_factory)
    declared = SourceIdentity(**{**identity.as_dict(), field: other})
    registry = _registry(session_factory, declared)
    try:
        result = await registry.measure("one")
    finally:
        await registry.aclose()
    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"
