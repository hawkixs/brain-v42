"""W33 "a rank is not a score" (2026-09-07): rendering must not lie about a
rank ordinal being a calibrated score.

Incident: a degraded reranker rescales scores by rank into (0, 1] (top result
gets exactly 1.0) so min_score filtering doesn't zero everything out
(incident d3cf29e9). Before this fix, format_search_results printed that
value as `[s:1.00]` — indistinguishable from a genuine cross-encoder
"perfect match" sigmoid. score_kind fixes the RENDERING half of that: a
non-"cross_encoder" result must print `[rank i/n]`, never `[s:...]`, and the
nominal ("cross_encoder") path must stay byte-identical.
"""

from __future__ import annotations

from brain_v42.mcp.tools.formatters import format_search_results
from brain_v42.models.brain import SearchResult
from tests.unit.mcp.tools.test_formatters import _make_decision, _make_learning


class TestNominalRenderingIsByteIdentical:
    def test_cross_encoder_rendering_is_byte_identical_to_the_pre_lot_golden(
        self,
    ) -> None:
        """Golden captured from the unmodified formatter before this lot —
        [s:X.XX], not [rank i/n], on the score_kind="cross_encoder" path.
        """
        results = [
            SearchResult(
                type="learning",
                score=0.85,
                score_kind="cross_encoder",
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=0.42,
                score_kind="cross_encoder",
                item=_make_decision().model_dump(mode="json"),
            ),
        ]

        rendered = format_search_results(results, query="x")

        expected = (
            '## 2 results for "x" (across all types)\n'
            "\n"
            "### Decisions (1)\n"
            "[s:0.42] 1. **Docker containers grouped by Compose stack** [active] "
            "(id:abc12345-0000-0000-0000-000000000000)\n"
            "   project:red | tags: docker, dashboard | access:0 | reads:0\n"
            "\n"
            "### Learnings (1)\n"
            "[s:0.85] 1. **GPU collector NVML** [high] "
            "(id:ee3b329a-ba3a-468d-a2cd-04a98c80ea9d)\n"
            "   project:red | tags: gpu, nvml | access:0 | reads:0"
        )
        assert rendered == expected


class TestDegradedRenderingNeverClaimsAScore:
    def test_rank_results_render_as_rank_never_as_score(self) -> None:
        """score_kind='rank' (rrf_fallback / rrf_only) must never emit '[s:'.

        Uses the exact incident shape: 20-candidate shard rank-rescaled
        scores, top result at 1.00 — the number a naive '[s:{score:.2f}]'
        would render as a perfect cross-encoder match.
        """
        n = 20
        results = [
            SearchResult(
                type="learning",
                score=(n - rank) / n,
                score_kind="rank",
                item=_make_learning().model_dump(mode="json"),
            )
            for rank in range(n)
        ]

        rendered = format_search_results(results, query="zzqx quokka widget nonexistent gizmo")

        assert "[s:" not in rendered
        assert "[rank 1/20]" in rendered
        assert "[rank 20/20]" in rendered

    def test_fts_rank_results_also_render_as_rank_not_score(self) -> None:
        """score_kind='fts_rank' (embedding-down FTS-only fallback) must
        equally never emit '[s:'.
        """
        results = [
            SearchResult(
                type="decision",
                score=1.0,
                score_kind="fts_rank",
                item=_make_decision().model_dump(mode="json"),
            ),
        ]

        rendered = format_search_results(results, query="q")

        assert "[s:" not in rendered
        assert "[rank 1/1]" in rendered

    def test_mixed_shards_render_each_by_its_own_score_kind(self) -> None:
        """A partial blackout — one shard reranked fine, another degraded —
        must render each SECTION according to its own score_kind, not a
        query-wide flag.
        """
        results = [
            SearchResult(
                type="learning",
                score=0.85,
                score_kind="cross_encoder",
                item=_make_learning().model_dump(mode="json"),
            ),
            SearchResult(
                type="decision",
                score=1.0,
                score_kind="rank",
                item=_make_decision().model_dump(mode="json"),
            ),
        ]

        rendered = format_search_results(results, query="q")

        assert "[s:0.85]" in rendered
        assert "[rank 1/1]" in rendered
        assert "[s:1.00]" not in rendered
