"""TDD tests for ADR _next_number advisory lock (race condition fix).

Workstream: repos-perf (2026-07-02)
Chantier 3: _next_number must acquire pg_advisory_xact_lock before MAX+1 to prevent
            duplicate number races under READ COMMITTED isolation.

Strategy: capture the SQL text strings passed to session.execute and assert that
          the advisory lock statement is executed BEFORE the MAX+1 query.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_adr_repo() -> Any:
    from brain_v42.repositories.pg_adr import PgADRRepo

    mock_factory = MagicMock()
    return PgADRRepo(session_factory=mock_factory)


class TestNextNumberAdvisoryLock:
    @pytest.mark.asyncio
    async def test_next_number_acquires_advisory_lock_before_max_query(self):
        """_next_number must call pg_advisory_xact_lock before COALESCE(MAX, 0)+1.

        The lock protects against two concurrent transactions both reading
        the same MAX and inserting the same number under READ COMMITTED.
        """
        from brain_v42.repositories.pg_adr import PgADRRepo

        captured_sqls: list[str] = []
        mock_session = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            captured_sqls.append(str(stmt))
            mock_result = MagicMock()
            mock_result.scalar_one.return_value = 1
            return mock_result

        mock_session.execute = mock_execute

        mock_factory = MagicMock()
        repo = PgADRRepo(session_factory=mock_factory)
        await repo._next_number(mock_session, "proj1")

        assert len(captured_sqls) >= 2, (
            f"Expected at least 2 SQL statements (lock + MAX query), got: {captured_sqls}"
        )
        lock_idx = next(
            (i for i, s in enumerate(captured_sqls) if "pg_advisory_xact_lock" in s),
            None,
        )
        max_idx = next(
            (i for i, s in enumerate(captured_sqls) if "MAX" in s.upper()),
            None,
        )
        assert lock_idx is not None, (
            f"pg_advisory_xact_lock not found in SQL statements: {captured_sqls}"
        )
        assert max_idx is not None, f"MAX query not found in SQL statements: {captured_sqls}"
        assert lock_idx < max_idx, (
            f"Advisory lock (idx={lock_idx}) must execute BEFORE MAX query (idx={max_idx}). "
            f"Statements: {captured_sqls}"
        )

    @pytest.mark.asyncio
    async def test_next_number_lock_uses_project_key_in_hash(self):
        """Advisory lock hash must incorporate project_key to scope the lock per-project.

        Two projects creating ADRs concurrently must not block each other.
        The lock key must be deterministic and project-scoped: the _ADR_LOCK_PREFIX
        constant must incorporate the project_key at call time.
        """
        from brain_v42.repositories.pg_adr import PgADRRepo

        captured_params: list[dict] = []
        mock_session = AsyncMock()

        async def mock_execute(stmt, params=None, *args, **kwargs):
            if params:
                captured_params.append(dict(params))
            mock_result = MagicMock()
            mock_result.scalar_one.return_value = 1
            return mock_result

        mock_session.execute = mock_execute

        mock_factory = MagicMock()
        repo = PgADRRepo(session_factory=mock_factory)
        await repo._next_number(mock_session, "my_project")

        # The lock SQL uses :key parameter — check the value passed includes project_key
        assert len(captured_params) >= 1, "No parameters captured for lock statement"
        lock_param = next(
            (p for p in captured_params if "key" in p),
            None,
        )
        assert lock_param is not None, (
            f"No 'key' parameter found in captured SQL params: {captured_params}"
        )
        assert "my_project" in lock_param["key"], (
            f"Lock key must include project_key 'my_project'. Got: {lock_param['key']}"
        )
        assert repo._ADR_LOCK_PREFIX in lock_param["key"], (
            f"Lock key must include prefix '{repo._ADR_LOCK_PREFIX}'. Got: {lock_param['key']}"
        )

    @pytest.mark.asyncio
    async def test_next_number_docstring_mentions_advisory_lock(self):
        """_next_number docstring must mention the advisory lock protection."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_factory = MagicMock()
        repo = PgADRRepo(session_factory=mock_factory)
        doc = repo._next_number.__doc__ or ""
        assert "lock" in doc.lower(), (
            "_next_number docstring must mention lock/locking for concurrent safety"
        )

    @pytest.mark.asyncio
    async def test_next_number_returns_integer(self):
        """_next_number must still return an integer after lock acquisition."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        call_count = 0
        mock_session = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            # First call: advisory lock (returns None typically)
            # Second call: MAX query returns 3
            if call_count >= 2:
                mock_result.scalar_one.return_value = 3
            else:
                mock_result.scalar_one.return_value = None
            return mock_result

        mock_session.execute = mock_execute

        mock_factory = MagicMock()
        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo._next_number(mock_session, "proj")
        assert isinstance(result, int)
        assert result == 3
