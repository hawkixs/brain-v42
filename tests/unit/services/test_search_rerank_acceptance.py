"""Acceptance counts searches, including empty shards, rather than shard calls."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.brain_service import BrainService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.search.hybrid import HybridSearcher
from brain_v42.services.search.reranker import HybridReranker
from tests.unit.services.test_brain_service import make_decision, make_learning, make_snippet


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize(
    "scenario",
    ["empty_and_reranked", "partial_rrf", "fts", "disabled", "rrf_only", "empty", "zero_limit"],
)
async def test_search_rerank_eligibility(scenario: str, grouped: bool) -> None:
    collector = MagicMock()
    embedding = MagicMock(embed_query=AsyncMock(return_value=[0.1]))
    if scenario == "fts":
        embedding.embed_query.side_effect = EmbeddingUnavailable("unavailable")
    client = MagicMock(rerank=AsyncMock(side_effect=lambda q, texts: [2.0] * len(texts)))
    if scenario == "partial_rrf":
        # Late shards rerank independently, so just one of three invokes RRF.
        client.rerank.side_effect = [RuntimeError("unavailable"), [2.0], [2.0]]
    services = []
    for index, entity in enumerate([make_decision(), make_learning(), make_snippet()]):
        empty = scenario == "empty" or (scenario == "empty_and_reranked" and index == 0)
        services.append(
            MagicMock(
                search=AsyncMock(return_value=[] if empty else [entity]),
                semantic_search=AsyncMock(return_value=[]),
            )
        )
    reranker = None if scenario in {"disabled", "rrf_only"} else HybridReranker(client)
    searcher = HybridSearcher(reranker, reranking_disabled=scenario == "disabled")
    if scenario == "partial_rrf":
        # Use a distinct adapter per shard to exercise mixed invoked outcomes.
        async def independent(**kwargs):
            return await HybridSearcher(HybridReranker(client)).search(**kwargs)

        searcher.search = independent
    brain = BrainService(
        *services, None, None, embedding, metrics_collector=collector, hybrid_searcher=searcher
    )
    kwargs = {
        "types": ["decision", "learning", "snippet"],
        "limit": 0 if scenario == "zero_limit" else 20,
    }
    response = await (
        brain.what_do_i_know_about("q", **kwargs) if grouped else brain.search("q", **kwargs)
    )
    eligible = scenario in {"empty_and_reranked", "partial_rrf", "zero_limit"}
    collector.record_search_rerank.assert_called_once_with(
        eligible=eligible, rrf=scenario == "partial_rrf"
    )
    if scenario == "empty_and_reranked":
        assert response.diagnostics.rerank_mode == "reranked"
    if scenario in {"disabled", "rrf_only", "empty", "fts"}:
        client.rerank.assert_not_called()


@pytest.mark.parametrize("grouped", [False, True])
async def test_unresolved_group_counts_one_ineligible_search(grouped: bool) -> None:
    collector = MagicMock()
    brain = BrainService(
        None,
        None,
        None,
        None,
        None,
        None,
        metrics_collector=collector,
        project_context_svc=MagicMock(get_keys_by_group=AsyncMock(return_value=[])),
    )
    if grouped:
        await brain.what_do_i_know_about("q", project_group="missing")
    else:
        await brain.search("q", project_group="missing")
    collector.record_search_rerank.assert_called_once_with(eligible=False, rrf=False)
