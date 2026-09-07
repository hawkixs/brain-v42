"""RED-phase tests for lot G2 — the empty search result explains itself.

``SearchResponse.diagnostics`` / ``WhatDoIKnowResponse.diagnostics`` carry
counters already known to the fan-out/threshold pipeline (candidates before
the min_score cut, best raw score, tags removed, project scope requested vs
effective, rerank mode, degraded flag) so the formatter can explain a 0-result
answer instead of rendering an empty, silent block. These tests populate the
diagnostics from the service layer; formatter-level rendering is covered in
``tests/unit/mcp/tools/test_search_empty_explains_itself.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.models.brain import SearchDiagnostics
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.search.hybrid import HybridSearcher
from tests.unit.services.test_brain_service import make_brain_service, make_decision

DREAM_PROJECT_KEY = "dream-owned"


class RecordingResolver:
    async def references_belong_to_project(self, project_key, references) -> bool:  # noqa: ANN001
        return True


def _dream_scope() -> DreamProjectScope:
    return DreamProjectScope(
        project_key=DREAM_PROJECT_KEY,
        resolver=RecordingResolver(),
        audit=DreamProjectAudit(principal="dream-codex-scan", phase="scan"),
        tool_name="brain_search",
    )


class AsyncMockRaising:
    """Callable stand-in raising EmbeddingUnavailable — used to force fts_fallback."""

    async def __call__(self, *args: object, **kwargs: object) -> None:
        raise EmbeddingUnavailable("GPU down", kind="unreachable")


class TestSearchResponseCarriesDiagnostics:
    """search() populates SearchResponse.diagnostics from already-known state."""

    @pytest.mark.asyncio
    async def test_diagnostics_is_a_search_diagnostics_instance(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query")
        assert isinstance(response.diagnostics, SearchDiagnostics)

    @pytest.mark.asyncio
    async def test_candidates_before_threshold_counts_all_fused_candidates(self) -> None:
        """Counts candidates BEFORE the min_score cut, not after."""
        high = make_decision(title="high")
        low = make_decision(title="low")
        brain, _ = make_brain_service(
            decision_results=[(high, 0.9), (low, 0.05)],
            min_score=0.5,
        )
        response = await brain.search("query")

        assert response.total == 1  # only "high" survives the cut
        assert response.diagnostics.candidates_before_threshold == 2

    @pytest.mark.asyncio
    async def test_best_raw_score_is_the_max_raw_score_seen(self) -> None:
        high = make_decision(title="high")
        low = make_decision(title="low")
        brain, _ = make_brain_service(
            decision_results=[(high, 0.9), (low, 0.05)],
            min_score=0.99,
        )
        response = await brain.search("query")

        assert response.total == 0
        assert response.diagnostics.best_raw_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_best_raw_score_is_none_when_no_candidates_at_all(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query")

        assert response.diagnostics.best_raw_score is None
        assert response.diagnostics.candidates_before_threshold == 0

    @pytest.mark.asyncio
    async def test_min_score_requested_equals_effective_when_healthy(self) -> None:
        brain, _ = make_brain_service(min_score=0.35)
        response = await brain.search("query")

        assert response.diagnostics.min_score_requested == pytest.approx(0.35)
        assert response.diagnostics.min_score_effective == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_min_score_effective_drops_to_zero_when_degraded_but_requested_stays(
        self,
    ) -> None:
        """Fix d3cf29e9: degraded mode forces effective_min_score=0.0 — the
        *requested* value must still be reported so the caller can tell
        "you asked for 0.5" from "0.5 was actually applied"."""
        decision = make_decision()
        brain, svcs = make_brain_service(min_score=0.5)
        decision_svc, _learning, _snippet, _runbook, _adr, embedding_svc = svcs
        decision_svc.search = AsyncMock(return_value=[decision])
        embedding_svc.embed_query = AsyncMockRaising()

        response = await brain.search("query", types=["decision"])

        assert response.degraded == {"search_mode": "fts_fallback"}
        assert response.diagnostics.min_score_requested == pytest.approx(0.5)
        assert response.diagnostics.min_score_effective == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_tags_filtered_out_counts_entities_removed_by_tags_only(self) -> None:
        matching = make_decision(tags=["dream:scan"])
        non_matching = make_decision(tags=["other"])
        brain, _ = make_brain_service(
            decision_results=[(matching, 0.9), (non_matching, 0.8)],
        )
        response = await brain.search("query", tags=["dream:scan"])

        assert response.total == 1
        assert response.diagnostics.tags_filtered_out == 1

    @pytest.mark.asyncio
    async def test_tags_filtered_out_zero_when_no_tags_requested(self) -> None:
        d = make_decision(tags=["other"])
        brain, _ = make_brain_service(decision_results=[(d, 0.9)])
        response = await brain.search("query")

        assert response.diagnostics.tags_filtered_out == 0

    @pytest.mark.asyncio
    async def test_types_searched_mirrors_the_top_level_field(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query", types=["decision", "learning"])

        assert response.diagnostics.types_searched == ["decision", "learning"]
        assert response.diagnostics.types_searched == response.types_searched

    @pytest.mark.asyncio
    async def test_project_key_requested_equals_effective_outside_dream_scope(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query", project_key="brain-v42")

        assert response.diagnostics.project_key_requested == "brain-v42"
        assert response.diagnostics.project_key_effective == "brain-v42"
        assert response.diagnostics.project_key_injected_by_dream_scope is False

    @pytest.mark.asyncio
    async def test_project_key_effective_is_the_injected_dream_scope_key(self) -> None:
        """The dream scope OVERRIDES whatever project_key the caller forged."""
        brain, _ = make_brain_service()

        with bind_dream_project_scope(_dream_scope()):
            response = await brain.search("query", project_key="forged-project")

        assert response.diagnostics.project_key_requested == "forged-project"
        assert response.diagnostics.project_key_effective == DREAM_PROJECT_KEY
        assert response.diagnostics.project_key_injected_by_dream_scope is True

    @pytest.mark.asyncio
    async def test_include_archived_is_reported(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query", include_archived=True)
        assert response.diagnostics.include_archived is True

    @pytest.mark.asyncio
    async def test_rerank_mode_is_none_without_a_hybrid_searcher(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.search("query")
        assert response.diagnostics.rerank_mode is None
        assert response.diagnostics.degraded is False

    @pytest.mark.asyncio
    async def test_rerank_mode_and_degraded_flag_reflect_hybrid_fallback(self) -> None:
        hybrid = MagicMock(spec=HybridSearcher)
        hybrid.search = AsyncMock(return_value=([], "rrf_fallback"))
        brain, _ = make_brain_service()
        brain._hybrid_searcher = hybrid  # noqa: SLF001

        response = await brain.search("query", types=["decision"])

        assert response.diagnostics.rerank_mode == "rrf_fallback"
        assert response.diagnostics.degraded is True


class TestWhatDoIKnowAboutCarriesDiagnostics:
    """what_do_i_know_about() populates WhatDoIKnowResponse.diagnostics."""

    @pytest.mark.asyncio
    async def test_diagnostics_is_a_search_diagnostics_instance(self) -> None:
        brain, _ = make_brain_service()
        response = await brain.what_do_i_know_about("topic")
        assert isinstance(response.diagnostics, SearchDiagnostics)

    @pytest.mark.asyncio
    async def test_candidates_before_threshold_and_best_raw_score(self) -> None:
        high = make_decision(title="high")
        low = make_decision(title="low")
        brain, _ = make_brain_service(
            decision_results=[(high, 0.9), (low, 0.05)],
            min_score=0.99,
        )
        response = await brain.what_do_i_know_about("topic")

        assert response.total == 0
        assert response.diagnostics.candidates_before_threshold == 2
        assert response.diagnostics.best_raw_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_tags_filtered_out_is_always_zero_grouped_has_no_tags(self) -> None:
        """WhatDoIKnowResponse has no tags parameter — grouped mode cannot
        remove anything by tag, so the counter must stay at zero."""
        brain, _ = make_brain_service()
        response = await brain.what_do_i_know_about("topic")
        assert response.diagnostics.tags_filtered_out == 0

    @pytest.mark.asyncio
    async def test_project_key_effective_is_the_injected_dream_scope_key(self) -> None:
        brain, _ = make_brain_service()

        with bind_dream_project_scope(_dream_scope()):
            response = await brain.what_do_i_know_about("topic", project_key="forged-project")

        assert response.diagnostics.project_key_requested == "forged-project"
        assert response.diagnostics.project_key_effective == DREAM_PROJECT_KEY
        assert response.diagnostics.project_key_injected_by_dream_scope is True
