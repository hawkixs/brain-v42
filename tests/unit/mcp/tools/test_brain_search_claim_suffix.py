"""brain_search's compact claim suffix, flat and grouped (spec 2026-09-19, section 6.6).

One batch fetch per result set (never per item); plan results carry no claim
(entity_type CHECK excludes it) and must never reach the batch as an entry, or
the whole fetch would refuse with invalid_argument and mask every OTHER item's
claim state too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from fastmcp import FastMCP

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.claim_rendering import CLAIM_SUFFIX_UNAVAILABLE
from brain_v42.models.brain import KnowledgeByType, SearchDiagnostics, SearchResponse, SearchResult
from brain_v42.models.claim_read import ClaimRead, VerdictRead, evaluate_claim
from brain_v42.services.claim_read_service import ClaimReadError
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

DECISION_ID = uuid4()
LEARNING_ID = uuid4()


def _fake_item(entity_id: UUID, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": str(entity_id),
        "title": "Test Decision",
        "description": "test description",
        "reasoning": "test reasoning",
        "tags": [],
        "metadata": {},
        "created_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
        "updated_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
    }
    item.update(overrides)
    return item


def _plan_item() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "plan_id": str(uuid4()),
        "section_title": "Test Plan Section",
        "section_path": "1/2",
        "content": "test plan content",
        "section_order": 0,
        "word_count": 3,
        "project_key": "brain-v42",
        "plan_type": "plan",
        "status": "active",
        "tags": [],
        "created_at": datetime(2026, 3, 1, 12, 0, 0).isoformat(),
    }


def _make_diagnostics() -> SearchDiagnostics:
    return SearchDiagnostics(
        candidates_before_threshold=1,
        best_raw_score=0.9,
        min_score_requested=0.2,
        min_score_effective=0.2,
        tags_filtered_out=0,
        types_searched=["decision"],
        project_key_requested=None,
        project_key_effective=None,
        project_key_injected_by_dream_scope=False,
        include_archived=False,
        rerank_mode="reranked",
        degraded=False,
    )


def _state() -> Any:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    verdict = VerdictRead(
        id=uuid4(),
        seq=1,
        verdict="holds",
        reason=None,
        emitted_at=now,
        recorded_at=now,
        observation_id=uuid4(),
        measurement={},
    )
    claim = ClaimRead(
        id=uuid4(),
        seq=1,
        entry_id=DECISION_ID,
        entity_type="decision",
        project_key="test",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name="lag",
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="tester",
        declared_at=now,
        recorded_at=now,
        retired_at=None,
        replaces_id=None,
        latest=verdict,
        conclusive=verdict,
    )
    return evaluate_claim(claim, now)


def _build(*, claim_read_svc: Any = None) -> tuple[FastMCP, MagicMock]:
    mcp = FastMCP("test-brain")
    mock_brain_svc = MagicMock()
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=mock_brain_svc,
        claim_read_svc=claim_read_svc,
    )
    return mcp, mock_brain_svc


async def _fn(mcp: FastMCP, name: str) -> Any:
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


async def test_flat_search_appends_the_suffix_for_an_active_claim() -> None:
    result = SearchResult(
        type="decision", score=0.9, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
    )
    response = SearchResponse(
        query="q",
        results=[result],
        total=1,
        types_searched=["decision"],
        diagnostics=_make_diagnostics(),
    )
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {("decision", DECISION_ID): (_state(),)}
    mcp, brain_svc = _build(claim_read_svc=claim_svc)
    brain_svc.search = AsyncMock(return_value=response)

    output = await (await _fn(mcp, "brain_search"))(query="q")

    assert "[claims : 1 tient]" in output
    claim_svc.batch_summaries.assert_awaited_once_with(
        [("decision", DECISION_ID)], trusted_project_key=None
    )


async def test_flat_search_excludes_plan_results_from_the_claim_batch() -> None:
    plan_result = SearchResult(
        type="plan", score=0.9, score_kind="cross_encoder", item=_plan_item()
    )
    decision_result = SearchResult(
        type="decision", score=0.8, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
    )
    response = SearchResponse(
        query="q",
        results=[plan_result, decision_result],
        total=2,
        types_searched=["decision", "plan"],
        diagnostics=_make_diagnostics(),
    )
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {}
    mcp, brain_svc = _build(claim_read_svc=claim_svc)
    brain_svc.search = AsyncMock(return_value=response)

    await (await _fn(mcp, "brain_search"))(query="q")

    entries = claim_svc.batch_summaries.await_args.args[0]
    assert entries == [("decision", DECISION_ID)]


async def test_flat_search_shows_a_visible_marker_when_the_lookup_fails() -> None:
    result = SearchResult(
        type="decision", score=0.9, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
    )
    response = SearchResponse(
        query="q",
        results=[result],
        total=1,
        types_searched=["decision"],
        diagnostics=_make_diagnostics(),
    )
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.side_effect = ClaimReadError("read_unavailable")
    mcp, brain_svc = _build(claim_read_svc=claim_svc)
    brain_svc.search = AsyncMock(return_value=response)

    output = await (await _fn(mcp, "brain_search"))(query="q")

    assert "Test Decision" in output
    assert CLAIM_SUFFIX_UNAVAILABLE in output


async def test_flat_search_is_byte_identical_with_no_active_claim() -> None:
    result = SearchResult(
        type="decision", score=0.9, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
    )
    response = SearchResponse(
        query="q",
        results=[result],
        total=1,
        types_searched=["decision"],
        diagnostics=_make_diagnostics(),
    )
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {("decision", DECISION_ID): ()}
    mcp_with, brain_svc_with = _build(claim_read_svc=claim_svc)
    brain_svc_with.search = AsyncMock(return_value=response)
    with_claims = await (await _fn(mcp_with, "brain_search"))(query="q")

    mcp_without, brain_svc_without = _build(claim_read_svc=None)
    brain_svc_without.search = AsyncMock(return_value=response)
    without_claims = await (await _fn(mcp_without, "brain_search"))(query="q")

    assert with_claims == without_claims


async def test_grouped_search_appends_the_suffix_with_one_batch_call() -> None:
    by_type = KnowledgeByType(
        decisions=[
            SearchResult(
                type="decision", score=0.9, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
            )
        ],
        learnings=[
            SearchResult(
                type="learning",
                score=0.8,
                score_kind="cross_encoder",
                item=_fake_item(
                    LEARNING_ID,
                    topic="t",
                    insight="i",
                    source_type="experience",
                    confidence="medium",
                ),
            )
        ],
    )
    from brain_v42.models.brain import WhatDoIKnowResponse

    response = WhatDoIKnowResponse(
        topic="q",
        by_type=by_type,
        total=2,
        types_searched=["decision", "learning"],
        diagnostics=_make_diagnostics(),
    )
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {("decision", DECISION_ID): (_state(),)}
    mcp, brain_svc = _build(claim_read_svc=claim_svc)
    brain_svc.what_do_i_know_about = AsyncMock(return_value=response)

    output = await (await _fn(mcp, "brain_search"))(query="q", group_by_type=True)

    assert "[claims : 1 tient]" in output
    claim_svc.batch_summaries.assert_awaited_once()
    entries = claim_svc.batch_summaries.await_args.args[0]
    assert set(entries) == {("decision", DECISION_ID), ("learning", LEARNING_ID)}


async def test_omitting_claim_read_svc_preserves_flat_search() -> None:
    result = SearchResult(
        type="decision", score=0.9, score_kind="cross_encoder", item=_fake_item(DECISION_ID)
    )
    response = SearchResponse(
        query="q",
        results=[result],
        total=1,
        types_searched=["decision"],
        diagnostics=_make_diagnostics(),
    )
    mcp, brain_svc = _build()
    brain_svc.search = AsyncMock(return_value=response)

    output = await (await _fn(mcp, "brain_search"))(query="q")

    assert "[claims" not in output
