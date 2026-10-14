"""The degraded-search banners are LLM-facing text and are written in English.

Operator ruling (brain decision 5da112d1, ticket bc245aa7): both formatters
render the same English banner for each degraded marker, and the two documents
that quote the banners quote the rendered text, so a later edit cannot let the
code and the documentation drift apart in silence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_v42.mcp.tools.formatters import format_knowledge_by_type, format_search_results
from brain_v42.models.brain import KnowledgeByType

REPO_ROOT = Path(__file__).resolve().parents[4]

BANNERS = {
    "fts_fallback": (
        {"search_mode": "fts_fallback"},
        "degraded: embedding service unavailable — FTS results only (text order)",
    ),
    "rrf_fallback": (
        {"rerank_mode": "rrf_fallback"},
        "degraded: reranker unavailable — RRF order (no cross-encoder re-scoring)",
    ),
    "rrf_only": (
        {"rerank_mode": "rrf_only"},
        "note: reranker not configured — RRF order (no cross-encoder re-scoring)",
    ),
}


def _search_banner(degraded: dict[str, str]) -> str:
    return format_search_results([], query="q", degraded=degraded).split("\n")[0]


def _grouped_banner(degraded: dict[str, str]) -> str:
    return format_knowledge_by_type(KnowledgeByType(), topic="q", degraded=degraded).split("\n")[0]


@pytest.mark.parametrize("render", [_search_banner, _grouped_banner], ids=["search", "grouped"])
@pytest.mark.parametrize("marker", sorted(BANNERS))
def test_each_formatter_renders_the_english_banner(render, marker: str) -> None:
    degraded, expected = BANNERS[marker]
    assert render(degraded) == expected


@pytest.mark.parametrize("marker", sorted(BANNERS))
def test_banner_is_ascii_apart_from_the_dash(marker: str) -> None:
    degraded, _ = BANNERS[marker]
    banner = _search_banner(degraded)
    assert banner.replace("—", "").isascii(), banner


@pytest.mark.parametrize("document", ["docs/MCP_TOOLS.md", "docs/ARCHITECTURE.md"])
@pytest.mark.parametrize("marker", sorted(BANNERS))
def test_documents_quote_the_rendered_banner(document: str, marker: str) -> None:
    degraded, _ = BANNERS[marker]
    text = (REPO_ROOT / document).read_text(encoding="utf-8")
    assert _search_banner(degraded) in text, f"{document} does not quote the {marker} banner"
