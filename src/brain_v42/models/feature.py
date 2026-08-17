"""Pydantic models for Feature tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from brain_v42.models.base import TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

VALID_FEATURE_STATUSES = (
    "planned",
    "research",
    "design",
    "building",
    "deployed",
    "done",
    "archived",
)
CreatableFeatureStatus = Literal[
    "planned",
    "research",
    "design",
    "building",
    "deployed",
    "done",
]
CREATABLE_FEATURE_STATUSES: tuple[CreatableFeatureStatus, ...] = (
    "planned",
    "research",
    "design",
    "building",
    "deployed",
    "done",
)


class FeatureCreate(ProjectKeyCanonicalMixin):
    """Payload for creating a new feature."""

    project_key: str = Field(..., max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=10_000)
    status: CreatableFeatureStatus = "planned"
    pinned: bool = True

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_non_blank_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("status")
    @classmethod
    def _validate_creatable_status(cls, value: str) -> str:
        if value not in CREATABLE_FEATURE_STATUSES:
            valid = ", ".join(CREATABLE_FEATURE_STATUSES)
            raise ValueError(f"feature creation status must be one of: {valid}")
        return value


class Feature(TimestampMixin, BaseModel):
    """Feature as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    project_key: str
    name: str
    description: str
    status: str
    status_updated_at: datetime
    pinned: bool = False


class RoadmapFeature(BaseModel):
    """Feature with artifact counts for roadmap display."""

    name: str
    status: str
    status_updated_at: datetime
    pinned: bool
    artifact_count: dict[str, int]
    last_activity: datetime | None


class RoadmapProject(BaseModel):
    """Project with features for roadmap display."""

    project_key: str
    name: str
    current_phase: str | None
    features: list[RoadmapFeature]
