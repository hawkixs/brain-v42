"""Pydantic models for Decision entity."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import DecayMixin, TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

DecisionStatus = Literal["active", "superseded", "deprecated"]


class DecisionBase(BaseModel):
    title: str = Field(..., max_length=200)
    description: str
    reasoning: str
    alternatives: list[str] = Field(default_factory=list)
    consequences: str | None = None
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    status: DecisionStatus = "active"
    metadata: dict = Field(default_factory=dict)


class DecisionCreate(DecisionBase, ProjectKeyCanonicalMixin):
    pass


class DecisionUpdate(ProjectKeyCanonicalMixin):
    # Reject unknown keys instead of silently dropping them. The decision
    # body lives in the single ``description`` column (Context + Decision
    # merged), so e.g. ``decision_made`` used to be ignored while still
    # returning "ok Updated" — a silent no-op footgun.
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    description: str | None = None
    reasoning: str | None = None
    alternatives: list[str] | None = None
    consequences: str | None = None
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] | None = None
    status: DecisionStatus | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None
    #: Posée par le SERVEUR seul — `brain_update` refuse une valeur fournie par
    #: l'appelant. Le trigger de la 043 l'efface si elle n'est pas redéclarée à
    #: chaque écriture : une provenance absente se voit, une fausse se croit.
    freshness_source: Literal["merge", "judgment", "score", "revive"] | None = None


class Decision(DecisionBase, TimestampMixin, DecayMixin):
    id: UUID = Field(default_factory=uuid4)
    superseded_by: UUID | None = None
    embedding: list[float] | None = None

    model_config = {"from_attributes": True}
