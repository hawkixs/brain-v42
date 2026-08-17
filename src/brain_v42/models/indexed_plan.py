"""Pydantic models for IndexedPlan entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from brain_v42.models.base import TimestampMixin


class IndexedPlanCreate(BaseModel):
    """Payload for creating/upserting an indexed plan."""

    file_path: str = Field(..., max_length=500)
    title: str = Field(..., max_length=500)
    plan_type: Literal["spec", "plan"]
    project_key: str = Field(..., max_length=50)
    content_hash: str = Field(..., max_length=64)
    content: str
    summary: str | None = None
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int = 0
    word_count: int = 0


class IndexedPlan(TimestampMixin, BaseModel):
    """IndexedPlan as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    file_path: str
    title: str
    plan_type: str
    project_key: str
    content_hash: str
    content: str
    summary: str | None = None
    status: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int
    word_count: int
    access_count: int = 0
    last_accessed_at: datetime | None = None
    freshness_status: str
    indexed_at: datetime
