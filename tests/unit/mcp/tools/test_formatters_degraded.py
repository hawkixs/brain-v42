"""Tests for degraded search rendering in format_search_results.

Fix 1 + Fix 2: When SearchResponse.degraded is set, format_search_results
must prepend a visible warning line so the LLM knows the results are degraded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from brain_v42.mcp.tools.formatters import format_knowledge_by_type, format_search_results
from brain_v42.models.brain import KnowledgeByType, SearchResult

NOW = datetime.now(UTC)


def _make_decision_dict(**kwargs) -> dict:
    defaults = {
        "id": str(uuid.uuid4()),
        "title": "Test Decision",
        "description": "desc",
        "reasoning": "reason",
        "project_key": "brain-v42",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "status": "active",
        "alternatives": [],
        "consequences": None,
        "superseded_by": None,
        "tags": [],
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
        "decided_at": None,
        "last_accessed_at": None,
        "embedding": None,
    }
    defaults.update(kwargs)
    return defaults


class TestFormatSearchResultsDegradedBanner:
    """format_search_results must prepend a warning when response.degraded is set."""

    def test_rrf_fallback_banner_present(self) -> None:
        """When degraded={'rerank_mode':'rrf_fallback'}, output starts with warning."""
        result = SearchResult(
            type="decision",
            score=0.7,
            item=_make_decision_dict(),
            title="Test",
        )
        output = format_search_results(
            [result],
            query="test",
            degraded={"rerank_mode": "rrf_fallback"},
        )
        # Must start with a warning line
        first_line = output.split("\n")[0]
        assert (
            "reranker" in first_line.lower()
            or "rrf" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected degraded warning in first line, got: {first_line!r}"

    def test_fts_fallback_banner_present(self) -> None:
        """When degraded={'search_mode':'fts_fallback'}, output starts with warning."""
        result = SearchResult(
            type="decision",
            score=0.7,
            item=_make_decision_dict(),
            title="Test",
        )
        output = format_search_results(
            [result],
            query="test",
            degraded={"search_mode": "fts_fallback"},
        )
        first_line = output.split("\n")[0]
        assert (
            "fts" in first_line.lower()
            or "embedding" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected fts_fallback warning in first line, got: {first_line!r}"

    def test_no_degraded_no_banner(self) -> None:
        """Without degraded marker, no warning line in output."""
        result = SearchResult(
            type="decision",
            score=0.7,
            item=_make_decision_dict(),
            title="Test",
        )
        output = format_search_results([result], query="test", degraded=None)
        # Output should NOT contain degraded keywords in first line
        first_line = output.split("\n")[0]
        assert "degraded" not in first_line.lower()
        assert "rrf_fallback" not in first_line.lower()
        assert "fts_fallback" not in first_line.lower()

    def test_degraded_none_still_formats_results(self) -> None:
        """format_search_results still works normally when degraded=None."""
        result = SearchResult(
            type="decision",
            score=0.7,
            item=_make_decision_dict(),
            title="Test",
        )
        output = format_search_results([result], query="test")
        # Must still have header and result
        assert "1 result" in output
        assert "Test Decision" in output

    def test_empty_results_with_degraded_still_shows_banner(self) -> None:
        """Even with 0 results, degraded banner is shown (so LLM understands why empty)."""
        output = format_search_results(
            [],
            query="test",
            degraded={"rerank_mode": "rrf_fallback"},
        )
        # Banner must appear even for empty results
        assert (
            "degraded" in output.lower() or "rrf" in output.lower() or "reranker" in output.lower()
        )

    def test_rrf_only_banner_present(self) -> None:
        """When degraded={'rerank_mode':'rrf_only'}, output starts with neutral banner.

        MINOR 2: rrf_only (no reranker configured) must also surface a banner
        so the LLM knows scores are RRF-based, not cross-encoder.
        """
        result = SearchResult(
            type="decision",
            score=0.7,
            item=_make_decision_dict(),
            title="Test",
        )
        output = format_search_results(
            [result],
            query="test",
            degraded={"rerank_mode": "rrf_only"},
        )
        first_line = output.split("\n")[0]
        assert (
            "rrf" in first_line.lower()
            or "reranker" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected rrf_only banner in first line, got: {first_line!r}"


# ---------------------------------------------------------------------------
# MAJOR: format_knowledge_by_type degraded banner (group_by_type=True mode)
# ---------------------------------------------------------------------------


def _make_learning_dict(**kwargs) -> dict:
    now = datetime.now(UTC)
    defaults = {
        "id": str(uuid.uuid4()),
        "topic": "Test Topic",
        "insight": "Test insight text",
        "project_key": "brain-v42",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "confidence": "medium",
        "source": None,
        "source_type": "experience",
        "validated_at": None,
        "tags": [],
        "access_count": 0,
        "freshness_status": "fresh",
        "merged_into": None,
        "last_accessed_at": None,
        "embedding": None,
    }
    defaults.update(kwargs)
    return defaults


class TestFormatKnowledgeByTypeDegradedBanner:
    """MAJOR review finding: format_knowledge_by_type must show a degraded banner
    when degraded is set, mirroring format_search_results behavior.

    Previously: no `degraded` param, banner never shown for group_by_type=True.
    This means rank-based scores looked like confident cross-encoder scores.
    """

    def test_format_knowledge_by_type_shows_rrf_fallback_banner(self) -> None:
        """When degraded={'rerank_mode':'rrf_fallback'}, banner must be prepended.

        MAJOR: format_knowledge_by_type had no degraded param before the fix.
        This test fails until the param is added and banner logic is implemented.
        """
        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=1.0,
                    item=_make_learning_dict(),
                )
            ]
        )
        output = format_knowledge_by_type(
            by_type, topic="test", degraded={"rerank_mode": "rrf_fallback"}
        )
        first_line = output.split("\n")[0]
        assert (
            "reranker" in first_line.lower()
            or "rrf" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected degraded banner in first line for group_by_type mode, got: {first_line!r}"

    def test_format_knowledge_by_type_shows_fts_fallback_banner(self) -> None:
        """When degraded={'search_mode':'fts_fallback'}, banner must be prepended."""
        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=0.9,
                    item=_make_learning_dict(),
                )
            ]
        )
        output = format_knowledge_by_type(
            by_type, topic="test", degraded={"search_mode": "fts_fallback"}
        )
        first_line = output.split("\n")[0]
        assert (
            "fts" in first_line.lower()
            or "embedding" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected fts_fallback banner in first line, got: {first_line!r}"

    def test_format_knowledge_by_type_shows_rrf_only_banner(self) -> None:
        """When degraded={'rerank_mode':'rrf_only'}, neutral banner must be prepended.

        MINOR 2: rrf_only is a permanent mode (no reranker configured).
        The LLM should see a neutral note that ordering is RRF-based.
        """
        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=0.5,
                    item=_make_learning_dict(),
                )
            ]
        )
        output = format_knowledge_by_type(
            by_type, topic="test", degraded={"rerank_mode": "rrf_only"}
        )
        first_line = output.split("\n")[0]
        assert (
            "rrf" in first_line.lower()
            or "reranker" in first_line.lower()
            or "degraded" in first_line.lower()
        ), f"Expected rrf_only banner in first line, got: {first_line!r}"

    def test_format_knowledge_by_type_no_banner_when_healthy(self) -> None:
        """Without degraded, format_knowledge_by_type must NOT show a banner.

        Backwards-compatible: existing callers without degraded kwarg still work.
        """
        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=0.9,
                    item=_make_learning_dict(),
                )
            ]
        )
        output = format_knowledge_by_type(by_type, topic="test")
        first_line = output.split("\n")[0]
        assert "degraded" not in first_line.lower()
        assert "rrf" not in first_line.lower()
        assert "fts" not in first_line.lower()

    def test_format_knowledge_by_type_no_banner_degraded_none(self) -> None:
        """Explicit degraded=None also produces no banner (matches healthy behavior)."""
        by_type = KnowledgeByType()
        output = format_knowledge_by_type(by_type, topic="test", degraded=None)
        assert "degraded" not in output.lower() or "## Everything" in output
