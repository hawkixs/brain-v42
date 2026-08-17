"""TDD tests for SKIP_COUNT FTS optimization in BasePgRepository.search_fts.

Workstream: repos-perf (2026-07-02)
Chantier 2: search_fts must accept skip_count parameter; when True, avoid the COUNT query.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

_META2 = MetaData()

_FTS_TABLE = Table(
    "fts_items",
    _META2,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("name", String(100)),
    Column("search_vector", sa.Text),
    Column("embedding", sa.Text),
    Column("created_at", sa.DateTime(timezone=True), nullable=False),
    Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings
    mock_mappings.all.return_value = []
    mock_result.scalar_one.return_value = 0
    session.execute = AsyncMock(return_value=mock_result)
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _patch_factory(mock_session: AsyncMock):
    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()
    return patch(
        "brain_v42.repositories.pg_base.get_session_factory",
        return_value=mock_factory,
    )


@pytest.fixture
def fts_repo():
    from brain_v42.repositories.pg_base import BasePgRepository

    class FTSRepo(BasePgRepository):
        table = _FTS_TABLE

    return FTSRepo()


# ===========================================================================
# Tests
# ===========================================================================


class TestSearchFtsSkipCount:
    @pytest.mark.asyncio
    async def test_search_fts_has_skip_count_parameter(self, fts_repo):
        """search_fts() must accept skip_count keyword argument (default False)."""
        import inspect

        sig = inspect.signature(fts_repo.search_fts)
        assert "skip_count" in sig.parameters, "search_fts must have skip_count parameter"

    @pytest.mark.asyncio
    async def test_search_fts_default_skip_count_false_executes_count_query(self, fts_repo):
        """search_fts(skip_count=False, default) executes 2 queries: COUNT + SELECT."""
        mock_session = _make_mock_session()
        execute_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal execute_count
            execute_count += 1
            mock_result = MagicMock()
            if execute_count == 1:
                mock_result.scalar_one.return_value = 1
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await fts_repo.search_fts("hello")

        assert execute_count == 2, f"Expected 2 queries (COUNT + SELECT), got {execute_count}"
        assert total == 1

    @pytest.mark.asyncio
    async def test_search_fts_skip_count_true_skips_count_query(self, fts_repo):
        """search_fts(skip_count=True) executes only 1 query (no COUNT scan)."""
        mock_session = _make_mock_session()
        execute_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal execute_count
            execute_count += 1
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await fts_repo.search_fts("hello", skip_count=True)

        assert execute_count == 1, f"Expected 1 query (SELECT only), got {execute_count}"

    @pytest.mark.asyncio
    async def test_search_fts_skip_count_true_returns_zero_total(self, fts_repo):
        """search_fts(skip_count=True) returns total=0 (sentinel for 'not counted')."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await fts_repo.search_fts("hello", skip_count=True)

        assert total == 0

    @pytest.mark.asyncio
    async def test_search_fts_skip_count_true_still_returns_results(self, fts_repo):
        """search_fts(skip_count=True) still returns rows from the SELECT."""
        import uuid as _uuid

        mock_session = _make_mock_session()
        row = {"id": _uuid.uuid4(), "name": "found", "rank": 0.9}

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await fts_repo.search_fts("found", skip_count=True)

        assert len(items) == 1
        assert total == 0
