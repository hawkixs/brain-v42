"""Tests for the shared decay refresh service used by the Codex gateway."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def test_entity_maintenance_service_is_available() -> None:
    from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

    assert EntityMaintenanceService is not None


@pytest.fixture
async def entity_store():
    metadata = sa.MetaData()
    # This template must carry EVERY column the service writes. It had drifted:
    # `freshness_source` (migration 043) was missing, and SQLAlchemy returned
    # `CompileError: Unconsumed column names` as soon as the service started
    # declaring its provenance. A test table one writes oneself cannot detect its
    # own drift from the six real tables — the proof of the real path lives in
    # integration (`tests/integration/db/test_freshness_source_provenance.py`).
    entities = sa.Table(
        "test_entities",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("project_key", sa.String(50), nullable=False),
        sa.Column("freshness_status", sa.String(20), nullable=False),
        sa.Column("freshness_source", sa.String(16), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
    )
    project_contexts = sa.Table(
        "project_contexts",
        metadata,
        sa.Column("project_key", sa.String(50), primary_key=True),
        sa.Column("project_group", sa.String(50)),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            project_contexts.insert().values(project_key="red-codex", project_group="red")
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory, entities
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_sets_entity_fresh_and_stamps_last_access(entity_store) -> None:
    from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

    factory, entities = entity_store
    entity_id = uuid4()
    old_access = datetime.now(UTC) - timedelta(days=10)
    async with factory() as session:
        await session.execute(
            entities.insert().values(
                id=entity_id,
                project_key="red-codex",
                freshness_status="stale",
                last_accessed_at=old_access,
            )
        )
        await session.commit()
    service = EntityMaintenanceService(factory, tables={"learning": entities})

    refreshed = await service.refresh("learning", entity_id)

    assert refreshed is not None
    assert refreshed.entity_type == "learning"
    assert refreshed.entity_id == entity_id
    assert refreshed.freshness_status == "fresh"
    assert refreshed.last_accessed_at.replace(tzinfo=UTC) > old_access


@pytest.mark.asyncio
async def test_refresh_returns_none_when_entity_does_not_exist(entity_store) -> None:
    from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

    factory, entities = entity_store
    service = EntityMaintenanceService(factory, tables={"learning": entities})

    assert await service.refresh("learning", uuid4()) is None


@pytest.mark.asyncio
async def test_refresh_rejects_an_unknown_entity_type(entity_store) -> None:
    from brain_v42.services.entity_maintenance_service import (
        EntityMaintenanceService,
        UnknownEntityTypeError,
    )

    factory, entities = entity_store
    service = EntityMaintenanceService(factory, tables={"learning": entities})

    with pytest.raises(UnknownEntityTypeError, match="unknown entity type"):
        await service.refresh("feature", uuid4())


@pytest.mark.asyncio
async def test_refresh_accepts_an_optional_project_group_scope(entity_store) -> None:
    from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

    factory, entities = entity_store
    service = EntityMaintenanceService(factory, tables={"learning": entities})

    assert await service.refresh("learning", uuid4(), project_group="red") is None
