"""Pydantic models for BrainService global search responses."""

from __future__ import annotations

from typing import Any, Literal, cast, get_args
from uuid import UUID

from pydantic import BaseModel, Field

MutableKnowledgeType = Literal["decision", "learning", "snippet", "runbook", "adr"]
KnowledgeType = Literal[MutableKnowledgeType, "plan"]

ALL_TYPES = cast(list[KnowledgeType], list(get_args(KnowledgeType)))


class SearchResult(BaseModel):
    """A single item returned by global search."""

    type: KnowledgeType
    score: float = Field(
        ...,
        ge=0.0,
        description="Relevance score (cosine similarity for vector search, RRF fusion score for hybrid search)",
    )
    item: dict[str, Any]
    # Convenience fields populated for all types (extracted from item for easy access)
    title: str | None = None
    project_key: str | None = None
    tags: list[str] | None = None
    # parent_id is populated for plan chunks — points to the parent indexed_plan row
    parent_id: UUID | None = None


class SearchDiagnostics(BaseModel):
    """Diagnostics computed from state the search pipeline already knows.

    Populated on EVERY call — no extra queries on the nominal path, these
    counters are read off ``_fan_out``/``_build_search_results`` at the point
    where the numbers already exist. The formatter only renders them when
    ``total == 0``: see ``formatters.clamp_list_limit``'s doctrine ("A cap
    applied silently makes the result lie" / "The notice is EMPTY in the
    nominal case") — the same reasoning applies to an unexplained 0-result
    answer (investigation W11, 2026-09-06: 1,027/2,936 empty brain_search
    calls, 0 of them carrying a top_score, "## 0 results" rendered
    identically for three structurally different reasons).

    All fields default to a benign zero/empty value so a caller that builds
    a ``SearchResponse``/``WhatDoIKnowResponse`` without diagnostics (older
    tests, direct fixtures) gets a harmless placeholder rather than ``None``
    — telemetry can always read this object without a null-check.
    """

    candidates_before_threshold: int = Field(
        default=0,
        description=(
            "Fused candidates across all searched types, BEFORE the min_score cut. "
            "Bounded by the hybrid fan-out's internal fused[:20] cap and, when "
            "limit < 20, further bounded by limit — a FLOOR on the true "
            "candidate pool, not necessarily its full size. Render it as 'at "
            "least N', never as an exact count."
        ),
    )
    best_raw_score: float | None = Field(
        default=None,
        description="Highest raw (pre-decay) score among candidates_before_threshold, or None.",
    )
    min_score_requested: float = Field(
        default=0.0,
        description="The min_score threshold that would apply absent degraded-mode override.",
    )
    min_score_effective: float = Field(
        default=0.0,
        description="The min_score threshold actually applied (0.0 when fan-out is degraded).",
    )
    tags_filtered_out: int = Field(
        default=0,
        description="Entities excluded by the post-filter tags overlap check (flat search only).",
    )
    survived_threshold: int = Field(
        default=0,
        description=(
            "Candidates that cleared the min_score cut (i.e. NOT excluded by "
            "min_score), before the archived/merged filter and the tags filter "
            "are applied. Counted independently of candidates_before_threshold "
            "and tags_filtered_out — the three do not sum to candidates_before_threshold "
            "when several filters remove different subsets."
        ),
    )
    project_group_requested: str | None = Field(
        default=None,
        description="The project_group argument as received from the caller.",
    )
    project_group_resolved_keys: list[str] = Field(
        default_factory=list,
        description=(
            "The project_keys project_group_requested resolved to via "
            "ProjectContextService.get_keys_by_group(), set on the RESOLVED path "
            "(project_group_unresolved=False) alongside project_group_requested. "
            "Empty when no project_group was requested, or when it was requested "
            "but resolved to zero keys (see project_group_unresolved instead)."
        ),
    )
    project_group_unresolved: bool = Field(
        default=False,
        description=(
            "True when project_group_requested resolved to zero project_keys via "
            "ProjectContextService.get_keys_by_group() — the fan-out never ran, "
            "so every other counter in this object is meaningless zero, not a "
            "measurement."
        ),
    )
    types_searched: list[KnowledgeType] = Field(
        default_factory=list,
        description="Mirrors the top-level types_searched field.",
    )
    project_key_requested: str | None = Field(
        default=None,
        description="The project_key argument as received from the caller.",
    )
    project_key_effective: str | None = Field(
        default=None,
        description="The project_key actually applied to the fan-out.",
    )
    project_key_injected_by_dream_scope: bool = Field(
        default=False,
        description=(
            "True when project_key_effective was overridden by the dream project "
            "scope (get_dream_project_scope()), not by the caller's own argument."
        ),
    )
    include_archived: bool = Field(default=False)
    rerank_mode: str | None = Field(
        default=None,
        description=(
            "Observed rerank mode: 'reranked', 'rrf_fallback', or 'rrf_only'. "
            "None when no hybrid searcher is configured or fan-out used the "
            "FTS-only embedding fallback (search_mode covers that case instead)."
        ),
    )
    degraded: bool = Field(
        default=False,
        description="True when the fan-out ran in ANY degraded mode (mirrors SearchResponse.degraded).",
    )


class SearchResponse(BaseModel):
    """Aggregated response from brain_search."""

    query: str
    results: list[SearchResult]
    total: int
    types_searched: list[KnowledgeType]
    related: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Graph neighbors for result entities (empty when graph is not available).",
    )
    degraded: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Set when the search ran in degraded mode. "
            "Keys: rerank_mode ('rrf_fallback' when reranker is down, "
            "'rrf_only' when no reranker is configured — permanent mode), "
            "search_mode ('fts_fallback' when embedding service is down). "
            "None when search ran fully."
        ),
    )
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)


class KnowledgeByType(BaseModel):
    """Results grouped by knowledge type (for what_do_i_know_about)."""

    decisions: list[SearchResult] = Field(default_factory=list)
    learnings: list[SearchResult] = Field(default_factory=list)
    snippets: list[SearchResult] = Field(default_factory=list)
    runbooks: list[SearchResult] = Field(default_factory=list)
    adrs: list[SearchResult] = Field(default_factory=list)
    plans: list[SearchResult] = Field(default_factory=list)


class WhatDoIKnowResponse(BaseModel):
    """Response for brain_what_do_i_know_about."""

    topic: str
    by_type: KnowledgeByType
    total: int
    types_searched: list[KnowledgeType]
    degraded: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Set when the search ran in degraded mode. "
            "Keys: rerank_mode ('rrf_fallback' when reranker is down, "
            "'rrf_only' when no reranker is configured), "
            "search_mode ('fts_fallback' when embedding service is down). "
            "None when search ran fully."
        ),
    )
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)
