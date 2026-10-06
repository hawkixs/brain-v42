"""Pydantic models for ADR (Architecture Decision Record) entity."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import DecayMixin, TimestampMixin
from brain_v42.models.input_bounds import (
    LIST_MAX_ITEMS,
    KnowledgeText,
    TagList,
)
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

ADRStatus = Literal["proposed", "accepted", "deprecated", "superseded"]


class AlternativeConsidered(BaseModel):
    """An alternative option that was considered."""

    title: str
    description: str
    reason_rejected: str | None = None


class ADRBase(BaseModel):
    title: str = Field(..., max_length=200)
    context: str
    decision: str
    consequences: str
    alternatives_considered: list[AlternativeConsidered] = Field(default_factory=list)
    project_key: str = Field(..., max_length=50)
    tags: list[str] = Field(default_factory=list)
    status: ADRStatus = "proposed"
    metadata: dict = Field(default_factory=dict)


class ADRCreate(ADRBase, ProjectKeyCanonicalMixin):
    pass


class ADRUpdate(BaseModel):
    # Reject unknown keys instead of silently dropping them (see DecisionUpdate).
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    context: KnowledgeText | None = None
    decision: KnowledgeText | None = None
    consequences: KnowledgeText | None = None
    alternatives_considered: (
        Annotated[list[AlternativeConsidered], Field(max_length=LIST_MAX_ITEMS)] | None
    ) = None
    tags: TagList | None = None
    status: ADRStatus | None = None
    decided_at: datetime | None = None
    superseded_by: int | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None
    #: Written by the SERVER alone — `brain_update` rejects a caller-supplied
    #: value. The 043 trigger clears it when a write does not redeclare it: a
    #: missing provenance is visible, a false one is believed.
    freshness_source: (
        Literal["merge", "judgment", "score", "revive", "manual_update", "plan_reindex"] | None
    ) = None


class ADR(ADRBase, TimestampMixin, DecayMixin):
    id: UUID = Field(default_factory=uuid4)
    number: int = 0
    decided_at: datetime | None = None
    superseded_by: int | None = None  # ADR number (not UUID)
    embedding: list[float] | None = None

    model_config = {"from_attributes": True}
