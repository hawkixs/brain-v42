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

import uuid

import pytest
from pydantic import ValidationError

from brain_v42.models.brain import SearchResult
from brain_v42.services.search.hybrid import SCORE_KIND_UNSET, RankedCandidate


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


class TestRankedCandidateScoreKindSentinelIsNeverPlausible:
    def test_default_score_kind_is_not_one_of_the_real_values(self) -> None:
        """A RankedCandidate built without an explicit score_kind (i.e. a
        producer that forgot to set it) must carry a value that is
        detectably wrong, not one of the three real ScoreKind values.

        If this ever regresses to a plausible default (e.g. "cross_encoder"),
        a missed producer would render a rank ordinal as a calibrated score
        with nothing downstream able to tell the difference.
        """
        candidate = RankedCandidate(
            id=uuid.uuid4(),
            entity=None,
            entity_type="learning",
            score=0.0,
            text="t",
        )

        assert candidate.score_kind == SCORE_KIND_UNSET
        assert candidate.score_kind not in {"cross_encoder", "rank", "fts_rank"}

    def test_sentinel_is_rejected_by_search_result(self) -> None:
        """The sentinel is not just "different" — it is REJECTED the moment
        it would reach the public SearchResult contract, which is the actual
        enforcement point (RankedCandidate itself has no runtime validation).
        """
        candidate = RankedCandidate(
            id=uuid.uuid4(),
            entity=None,
            entity_type="learning",
            score=0.0,
            text="t",
        )

        with pytest.raises(ValidationError):
            SearchResult(
                type="learning",
                score=candidate.score,
                score_kind=candidate.score_kind,
                item={},
            )
