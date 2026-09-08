"""Bounded immutable history reads using the caller's PostgreSQL session."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import delivery_contract_revisions as revisions
from brain_v42.db.tables import delivery_receipts as receipts
from brain_v42.models.delivery import (
    ContractHistoryEntry,
    ContractRevision,
    DeliveryError,
    DeliveryHistoryEntry,
    DeliveryHistoryPage,
    DeliveryView,
    MilestoneReceipt,
    ReceiptHistoryEntry,
)


def history_position(cursor: str, ticket_id: UUID) -> tuple[datetime, str]:
    """Reject malformed or cross-ticket cursors without reflecting their values."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000:
            raise ValueError
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"v", "ticket", "at", "key"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or value["ticket"] != str(ticket_id)
        ):
            raise ValueError
        at, key = datetime.fromisoformat(value["at"]), value["key"]
        if at.tzinfo is None or not isinstance(key, str):
            raise ValueError
        if key.startswith("r:"):
            if str(UUID(key[2:])) != key[2:]:
                raise ValueError
        elif not (
            key.startswith("c:") and len(key) == 12 and key[2:].isascii() and key[2:].isdigit()
        ):
            raise ValueError
        return at, key
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
        raise DeliveryError("invalid_cursor", "delivery history cursor is invalid") from None


def _entry(row: sa.RowMapping, view: DeliveryView) -> DeliveryHistoryEntry:
    payload = json.dumps(row["payload"])
    if row["kind"] == "contract_revision":
        contract = ContractRevision.model_validate_json(payload)
        valid = (
            contract.ticket_id == view.contract.ticket_id
            and f"c:{contract.contract_revision:010d}" == row["key"]
            and contract.created_at == row["recorded_at"]
            and contract.content_digest == row["contract_digest"]
            and contract.author_project == row["issuer"]
            and contract.amendment_reason == row["basis"]
        )
        item: DeliveryHistoryEntry = ContractHistoryEntry(
            contract=contract,
            superseded=contract.contract_revision != view.contract.contract_revision,
        )
    else:
        receipt = MilestoneReceipt.model_validate_json(payload)
        valid = (
            receipt.ticket_id == view.contract.ticket_id
            and f"r:{receipt.id}" == row["key"]
            and receipt.issued_at == row["recorded_at"]
            and receipt.contract_revision == row["revision"]
            and receipt.attempt == row["attempt"]
            and receipt.contract_digest == row["contract_digest"]
            and receipt.delivery_digest == row["delivery_digest"]
            and receipt.milestone == row["milestone"]
            and receipt.proof.issuer.issuer_identity == row["issuer"]
            and receipt.acceptance_basis == row["basis"]
        )
        current = {
            value.id
            for value in (view.integration_receipt, view.fulfillment_receipt)
            if value is not None
        }
        item = ReceiptHistoryEntry(receipt=receipt, superseded=receipt.id not in current)
    if not valid:
        raise DeliveryError("history_invalid", "stored delivery history identity is inconsistent")
    return item


async def load_history(
    session: AsyncSession, view: DeliveryView, *, limit: int, cursor: str | None
) -> DeliveryHistoryPage:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DeliveryError("invalid_limit", "history limit must be between 1 and 100")
    ticket_id = view.contract.ticket_id
    contracts = sa.select(
        sa.literal("contract_revision").label("kind"),
        revisions.c.normalized_contract.label("payload"),
        revisions.c.created_at.label("recorded_at"),
        (
            sa.literal("c:")
            + sa.func.lpad(sa.cast(revisions.c.contract_revision, sa.String), 10, "0")
        ).label("key"),
        revisions.c.content_digest.label("contract_digest"),
        revisions.c.author_project.label("issuer"),
        revisions.c.amendment_reason.label("basis"),
        revisions.c.contract_revision.label("revision"),
        sa.cast(sa.null(), sa.Integer).label("attempt"),
        sa.cast(sa.null(), sa.String).label("delivery_digest"),
        sa.cast(sa.null(), sa.String).label("milestone"),
    ).where(revisions.c.ticket_id == ticket_id)
    proofs = (
        sa.select(
            sa.literal("receipt").label("kind"),
            receipts.c.payload,
            receipts.c.issued_at.label("recorded_at"),
            (sa.literal("r:") + sa.cast(receipts.c.id, sa.String)).label("key"),
            revisions.c.content_digest.label("contract_digest"),
            receipts.c.issuer,
            receipts.c.basis,
            receipts.c.contract_revision.label("revision"),
            receipts.c.attempt,
            receipts.c.delivery_digest,
            receipts.c.milestone,
        )
        .join(
            revisions,
            sa.and_(
                revisions.c.ticket_id == receipts.c.ticket_id,
                revisions.c.contract_revision == receipts.c.contract_revision,
            ),
        )
        .where(receipts.c.ticket_id == ticket_id)
    )
    records = contracts.union_all(proofs).subquery("delivery_history")
    filters = []
    if cursor is not None:
        at, key = history_position(cursor, ticket_id)
        filters.append(
            sa.tuple_(records.c.recorded_at, records.c.key)
            < sa.tuple_(sa.literal(at), sa.literal(key))
        )
    rows = (
        (
            await session.execute(
                sa.select(records)
                .where(*filters)
                .order_by(records.c.recorded_at.desc(), records.c.key.desc())
                .limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    remaining = await session.scalar(
        sa.select(sa.func.count()).select_from(records).where(*filters)
    )
    selected = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "ticket": str(ticket_id),
                        "at": last["recorded_at"].isoformat(),
                        "key": last["key"],
                    },
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return DeliveryHistoryPage(
        items=tuple(_entry(row, view) for row in selected),
        next_cursor=next_cursor,
        omitted_count=max(0, (remaining or 0) - len(selected)),
    )
