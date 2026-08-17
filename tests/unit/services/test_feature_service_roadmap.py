"""Tests for FeatureService.roadmap_alive (mocked session — SQL is PG-only)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services.feature_service import FeatureService, RoadmapAliveFeature


def _factory_with_rows(rows: list[dict]):
    mock_session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    mock_session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory, mock_session


class TestRoadmapAlive:
    @pytest.mark.asyncio
    async def test_maps_rows_to_dataclass(self):
        rows = [
            {
                "name": "Recherche hybride",
                "status": "building",
                "pinned": True,
                "artifact_count": 7,
                "last_artifact_at": datetime(2026, 7, 2, tzinfo=UTC),
            },
            {
                "name": "Cluster X",
                "status": "research",
                "pinned": False,
                "artifact_count": 0,
                "last_artifact_at": None,
            },
        ]
        factory, _ = _factory_with_rows(rows)
        svc = FeatureService(factory)
        items = await svc.roadmap_alive("brain-v42", limit=5)
        assert items == [
            RoadmapAliveFeature(
                name="Recherche hybride",
                status="building",
                pinned=True,
                artifact_count=7,
                last_artifact_at=datetime(2026, 7, 2, tzinfo=UTC),
            ),
            RoadmapAliveFeature(
                name="Cluster X",
                status="research",
                pinned=False,
                artifact_count=0,
                last_artifact_at=None,
            ),
        ]

    @pytest.mark.asyncio
    async def test_query_filters_alive_and_orders_pinned_first(self):
        factory, session = _factory_with_rows([])
        svc = FeatureService(factory)
        await svc.roadmap_alive("brain-v42")
        sql = str(session.execute.call_args[0][0])
        assert "NOT IN ('done', 'archived')" in sql
        assert "merged_into IS NULL" in sql
        assert "pinned" in sql
        assert "NULLS LAST" in sql

    @pytest.mark.asyncio
    async def test_in_flight_removed(self):
        assert not hasattr(FeatureService, "in_flight")
