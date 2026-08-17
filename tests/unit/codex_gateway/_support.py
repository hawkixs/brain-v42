"""In-memory domain collaborators for gateway behavior tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
)
from brain_v42.services.proposal_service import ProposalMutationResult
from brain_v42.services.ticket_service import TicketService

GATEWAY_TOKEN = "gateway-secret-that-is-at-least-32-bytes"


class InMemoryTicketRepo:
    def __init__(self) -> None:
        self.tickets: dict[UUID, Ticket] = {}
        self.messages: dict[UUID, list[TicketMessage]] = {}

    async def create(self, data: TicketCreate) -> Ticket:
        ticket = Ticket(**data.model_dump())
        self.tickets[ticket.id] = ticket
        self.messages[ticket.id] = []
        return ticket

    async def get_by_id(self, ticket_id: UUID) -> Ticket | None:
        return self.tickets.get(ticket_id)

    async def get_messages(self, ticket_id: UUID) -> list[TicketMessage]:
        return list(self.messages.get(ticket_id, []))

    async def add_message(
        self,
        ticket_id: UUID,
        author_project: str,
        body: str,
        status_to: TicketStatus | None = None,
    ) -> TicketMessage:
        message = TicketMessage(
            ticket_id=ticket_id,
            author_project=author_project,
            body=body,
            status_to=status_to,
        )
        self.messages[ticket_id].append(message)
        return message

    async def apply_transition(
        self,
        ticket_id: UUID,
        new_status: TicketStatus,
        *,
        expected_status: TicketStatus,
        resolved_at: datetime | None,
        closed_at: datetime | None,
        extraction_status: ExtractionStatus | None,
        message_author: str | None = None,
        message_body: str | None = None,
    ) -> Ticket | None:
        current = self.tickets.get(ticket_id)
        if current is None or current.status is not expected_status:
            return None
        updated = current.model_copy(
            update={
                "status": new_status,
                "resolved_at": resolved_at,
                "closed_at": closed_at,
                "extraction_status": extraction_status,
                "updated_at": datetime.now(UTC),
            }
        )
        self.tickets[ticket_id] = updated
        if message_author is not None and message_body is not None:
            await self.add_message(
                ticket_id,
                message_author,
                message_body,
                status_to=new_status,
            )
        return updated

    async def resolve_id_prefix(self, prefix_hex: str) -> list[UUID]:
        return [value for value in self.tickets if value.hex.startswith(prefix_hex)]

    async def list_grouped(self, project_key: str) -> TicketGroups:
        return TicketGroups()


class KnownProjectRepo:
    async def get_by_key(self, project_key: str) -> object | None:
        if project_key in {"red-codex", "brain-v42"}:
            return object()
        return None


class LearningServiceStub:
    def __init__(self, known_id: UUID) -> None:
        self.known_id = known_id

    async def validate(
        self,
        learning_id: UUID,
        *,
        project_group: str | None = None,
    ) -> dict[str, Any] | None:
        assert project_group == "red"
        if learning_id != self.known_id:
            return None
        return {"id": learning_id, "validated_at": datetime.now(UTC)}


class EntityMaintenanceStub:
    def __init__(self, known_id: UUID) -> None:
        self.known_id = known_id

    async def refresh(
        self,
        entity_type: str,
        entity_id: UUID,
        *,
        project_group: str | None = None,
    ) -> dict[str, Any] | None:
        assert project_group == "red"
        if entity_id != self.known_id:
            return None
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "freshness_status": "fresh",
            "last_accessed_at": datetime.now(UTC),
        }


class FeatureServiceStub:
    def __init__(self, known_id: UUID) -> None:
        self.known_id = known_id
        self.last_patch: dict[str, Any] | None = None

    async def patch(
        self,
        feature_id: UUID,
        *,
        status: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        project_group: str | None = None,
    ) -> dict[str, Any] | None:
        assert project_group == "red"
        self.last_patch = {"status": status, "pinned": pinned, "archived": archived}
        if feature_id != self.known_id:
            return None
        return {
            "id": feature_id,
            "status": "archived" if archived else status or "planned",
            "pinned": pinned if pinned is not None else False,
        }


class ProposalServiceStub:
    async def apply_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        assert project_group == "red"
        return ProposalMutationResult(proposal_id, "ticket-extraction", "applied")

    async def reject_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        assert project_group == "red"
        return ProposalMutationResult(proposal_id, "ticket-extraction", "rejected")

    async def apply_roadmap_curation(
        self,
        proposal_id: int,
        allowed_ops: tuple[str, ...] | None = None,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        assert project_group == "red"
        return ProposalMutationResult(
            proposal_id,
            "roadmap-curation",
            "applied",
            operation="status",
            apply_log={"status": "done"},
        )

    async def reject_roadmap_curation(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ) -> ProposalMutationResult:
        assert project_group == "red"
        return ProposalMutationResult(proposal_id, "roadmap-curation", "rejected")


class KillswitchReaderStub:
    async def read(self) -> dict[str, Any]:
        return {
            "promote_enabled": True,
            "promote_dry": False,
            "reorg_enabled": True,
            "reorg_dry": False,
            "extract_enabled": True,
            "extract_dry": True,
            "roadmap_enabled": True,
            "roadmap_dry": True,
        }


@dataclass
class GatewayFixture:
    app: Any
    ticket_repo: InMemoryTicketRepo
    learning_id: UUID
    entity_id: UUID
    feature_id: UUID
    feature_service: Any

    async def seed_ticket(self) -> Ticket:
        return await self.ticket_repo.create(
            TicketCreate(
                kind="request",
                title="Contract",
                body="Ship the gateway",
                from_project="red-codex",
                to_project="brain-v42",
            )
        )


def build_gateway_fixture(
    *,
    token: str = GATEWAY_TOKEN,
    learning_service: Any | None = None,
    feature_service: Any | None = None,
    proposal_service: Any | None = None,
    readiness: Any | None = None,
    readiness_timeout_s: float = 2.0,
) -> GatewayFixture:
    from brain_v42.codex_gateway.app import create_app
    from brain_v42.codex_gateway.dependencies import GatewayServices

    ticket_repo = InMemoryTicketRepo()
    learning_id = uuid4()
    entity_id = uuid4()
    feature_id = uuid4()
    configured_feature_service = feature_service or FeatureServiceStub(feature_id)
    services = GatewayServices(
        ticket=TicketService(ticket_repo, KnownProjectRepo()),  # type: ignore[arg-type]
        learning=learning_service or LearningServiceStub(learning_id),
        entity_maintenance=EntityMaintenanceStub(entity_id),
        feature=configured_feature_service,
        proposal=proposal_service or ProposalServiceStub(),
        killswitch=KillswitchReaderStub(),
    )
    app = create_app(
        services=services,
        token=SecretStr(token),
        readiness=readiness,
        readiness_timeout_s=readiness_timeout_s,
    )
    return GatewayFixture(
        app=app,
        ticket_repo=ticket_repo,
        learning_id=learning_id,
        entity_id=entity_id,
        feature_id=feature_id,
        feature_service=configured_feature_service,
    )
