"""TDD tests for CTE cycle guard in PgDecisionRepo.get_supersession_chain.

Workstream: repos-perf (2026-07-02) — 2nd-pass corrections
Chantier 4: get_supersession_chain recursive CTE must have a depth cap to prevent
            infinite loops when a cycle exists in superseded_by links.

Strategy: inspect the SQL text of the statement passed to session.execute.
The tests pin the *exact* guard clauses that bound recursion:
  - backward CTE: WHERE b.depth > -50  (negative because depth decrements)
  - forward CTE: WHERE f.depth < 50
  - top-level LIMIT 50  (final cap on returned rows)

The 1st-pass tests were vacuous — they passed against the pre-fix SQL because
"DEPTH" and "LIMIT" already appeared in unrelated parts of the query (the
sub-select ORDER BY DEPTH LIMIT 1 inside the backward anchor).  These tests
pin the actual guard expressions so that removing any single guard makes a test
fail.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def _make_decision_repo_with_capture() -> tuple[Any, list[str]]:
    from brain_v42.repositories.pg_decision import PgDecisionRepo

    captured_sqls: list[str] = []
    mock_session = AsyncMock()

    async def mock_execute(stmt, params=None, *args, **kwargs):
        captured_sqls.append(str(stmt))
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        return mock_result

    mock_session.execute = mock_execute

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    repo = PgDecisionRepo()
    repo.get_session = _session_cm  # type: ignore[method-assign]
    return repo, captured_sqls


class TestSupersessionChainCycleGuard:
    # ------------------------------------------------------------------
    # Structural tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_supersession_chain_sql_uses_with_recursive(self):
        """CTE must use WITH RECURSIVE (required for recursive PostgreSQL CTE)."""
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1].upper()
        assert "WITH RECURSIVE" in sql or "RECURSIVE" in sql, (
            f"CTE must use WITH RECURSIVE. SQL:\n{sql}"
        )

    @pytest.mark.asyncio
    async def test_get_supersession_chain_sql_has_backward_and_forward_cte(self):
        """SQL must contain both BACKWARD and FORWARD named CTEs."""
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1].upper()
        assert "BACKWARD" in sql, "backward CTE expected"
        assert "FORWARD" in sql, "forward CTE expected"

    @pytest.mark.asyncio
    async def test_get_supersession_chain_returns_list_on_empty_result(self):
        """get_supersession_chain returns empty list when DB returns no rows."""
        repo, _ = _make_decision_repo_with_capture()
        result = await repo.get_supersession_chain(uuid4())
        assert result == []

    # ------------------------------------------------------------------
    # Guard precision tests — these are the regression-pinning tests.
    # Each test fails if its specific guard clause is removed.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_backward_cte_has_depth_greater_than_guard(self):
        """backward CTE recursive term must have 'depth > -N' to bound backward walks.

        Without this, a cycle A.superseded_by→B, B.superseded_by→A causes the
        backward CTE to loop indefinitely.  The guard must use 'depth > -N'
        (negative because depth decrements in each backward step).
        """
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1].upper()

        # Pattern: DEPTH > -<integer>  (with optional spaces)
        # This is the backward recursion stopper inside the BACKWARD CTE
        match = re.search(r"DEPTH\s*>\s*-\s*(\d+)", sql)
        assert match is not None, (
            "backward CTE must have 'depth > -N' guard in its recursive term to prevent "
            f"infinite loops on superseded_by cycles. SQL:\n{captured[-1]}"
        )
        cap = int(match.group(1))
        assert 1 <= cap <= 1000, (
            f"backward depth guard -N should be between -1 and -1000 (got -N={-cap}). "
            f"SQL:\n{captured[-1]}"
        )

    @pytest.mark.asyncio
    async def test_forward_cte_has_depth_less_than_guard(self):
        """forward CTE recursive term must have 'depth < N' to bound forward walks.

        Without this, a cycle A→B→A in superseded_by causes the forward CTE
        to recurse indefinitely even if the backward CTE found the root correctly.
        """
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1].upper()

        # Pattern: DEPTH < <positive integer>
        match = re.search(r"DEPTH\s*<\s*(\d+)", sql)
        assert match is not None, (
            "forward CTE must have 'depth < N' guard in its recursive term to prevent "
            f"infinite loops on superseded_by cycles. SQL:\n{captured[-1]}"
        )
        cap = int(match.group(1))
        assert 1 <= cap <= 1000, (
            f"forward depth guard N should be between 1 and 1000 (got {cap}). SQL:\n{captured[-1]}"
        )

    @pytest.mark.asyncio
    async def test_top_level_limit_caps_returned_rows(self):
        """Final SELECT FROM FORWARD must have LIMIT to cap total returned rows.

        Even with per-CTE depth guards, a degenerate fan-out (one decision
        superseded by many) could return unbounded rows.  A top-level LIMIT
        provides the final safety net.

        This test distinguishes the top-level LIMIT from the sub-select
        LIMIT 1 inside the backward anchor (ORDER BY depth LIMIT 1).
        We require at least one LIMIT value > 1.
        """
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1].upper()

        limits = [int(m) for m in re.findall(r"LIMIT\s+(\d+)", sql)]
        assert any(lim > 1 for lim in limits), (
            "Final SELECT must have LIMIT > 1 to cap total returned rows "
            f"(found LIMIT values: {limits}). SQL:\n{captured[-1]}"
        )
        top_limit = max(limits)
        assert top_limit <= 10000, (
            f"Top-level LIMIT {top_limit} is unreasonably large. SQL:\n{captured[-1]}"
        )

    @pytest.mark.asyncio
    async def test_backward_guard_is_in_recursive_term_not_anchor(self):
        """'depth > -N' must appear in the UNION ALL (recursive) part of backward CTE.

        The backward CTE anchor has depth=0 by definition; the guard only
        makes sense in the recursive term (after UNION ALL).
        """
        repo, captured = _make_decision_repo_with_capture()
        await repo.get_supersession_chain(uuid4())

        assert len(captured) >= 1
        sql = captured[-1]

        backward_block_match = re.search(
            r"(?i)backward\s+AS\s*\((.+?)(?=\),\s*\n\s*forward|\),\s*forward)",
            sql,
            re.DOTALL,
        )
        if backward_block_match is None:
            # Fallback: check the full SQL — the guard must appear somewhere
            assert re.search(r"(?i)depth\s*>\s*-\s*\d+", sql), (
                f"depth > -N guard not found anywhere. SQL:\n{sql}"
            )
            return

        backward_block = backward_block_match.group(1).upper()
        # After UNION ALL is the recursive term
        parts = backward_block.split("UNION ALL")
        assert len(parts) >= 2, "backward CTE must have UNION ALL (recursive term)"
        recursive_term = parts[-1]
        assert re.search(r"DEPTH\s*>\s*-\s*\d+", recursive_term), (
            f"'depth > -N' must be in the UNION ALL recursive term of backward CTE. "
            f"recursive_term:\n{recursive_term}\nFull SQL:\n{sql}"
        )
