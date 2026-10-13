"""Search scope in BasePgRepository: live rows before LIMIT, HNSW scan settings.

Ticket 73e68261 (audit 2026-10-02): the search SQL applied LIMIT before any
archived/merged row was dropped, and ``hnsw.ef_search`` / ``hnsw.iterative_scan``
were never set. No database: the session is mocked and statements inspected.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from brain_v42.repositories.pg_base import BasePgRepository

_META = MetaData()

_LIVE_TABLE = Table(
    "live_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("search_vector", sa.Text),
    Column("embedding", sa.Text),
    Column("freshness_status", String(10)),
    Column("merged_into", PG_UUID(as_uuid=True), nullable=True),
)

_PLAIN_TABLE = Table(
    "plain_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("search_vector", sa.Text),
    Column("embedding", sa.Text),
)


class _LiveRepo(BasePgRepository):
    table = _LIVE_TABLE


class _PlainRepo(BasePgRepository):
    table = _PLAIN_TABLE


def _compile(stmt: Any) -> Any:
    return stmt.compile(dialect=postgresql.dialect())


def _recording_session(extversion: str | None = "0.8.1") -> tuple[AsyncMock, list[Any]]:
    """Session whose execute() records (statement, params) and answers by statement."""
    calls: list[Any] = []
    session = AsyncMock()

    async def execute(stmt: Any, params: Any = None) -> MagicMock:
        calls.append((stmt, params))
        result = MagicMock()
        result.scalar.return_value = extversion
        result.scalar_one.return_value = 0
        result.mappings.return_value.all.return_value = []
        return result

    session.execute = execute
    return session, calls


def _patch_factory(session: AsyncMock):
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    factory = MagicMock(side_effect=lambda: _cm())
    return patch("brain_v42.repositories.pg_base.get_session_factory", return_value=factory)


class TestLiveRowClauses:
    def test_live_clauses_exclude_merged_and_archived(self) -> None:
        compiled = [_compile(c) for c in _LiveRepo().live_row_clauses()]
        assert [str(c) for c in compiled][0] == "live_items.merged_into IS NULL"
        assert str(compiled[1]) == "live_items.freshness_status != %(freshness_status_1)s"
        assert compiled[1].params == {"freshness_status_1": "archived"}

    def test_live_clauses_skip_columns_the_table_lacks(self) -> None:
        assert _PlainRepo().live_row_clauses() == []

    @pytest.mark.asyncio
    async def test_fts_applies_live_clauses_before_the_limit(self) -> None:
        session, calls = _recording_session()
        repo = _LiveRepo()
        await repo.search_fts(
            "q", limit=7, skip_count=True, extra_clauses=repo.live_row_clauses(), session=session
        )
        _assert_live_and_limited(calls[-1][0], limit=7)

    @pytest.mark.asyncio
    async def test_vector_applies_live_clauses_before_the_limit(self) -> None:
        session, calls = _recording_session()
        repo = _LiveRepo()
        await repo.search_vector(
            [0.1] * 1536, limit=7, extra_clauses=repo.live_row_clauses(), session=session
        )
        _assert_live_and_limited(calls[-1][0], limit=7)


def _assert_live_and_limited(stmt: Any, *, limit: int) -> None:
    compiled = _compile(stmt)
    sql = str(compiled)
    assert "live_items.merged_into IS NULL" in sql
    assert "live_items.freshness_status != " in sql
    assert "archived" in compiled.params.values()
    limit_param = re.search(r"LIMIT %\((\w+)\)s", sql)
    assert limit_param is not None
    assert compiled.params[limit_param.group(1)] == limit


def _set_config_calls(calls: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(str(stmt), params) for stmt, params in calls if "set_config" in str(stmt)]


class TestVectorScanSettings:
    @pytest.mark.asyncio
    async def test_ef_search_and_iterative_scan_are_set_before_the_select(self) -> None:
        session, calls = _recording_session(extversion="0.8.1")
        await _LiveRepo().search_vector([0.1] * 1536, limit=50, session=session)

        texts = [str(stmt) for stmt, _ in calls]
        select_at = next(i for i, t in enumerate(texts) if "ORDER BY" in t)
        ef_at = next(i for i, t in enumerate(texts) if "hnsw.ef_search" in t)
        iter_at = next(i for i, t in enumerate(texts) if "hnsw.iterative_scan" in t)
        assert ef_at < select_at and iter_at < select_at
        by_setting = {
            ("ef" if "ef_search" in t else "iter"): p for t, p in _set_config_calls(calls)
        }
        assert by_setting["ef"] == {"v": "50"}
        assert "relaxed_order" in next(t for t in texts if "iterative_scan" in t)

    @pytest.mark.asyncio
    async def test_ef_search_never_drops_below_the_pgvector_default(self) -> None:
        session, calls = _recording_session()
        await _LiveRepo().search_vector([0.1] * 1536, limit=5, session=session)
        ef = next(p for t, p in _set_config_calls(calls) if "ef_search" in t)
        assert ef == {"v": "40"}

    @pytest.mark.asyncio
    async def test_settings_are_transaction_local(self) -> None:
        session, calls = _recording_session()
        await _LiveRepo().search_vector([0.1] * 1536, limit=5, session=session)
        for text, _ in _set_config_calls(calls):
            assert text.rstrip().endswith("true)")

    @pytest.mark.asyncio
    async def test_iterative_scan_is_skipped_below_pgvector_0_8(self) -> None:
        session, calls = _recording_session(extversion="0.7.4")
        await _LiveRepo().search_vector([0.1] * 1536, limit=50, session=session)
        assert not any("iterative_scan" in str(s) for s, _ in calls)
        assert any("hnsw.ef_search" in str(s) for s, _ in calls)

    @pytest.mark.asyncio
    async def test_extension_version_is_read_once_per_repository(self) -> None:
        session, calls = _recording_session()
        repo = _LiveRepo()
        await repo.search_vector([0.1] * 1536, session=session)
        await repo.search_vector([0.1] * 1536, session=session)
        assert sum("pg_extension" in str(s) for s, _ in calls) == 1

    @pytest.mark.asyncio
    async def test_fts_does_not_touch_hnsw_settings(self) -> None:
        session, calls = _recording_session()
        await _LiveRepo().search_fts("q", skip_count=True, session=session)
        assert _set_config_calls(calls) == []
