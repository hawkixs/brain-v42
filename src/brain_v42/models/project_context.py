"""Pydantic models for ProjectContext entity."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from brain_v42.models.base import TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin


class ProjectContextBase(BaseModel):
    project_key: str = Field(..., max_length=50)
    name: str = Field(..., max_length=200)
    description: str
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    code_style: str | None = None
    git_workflow: str | None = None
    test_strategy: str | None = None
    current_phase: str | None = None
    current_focus: str | None = None
    blockers: list[str] = Field(default_factory=list)
    related_projects: list[str] = Field(default_factory=list)
    local_path: str | None = None
    repo_url: str | None = None
    metadata: dict = Field(default_factory=dict)
    plan_scan_paths: list[str] = Field(default_factory=list)
    gitlab_project_path: str | None = Field(None, max_length=200)
    project_group: str | None = Field(None, max_length=50)


class ProjectContextCreate(ProjectContextBase, ProjectKeyCanonicalMixin):
    # project_key canonicalization/validation comes from ProjectKeyCanonicalMixin
    # (single source of truth in brain_v42.models.project_key).
    pass


class ProjectContextUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    languages: list[str] | None = None
    frameworks: list[str] | None = None
    databases: list[str] | None = None
    code_style: str | None = None
    git_workflow: str | None = None
    test_strategy: str | None = None
    current_phase: str | None = None
    current_focus: str | None = None
    blockers: list[str] | None = None
    related_projects: list[str] | None = None
    local_path: str | None = None
    repo_url: str | None = None
    metadata: dict | None = None
    plan_scan_paths: list[str] | None = None
    gitlab_project_path: str | None = Field(None, max_length=200)
    project_group: str | None = Field(None, max_length=50)


class ProjectContext(ProjectContextBase, TimestampMixin):
    id: UUID = Field(default_factory=uuid4)
    focus_revision: int = Field(default=0, ge=0)
    # Read-only: set by the focus write paths, never supplied by a caller.
    # None means the focus was never written since migration 040 landed.
    focus_updated_at: datetime | None = None
    decisions_count: int = 0
    learnings_count: int = 0
    snippets_count: int = 0
    runbooks_count: int = 0
    adrs_count: int = 0

    model_config = {"from_attributes": True}
