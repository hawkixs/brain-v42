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
