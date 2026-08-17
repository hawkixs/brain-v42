"""Pydantic models for Learning entity."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import DecayMixin, TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

SourceType = Literal[
    "experience",
    "documentation",
    "code_review",
    "bug",
    "external",
    "article",
    "video",
    "book",
    "conversation",
    "research",
    "automated",
]
Confidence = Literal["low", "medium", "high"]


class LearningBase(BaseModel):
    topic: str = Field(..., max_length=200)
    insight: str
    source: str | None = None
    source_type: SourceType = "experience"
    confidence: Confidence = "medium"
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class LearningCreate(LearningBase, ProjectKeyCanonicalMixin):
    pass


class LearningUpdate(ProjectKeyCanonicalMixin):
    # Reject unknown keys instead of silently dropping them (see DecisionUpdate).
    model_config = {"extra": "forbid"}

    topic: str | None = Field(None, max_length=200)
    insight: str | None = None
    source: str | None = None
    source_type: SourceType | None = None
    confidence: Confidence | None = None
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None


class Learning(LearningBase, TimestampMixin, DecayMixin):
    id: UUID = Field(default_factory=uuid4)
    validated_at: datetime | None = None
    embedding: list[float] | None = None

    model_config = {"from_attributes": True}
