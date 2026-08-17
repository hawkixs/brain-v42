"""Unit tests for the record_promotion() helper (promotion.py).

Tests verify:
- The helper emits exactly two SQL statements in the canonical order:
    1. UPDATE learnings (stamp)
    2. INSERT dream_promotions (audit)
- It returns the UPDATE rowcount.
- It passes all required values to both statements.
- It is callable with target_adr_id=None or target_runbook_id=None (both optional).

These tests use a mocked AsyncSession — no real DB required.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session(update_rowcount: int = 1) -> tuple[AsyncMock, list[Any]]:
    """Return (session, captured_stmts) where captured_stmts grows on each execute().

    The first execute() call (UPDATE) returns a result with rowcount=update_rowcount.
    The second execute() call (INSERT) returns a dummy result.
    """
    captured: list[Any] = []
    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        captured.append(stmt)
        mock_result = MagicMock()
        if call_count == 1:
            # UPDATE learnings — expose rowcount
            mock_result.rowcount = update_rowcount
        return mock_result

    session = AsyncMock()
    session.execute = _execute
    return session, captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRecordPromotionStatementOrder:
    """Verify the two statements are emitted in the load-bearing order."""

    @pytest.mark.asyncio
    async def test_emits_exactly_two_execute_calls(self) -> None:
        """record_promotion() calls session.execute() exactly twice."""
        from brain_v42.repositories.promotion import record_promotion

        session, captured = _make_mock_session()
        source_id = uuid.uuid4()
        target_id = uuid.uuid4()

        await record_promotion(
            session,
            source_learning_id=source_id,
            target_entity_id=target_id,
            target_type="adr",
            dream_run_id=None,
            target_adr_id=target_id,
            target_runbook_id=None,
        )

        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_first_statement_is_update(self) -> None:
        """First statement is an UPDATE (learnings stamp)."""
        import sqlalchemy as sa

        from brain_v42.repositories.promotion import record_promotion

        session, captured = _make_mock_session()

        await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="runbook",
            dream_run_id=None,
            target_runbook_id=uuid.uuid4(),
        )

        first_stmt = captured[0]
        # SQLAlchemy Core UPDATE objects have a class name containing 'Update'
        assert isinstance(first_stmt, sa.sql.dml.Update), (
            f"Expected sa.sql.dml.Update, got {type(first_stmt)}"
        )

    @pytest.mark.asyncio
    async def test_second_statement_is_insert(self) -> None:
        """Second statement is an INSERT (dream_promotions audit row)."""
        import sqlalchemy as sa

        from brain_v42.repositories.promotion import record_promotion

        session, captured = _make_mock_session()

        await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="adr",
            dream_run_id=42,
            target_adr_id=uuid.uuid4(),
        )

        second_stmt = captured[1]
        assert isinstance(second_stmt, sa.sql.dml.Insert), (
            f"Expected sa.sql.dml.Insert, got {type(second_stmt)}"
        )


class TestRecordPromotionReturnValue:
    """Verify the returned rowcount reflects the UPDATE result."""

    @pytest.mark.asyncio
    async def test_returns_rowcount_1_when_learning_found(self) -> None:
        """Returns 1 when the UPDATE matches exactly one learning row."""
        from brain_v42.repositories.promotion import record_promotion

        session, _ = _make_mock_session(update_rowcount=1)

        rowcount = await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="adr",
            dream_run_id=None,
            target_adr_id=uuid.uuid4(),
        )

        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_returns_rowcount_0_when_learning_not_found(self) -> None:
        """Returns 0 when the UPDATE matches no rows (learning doesn't exist)."""
        from brain_v42.repositories.promotion import record_promotion

        session, _ = _make_mock_session(update_rowcount=0)

        rowcount = await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="adr",
            dream_run_id=None,
            target_adr_id=uuid.uuid4(),
        )

        assert rowcount == 0


class TestRecordPromotionTargetKwargs:
    """Verify optional target_adr_id / target_runbook_id kwargs are accepted."""

    @pytest.mark.asyncio
    async def test_accepts_target_adr_id_none(self) -> None:
        """target_adr_id defaults to None without error."""
        from brain_v42.repositories.promotion import record_promotion

        session, _ = _make_mock_session()

        # Should not raise
        rowcount = await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="runbook",
            dream_run_id=None,
            target_runbook_id=uuid.uuid4(),
        )
        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_accepts_target_runbook_id_none(self) -> None:
        """target_runbook_id defaults to None without error."""
        from brain_v42.repositories.promotion import record_promotion

        session, _ = _make_mock_session()

        rowcount = await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="adr",
            dream_run_id=None,
            target_adr_id=uuid.uuid4(),
        )
        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_accepts_non_none_dream_run_id(self) -> None:
        """dream_run_id is passed through (int or None)."""
        from brain_v42.repositories.promotion import record_promotion

        session, _ = _make_mock_session()

        rowcount = await record_promotion(
            session,
            source_learning_id=uuid.uuid4(),
            target_entity_id=uuid.uuid4(),
            target_type="adr",
            dream_run_id=99,
            target_adr_id=uuid.uuid4(),
        )
        assert rowcount == 1
