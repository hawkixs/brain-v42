"""Pydantic models for GitLabEvent entity."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GitLabEventCreate(BaseModel):
    """Payload for creating a GitLab event."""

    gitlab_event_id: str | None = Field(None, max_length=100)
    event_type: str = Field(..., max_length=30)
    project_key: str = Field(..., max_length=50)
    gitlab_project_id: int | None = None
    ref: str | None = Field(None, max_length=200)
    title: str | None = Field(None, max_length=500)
    feature_id: UUID | None = None


class GitLabEvent(BaseModel):
    """GitLabEvent as stored in the database."""

    model_config = {"from_attributes": True}

    id: UUID
    gitlab_event_id: str | None = None
    event_type: str
    project_key: str
    gitlab_project_id: int | None = None
    ref: str | None = None
    title: str | None = None
    feature_id: UUID | None = None
    processed_at: datetime
