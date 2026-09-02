"""Pydantic models for IndexedPlanChunk entity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class IndexedPlanChunkCreate(BaseModel):
    """Payload for inserting a chunk.

    `plan_id` is optional because the repository sets it from the parent plan
    row it just upserted; callers build chunks without knowing the plan UUID.
    """

    plan_id: UUID | None = None
    section_title: str = Field(..., max_length=500)
    section_path: str = Field(..., max_length=1000)
    content: str
    section_order: int
    word_count: int = 0
    project_key: str = Field(..., max_length=50)
    plan_type: Literal["spec", "plan"]
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)


class IndexedPlanChunk(BaseModel):
    """IndexedPlanChunk as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    plan_id: UUID
    section_title: str
    section_path: str
    content: str
    section_order: int
    word_count: int
    project_key: str
    plan_type: str
    status: str
    tags: list[str] = Field(default_factory=list)
    access_count: int = 0
    last_accessed_at: datetime | None = None
    created_at: datetime
    # Search-only hydration from the canonical indexed_plans parent. These
    # fields drive decay filtering/scoring but never expand public MCP payloads.
    parent_access_count: int | None = Field(default=None, exclude=True)
    parent_last_accessed_at: datetime | None = Field(default=None, exclude=True)
    # A plan's human signal can come ONLY from the parent: the `_human` columns
    # of migrations 041/044 live on `indexed_plans` and do not exist on chunks
    # (verified in `tables.py`). Without these two fields, the human branch of
    # `brain_service` read the attribute on the CHUNK, never found it
    # there, and scored EVERY plan at 0 human accesses and zero recency.
    parent_access_count_human: int | None = Field(default=None, exclude=True)
    parent_last_accessed_at_human: datetime | None = Field(default=None, exclude=True)
    parent_freshness_status: str | None = Field(default=None, exclude=True)
    parent_created_at: datetime | None = Field(default=None, exclude=True)
