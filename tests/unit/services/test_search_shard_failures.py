"""Shard failures must stay visible without changing healthy score thresholds."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.dream_project_authorization import bind_dream_project_scope
from brain_v42.mcp.tools.brain_tools import _no_embedding_model_served
from brain_v42.mcp.tools.formatters import format_knowledge_by_type, format_search_results
from brain_v42.models.brain import (
    ALL_TYPES,
    KnowledgeByType,
    KnowledgeType,
    SearchDiagnostics,
    SearchResponse,
    WhatDoIKnowResponse,
)
from brain_v42.services.brain_service import BrainService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.search.hybrid import HybridSearcher
from brain_v42.services.search.reranker import HybridReranker
from tests.unit.services.test_brain_service import make_decision
from tests.unit.services.test_search_diagnostics import _dream_scope


def _brain() -> tuple[BrainService, dict[KnowledgeType, MagicMock], MagicMock]:
    services = {t: MagicMock() for t in ALL_TYPES}
    for service in services.values():
        service.search = AsyncMock(return_value=[])
        service.semantic_search = AsyncMock(return_value=[])
    embedding = MagicMock()
    embedding.embed_query = AsyncMock(return_value=[0.1] * 1536)
    client = MagicMock()
    client.rerank = AsyncMock(return_value=[4.0, -4.0])
    brain = BrainService(
        decision_svc=services["decision"],
        learning_svc=services["learning"],
        snippet_svc=services["snippet"],
        runbook_svc=services["runbook"],
        adr_svc=services["adr"],
        plan_search_svc=services["plan"],
        embedding_svc=embedding,
        hybrid_searcher=HybridSearcher(reranker=HybridReranker(client=client)),
        min_score=0.2,
    )
    return brain, services, embedding


def _fail(service: MagicMock, message: str = "shard unavailable") -> None:
    service.search.side_effect = RuntimeError(message)
    service.semantic_search.side_effect = RuntimeError(message)


async def _search(
    brain: BrainService,
    grouped: bool,
    types: list[KnowledgeType] | None = None,
    min_score: float | None = None,
) -> SearchResponse | WhatDoIKnowResponse:
    if grouped:
        return await brain.what_do_i_know_about("topic", types=types, min_score=min_score)
    return await brain.search("topic", types=types, min_score=min_score)


def _render(response: SearchResponse | WhatDoIKnowResponse) -> str:
    if isinstance(response, WhatDoIKnowResponse):
        return format_knowledge_by_type(
            response.by_type,
            response.topic,
            degraded=response.degraded,
            diagnostics=response.diagnostics,
        )
    return format_search_results(
        response.results,
        response.query,
        degraded=response.degraded,
        diagnostics=response.diagnostics,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
@pytest.mark.parametrize("min_score", [None, 0.7], ids=["configured-floor", "override-floor"])
async def test_failed_shard_keeps_the_floor_on_healthy_reranked_results(
    grouped: bool, min_score: float | None
) -> None:
    brain, services, _ = _brain()
    high, low = make_decision(title="High"), make_decision(title="Low")
    services["decision"].search.return_value = [high, low]
    services["decision"].semantic_search.return_value = [(high, 0.9), (low, 0.8)]
    _fail(services["learning"])

    response = await _search(brain, grouped, ["decision", "learning", "snippet"], min_score)

    assert response.degraded == {"failed_types": ["learning"]}
    assert response.diagnostics.degraded is True
    assert response.diagnostics.failed_types == ["learning"]
    assert response.diagnostics.min_score_effective == pytest.approx(min_score or 0.2)
    assert response.diagnostics.candidates_before_threshold == 2
    results = (
        response.by_type.decisions
        if isinstance(response, WhatDoIKnowResponse)
        else response.results
    )
    assert [r.item["id"] for r in results] == [str(high.id)]
    assert results[0].score_kind == "cross_encoder"
    assert response.diagnostics.rerank_mode == "reranked"
    assert (
        _render(response).count("degraded: recherche incomplète — types en échec : learning") == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
async def test_all_failed_shards_explain_an_incomplete_search(grouped: bool) -> None:
    brain, services, _ = _brain()
    for service in services.values():
        _fail(service)

    response = await _search(brain, grouped)

    assert response.total == 0
    assert response.degraded == {"failed_types": ALL_TYPES}
    assert response.diagnostics.degraded is True
    assert response.diagnostics.failed_types == ALL_TYPES
    text = _render(response)
    names = ", ".join(ALL_TYPES)
    assert text.count(f"degraded: recherche incomplète — types en échec : {names}") == 1
    assert f"failed types: {names}" in text
    assert "0 candidates in scope" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
async def test_failed_types_never_exposes_exception_messages(grouped: bool) -> None:
    brain, services, _ = _brain()
    secret = "sk-test-do-not-expose-shard-error"
    _fail(services["learning"], secret)

    response = await _search(brain, grouped, ["learning"])

    assert response.degraded == {"failed_types": ["learning"]}
    assert response.diagnostics.failed_types == ["learning"]
    assert secret not in response.model_dump_json()
    assert secret not in response.diagnostics.model_dump_json()
    assert secret not in _render(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
async def test_fts_fallback_and_failed_shard_keep_both_banners_and_zero_floor(
    grouped: bool,
) -> None:
    brain, services, embedding = _brain()
    decision = make_decision()
    services["decision"].search.return_value = [decision]
    _fail(services["learning"])
    embedding.embed_query.side_effect = EmbeddingUnavailable("offline", kind="unreachable")

    response = await _search(brain, grouped, ["decision", "learning"], min_score=1.1)

    assert response.degraded == {"search_mode": "fts_fallback", "failed_types": ["learning"]}
    assert response.diagnostics.min_score_effective == 0.0
    assert response.total == 1
    text = _render(response)
    assert text.count("degraded: embedding service indisponible") == 1
    assert text.count("degraded: recherche incomplète — types en échec : learning") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "dream-scoped"])
async def test_graph_failure_only_changes_optional_enrichment_diagnostics(scoped: bool) -> None:
    brain, services, _ = _brain()
    high, low = make_decision(), make_decision()
    services["decision"].search.return_value = [high, low]
    services["decision"].semantic_search.return_value = [(high, 0.9), (low, 0.8)]
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(side_effect=RuntimeError("graph unavailable"))
    with bind_dream_project_scope(_dream_scope()) if scoped else nullcontext():
        baseline = await brain.search("topic", types=["decision"])
        brain._graph = graph  # noqa: SLF001
        response = await brain.search("topic", types=["decision"])

    graph.get_related_ids.assert_awaited_once()
    assert baseline.diagnostics.graph_degraded is False
    assert response.diagnostics.graph_degraded is True
    assert response.diagnostics.degraded is False
    assert response.degraded is None
    assert response.results == baseline.results
    assert response.related == baseline.related == []
    assert _render(response) == _render(baseline)


def test_old_diagnostics_payloads_default_new_fields_and_round_trip() -> None:
    diagnostics = SearchDiagnostics.model_validate({"degraded": False})
    assert diagnostics.failed_types == []
    assert diagnostics.graph_degraded is False
    assert SearchDiagnostics.model_validate(diagnostics.model_dump()) == diagnostics


@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
def test_only_failed_types_does_not_claim_no_embedding_model_served(grouped: bool) -> None:
    marker = {"failed_types": ["learning"]}
    if grouped:
        response = WhatDoIKnowResponse(
            topic="topic", by_type=KnowledgeByType(), total=0, types_searched=[], degraded=marker
        )
    else:
        response = SearchResponse(
            query="topic", results=[], total=0, types_searched=[], degraded=marker
        )
    assert _no_embedding_model_served(response) is False


@pytest.mark.parametrize("grouped", [False, True], ids=["search", "grouped"])
def test_empty_failure_explanation_uses_marker_without_diagnostics(grouped: bool) -> None:
    marker = {"failed_types": ["decision", "learning"]}
    if grouped:
        text = format_knowledge_by_type(KnowledgeByType(), "topic", degraded=marker)
    else:
        text = format_search_results([], "topic", degraded=marker)
    assert "failed types: decision, learning" in text
    assert "0 candidates in scope" not in text


def test_incomplete_search_keeps_the_threshold_hint_of_the_types_that_answered() -> None:
    """A failed type must not hide WHY the types that answered returned nothing."""
    diagnostics = SearchDiagnostics(
        candidates_before_threshold=2,
        best_raw_score=0.15,
        min_score_requested=0.2,
        min_score_effective=0.2,
        types_searched=list(ALL_TYPES),
        failed_types=["learning"],
        degraded=True,
    )

    text = format_search_results(
        [], "topic", degraded={"failed_types": ["learning"]}, diagnostics=diagnostics
    )

    assert "failed types: learning" in text
    assert "none above min_score 0.2 (best raw score 0.15" in text


def test_failed_types_with_no_candidate_do_not_claim_an_empty_scope() -> None:
    """With no candidate at all, the reason names the types that answered, not the scope."""
    diagnostics = SearchDiagnostics(
        candidates_before_threshold=0,
        types_searched=list(ALL_TYPES),
        failed_types=["learning"],
        degraded=True,
    )

    text = format_search_results(
        [], "topic", degraded={"failed_types": ["learning"]}, diagnostics=diagnostics
    )

    assert "failed types: learning" in text
    assert "0 candidates in the types that answered" in text
    assert "0 candidates in scope" not in text
