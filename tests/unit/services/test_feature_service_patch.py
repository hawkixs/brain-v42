"""Behavior tests for the Codex-facing partial feature mutation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.services.feature_service import FeatureService, FeatureStateConflictError

_METADATA = sa.MetaData()
_FEATURES = sa.Table(
    "features",
    _METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("project_key", sa.String(50), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("pinned", sa.Boolean, nullable=False),
    sa.Column("merged_into", sa.Uuid(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
_PROJECT_CONTEXTS = sa.Table(
    "project_contexts",
    _METADATA,
    sa.Column("project_key", sa.String(50), primary_key=True),
    sa.Column("project_group", sa.String(50)),
)


def test_feature_service_exposes_partial_patch_contract() -> None:
    assert hasattr(FeatureService, "patch")


@pytest.fixture
async def feature_store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(_METADATA.create_all)
        await connection.execute(
            _PROJECT_CONTEXTS.insert().values(project_key="red-codex", project_group="red")
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_feature(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "planned",
    pinned: bool = False,
    merged_into: UUID | None = None,
) -> tuple[UUID, datetime]:
    feature_id = uuid4()
    old_status_at = datetime.now(UTC) - timedelta(days=3)
    now = datetime.now(UTC)
    async with factory() as session:
        await session.execute(
            _FEATURES.insert().values(
                id=feature_id,
                project_key="red-codex",
                name="Gateway",
                description="Management API",
                status=status,
                status_updated_at=old_status_at,
                pinned=pinned,
                merged_into=merged_into,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return feature_id, old_status_at


@pytest.mark.asyncio
async def test_patch_status_reuses_canonical_auto_pin_semantics(feature_store) -> None:
    feature_id, old_status_at = await _seed_feature(feature_store)
    service = FeatureService(feature_store, table=_FEATURES)

    updated = await service.patch(feature_id, status="building")

    assert updated is not None
    assert updated.status == "building"
    assert updated.pinned is True
    assert updated.status_updated_at.replace(tzinfo=UTC) > old_status_at


@pytest.mark.asyncio
async def test_patch_explicit_pin_overrides_status_auto_pin(feature_store) -> None:
    feature_id, _ = await _seed_feature(feature_store, pinned=True)
    service = FeatureService(feature_store, table=_FEATURES)

    updated = await service.patch(feature_id, status="deployed", pinned=False)

    assert updated is not None
    assert updated.status == "deployed"
    assert updated.pinned is False


@pytest.mark.asyncio
async def test_patch_archived_alias_sets_archived_without_adding_a_pin(feature_store) -> None:
    feature_id, _ = await _seed_feature(feature_store, pinned=False)
    service = FeatureService(feature_store, table=_FEATURES)

    updated = await service.patch(feature_id, archived=True)

    assert updated is not None
    assert updated.status == "archived"
    assert updated.pinned is False


@pytest.mark.asyncio
async def test_patch_pin_only_preserves_status_timestamp(feature_store) -> None:
    feature_id, old_status_at = await _seed_feature(feature_store)
    service = FeatureService(feature_store, table=_FEATURES)

    updated = await service.patch(feature_id, pinned=True)

    assert updated is not None
    assert updated.status == "planned"
    assert updated.pinned is True
    assert updated.status_updated_at.replace(tzinfo=UTC) == old_status_at


@pytest.mark.asyncio
async def test_patch_returns_none_for_unknown_feature(feature_store) -> None:
    service = FeatureService(feature_store, table=_FEATURES)

    assert await service.patch(uuid4(), pinned=True) is None


@pytest.mark.asyncio
async def test_patch_rejects_empty_or_invalid_mutations(feature_store) -> None:
    feature_id, _ = await _seed_feature(feature_store)
    service = FeatureService(feature_store, table=_FEATURES)

    with pytest.raises(ValueError, match="at least one"):
        await service.patch(feature_id)
    with pytest.raises(ValueError, match="invalid feature status"):
        await service.patch(feature_id, status="in_progress")


@pytest.mark.asyncio
async def test_patch_accepts_an_optional_project_group_scope(feature_store) -> None:
    service = FeatureService(feature_store, table=_FEATURES)

    assert await service.patch(uuid4(), pinned=True, project_group="red") is None


@pytest.mark.asyncio
async def test_patch_cannot_reactivate_a_merged_feature(feature_store) -> None:
    winner = uuid4()
    feature_id, _ = await _seed_feature(
        feature_store,
        status="archived",
        merged_into=winner,
    )
    service = FeatureService(feature_store, table=_FEATURES)

    with pytest.raises(FeatureStateConflictError, match="merged"):
        await service.patch(feature_id, status="building")

    async with feature_store() as session:
        row = (
            await session.execute(
                sa.select(_FEATURES.c.status, _FEATURES.c.merged_into).where(
                    _FEATURES.c.id == feature_id
                )
            )
        ).one()
    assert row.status == "archived"
    assert row.merged_into == winner
