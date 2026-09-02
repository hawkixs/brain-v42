"""Pydantic models for Runbook entity."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from brain_v42.models.base import DecayMixin, TimestampMixin
from brain_v42.models.project_key import ProjectKeyCanonicalMixin

ExecutionStatus = Literal["success", "failed", "partial", "skipped"]


class RunbookStep(BaseModel):
    """A single step in a runbook."""

    order: int
    title: str
    command: str | None = None
    description: str | None = None
    expected_output: str | None = None
    timeout_seconds: int | None = None


def _number_steps(value: Any) -> Any:
    """Number the steps that omit ``order``, by position, starting at 1.

    Lives on the models rather than in the MCP tool so both write paths share
    one definition of a valid step. It used to sit in ``brain_create_runbook``
    only, so a runbook created without ``order`` could not be re-updated with
    the same payload — reported by red-shrik alongside ticket 2af71e69.

    A caller that numbers its own steps keeps control: an explicit ``order`` is
    never overwritten, including in a partially-numbered list. Anything that is
    not a list of dicts is handed to Pydantic untouched, so genuine shape errors
    still surface as ordinary validation failures.
    """
    if not isinstance(value, list):
        return value
    return [
        {"order": index + 1, **item} if isinstance(item, dict) and "order" not in item else item
        for index, item in enumerate(value)
    ]


class RunbookBase(BaseModel):
    title: str = Field(..., max_length=200)
    description: str
    project_key: str = Field(..., max_length=50)
    trigger: str
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[RunbookStep] = Field(default_factory=list)
    rollback_steps: list[RunbookStep] = Field(default_factory=list)
    estimated_duration: str | None = Field(None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    _number_steps = field_validator("steps", "rollback_steps", mode="before")(
        staticmethod(_number_steps)
    )


class RunbookCreate(RunbookBase, ProjectKeyCanonicalMixin):
    pass


class RunbookUpdate(BaseModel):
    # Reject unknown keys instead of silently dropping them (see DecisionUpdate).
    model_config = {"extra": "forbid"}

    title: str | None = Field(None, max_length=200)
    description: str | None = None
    trigger: str | None = None
    prerequisites: list[str] | None = None
    steps: list[RunbookStep] | None = None
    rollback_steps: list[RunbookStep] | None = None
    estimated_duration: str | None = Field(None, max_length=50)
    tags: list[str] | None = None
    metadata: dict | None = None
    freshness_status: Literal["fresh", "stale", "archived"] | None = None
    #: Written by the SERVER alone — `brain_update` rejects a caller-supplied
    #: value. The 043 trigger clears it when a write does not redeclare it: a
    #: missing provenance is visible, a false one is believed.
    freshness_source: (
        Literal["merge", "judgment", "score", "revive", "manual_update", "plan_reindex"] | None
    ) = None

    _number_steps = field_validator("steps", "rollback_steps", mode="before")(
        staticmethod(_number_steps)
    )


class Runbook(RunbookBase, TimestampMixin, DecayMixin):
    id: UUID = Field(default_factory=uuid4)
    execution_count: int = 0
    last_executed_at: datetime | None = None
    last_execution_status: ExecutionStatus | None = None
    embedding: list[float] | None = None

    model_config = {"from_attributes": True}
