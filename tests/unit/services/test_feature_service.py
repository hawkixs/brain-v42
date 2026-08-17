"""Tests for FeatureService (stale_pinned query)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.services.feature_service import FeatureService

# ---------------------------------------------------------------------------
# SQLite-compatible table definition (no pgvector, no PG-specific types).
# Mirrors brain_v42.db.tables.features columns used by FeatureService.
# ---------------------------------------------------------------------------
_META = MetaData()
_features = Table(
    "features",
    _META,
    Column("id", String(36), primary_key=True),
    Column("project_key", String(50), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("status_updated_at", DateTime(timezone=True), nullable=False),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_META.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_feature(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_key: str,
    name: str,
    status: str,
    pinned: bool = False,
    updated_at: datetime | None = None,
) -> None:
    now = datetime.now(tz=UTC)
    async with factory() as session:
        await session.execute(
            sa.insert(_features).values(
                id=str(uuid4()),
                project_key=project_key,
                name=name,
                description="",
                status=status,
                status_updated_at=now,
                pinned=pinned,
                created_at=now,
                updated_at=updated_at or now,
            )
        )
        await session.commit()


class TestStalePinned:
    @pytest.mark.asyncio
    async def test_returns_pinned_older_than_threshold(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        recent = datetime.now(tz=UTC) - timedelta(days=5)
        await _insert_feature(
            session_factory,
            project_key="p",
            name="stale",
            status="in_progress",
            pinned=True,
            updated_at=old,
        )
        await _insert_feature(
            session_factory,
            project_key="p",
            name="fresh",
            status="in_progress",
            pinned=True,
            updated_at=recent,
        )

        svc = FeatureService(session_factory, table=_features)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert [f.name for f in result] == ["stale"]

    @pytest.mark.asyncio
    async def test_excludes_unpinned(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        await _insert_feature(
            session_factory,
            project_key="p",
            name="unpinned",
            status="in_progress",
            pinned=False,
            updated_at=old,
        )

        svc = FeatureService(session_factory, table=_features)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_orders_oldest_first(self, session_factory):
        d50 = datetime.now(tz=UTC) - timedelta(days=50)
        d40 = datetime.now(tz=UTC) - timedelta(days=40)
        await _insert_feature(
            session_factory,
            project_key="p",
            name="d40",
            status="planned",
            pinned=True,
            updated_at=d40,
        )
        await _insert_feature(
            session_factory,
            project_key="p",
            name="d50",
            status="planned",
            pinned=True,
            updated_at=d50,
        )

        svc = FeatureService(session_factory, table=_features)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert [f.name for f in result] == ["d50", "d40"]

    @pytest.mark.asyncio
    async def test_filters_by_project_key(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        await _insert_feature(
            session_factory,
            project_key="p1",
            name="X",
            status="planned",
            pinned=True,
            updated_at=old,
        )
        await _insert_feature(
            session_factory,
            project_key="p2",
            name="Y",
            status="planned",
            pinned=True,
            updated_at=old,
        )

        svc = FeatureService(session_factory, table=_features)
        result = await svc.stale_pinned(project_key="p1", stale_days=30)
        assert [f.name for f in result] == ["X"]

    @pytest.mark.asyncio
    async def test_excludes_archived_even_if_pinned_and_stale(self, session_factory):
        """stale_pinned must skip archived features — they are intentionally inactive.

        After the purge run, ~500 features get archived with pinned=True and a
        recent status_updated_at. Without the filter they would flood the briefing
        section every session until their updated_at aged out 30+ days later.
        """
        old = datetime.now(tz=UTC) - timedelta(days=45)
        await _insert_feature(
            session_factory,
            project_key="p",
            name="archived-old",
            status="archived",
            pinned=True,
            updated_at=old,
        )
        await _insert_feature(
            session_factory,
            project_key="p",
            name="alive-old",
            status="planned",
            pinned=True,
            updated_at=old,
        )

        svc = FeatureService(session_factory, table=_features)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        names = [f.name for f in result]
        assert "archived-old" not in names
        assert "alive-old" in names
