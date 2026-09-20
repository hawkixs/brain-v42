"""Real PostgreSQL evidence for the graph projection lag fact."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.facts import FactRegistry, FactTarget, Measured, SourceIdentity
from brain_v42.facts.probes import GraphProjectionLagProbe
from brain_v42.facts.sources import PostgresSourceFactory

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@dataclass
class _ProjectionFixture:
    """Remember only test-owned rows and singleton state that cleanup must restore."""

    undelivered_ids: list[int]
    lease: dict[str, Any] | None
    entity_ids: list[UUID] = field(default_factory=list)


async def _measured_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> SourceIdentity:
    """Derive expected identity from the actual source, never a DSN declaration."""
    async with PostgresSourceFactory(session_factory)() as source:
        return await source.identity()


async def _measure_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    """Exercise the registry and source boundary used by a real fact reader."""
    expected = await _measured_identity(session_factory)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: PostgresSourceFactory(session_factory)},
        expected={FactTarget.PRODUCTION: expected},
    )
    registry.register(GraphProjectionLagProbe())
    registry.freeze()
    try:
        result = await registry.measure("graph_projection_lag")
    finally:
        await registry.aclose()
    assert isinstance(result, Measured)
    return result.value


@asynccontextmanager
async def _projection_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    generation: int | None = 42,
    armed: bool = True,
    lease_active: bool = True,
    recovery_active: bool = False,
) -> AsyncIterator[_ProjectionFixture]:
    """Isolate singleton state while preserving unrelated test-database rows afterward."""
    async with session_factory() as session:
        undelivered_ids = list(
            (
                await session.execute(
                    sa.text("SELECT id FROM graph_outbox WHERE delivered_at IS NULL")
                )
            ).scalars()
        )
        lease = (
            (
                await session.execute(
                    sa.text(
                        "SELECT slot, protocol_version, generation, owner, leased_until, "
                        "neo4j_armed_generation, updated_at, recovery_id, recovery_phase, "
                        "last_completed_recovery_id "
                        "FROM graph_projection_leases WHERE slot = 'neo4j'"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        fixture = _ProjectionFixture(
            undelivered_ids=undelivered_ids,
            lease=None if lease is None else dict(lease),
        )
        await session.execute(sa.text("UPDATE graph_outbox SET delivered_at = clock_timestamp()"))
        await session.execute(sa.text("DELETE FROM graph_projection_leases WHERE slot = 'neo4j'"))
        if generation is not None:
            recovery_id = uuid4() if recovery_active else None
            recovery_phase = "neo_ready" if recovery_active else "idle"
            await session.execute(
                sa.text(
                    "INSERT INTO graph_projection_leases ("
                    "slot, protocol_version, generation, owner, leased_until, "
                    "neo4j_armed_generation, recovery_id, recovery_phase, "
                    "last_completed_recovery_id"
                    ") VALUES ("
                    "'neo4j', 2, :generation, :owner, "
                    "clock_timestamp() + (:lease_seconds * INTERVAL '1 second'), "
                    ":armed_generation, :recovery_id, :recovery_phase, NULL"
                    ")"
                ),
                {
                    "generation": generation,
                    "owner": "facts-probe" if lease_active or recovery_active else "facts-probe",
                    "lease_seconds": 300 if lease_active else -300,
                    "armed_generation": generation if armed else None,
                    "recovery_id": recovery_id,
                    "recovery_phase": recovery_phase,
                },
            )
        await session.commit()

    try:
        yield fixture
    finally:
        async with session_factory() as session:
            for entity_id in fixture.entity_ids:
                await session.execute(
                    sa.text("DELETE FROM brain_entities WHERE id = :entity_id"),
                    {"entity_id": entity_id},
                )
            await session.execute(
                sa.text("DELETE FROM graph_projection_leases WHERE slot = 'neo4j'")
            )
            if fixture.lease is not None:
                await session.execute(
                    sa.text(
                        "INSERT INTO graph_projection_leases ("
                        "slot, protocol_version, generation, owner, leased_until, "
                        "neo4j_armed_generation, updated_at, recovery_id, recovery_phase, "
                        "last_completed_recovery_id"
                        ") VALUES ("
                        ":slot, :protocol_version, :generation, :owner, :leased_until, "
                        ":neo4j_armed_generation, :updated_at, :recovery_id, :recovery_phase, "
                        ":last_completed_recovery_id"
                        ")"
                    ),
                    fixture.lease,
                )
            for outbox_id in fixture.undelivered_ids:
                await session.execute(
                    sa.text("UPDATE graph_outbox SET delivered_at = NULL WHERE id = :outbox_id"),
                    {"outbox_id": outbox_id},
                )
            await session.commit()


async def _add_outbox_row(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: _ProjectionFixture,
    *,
    age_seconds: int,
    error_code: str | None = None,
) -> None:
    """Seed one directly observed outbox row with a unique referential anchor."""
    entity_id = uuid4()
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO brain_entities (id, entity_type, entity_key, scope_kind) "
                "VALUES (:entity_id, 'decision', :entity_key, 'global')"
            ),
            {
                "entity_id": entity_id,
                "entity_key": f"facts-projection-lag-{entity_id}",
            },
        )
        await session.execute(
            sa.text(
                "INSERT INTO graph_outbox ("
                "entity_id, aggregate_revision, operation, available_at, delivered_at, "
                "last_error_code, created_at"
                ") VALUES ("
                ":entity_id, 1, 'upsert_entity', clock_timestamp(), NULL, "
                ":error_code, clock_timestamp() - (:age_seconds * INTERVAL '1 second')"
                ")"
            ),
            {
                "entity_id": entity_id,
                "error_code": error_code,
                "age_seconds": age_seconds,
            },
        )
        await session.commit()
    fixture.entity_ids.append(entity_id)


async def test_empty_outbox_with_armed_lease_is_healthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An armed active singleton lease reports a zero integer lag with no work."""
    async with _projection_state(session_factory):
        value = await _measure_value(session_factory)

    assert value["lag_seconds"] == 0
    assert type(value["lag_seconds"]) is int
    assert value["healthy"] is True


async def test_undelivered_row_reports_its_oldest_age_in_whole_seconds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fact makes a six-hundred-second projection delay visible as an integer."""
    async with _projection_state(session_factory) as fixture:
        await _add_outbox_row(session_factory, fixture, age_seconds=600)
        value = await _measure_value(session_factory)

    assert value["lag_seconds"] >= 600
    assert type(value["lag_seconds"]) is int


async def test_unarmed_lease_is_unhealthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lease without an armed generation cannot assert healthy projection progress."""
    async with _projection_state(session_factory, armed=False):
        value = await _measure_value(session_factory)

    assert value["armed"] is False
    assert value["healthy"] is False
    assert type(value["lag_seconds"]) is int


async def test_missing_lease_keeps_generation_null_and_is_unhealthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh database must not fabricate a generation or healthy projector."""
    async with _projection_state(session_factory, generation=None):
        value = await _measure_value(session_factory)

    assert value["generation"] is None
    assert value["healthy"] is False
    assert type(value["lag_seconds"]) is int


async def test_expired_lease_is_not_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lease in the past cannot be mistaken for an actively held projector lease."""
    async with _projection_state(session_factory, lease_active=False):
        value = await _measure_value(session_factory)

    assert value["lease_active"] is False
    assert type(value["lag_seconds"]) is int


async def test_recovery_lease_is_reported_as_active_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Recovery interlock remains a measured fact rather than an inferred outage."""
    async with _projection_state(session_factory, recovery_active=True):
        value = await _measure_value(session_factory)

    assert value["recovery_active"] is True
    assert type(value["lag_seconds"]) is int


async def test_exhausted_rows_are_loud_but_not_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The shared SQL preserves metrics' exclusion of exhausted work from pending."""
    async with _projection_state(session_factory) as fixture:
        await _add_outbox_row(session_factory, fixture, age_seconds=60, error_code="max_attempts")
        value = await _measure_value(session_factory)

    assert value["exhausted"] >= 1
    assert value["pending"] == 0
    assert type(value["lag_seconds"]) is int
