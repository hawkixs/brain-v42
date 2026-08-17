"""Strict JSON input models for gateway write operations."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from brain_v42.models.ticket import TicketKind

EntityType = Literal["decision", "learning", "snippet", "runbook", "adr", "plan"]
FeatureStatus = Literal["planned", "research", "design", "building", "deployed", "done", "archived"]


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TicketCreatePayload(StrictPayload):
    kind: TicketKind
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    from_project: str = Field(min_length=1, max_length=50)
    to_project: str = Field(min_length=1, max_length=50)


class TicketReplyPayload(StrictPayload):
    actor_project: str = Field(min_length=1, max_length=50)
    body: str = Field(min_length=1)


class TicketTransitionPayload(StrictPayload):
    actor_project: str = Field(min_length=1, max_length=50)
    action: str = Field(min_length=1)
    message: str | None = Field(default=None, min_length=1)


class FeaturePatchPayload(StrictPayload):
    status: FeatureStatus | None = None
    pinned: bool | None = None
    archived: Literal[True] | None = None

    @model_validator(mode="after")
    def _one_consistent_mutation(self) -> Self:
        if self.status is None and self.pinned is None and self.archived is None:
            raise ValueError("at least one feature mutation is required")
        if self.archived is True and self.status not in {None, "archived"}:
            raise ValueError("archived=true conflicts with a non-archived status")
        return self
