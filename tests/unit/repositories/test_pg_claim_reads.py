"""Claim reads use scoped, bounded, SELECT-only SQL."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from brain_v42.repositories.pg_claim_reads import (
    fetch_active_for_entries,
    fetch_claim,
    fetch_verdict_page,
    list_occurrences,
)


def _session() -> tuple[AsyncMock, list[object]]:
    statements: list[object] = []
    session = AsyncMock()

    async def execute(statement: object) -> MagicMock:
        statements.append(statement)
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        result.mappings.return_value.one_or_none.return_value = None
        return result

    session.execute.side_effect = execute
    return session, statements


def _sql(statement: object) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


@pytest.mark.asyncio
async def test_batch_uses_one_scoped_select_on_source_uuid_and_entity_type() -> None:
    session, statements = _session()
    entries = [("learning", uuid4()), ("decision", uuid4())]
    assert await fetch_active_for_entries(session, entries, trusted_project_key="project-a") == []
    assert len(statements) == 1
    sql = _sql(statements[0])
    assert sql.startswith("SELECT")
    assert "brain_entities.source_uuid" in sql
    assert "brain_entities.entity_type" in sql
    assert "brain_entities.project_key" in sql
    assert "knowledge_claims.project_key = 'project-a'" in sql
    assert "knowledge_claims.retired_at IS NULL" in sql
    assert "knowledge_claim_current" in sql
    assert "latest_verdict.emitted_at" in sql
    assert "conclusive_verdict.emitted_at" in sql


@pytest.mark.asyncio
async def test_empty_batch_does_not_query() -> None:
    session, statements = _session()
    assert await fetch_active_for_entries(session, [], trusted_project_key="project-a") == []
    assert statements == []


@pytest.mark.asyncio
async def test_occurrence_page_reapplies_filters_and_requests_one_extra_row() -> None:
    session, statements = _session()
    entry_id = uuid4()
    page = await list_occurrences(
        session,
        project_key="project-a",
        entry_id=entry_id,
        include_retired=False,
        after_seq=17,
        limit=2,
        trusted_project_key="project-a",
    )
    assert page.items == ()
    assert page.next_after_seq is None
    assert len(statements) == 1
    sql = _sql(statements[0])
    assert "brain_entities.source_uuid" in sql
    assert "knowledge_claims.project_key = 'project-a'" in sql
    assert "knowledge_claims.seq > 17" in sql
    assert "knowledge_claims.retired_at IS NULL" in sql
    assert "LIMIT 3" in sql


@pytest.mark.asyncio
async def test_trusted_scope_checks_immutable_claim_project_even_without_project_filter() -> None:
    session, statements = _session()
    await list_occurrences(session, trusted_project_key="project-a")
    assert "knowledge_claims.project_key = 'project-a'" in _sql(statements[0])


@pytest.mark.asyncio
async def test_scoped_missing_claim_and_empty_verdict_page_are_safe() -> None:
    session, statements = _session()
    assert await fetch_claim(session, uuid4(), trusted_project_key="project-a") is None
    assert len(statements) == 1
    assert "knowledge_claims.project_key" in _sql(statements[0])
    verdicts = await fetch_verdict_page(session, uuid4(), after_seq=0, limit=2)
    assert verdicts.items == ()
    assert verdicts.next_after_seq is None
    assert "ORDER BY knowledge_claim_verdicts.seq ASC" in _sql(statements[1])
