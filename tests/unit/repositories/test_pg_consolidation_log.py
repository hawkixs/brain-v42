"""Unit tests for PgConsolidationLogRepo — track handled duplicate pairs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo


class TestGetHandledPairs:
    @pytest.mark.asyncio
    async def test_returns_set_of_tuples_both_directions(self) -> None:
        """get_handled_pairs returns set with both (a,b) and (b,a)."""
        id_a = uuid4()
        id_b = uuid4()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            {"source_id": id_a, "target_id": id_b},
        ]
        session.execute = AsyncMock(return_value=mock_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgConsolidationLogRepo(session_factory=session_factory)
        result = await repo.get_handled_pairs("decision")

        assert (id_a, id_b) in result
        assert (id_b, id_a) in result
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_no_rows(self) -> None:
        """get_handled_pairs returns empty set when no rows exist."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgConsolidationLogRepo(session_factory=session_factory)
        result = await repo.get_handled_pairs("learning")

        assert result == set()

    @pytest.mark.asyncio
    async def test_multiple_pairs(self) -> None:
        """get_handled_pairs with multiple rows returns all directions."""
        id_a, id_b = uuid4(), uuid4()
        id_c, id_d = uuid4(), uuid4()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            {"source_id": id_a, "target_id": id_b},
            {"source_id": id_c, "target_id": id_d},
        ]
        session.execute = AsyncMock(return_value=mock_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgConsolidationLogRepo(session_factory=session_factory)
        result = await repo.get_handled_pairs("snippet")

        assert len(result) == 4
        assert (id_a, id_b) in result
        assert (id_b, id_a) in result
        assert (id_c, id_d) in result
        assert (id_d, id_c) in result


class TestLogAction:
    @pytest.mark.asyncio
    async def test_inserts_record(self) -> None:
        """log_action inserts a record and commits."""
        session = AsyncMock()

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgConsolidationLogRepo(session_factory=session_factory)
        src_id, tgt_id = uuid4(), uuid4()

        await repo.log_action(
            source_id=src_id,
            target_id=tgt_id,
            entity_type="decision",
            similarity=0.95,
            action="merged",
        )

        session.execute.assert_called_once()
        session.commit.assert_called_once()
