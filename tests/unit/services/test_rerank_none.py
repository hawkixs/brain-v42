"""RRF scores stay usable when reranking is intentionally disabled."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.config import Settings
from brain_v42.mcp.tools.formatters import format_search_results
from brain_v42.services.embedding_factory import build_reranker_client
from brain_v42.services.search.hybrid import HybridSearcher
from tests.unit.services.test_brain_service import make_brain_service, make_decision


def test_none_factory_never_reads_a_file_or_calibrates() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
        rerank_backend="none",
        brain_embedding_token_file=Path("unused-token"),
        _env_file=None,
    )
    with (
        patch.object(Path, "read_text", side_effect=AssertionError("unexpected file read")),
        patch(
            "brain_v42.services.reranker_client.calibration_for_identity",
            side_effect=AssertionError("unexpected calibration"),
        ),
        patch(
            "brain_v42.services.embedding_factory.RerankerClient",
            side_effect=AssertionError("unexpected reranker client"),
        ),
    ):
        assert build_reranker_client(settings) is None


@pytest.mark.parametrize("floor", [0.2, 0.5])
@pytest.mark.parametrize("explicit", [False, True])
async def test_disabled_search_preserves_rrf_scores_without_a_degraded_banner(
    floor: float,
    explicit: bool,
) -> None:
    decision = make_decision()
    brain, services = make_brain_service(
        min_score=floor,
        rerank_identity="none",
        hybrid_searcher=HybridSearcher(reranking_disabled=True),
    )
    services[0].search = AsyncMock(return_value=[decision])
    services[0].semantic_search.return_value = [(decision, 0.9)]
    response = await brain.search(
        "rollback", types=["decision"], min_score=floor if explicit else None
    )
    assert response.diagnostics.rerank_mode == "disabled"
    assert response.diagnostics.rerank_identity == "none"
    assert response.degraded is None
    assert response.diagnostics.degraded is False
    assert response.diagnostics.min_score_requested == floor
    assert response.diagnostics.min_score_effective == 0.0
    assert len(response.results) == 1
    assert 0.0 < response.results[0].score < 0.2
    assert response.results[0].score_kind == "rank"
    output = format_search_results(response.results, response.query, degraded=response.degraded)
    assert "degraded:" not in output


async def test_disabled_empty_search_reports_disabled() -> None:
    brain, services = make_brain_service(
        min_score=0.5,
        rerank_identity="none",
        hybrid_searcher=HybridSearcher(reranking_disabled=True),
    )
    services[0].search = AsyncMock(return_value=[])
    response = await brain.search("missing", types=["decision"])
    assert response.diagnostics.rerank_mode == "disabled"
    assert response.diagnostics.min_score_effective == 0.0
    assert response.degraded is None


async def test_disabled_knowledge_summary_preserves_rrf_results() -> None:
    decision = make_decision()
    brain, services = make_brain_service(
        min_score=0.5,
        rerank_identity="none",
        hybrid_searcher=HybridSearcher(reranking_disabled=True),
    )
    services[0].search = AsyncMock(return_value=[decision])
    services[0].semantic_search.return_value = [(decision, 0.9)]
    for service in services[1:5]:
        service.search = AsyncMock(return_value=[])
    response = await brain.what_do_i_know_about("rollback", min_score=0.5)
    assert response.total == 1
    assert response.diagnostics.min_score_effective == 0.0
    assert response.diagnostics.rerank_mode == "disabled"
    assert response.degraded is None


@pytest.mark.parametrize("summary", [False, True])
async def test_none_unresolved_group_diagnostics_keep_zero_effective_floor(summary: bool) -> None:
    context = MagicMock(get_keys_by_group=AsyncMock(return_value=[]))
    brain, _ = make_brain_service(
        min_score=0.5,
        rerank_identity="none",
        project_context_svc=context,
        hybrid_searcher=HybridSearcher(reranking_disabled=True),
    )
    search = brain.what_do_i_know_about if summary else brain.search
    response = await search("rollback", project_group="missing", min_score=0.5)
    assert response.diagnostics.project_group_unresolved is True
    assert response.diagnostics.min_score_requested == 0.5
    assert response.diagnostics.min_score_effective == 0.0
