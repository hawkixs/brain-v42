"""Unit coverage for parent decay hydration in plan chunk search."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService


def _search_service() -> tuple[IndexedPlanSearchService, AsyncMock]:
    now = datetime.now(UTC)
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {
            "id": uuid4(),
            "plan_id": uuid4(),
            "section_title": "COR1",
            "section_path": "cor1",
            "content": "usage evidence",
            "section_order": 0,
            "word_count": 2,
            "tags": [],
            "project_key": "brain-v42",
            "plan_type": "plan",
            "status": "active",
            "access_count": 2,
            "last_accessed_at": now,
            "created_at": now,
            "parent_access_count": 17,
            "parent_last_accessed_at": now,
            "parent_freshness_status": "stale",
            "parent_created_at": now,
            "score": 0.8,
        }
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    return IndexedPlanSearchService(factory), session


@pytest.mark.parametrize("search_mode", ["fts", "vector"])
@pytest.mark.asyncio
async def test_search_joins_and_hydrates_parent_decay_state(search_mode: str) -> None:
    """Both retrieval paths carry canonical indexed_plans decay state."""
    service, session = _search_service()

    if search_mode == "fts":
        results: list[Any] = await service.search("usage", project_key="brain-v42")
        chunk = results[0]
    else:
        pairs = await service.semantic_search(
            "usage",
            embedding=[0.1] * 1536,
            project_key="brain-v42",
        )
        chunk = pairs[0][0]

    rendered_sql = str(session.execute.await_args.args[0])
    assert (
        "JOIN indexed_plans p ON p.id = c.plan_id AND p.project_key = c.project_key" in rendered_sql
    )
    assert "p.access_count AS parent_access_count" in rendered_sql
    assert "p.last_accessed_at AS parent_last_accessed_at" in rendered_sql
    assert "p.freshness_status AS parent_freshness_status" in rendered_sql
    assert "p.created_at AS parent_created_at" in rendered_sql
    assert chunk.parent_access_count == 17
    assert chunk.parent_freshness_status == "stale"


@pytest.mark.parametrize("search_mode", ["fts", "vector"])
@pytest.mark.parametrize("include_archived", [False, True])
@pytest.mark.asyncio
async def test_parent_archive_filter_is_applied_before_limit(
    search_mode: str,
    include_archived: bool,
) -> None:
    """Archived parents are excluded in SQL, before the database LIMIT."""
    service, session = _search_service()

    if search_mode == "fts":
        await service.search(
            "usage",
            project_key="brain-v42",
            include_archived=include_archived,
        )
    else:
        await service.semantic_search(
            "usage",
            embedding=[0.1] * 1536,
            project_key="brain-v42",
            include_archived=include_archived,
        )

    rendered_sql = str(session.execute.await_args.args[0])
    archive_clause = "p.freshness_status != 'archived'"
    if include_archived:
        assert archive_clause not in rendered_sql
    else:
        assert archive_clause in rendered_sql
        assert rendered_sql.index(archive_clause) < rendered_sql.index("LIMIT")
