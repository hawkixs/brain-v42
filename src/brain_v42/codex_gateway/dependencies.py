"""Typed dependency bundle injected into Codex gateway routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from brain_v42.models.ticket import TicketCreate


class TicketOperations(Protocol):
    async def create(self, data: TicketCreate) -> Any: ...

    async def reply(self, ticket_id: UUID, author_project: str, body: str) -> Any: ...

    async def transition(
        self,
        ticket_id: UUID,
        author_project: str,
        action: str,
        message: str | None = None,
    ) -> Any: ...

    async def get_with_thread(self, ticket_id: UUID) -> tuple[Any, list[Any]] | None: ...


class LearningOperations(Protocol):
    async def validate(
        self,
        learning_id: UUID,
        *,
        project_group: str | None = None,
    ) -> Any | None: ...


class EntityMaintenanceOperations(Protocol):
    async def refresh(
        self,
        entity_type: str,
        entity_id: UUID,
        *,
        project_group: str | None = None,
    ) -> Any | None: ...


class FeatureOperations(Protocol):
    async def patch(
        self,
        feature_id: UUID,
        *,
        status: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        project_group: str | None = None,
    ) -> Any | None: ...


class ProposalOperations(Protocol):
    async def apply_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> Any: ...

    async def reject_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> Any: ...

    async def apply_roadmap_curation(
        self,
        proposal_id: int,
        allowed_ops: tuple[str, ...] | None = None,
        *,
        project_group: str | None = None,
    ) -> Any: ...

    async def reject_roadmap_curation(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> Any: ...


class KillswitchOperations(Protocol):
    async def read(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class GatewayServices:
    ticket: TicketOperations
    learning: LearningOperations
    entity_maintenance: EntityMaintenanceOperations
    feature: FeatureOperations
    proposal: ProposalOperations
    killswitch: KillswitchOperations
