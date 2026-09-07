"""W33 "a rank is not a score" (2026-09-07) — score_kind has no plausible default.

SearchResult.score is a single untyped float that meant three incompatible
things: a cross-encoder sigmoid, a rank ordinal rescaled into (0, 1]
(reranker fallback), or another rank ordinal (FTS-only fallback). None of
those are distinguishable by value alone — the fallback's top score is
EXACTLY 1.0, indistinguishable from a genuine "perfect match" cross-encoder
sigmoid.

score_kind fixes that by naming the PROVENANCE, not the value. This module
pins the property that makes the fix load-bearing: a producer that forgets
to set score_kind must fail loudly, never silently masquerade as a real
score by picking up some plausible-looking default.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_v42.models.brain import SearchResult


class TestSearchResultScoreKindHasNoPlausibleDefault:
    def test_omitting_score_kind_is_rejected(self) -> None:
        """SearchResult.score_kind is REQUIRED — no default at all.

        A missed producer cannot silently construct a SearchResult without
        naming the provenance of its score: pydantic rejects construction
        outright, rather than falling back to a value that would render
        identically to a real cross-encoder score.
        """
        with pytest.raises(ValidationError):
            SearchResult(type="learning", score=0.5, item={})

    def test_only_the_three_named_values_are_accepted(self) -> None:
        """score_kind is a closed Literal — a stray/typo'd value is rejected,
        not silently accepted and rendered as if it were legitimate.
        """
        for good in ("cross_encoder", "rank", "fts_rank"):
            SearchResult(type="learning", score=0.5, score_kind=good, item={})

        with pytest.raises(ValidationError):
            SearchResult(
                type="learning",
                score=0.5,
                score_kind="score_kind_unset",
                item={},
            )


# RankedCandidate (services/search/hybrid.py) used to carry its own
# score_kind field with a SCORE_KIND_UNSET sentinel, set by reranker.py but
# never read by anything — a second, unenforced source of truth alongside
# the real one below. It has been removed (2026-09-07 fix round): the only
# place score_kind is decided is brain_service._fan_out's rerank_mode ->
# score_kind mapping, and the only enforcement point is direct dict indexing
# (no `.get(..., default)`) at the two SearchResult construction sites — see
# tests/unit/services/test_brain_service.py::TestScoreKindByTypeHasNoPlausibleDefault.
