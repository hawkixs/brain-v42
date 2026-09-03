"""Unit tests for PgAccessLogRepo — aggregate + purge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.repositories.pg_access_log import PgAccessLogRepo


@pytest.fixture
def repo() -> PgAccessLogRepo:
    return PgAccessLogRepo(session_factory=MagicMock())


class TestPurgeOld:
    @pytest.mark.asyncio
    async def test_purge_old_executes_delete(self) -> None:
        """purge_old deletes entries older than N days."""
        session = AsyncMock()
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        await repo.purge_old(days=30)

        session.execute.assert_called_once()
        session.commit.assert_called_once()


class TestRowCount:
    @pytest.mark.asyncio
    async def test_row_count_returns_integer(self) -> None:
        """row_count returns the number of rows in access_log."""
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 42
        session.execute = AsyncMock(return_value=count_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        result = await repo.row_count()

        assert result == 42
