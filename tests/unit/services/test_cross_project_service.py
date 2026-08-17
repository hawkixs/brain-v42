"""Tests for CrossProjectBriefingService (Spec C MVP β)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.cross_project_service import (
    CrossProjectBlock,
    CrossProjectBriefingService,
)


def _mk_service(graph: AsyncMock, rows_by_query: list[list[dict]] | None = None):
    """Service with a mocked session factory returning canned PG rows."""
    session = MagicMock()
    results = []
    for rows in rows_by_query or []:
        r = MagicMock()
        r.mappings.return_value.all.return_value = rows
        results.append(r)
    session.execute = AsyncMock(side_effect=results)
    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(return_value=session)
    sf.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = CrossProjectBriefingService(sf, graph, top_n=2, entries_max=5)
    return svc, session


@pytest.mark.asyncio
async def test_none_when_no_active_domains():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = []
    svc, _ = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None


@pytest.mark.asyncio
async def test_none_when_no_cross_project_entities():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    graph.fetch_cross_project_entity_ids.return_value = []
    svc, _ = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None


@pytest.mark.asyncio
async def test_entries_sorted_by_recency_and_capped():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml", "memory"]
    ids = [str(uuid4()) for _ in range(3)]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": ids[0], "labels": ["Decision"], "project_key": "red-shrik"},
        {"id": ids[1], "labels": ["Decision"], "project_key": "red-monitor"},
        {"id": ids[2], "labels": ["Learning"], "project_key": "red-shrik"},
    ]
    old, mid, new = (
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 15, tzinfo=UTC),
        datetime(2026, 5, 1, tzinfo=UTC),
    )
    decision_rows = [
        {"id": ids[0], "display": "old decision", "created_at": old},
        {"id": ids[1], "display": "new decision", "created_at": new},
    ]
    learning_rows = [{"id": ids[2], "display": "mid learning", "created_at": mid}]
    svc, _ = _mk_service(graph, rows_by_query=[decision_rows, learning_rows])
    block = await svc.fetch_block("brain-v42")
    assert isinstance(block, CrossProjectBlock)
    assert block.domains == ["ml", "memory"]
    assert [e.display for e in block.entries] == ["new decision", "mid learning", "old decision"]
    assert block.entries[0].project_key == "red-monitor"
    assert block.entries[0].entity_type == "Decision"


@pytest.mark.asyncio
async def test_unknown_labels_are_skipped():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": str(uuid4()), "labels": ["Feature"], "project_key": "watchk"},
    ]
    svc, session = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_display_truncated_at_60_chars():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    eid = str(uuid4())
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": eid, "labels": ["Decision"], "project_key": "red-shrik"},
    ]
    long_title = "x" * 80
    rows = [{"id": eid, "display": long_title, "created_at": datetime(2026, 5, 1, tzinfo=UTC)}]
    svc, _ = _mk_service(graph, rows_by_query=[rows])
    block = await svc.fetch_block("brain-v42")
    assert block.entries[0].display == "x" * 60 + "…"


@pytest.mark.asyncio
async def test_entries_max_cap_applied():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    ids = [str(uuid4()) for _ in range(7)]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": i, "labels": ["Decision"], "project_key": "red-shrik"} for i in ids
    ]
    rows = [
        {"id": i, "display": f"d{n}", "created_at": datetime(2026, 5, 1, n + 1, tzinfo=UTC)}
        for n, i in enumerate(ids)
    ]
    svc, _ = _mk_service(graph, rows_by_query=[rows])
    block = await svc.fetch_block("brain-v42")
    assert len(block.entries) == 5
