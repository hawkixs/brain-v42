"""Pydantic models for Snippet entity."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import DecayMixin, TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

SnippetLanguage = Annotated[str, Field(max_length=50)]


class SnippetBase(BaseModel):
    title: str = Field(..., max_length=200)
    intention: str
    code: str
    language: SnippetLanguage
    dependencies: list[str] = Field(default_factory=list)
    usage_example: str | None = None
    gotchas: str | None = None
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class SnippetCreate(SnippetBase, ProjectKeyCanonicalMixin):
    pass


class SnippetUpdate(ProjectKeyCanonicalMixin):
    # Reject unknown keys instead of silently dropping them (see DecisionUpdate).
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    intention: str | None = None
    code: str | None = None
    language: SnippetLanguage | None = None
    dependencies: list[str] | None = None
    usage_example: str | None = None
    gotchas: str | None = None
    project_key: str | None = Field(None, max_length=50)
    tags: list[str] | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None
    #: Posée par le SERVEUR seul — `brain_update` refuse une valeur fournie par
    #: l'appelant. Le trigger de la 043 l'efface si elle n'est pas redéclarée à
    #: chaque écriture : une provenance absente se voit, une fausse se croit.
    freshness_source: Literal["merge", "judgment", "score", "revive"] | None = None


class Snippet(SnippetBase, TimestampMixin, DecayMixin):
    id: UUID = Field(default_factory=uuid4)
    use_count: int = 0
    last_used_at: datetime | None = None
    embedding: list[float] | None = None

    model_config = {"from_attributes": True}
