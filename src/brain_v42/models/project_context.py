"""Pydantic models for ProjectContext entity."""

from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from brain_v42.models.base import TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin


def _validate_scan_paths(value: list[str] | None) -> list[str] | None:
    """Refuse a scan path no environment can resolve, where it is written.

    `plan_indexer` refuses four ways at read time: `relative`, `missing`,
    `not_directory`, `unreadable`. Only the first is a property of the string
    itself -- the other three are filesystem state that is true at read time and
    can change after the write, so they stay where they are. Validating
    existence here would make a project context unwritable from a host where
    the directory is absent: a CI job, a restored clone, a second machine.

    Refusing relative paths here is not redundant with that reader. Measured
    2026-09-22: 25 configured paths across 12 projects are relative, the
    indexer has been warning `invalid_scan_path` about them at every single
    boot, and 131 plans have never been indexed as a result. A warning repeated
    at every boot is a guard that already lost; the write is where it can still
    be won.

    `~/...` is caught by the same rule and deserves its own mention: it reads
    absolute to a human and is relative to `is_absolute()`, and nothing in the
    read path expands it.
    """
    if value is None:
        return None
    for path in value:
        if not PurePosixPath(path).is_absolute():
            raise ValueError(
                f"plan scan path must be absolute, got {path!r}"
                + (
                    " -- `~` is not expanded anywhere in the read path"
                    if path.startswith("~")
                    else ""
                )
            )
    return value


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

    _check_scan_paths = field_validator("plan_scan_paths")(_validate_scan_paths)


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

    _check_scan_paths = field_validator("plan_scan_paths")(_validate_scan_paths)


class ProjectContext(ProjectContextBase, TimestampMixin):
    id: UUID = Field(default_factory=uuid4)
    focus_revision: int = Field(default=0, ge=0)
    # Read-only: set by the focus write paths, never supplied by a caller.
    # None means the focus was never written since migration 040 landed.
    focus_updated_at: datetime | None = None
    # Read-only, and deliberately NOT on ProjectContextBase: putting it there
    # would put it on Create and Update, and `brain_set_project_context` is not
    # a PATCH — a caller omitting the field would silently un-archive the
    # project. The archive verb owns these two columns and nothing else writes
    # them. None means active.
    archived_at: datetime | None = None
    archived_reason: str | None = None
    decisions_count: int = 0
    learnings_count: int = 0
    snippets_count: int = 0
    runbooks_count: int = 0
    adrs_count: int = 0

    model_config = {"from_attributes": True}
