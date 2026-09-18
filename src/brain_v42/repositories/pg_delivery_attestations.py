"""Append-only issuer-declared delivery attestations, INSERT and SELECT only.

Ticket 04bc1f4a. Brain is the LEDGER: this repository stores a declared fact and
its server-computed digest, and it never evaluates the kind. There is no UPDATE
and no DELETE path, exactly like `delivery_receipts`.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    delivery_attestations,
    delivery_contract_revisions,
    delivery_workflows,
    tickets,
)
from brain_v42.models.delivery import (
    DeliveryAttestation,
    DeliveryAttestationPage,
    DeliveryError,
)
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.repositories.pg_base import BasePgRepository
from brain_v42.repositories.pg_delivery import lock_workflows


def attestation_position(cursor: str, ticket_id: UUID) -> tuple[datetime, UUID]:
    """Reject malformed or cross-ticket cursors without reflecting their values."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000:
            raise ValueError
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"v", "ticket", "at", "id"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or value["ticket"] != str(ticket_id)
        ):
            raise ValueError
        at, identifier = datetime.fromisoformat(value["at"]), value["id"]
        if at.tzinfo is None or not isinstance(identifier, str):
            raise ValueError
        if str(UUID(identifier)) != identifier:
            raise ValueError
        return at, UUID(identifier)
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
        raise DeliveryError("invalid_cursor", "delivery attestation cursor is invalid") from None


def _page_cursor(ticket_id: UUID, emitted_at: datetime, identifier: UUID) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": 1,
                    "ticket": str(ticket_id),
                    "at": emitted_at.isoformat(),
                    "id": str(identifier),
                },
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )


class PgDeliveryAttestationsRepo(BasePgRepository):
    """Persist immutable issuer-declared facts; callers retain transaction ownership."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        super().__init__(session_factory)

    async def attest(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        actor_project: str,
        caller_identity: str,
        kind: str,
        payload: dict[str, object],
        idempotency_key: str,
        emitted_at: datetime,
        contract_revision: int | None,
    ) -> DeliveryAttestation:
        """Store one declared fact under a per-ticket lock, replay-safe by idempotency key."""
        async with lock_workflows(session, (ticket_id,)):
            ticket = (
                (await session.execute(sa.select(tickets).where(tickets.c.id == ticket_id)))
                .mappings()
                .one_or_none()
            )
            if ticket is None:
                raise DeliveryError("ticket_not_found", "ticket was not found")
            # NO status restriction: incident_detected and rolled_back legitimately
            # arrive after the ticket resolved or closed.
            if actor_project not in {ticket["from_project"], ticket["to_project"]}:
                raise DeliveryError("not_allowed", "actor is not a ticket participant")
            workflow = await session.scalar(
                sa.select(delivery_workflows.c.ticket_id).where(
                    delivery_workflows.c.ticket_id == ticket_id
                )
            )
            if workflow is None:
                raise DeliveryError("contract_not_found", "delivery contract was not found")
            if contract_revision is not None:
                revision = await session.scalar(
                    sa.select(delivery_contract_revisions.c.contract_revision).where(
                        delivery_contract_revisions.c.ticket_id == ticket_id,
                        delivery_contract_revisions.c.contract_revision == contract_revision,
                    )
                )
                if revision is None:
                    raise DeliveryError(
                        "revision_not_found", "delivery contract revision was not found"
                    )
            digest = canonical_digest(payload, domain="attestation")
            existing = (
                (
                    await session.execute(
                        sa.select(delivery_attestations).where(
                            delivery_attestations.c.ticket_id == ticket_id,
                            delivery_attestations.c.issuer_project == actor_project,
                            delivery_attestations.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["kind"] == kind
                    and existing["digest"] == digest
                    and existing["issuer_identity"] == caller_identity
                    and existing["contract_revision"] == contract_revision
                    and existing["emitted_at"] == emitted_at
                ):
                    return DeliveryAttestation.model_validate(dict(existing))
                raise DeliveryError(
                    "idempotency_key_reused",
                    "idempotency key was already used for different content",
                )
            stored = DeliveryAttestation(
                ticket_id=ticket_id,
                contract_revision=contract_revision,
                kind=kind,
                payload=payload,
                digest=digest,
                issuer_project=actor_project,
                issuer_identity=caller_identity,
                idempotency_key=idempotency_key,
                emitted_at=emitted_at,
                recorded_at=datetime.now(UTC),
            )
            row = (
                (
                    await session.execute(
                        delivery_attestations.insert()
                        .values(**stored.model_dump(exclude={"id", "recorded_at"}))
                        .returning(delivery_attestations)
                    )
                )
                .mappings()
                .one()
            )
            return DeliveryAttestation.model_validate(dict(row))

    async def list_for_ticket(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> DeliveryAttestationPage:
        """Read one ticket's facts newest first, filtered and keyset-paginated."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("invalid_limit", "attestation limit must be between 1 and 100")
        filters = [delivery_attestations.c.ticket_id == ticket_id]
        if kind is not None:
            filters.append(delivery_attestations.c.kind == kind)
        if since is not None:
            filters.append(delivery_attestations.c.emitted_at >= since)
        if until is not None:
            filters.append(delivery_attestations.c.emitted_at <= until)
        if cursor is not None:
            at, identifier = attestation_position(cursor, ticket_id)
            filters.append(
                sa.tuple_(delivery_attestations.c.emitted_at, delivery_attestations.c.id)
                < sa.tuple_(sa.literal(at), sa.literal(identifier))
            )
        rows = (
            (
                await session.execute(
                    sa.select(delivery_attestations)
                    .where(*filters)
                    .order_by(
                        delivery_attestations.c.emitted_at.desc(),
                        delivery_attestations.c.id.desc(),
                    )
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        remaining = await session.scalar(
            sa.select(sa.func.count()).select_from(delivery_attestations).where(*filters)
        )
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = _page_cursor(ticket_id, last["emitted_at"], last["id"])
        return DeliveryAttestationPage(
            items=tuple(DeliveryAttestation.model_validate(dict(row)) for row in selected),
            next_cursor=next_cursor,
            omitted_count=max(0, (remaining or 0) - len(selected)),
        )
