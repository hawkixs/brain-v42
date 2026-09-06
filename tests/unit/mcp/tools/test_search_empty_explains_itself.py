"""RED-phase tests for lot G2 — the empty search result explains itself.

Investigation W11 (2026-09-06): 1,027 of 2,936 brain_search calls returned 0
results and none of them carried a top_score — "## 0 results" rendered
identically whether (a) there was nothing in scope, (b) candidates existed but
fell under min_score, or (c) the tags filter removed everything. This module
pins the three distinguishable empty renderings (flat and grouped), plus the
guarantee that a NON-empty render does not change by a single byte.

Doctrine cited: formatters.py ``clamp_list_limit`` (~864-882) — "A cap applied
silently makes the result lie" / "The notice is EMPTY in the nominal case".
The same rule applies here: a 0-result answer that does not say WHY teaches
the caller nothing about whether to retry, loosen a filter, or give up.
"""

from __future__ import annotations

from brain_v42.mcp.tools.formatters import format_knowledge_by_type, format_search_results
from brain_v42.models.brain import KnowledgeByType, SearchDiagnostics, SearchResult
from tests.unit.mcp.tools.test_formatters import _make_decision, _make_learning


class TestFlatEmptyExplainsItself:
    """format_search_results([], ...) with diagnostics distinguishes the 3 kinds."""

    def test_zero_candidates_in_scope(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            best_raw_score=None,
            min_score_requested=0.2,
            min_score_effective=0.2,
            tags_filtered_out=0,
            types_searched=["decision", "learning"],
            project_key_requested="brain-v42",
            project_key_effective="brain-v42",
            project_key_injected_by_dream_scope=False,
            include_archived=False,
            rerank_mode="reranked",
            degraded=False,
        )
        output = format_search_results([], query="nothing here", diagnostics=diagnostics)

        assert "0 candidates in scope" in output
        assert "decision" in output
        assert "learning" in output
        assert "brain-v42" in output
        assert "archived excluded" in output

    def test_zero_candidates_in_scope_marks_dream_scope_injection(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision"],
            project_key_requested="forged-project",
            project_key_effective="dream-owned",
            project_key_injected_by_dream_scope=True,
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "dream-owned" in output
        assert "injected" in output.lower()

    def test_candidates_below_min_score(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=5,
            best_raw_score=0.14,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision", "learning"],
            project_key_requested=None,
            project_key_effective=None,
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "5 candidates" in output
        assert "none above min_score" in output
        assert "0.2" in output
        assert "0.14" in output
        assert "before decay" in output
        assert "0 candidates in scope" not in output

    def test_candidates_removed_by_tags_filter(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=3,
            best_raw_score=0.91,
            min_score_requested=0.2,
            min_score_effective=0.2,
            tags_filtered_out=3,
            types_searched=["decision"],
        )
        output = format_search_results(
            [], query="x", diagnostics=diagnostics, tags=["dream:scan", "sec2"]
        )

        assert "3 candidates" in output
        assert "removed by the tags filter" in output
        assert "dream:scan" in output
        assert "sec2" in output
        assert "none above min_score" not in output

    def test_mixed_pool_reports_tags_filtered_count_not_total_candidates(self) -> None:
        """Fix round (lot G2 blocker): a MIXED pool — most candidates killed
        by min_score, a disjoint few removed by tags — must report the
        tags-removal count, not the whole pool. Before the fix this branch
        read candidates_before_threshold (40), not tags_filtered_out (1),
        pointing the caller at "drop a tag" when the real fix is "lower
        min_score" for the other 39.
        """
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=40,
            best_raw_score=0.91,
            min_score_requested=0.2,
            min_score_effective=0.2,
            tags_filtered_out=1,
            types_searched=["decision"],
        )
        output = format_search_results([], query="x", diagnostics=diagnostics, tags=["sec2"])

        assert "1 candidate removed by the tags filter" in output
        assert "40 candidates" not in output

    def test_tags_filter_reason_falls_through_when_caller_tags_missing(self) -> None:
        """Regression (minor): if tags_filtered_out > 0 but the formatter's own
        `tags` argument is empty (a drifted/mismatched caller), the tags-filter
        kind must not render an empty bracket — fall through to the next kind
        instead of asserting a fact ('the tags filter removed these') the
        formatter cannot name."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=5,
            best_raw_score=0.91,
            min_score_requested=0.2,
            min_score_effective=0.2,
            tags_filtered_out=5,
            types_searched=["decision"],
        )
        output = format_search_results([], query="x", diagnostics=diagnostics, tags=None)

        assert "removed by the tags filter" not in output

    def test_fallback_kind_uses_survived_threshold_not_total_candidates(self) -> None:
        """Fix round (lot G2 major): the archived/merged-filter fallback kind
        must report how many candidates SURVIVED min_score, not the raw
        candidates_before_threshold count (which also counts candidates that
        never cleared min_score at all)."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=10,
            best_raw_score=0.91,
            min_score_requested=0.2,
            min_score_effective=0.2,
            survived_threshold=3,
            tags_filtered_out=0,
            types_searched=["decision"],
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "3 candidates above min_score" in output
        assert "10 candidates above min_score" not in output

    def test_project_group_unresolved_names_itself(self) -> None:
        """Fix round (lot G2 major): an unresolvable project_group must not
        render as '0 candidates in scope' / 'project: none (admin scope)' —
        that claims the whole corpus was searched and empty, when the
        fan-out never ran at all because the group name matched nothing."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision", "learning"],
            project_group_requested="typo-group-name",
            project_group_unresolved=True,
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "typo-group-name" in output
        assert "matched 0 projects" in output
        assert "0 candidates in scope" not in output
        assert "admin scope" not in output

    def test_rerank_mode_surfaced_when_not_nominal(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision"],
            rerank_mode="rrf_fallback",
            degraded=True,
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "rrf_fallback" in output

    def test_rerank_mode_not_mentioned_twice_needlessly_when_nominal(self) -> None:
        """A healthy 'reranked' mode is not degraded — no need to call it out
        beyond what the existing degraded-banner mechanism already covers."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision"],
            rerank_mode="reranked",
            degraded=False,
        )
        output = format_search_results([], query="x", diagnostics=diagnostics)

        assert "rerank mode: reranked" not in output.lower()

    def test_no_diagnostics_falls_back_to_bare_header(self) -> None:
        """Callers that never pass diagnostics (raw formatter unit tests, older
        call sites) keep today's minimal '0 results' rendering — no crash."""
        output = format_search_results([], query="nothing")
        assert "0 results" in output
        assert "candidates" not in output

    def test_nominal_non_empty_output_is_byte_identical_with_or_without_diagnostics(
        self,
    ) -> None:
        """The nominal (non-empty) path must not change by a single byte —
        diagnostics only affects the EMPTY rendering."""
        results = [
            SearchResult(
                type="learning",
                score=0.85,
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=0.80,
                item=_make_decision().model_dump(mode="json"),
            ),
        ]
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=2,
            best_raw_score=0.85,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision", "learning"],
        )
        without = format_search_results(results, query="monitoring")
        with_diag = format_search_results(results, query="monitoring", diagnostics=diagnostics)

        assert without == with_diag


class TestGroupedEmptyExplainsItself:
    """format_knowledge_by_type(...) with diagnostics mirrors the flat rendering."""

    def test_zero_candidates_in_scope(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision", "learning", "snippet", "runbook", "adr", "plan"],
            project_key_requested=None,
            project_key_effective=None,
            include_archived=False,
        )
        output = format_knowledge_by_type(
            KnowledgeByType(), topic="nothing", diagnostics=diagnostics
        )

        assert "0 candidates in scope" in output
        assert "archived excluded" in output

    def test_project_group_unresolved_names_itself(self) -> None:
        """Fix round (lot G2 major): grouped mode carries the same
        project_group_unresolved kind as flat search — the WDIKA early return
        must not be rendered as an empty-corpus scan either."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=0,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision"],
            project_group_requested="typo-group-name",
            project_group_unresolved=True,
        )
        output = format_knowledge_by_type(KnowledgeByType(), topic="x", diagnostics=diagnostics)

        assert "typo-group-name" in output
        assert "matched 0 projects" in output
        assert "0 candidates in scope" not in output

    def test_candidates_below_min_score(self) -> None:
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=7,
            best_raw_score=0.05,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["decision"],
        )
        output = format_knowledge_by_type(KnowledgeByType(), topic="x", diagnostics=diagnostics)

        assert "7 candidates" in output
        assert "none above min_score" in output
        assert "0.05" in output

    def test_grouped_mode_never_reports_tags_filtered_since_it_has_no_tags_param(self) -> None:
        """WhatDoIKnowResponse has no tags argument — tags_filtered_out is
        always 0, so the tags-filter kind can never surface here."""
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=4,
            best_raw_score=0.9,
            min_score_requested=0.2,
            min_score_effective=0.2,
            tags_filtered_out=0,
            types_searched=["decision"],
        )
        output = format_knowledge_by_type(KnowledgeByType(), topic="x", diagnostics=diagnostics)

        assert "removed by the tags filter" not in output

    def test_no_diagnostics_falls_back_to_bare_header(self) -> None:
        output = format_knowledge_by_type(KnowledgeByType(), topic="nothing")
        assert "0 items" in output
        assert "candidates" not in output

    def test_nominal_non_empty_output_is_byte_identical_with_or_without_diagnostics(
        self,
    ) -> None:
        by_type = KnowledgeByType(
            learnings=[
                SearchResult(
                    type="learning",
                    score=0.9,
                    item=_make_learning().model_dump(mode="json"),
                )
            ],
        )
        diagnostics = SearchDiagnostics(
            candidates_before_threshold=1,
            best_raw_score=0.9,
            min_score_requested=0.2,
            min_score_effective=0.2,
            types_searched=["learning"],
        )
        without = format_knowledge_by_type(by_type, topic="monitoring")
        with_diag = format_knowledge_by_type(by_type, topic="monitoring", diagnostics=diagnostics)

        assert without == with_diag
