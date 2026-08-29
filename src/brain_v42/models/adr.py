"""Pydantic models for ADR (Architecture Decision Record) entity."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import DecayMixin, TimestampMixin
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
    context: str | None = None
    decision: str | None = None
    consequences: str | None = None
    alternatives_considered: list[AlternativeConsidered] | None = None
    tags: list[str] | None = None
    status: ADRStatus | None = None
    decided_at: datetime | None = None
    superseded_by: int | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None
    #: Posée par le SERVEUR seul — `brain_update` refuse une valeur fournie par
    #: l'appelant. Le trigger de la 043 l'efface si elle n'est pas redéclarée à
    #: chaque écriture : une provenance absente se voit, une fausse se croit.
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
