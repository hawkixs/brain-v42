"""Knowledge repositories forward ``extra_clauses`` to the search SQL.

Ticket 73e68261: the services pass ``live_row_clauses()`` so archived/merged rows
are excluded in the WHERE, before the LIMIT. Each repository wrapper has to carry
the clauses down to the base search; a wrapper that drops them reopens the bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo

_MARKER = "zz_extra_marker"
_EMB = [0.1] * 1536

# (repo class, FTS method, FTS keyword args, vector method)
_CASES = [
    pytest.param(PgLearningRepo, "search_fts", {}, "search_vector", id="learning"),
    pytest.param(PgDecisionRepo, "search_fts", {}, "search_vector", id="decision"),
    pytest.param(PgSnippetRepo, "search", {}, "vector_search", id="snippet"),
    pytest.param(PgRunbookRepo, "search_fts", {}, "vector_search", id="runbook"),
    pytest.param(PgADRRepo, "search", {"query": "q"}, "vector_search", id="adr"),
]


def _session() -> tuple[AsyncMock, list[Any]]:
    stmts: list[Any] = []
    session = AsyncMock()

    async def execute(stmt: Any, params: Any = None) -> MagicMock:
        stmts.append(stmt)
        result = MagicMock()
        result.scalar.return_value = "0.8.1"
        result.scalar_one.return_value = 0
        result.mappings.return_value.all.return_value = []
        return result

    session.execute = execute
    return session, stmts


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(("repo_cls", "fts", "fts_kwargs", "vec"), _CASES)
class TestExtraClausesReachTheSql:
    @pytest.mark.asyncio
    async def test_fts_search_where_carries_the_clause(
        self, repo_cls: Any, fts: str, fts_kwargs: dict[str, Any], vec: str
    ) -> None:
        session, stmts = _session()
        args = () if "query" in fts_kwargs else ("q",)
        await getattr(repo_cls(), fts)(
            *args,
            **fts_kwargs,
            extra_clauses=[sa.literal_column(_MARKER)],
            session=session,
        )
        assert _MARKER in _sql(stmts[-1])

    @pytest.mark.asyncio
    async def test_vector_search_where_carries_the_clause(
        self, repo_cls: Any, fts: str, fts_kwargs: dict[str, Any], vec: str
    ) -> None:
        session, stmts = _session()
        await getattr(repo_cls(), vec)(
            _EMB, extra_clauses=[sa.literal_column(_MARKER)], session=session
        )
        assert _MARKER in _sql(stmts[-1])


class TestExtraClausesMergeWithTheTagsClause:
    @pytest.mark.asyncio
    async def test_decision_fts_keeps_tags_and_extra(self) -> None:
        session, stmts = _session()
        await PgDecisionRepo().search_fts(
            "q",
            tags=["t"],
            extra_clauses=[sa.literal_column(_MARKER)],
            session=session,
        )
        sql = _sql(stmts[-1])
        assert _MARKER in sql and "&&" in sql

    @pytest.mark.asyncio
    async def test_adr_without_query_keeps_tags_and_extra(self) -> None:
        session, stmts = _session()
        await PgADRRepo().search(
            query=None,
            tags=["t"],
            extra_clauses=[sa.literal_column(_MARKER)],
            session=session,
        )
        sql = _sql(stmts[-1])
        assert _MARKER in sql and "&&" in sql
