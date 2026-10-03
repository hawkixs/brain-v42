"""The one way a focus slot is written, and the one definition of its receipts.

`db/focus_history.py`'s pattern, for slots (ADR #34, spec §2): every function
runs INSIDE the caller's transaction, fails closed, and reads the revision from
`RETURNING`, never computes it. `record_slot_history` is the only insert into
`focus_slot_history`; the deferred trigger of migration 060 refuses a COMMIT
that moved a slot revision without it.

Lock order, everywhere: session row, then slot row, then `project_contexts`.
The receipt predicates live here once, shared by `brain_slot_open` (refuse an
anchor already received), the receipt hooks, the briefing and the sidecar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_attestations,
    delivery_receipts,
    delivery_workflows,
    focus_slot_anchors,
    focus_slot_history,
    focus_slots,
    tickets,
)
from brain_v42.models.focus_slot import FocusSlot, FocusSlotError, SlotAnchor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

Row = dict[str, Any]
FocusSlotHistorySource = Literal["slot_open", "session_end", "session_relay", "slot_close"]

#: The delivery observer's identity. A copy of
#: `repositories.pg_release_derivation.OBSERVER_IDENTITY`, pinned equal by a unit
#: test: `db` must not import `repositories`.
OBSERVER_IDENTITY = "brain-v42-delivery-observer"
_ACTOR_CHARS = 64


async def record_slot_history(
    session: AsyncSession,
    *,
    slot_id: UUID,
    revision: int,
    body: str,
    source: FocusSlotHistorySource,
    session_id: UUID | None = None,
    actor: str | None = None,
) -> None:
    """Append the history row of a slot revision that has just been persisted.

    `ON CONFLICT DO NOTHING` on `(slot_id, revision)`: a replayed write must not
    add a second row for a revision that has one. Two different bodies at one
    revision cannot happen, the revision being what the write returned.
    """
    statement = pg_insert(focus_slot_history).values(
        slot_id=slot_id,
        revision=revision,
        body=body,
        source=source,
        session_id=session_id,
        actor=actor[:_ACTOR_CHARS] if actor else None,
    )
    await session.execute(statement.on_conflict_do_nothing(index_elements=["slot_id", "revision"]))


async def lock_slot(session: AsyncSession, slot_id: UUID) -> Row | None:
    row = (
        (
            await session.execute(
                sa.select(focus_slots).where(focus_slots.c.id == slot_id).with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def cas_slot_body(
    session: AsyncSession, *, slot_id: UUID, expected: int, body: str
) -> Row | None:
    """ADR D3: write the body only at `expected` on an open slot; `None` if lost.

    `body_updated_at` moves only when the text changes: re-posting the same body
    is the copy-forward the staleness rule must still see as old.
    """
    await lock_slot(session, slot_id)
    statement = (
        focus_slots.update()
        .where(
            focus_slots.c.id == slot_id,
            focus_slots.c.revision == expected,
            focus_slots.c.closed_at.is_(None),
        )
        .values(
            body=body,
            revision=focus_slots.c.revision + 1,
            body_updated_at=sa.case(
                (focus_slots.c.body.is_distinct_from(body), sa.func.now()),
                else_=focus_slots.c.body_updated_at,
            ),
        )
        .returning(focus_slots)
    )
    row = (await session.execute(statement)).mappings().one_or_none()
    return dict(row) if row is not None else None


async def close_slot(session: AsyncSession, *, slot_id: UUID, reason: str, note: str | None) -> Row:
    """Close an open slot: `closed_at`, the reason, `revision + 1`, a `slot_close` row.

    The revision bump is what lets a stale bound `end` record `conflict`: the
    ended CHECK requires `focus_revision_at_end <> end_expected_focus_revision`.
    """
    statement = (
        focus_slots.update()
        .where(focus_slots.c.id == slot_id, focus_slots.c.closed_at.is_(None))
        .values(
            closed_at=sa.func.now(),
            close_reason=reason,
            close_note=note,
            revision=focus_slots.c.revision + 1,
        )
        .returning(focus_slots)
    )
    row = (await session.execute(statement)).mappings().one_or_none()
    if row is None:
        raise FocusSlotError("slot_closed", f"slot {slot_id} is already closed")
    await record_slot_history(
        session,
        slot_id=slot_id,
        revision=int(row["revision"]),
        body=str(row["body"]),
        source="slot_close",
    )
    return dict(row)


def _anchor_from_row(row: Mapping[Any, Any]) -> SlotAnchor:
    return SlotAnchor(
        kind=row["kind"],
        ticket_id=row["ticket_id"],
        target_release=row["target_release"],
        repository_id=row["repository_id"],
        pr_number=row["pr_number"],
    )


async def load_anchors(
    session: AsyncSession, slot_ids: Sequence[UUID]
) -> dict[UUID, list[SlotAnchor]]:
    grouped: dict[UUID, list[SlotAnchor]] = {slot_id: [] for slot_id in slot_ids}
    if not slot_ids:
        return grouped
    a = focus_slot_anchors
    rows = await session.execute(
        sa.select(
            a.c.slot_id,
            a.c.kind,
            a.c.ticket_id,
            a.c.target_release,
            a.c.repository_id,
            a.c.pr_number,
        )
        .where(a.c.slot_id.in_(list(slot_ids)))
        .order_by(a.c.slot_id, a.c.created_at, a.c.id)
    )
    for row in rows.mappings():
        grouped[row["slot_id"]].append(_anchor_from_row(row))
    return grouped


def to_focus_slot(row: Mapping[Any, Any], anchors: Sequence[SlotAnchor]) -> FocusSlot:
    return FocusSlot.model_validate({**dict(row), "anchors": list(anchors)})


@dataclass(frozen=True, slots=True)
class AnchorState:
    kind: str
    ref: str
    completing_row_id: UUID | None

    @property
    def satisfied(self) -> bool:
        return self.completing_row_id is not None


def _ticket_receipt(ticket_id: Any) -> Any:
    """An integration receipt at the workflow's current (revision, attempt)."""
    r = delivery_receipts.alias("slot_ticket_receipt")
    w = delivery_workflows.alias("slot_ticket_workflow")
    return (
        sa.select(r.c.id)
        .select_from(
            r.join(
                w,
                sa.and_(
                    w.c.ticket_id == r.c.ticket_id,
                    w.c.current_revision == r.c.contract_revision,
                    w.c.attempt == r.c.attempt,
                ),
            )
        )
        .where(r.c.ticket_id == ticket_id, r.c.milestone == "integration")
        .order_by(r.c.issued_at, r.c.id)
        .limit(1)
        .scalar_subquery()
    )


def _pr_receipt(project_key: Any, repository_id: Any, pr_number: Any) -> Any:
    """The same predicate on the ticket of an active, current binding of the PR."""
    b = delivery_artifact_bindings.alias("slot_pr_binding")
    t = tickets.alias("slot_pr_ticket")
    w = delivery_workflows.alias("slot_pr_workflow")
    r = delivery_receipts.alias("slot_pr_receipt")
    return (
        sa.select(r.c.id)
        .select_from(
            b.join(t, t.c.id == b.c.ticket_id)
            .join(
                w,
                sa.and_(
                    w.c.ticket_id == b.c.ticket_id,
                    w.c.current_revision == b.c.contract_revision,
                    w.c.attempt == b.c.attempt,
                ),
            )
            .join(
                r,
                sa.and_(
                    r.c.ticket_id == b.c.ticket_id,
                    r.c.contract_revision == b.c.contract_revision,
                    r.c.attempt == b.c.attempt,
                ),
            )
        )
        .where(
            b.c.active.is_(True),
            b.c.repository_id == repository_id,
            b.c.pr_number == pr_number,
            t.c.to_project == project_key,
            r.c.milestone == "integration",
        )
        .order_by(r.c.issued_at, r.c.id)
        .limit(1)
        .scalar_subquery()
    )


def _lot_release(project_key: Any, target_release: Any) -> Any:
    """An observer `released` of tag v<target_release> on a ticket of the project."""
    a = delivery_attestations.alias("slot_lot_release")
    t = tickets.alias("slot_lot_ticket")
    return (
        sa.select(a.c.id)
        .select_from(a.join(t, t.c.id == a.c.ticket_id))
        .where(
            a.c.kind == "released",
            a.c.issuer_identity == OBSERVER_IDENTITY,
            a.c.issuer_project == project_key,
            a.c.payload["tag"].astext == sa.func.concat("v", target_release),
            t.c.to_project == project_key,
        )
        .order_by(a.c.recorded_at, a.c.id)
        .limit(1)
        .scalar_subquery()
    )


def anchor_states_statement(slot_ids: Sequence[UUID]) -> sa.Select[Any]:
    a, s = focus_slot_anchors, focus_slots
    completing = sa.case(
        (a.c.kind == "ticket", _ticket_receipt(a.c.ticket_id)),
        (a.c.kind == "pr", _pr_receipt(s.c.project_key, a.c.repository_id, a.c.pr_number)),
        else_=_lot_release(s.c.project_key, a.c.target_release),
    ).label("completing_row_id")
    return (
        sa.select(
            a.c.slot_id,
            a.c.kind,
            a.c.ticket_id,
            a.c.target_release,
            a.c.repository_id,
            a.c.pr_number,
            completing,
        )
        .select_from(a.join(s, s.c.id == a.c.slot_id))
        .where(a.c.slot_id.in_(list(slot_ids)))
        .order_by(a.c.slot_id, a.c.created_at, a.c.id)
    )


async def anchor_states(
    session: AsyncSession, slot_ids: Sequence[UUID]
) -> dict[UUID, list[AnchorState]]:
    states: dict[UUID, list[AnchorState]] = {slot_id: [] for slot_id in slot_ids}
    if not slot_ids:
        return states
    for row in (await session.execute(anchor_states_statement(slot_ids))).mappings():
        anchor = _anchor_from_row(row)
        states[row["slot_id"]].append(
            AnchorState(
                kind=anchor.kind, ref=anchor.ref, completing_row_id=row["completing_row_id"]
            )
        )
    return states


async def received_anchor(
    session: AsyncSession, *, project_key: str, anchor: SlotAnchor
) -> UUID | None:
    """The completing row of an anchor not yet written, for `anchor_already_received`."""
    project = sa.literal(project_key, sa.String)
    if anchor.kind == "ticket":
        expression = _ticket_receipt(sa.literal(anchor.ticket_id, PG_UUID(as_uuid=True)))
    elif anchor.kind == "pr":
        expression = _pr_receipt(
            project,
            sa.literal(anchor.repository_id, sa.BigInteger),
            sa.literal(anchor.pr_number, sa.Integer),
        )
    else:
        expression = _lot_release(project, sa.literal(anchor.target_release, sa.Text))
    value = await session.scalar(sa.select(expression))
    return value if isinstance(value, UUID) else None


def slot_satisfied(states: Sequence[AnchorState]) -> bool:
    """Q2: any lot anchor decides; otherwise every ticket and PR anchor must be integrated."""
    lots = [state for state in states if state.kind == "lot"]
    decisive = lots or list(states)
    return bool(decisive) and all(state.satisfied for state in decisive)


async def close_slots_satisfied_by(
    session: AsyncSession,
    *,
    ticket_ids: Sequence[UUID],
    completing_row_id: UUID,
) -> list[UUID]:
    """Close every open slot that the receipt just written satisfies (spec §5, ADR D5).

    Runs inside the receipt writer's transaction, after the workflow locks it
    already holds; it locks SLOTS only, ordered by id. Candidates are over-chosen
    on purpose (a ticket anchor on the ticket, a PR anchor whose binding is on
    it, any lot anchor of its project); the predicate decides, with Q2's rule.
    The close writes no base text and no summary. A closed slot is skipped, so a
    replayed receipt or attestation closes nothing twice.
    """
    if not ticket_ids:
        return []
    ids = list(ticket_ids)
    a, s, b, t = focus_slot_anchors, focus_slots, delivery_artifact_bindings, tickets
    touched = sa.or_(
        sa.and_(a.c.kind == "ticket", a.c.ticket_id.in_(ids)),
        sa.and_(
            a.c.kind == "pr",
            sa.exists().where(
                b.c.repository_id == a.c.repository_id,
                b.c.pr_number == a.c.pr_number,
                b.c.active.is_(True),
                b.c.ticket_id.in_(ids),
            ),
        ),
        sa.and_(
            a.c.kind == "lot",
            sa.exists().where(t.c.id.in_(ids), t.c.to_project == s.c.project_key),
        ),
    )
    candidates = (
        sa.select(a.c.slot_id)
        .select_from(a.join(s, s.c.id == a.c.slot_id))
        .where(s.c.closed_at.is_(None), touched)
    )
    locked = list(
        (
            await session.execute(
                sa.select(s.c.id)
                .where(s.c.id.in_(candidates), s.c.closed_at.is_(None))
                .order_by(s.c.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not locked:
        return []
    states = await anchor_states(session, locked)
    closed: list[UUID] = []
    for slot_id in locked:
        if slot_satisfied(states[slot_id]):
            await close_slot(
                session, slot_id=slot_id, reason=f"receipt:{completing_row_id}", note=None
            )
            closed.append(slot_id)
    return closed
