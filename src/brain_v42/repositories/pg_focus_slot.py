"""PostgreSQL persistence of focus slots: open, list, close (ADR #34, spec §3).

Every write goes through `brain_v42.db.focus_slots`; this module validates,
orders and reads. Nothing here touches `project_contexts` except to check that
a project exists (S1).
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.focus_slots import (
    AnchorState,
    anchor_states,
    close_slot,
    load_anchors,
    lock_slot,
    received_anchor,
    record_slot_history,
    slot_satisfied,
    to_focus_slot,
)
from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_session_checkpoints,
    brain_sessions,
    decisions,
    delivery_artifact_bindings,
    focus_slot_anchors,
    focus_slots,
    project_contexts,
    tickets,
)
from brain_v42.models.brain_session import SESSION_STALE_AFTER, BrainSessionCheckpoint
from brain_v42.models.focus_slot import (
    FocusSlot,
    FocusSlotCloseResult,
    FocusSlotError,
    FocusSlotListResult,
    FocusSlotOpenResult,
    FocusSlotView,
    PreviousSlotSession,
    SlotAnchor,
    SlotBriefing,
    SlotStatusFilter,
    slot_is_stale,
)
from brain_v42.repositories.pg_base import BasePgRepository

_BRIEFING_SLOT_CAP = 20
_DISTILL_CAP = 10


class PgFocusSlotRepo(BasePgRepository):
    """Own the slot rows; the session rows stay with `PgBrainSessionRepo`."""

    table = focus_slots
    fts_columns: builtins.list[str] = []

    async def open(
        self, project_key: str, title: str, body: str, anchors: Sequence[SlotAnchor]
    ) -> FocusSlotOpenResult:
        async with self.transaction() as session:
            await _require_project(session, project_key)
            existing = await self._open_by_title(session, project_key, title)
            if existing is not None:
                return self._replay_open(existing, body, anchors)
            for anchor in anchors:
                await self._admit_anchor(session, project_key, anchor)
            inserted = (
                (
                    await session.execute(
                        pg_insert(focus_slots)
                        .values(project_key=project_key, title=title, body=body)
                        .on_conflict_do_nothing(
                            index_elements=[focus_slots.c.project_key, focus_slots.c.title],
                            index_where=focus_slots.c.closed_at.is_(None),
                        )
                        .returning(focus_slots)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if inserted is None:  # a concurrent open committed the same title first
                raced = await self._open_by_title(session, project_key, title)
                if raced is None:
                    raise FocusSlotError(
                        "slot_title_conflict", f"an open slot titled {title!r} changed concurrently"
                    )
                return self._replay_open(raced, body, anchors)
            slot_id = inserted["id"]
            await session.execute(
                focus_slot_anchors.insert(),
                [
                    {
                        "slot_id": slot_id,
                        "kind": anchor.kind,
                        "ticket_id": anchor.ticket_id,
                        "target_release": anchor.target_release,
                        "repository_id": anchor.repository_id,
                        "pr_number": anchor.pr_number,
                    }
                    for anchor in anchors
                ],
            )
            await record_slot_history(
                session, slot_id=slot_id, revision=0, body=body, source="slot_open"
            )
            return FocusSlotOpenResult(slot=to_focus_slot(inserted, anchors), replayed=False)

    async def list(
        self,
        project_key: str,
        status: SlotStatusFilter,
        *,
        limit: int,
        offset: int,
        now: datetime | None = None,
    ) -> FocusSlotListResult:
        filters: builtins.list[Any] = [focus_slots.c.project_key == project_key]
        if status == "open":
            filters.append(focus_slots.c.closed_at.is_(None))
        elif status == "closed":
            filters.append(focus_slots.c.closed_at.is_not(None))
        async with self.get_session() as session:
            await _require_project(session, project_key)
            total = int(
                await session.scalar(
                    sa.select(sa.func.count()).select_from(focus_slots).where(*filters)
                )
                or 0
            )
            rows = (
                (
                    await session.execute(
                        sa.select(focus_slots)
                        .where(*filters)
                        .order_by(focus_slots.c.opened_at.desc(), focus_slots.c.id)
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
            views = await self.views(session, rows, now=now)
        return FocusSlotListResult(slots=views, total=total, limit=limit, offset=offset)

    async def close(self, slot_id: UUID, expected_revision: int, note: str) -> FocusSlotCloseResult:
        async with self.transaction() as session:
            slot = await lock_slot(session, slot_id)
            if slot is None:
                raise FocusSlotError("slot_not_found", f"slot {slot_id} was not found")
            if slot["closed_at"] is not None:
                raise FocusSlotError("slot_closed", f"slot {slot_id} is already closed")
            if slot["revision"] != expected_revision:
                raise FocusSlotError(
                    "slot_revision_conflict",
                    f"slot {slot_id} is at revision {slot['revision']}, not {expected_revision}",
                )
            holder = await session.scalar(
                sa.select(brain_sessions.c.id).where(
                    brain_sessions.c.slot_id == slot_id, brain_sessions.c.status == "open"
                )
            )
            if holder is not None:
                raise FocusSlotError(
                    "slot_bound",
                    f"session {holder} is bound to slot {slot_id}: end, relay or abandon it first",
                )
            closed = await close_slot(session, slot_id=slot_id, reason="explicit", note=note)
            anchors = await load_anchors(session, [slot_id])
            return FocusSlotCloseResult(slot=to_focus_slot(closed, anchors[slot_id]))

    async def briefing_view(
        self, project_key: str, session_id: UUID, *, now: datetime | None = None
    ) -> SlotBriefing:
        """Everything the briefing shows about slots, read-only (spec §6)."""
        async with self.get_session() as session:
            open_rows = (
                (
                    await session.execute(
                        sa.select(focus_slots)
                        .where(
                            focus_slots.c.project_key == project_key,
                            focus_slots.c.closed_at.is_(None),
                        )
                        .order_by(focus_slots.c.opened_at, focus_slots.c.id)
                        .limit(_BRIEFING_SLOT_CAP)
                    )
                )
                .mappings()
                .all()
            )
            views = await self.views(session, open_rows, now=now)
            bound_id = await session.scalar(
                sa.select(brain_sessions.c.slot_id).where(brain_sessions.c.id == session_id)
            )
            bound: FocusSlot | None = None
            previous: PreviousSlotSession | None = None
            if bound_id is not None:
                row = (
                    (
                        await session.execute(
                            sa.select(focus_slots).where(focus_slots.c.id == bound_id)
                        )
                    )
                    .mappings()
                    .one()
                )
                anchors = await load_anchors(session, [bound_id])
                bound = to_focus_slot(row, anchors[bound_id])
                previous = await self._previous(session, bound_id, session_id)
            to_distill = await self._to_distill(session, project_key)
        return SlotBriefing(
            open_slots=views, bound_slot=bound, previous=previous, to_distill=to_distill
        )

    @staticmethod
    async def _previous(
        session: AsyncSession, slot_id: UUID, session_id: UUID
    ) -> PreviousSlotSession | None:
        """The last session ended on this slot: gaps (a) and (c) of the relay test."""
        s = brain_sessions
        prev = (
            (
                await session.execute(
                    sa.select(s.c.id.label("session_id"), s.c.ended_at, s.c.summary)
                    .where(s.c.slot_id == slot_id, s.c.status == "ended", s.c.id != session_id)
                    .order_by(s.c.ended_at.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if prev is None:
            return None
        decided = (
            await session.execute(
                sa.select(decisions.c.id, decisions.c.title)
                .select_from(
                    decisions.join(
                        brain_session_artifacts,
                        brain_session_artifacts.c.knowledge_id == decisions.c.id,
                    )
                )
                .where(brain_session_artifacts.c.session_id == prev["session_id"])
                .order_by(decisions.c.created_at, decisions.c.id)
            )
        ).all()
        checkpoint = (
            (
                await session.execute(
                    sa.select(brain_session_checkpoints)
                    .where(brain_session_checkpoints.c.session_id == prev["session_id"])
                    .order_by(brain_session_checkpoints.c.seq.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        # Built from the row mapping, like every other session reader: this READS the summary an
        # explicit `end` or `relay` stored and never produces one (the summary census in
        # `test_end_gate_is_judgement_only` surveys the `summary=` keyword, i.e. writers).
        return PreviousSlotSession.model_validate(
            {
                **prev,
                "decisions": [(row[0], str(row[1])) for row in decided],
                "last_checkpoint": (
                    BrainSessionCheckpoint.model_validate(dict(checkpoint)) if checkpoint else None
                ),
            }
        )

    async def _to_distill(
        self, session: AsyncSession, project_key: str
    ) -> builtins.list[FocusSlot]:
        """Slots closed on a receipt since the base was last written (ADR D5)."""
        base_written = await session.scalar(
            sa.select(project_contexts.c.focus_updated_at).where(
                project_contexts.c.project_key == project_key
            )
        )
        filters: builtins.list[Any] = [
            focus_slots.c.project_key == project_key,
            focus_slots.c.close_reason.like("receipt:%"),
        ]
        if base_written is not None:
            filters.append(focus_slots.c.closed_at > base_written)
        rows = (
            (
                await session.execute(
                    sa.select(focus_slots)
                    .where(*filters)
                    .order_by(focus_slots.c.closed_at.desc())
                    .limit(_DISTILL_CAP)
                )
            )
            .mappings()
            .all()
        )
        anchors = await load_anchors(session, [row["id"] for row in rows])
        return [to_focus_slot(row, anchors[row["id"]]) for row in rows]

    async def views(
        self,
        session: AsyncSession,
        rows: Sequence[Mapping[Any, Any]],
        *,
        now: datetime | None = None,
    ) -> builtins.list[FocusSlotView]:
        """Derive bound session, staleness (ADR D6) and receipt_pending; mutate nothing."""
        ids = [row["id"] for row in rows]
        if not ids:
            return []
        anchors = await load_anchors(session, ids)
        states = await anchor_states(session, ids)
        bound: dict[UUID, UUID] = dict(
            (
                await session.execute(
                    sa.select(brain_sessions.c.slot_id, brain_sessions.c.id).where(
                        brain_sessions.c.slot_id.in_(ids), brain_sessions.c.status == "open"
                    )
                )
            )
            .tuples()
            .all()
        )
        last_ended: dict[UUID, datetime | None] = dict(
            (
                await session.execute(
                    sa.select(brain_sessions.c.slot_id, sa.func.max(brain_sessions.c.ended_at))
                    .where(brain_sessions.c.slot_id.in_(ids))
                    .group_by(brain_sessions.c.slot_id)
                )
            )
            .tuples()
            .all()
        )
        reference = now or datetime.now(UTC)
        views: builtins.list[FocusSlotView] = []
        for row in rows:
            slot_id = row["id"]
            is_open = row["closed_at"] is None
            views.append(
                FocusSlotView.model_validate(
                    {
                        **dict(row),
                        "anchors": anchors[slot_id],
                        "bound_session_id": bound.get(slot_id),
                        "is_stale": slot_is_stale(
                            is_open=is_open,
                            bound=bound.get(slot_id) is not None,
                            body_updated_at=row["body_updated_at"],
                            last_bound_ended_at=last_ended.get(slot_id),
                            now=reference,
                        ),
                        "receipt_pending": is_open and slot_satisfied(states[slot_id]),
                    }
                )
            )
        return views

    async def _open_by_title(
        self, session: AsyncSession, project_key: str, title: str
    ) -> FocusSlot | None:
        row = (
            (
                await session.execute(
                    sa.select(focus_slots).where(
                        focus_slots.c.project_key == project_key,
                        focus_slots.c.title == title,
                        focus_slots.c.closed_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        anchors = await load_anchors(session, [row["id"]])
        return to_focus_slot(row, anchors[row["id"]])

    @staticmethod
    def _replay_open(
        existing: FocusSlot, body: str, anchors: Sequence[SlotAnchor]
    ) -> FocusSlotOpenResult:
        same = existing.body == body and {a.key() for a in existing.anchors} == {
            a.key() for a in anchors
        }
        if not same:
            raise FocusSlotError(
                "slot_title_conflict",
                f"an open slot titled {existing.title!r} exists ({existing.id}) with another "
                "body or anchor set",
            )
        return FocusSlotOpenResult(slot=existing, replayed=True)

    @staticmethod
    async def _admit_anchor(session: AsyncSession, project_key: str, anchor: SlotAnchor) -> None:
        if anchor.kind == "ticket":
            owner = await session.scalar(
                sa.select(tickets.c.to_project).where(tickets.c.id == anchor.ticket_id)
            )
            if owner != project_key:
                raise FocusSlotError(
                    "anchor_ticket_foreign",
                    f"ticket {anchor.ticket_id} does not exist or does not target {project_key}",
                )
        elif anchor.kind == "pr":
            b = delivery_artifact_bindings
            bound = await session.scalar(
                sa.select(
                    sa.exists().where(
                        b.c.active.is_(True),
                        b.c.repository_id == anchor.repository_id,
                        b.c.pr_number == anchor.pr_number,
                        tickets.c.id == b.c.ticket_id,
                        tickets.c.to_project == project_key,
                    )
                )
            )
            if not bound:
                raise FocusSlotError(
                    "anchor_unbound_pr",
                    f"{anchor.ref} has no active binding on a ticket of {project_key}",
                )
        else:
            planned = await session.scalar(
                sa.select(
                    sa.exists().where(
                        tickets.c.to_project == project_key,
                        tickets.c.target_release == anchor.target_release,
                    )
                )
            )
            if not planned:
                raise FocusSlotError(
                    "anchor_lot_unplanned",
                    f"no ticket of {project_key} is planned for {anchor.target_release}",
                )
        if await received_anchor(session, project_key=project_key, anchor=anchor) is not None:
            raise FocusSlotError(
                "anchor_already_received", f"{anchor.ref} already has its closing receipt"
            )


async def _require_project(session: AsyncSession, project_key: str) -> None:
    known = await session.scalar(
        sa.select(project_contexts.c.project_key).where(
            project_contexts.c.project_key == project_key
        )
    )
    if known is None:
        raise FocusSlotError("project_not_found", f"project {project_key!r} was not found")


def session_slots_statements() -> dict[str, sa.Select[Any]]:
    """The four read-only sidecar queries (S13)."""
    s, b, p = focus_slots, brain_sessions, project_contexts
    operator = sa.or_(b.c.nature.is_(None), b.c.nature != "agent")
    bound = (
        sa.select(b.c.id)
        .where(b.c.slot_id == s.c.id, b.c.status == "open")
        .limit(1)
        .scalar_subquery()
    )
    last_bound_end = (
        sa.select(sa.func.max(b.c.ended_at)).where(b.c.slot_id == s.c.id).scalar_subquery()
    )
    return {
        "slots": sa.select(
            s.c.id,
            s.c.project_key,
            s.c.title,
            s.c.revision,
            s.c.opened_at,
            s.c.body_updated_at,
            bound.label("bound_session_id"),
            last_bound_end.label("last_bound_ended_at"),
        )
        .where(s.c.closed_at.is_(None))
        .order_by(s.c.project_key, s.c.opened_at, s.c.id),
        "sessions": sa.select(
            b.c.id,
            b.c.project_key,
            b.c.slot_id,
            b.c.started_at,
            b.c.last_heartbeat_at,
            b.c.last_observed_at,
        )
        .where(b.c.status == "open", operator)
        .order_by(b.c.project_key, b.c.started_at, b.c.id),
        "traces": sa.select(b.c.project_key, sa.func.count().label("open_traces"))
        .where(b.c.status == "open", b.c.nature == "agent")
        .group_by(b.c.project_key),
        "bases": sa.select(
            p.c.project_key,
            p.c.focus_revision,
            p.c.focus_updated_at,
            sa.func.coalesce(sa.func.char_length(p.c.current_focus), 0).label("chars"),
        ),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def assemble_session_slots_block(
    *,
    bases: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
    states: Mapping[UUID, Sequence[AnchorState]],
    sessions: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Build the D12 public shape without exposing private slot or session text."""
    by_project = {row["project_key"]: row for row in bases}
    trace_counts = {row["project_key"]: int(row["open_traces"]) for row in traces}
    projects = sorted(
        {row["project_key"] for row in slots}
        | {row["project_key"] for row in sessions}
        | set(trace_counts)
    )
    out: builtins.list[dict[str, Any]] = []
    for project in projects:
        base = by_project.get(project)
        project_slots = [row for row in slots if row["project_key"] == project]
        project_sessions = [row for row in sessions if row["project_key"] == project]
        out.append(
            {
                "project": project,
                "base": {
                    "revision": int(base["focus_revision"]) if base else 0,
                    "focus_updated_at": _iso(base["focus_updated_at"]) if base else None,
                    "chars": int(base["chars"]) if base else 0,
                },
                "slots": [
                    {
                        "slot_id": str(row["id"]),
                        "title": row["title"],
                        "revision": int(row["revision"]),
                        "opened_at": _iso(row["opened_at"]),
                        "body_updated_at": _iso(row["body_updated_at"]),
                        "anchors": [
                            {"kind": state.kind, "ref": state.ref}
                            for state in states.get(row["id"], [])
                        ],
                        "bound_session_id": str(row["bound_session_id"])
                        if row["bound_session_id"]
                        else None,
                        "is_stale": slot_is_stale(
                            is_open=True,
                            bound=row["bound_session_id"] is not None,
                            body_updated_at=row["body_updated_at"],
                            last_bound_ended_at=row["last_bound_ended_at"],
                            now=now,
                        ),
                        "receipt_pending": slot_satisfied(states.get(row["id"], [])),
                    }
                    for row in project_slots
                ],
                "sessions": [
                    {
                        "session_id": str(row["id"]),
                        "slot_id": str(row["slot_id"]) if row["slot_id"] else None,
                        "started_at": _iso(row["started_at"]),
                        "last_heartbeat_at": _iso(row["last_heartbeat_at"]),
                        "last_observed_at": _iso(row["last_observed_at"]),
                        "is_stale": row["last_heartbeat_at"] <= now - SESSION_STALE_AFTER,
                    }
                    for row in project_sessions
                ],
                "agent_traces_open": trace_counts.get(project, 0),
            }
        )
    return {"projects": out}


async def session_slots_block(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read the D12 sidecar block; the cache adds generated_at."""
    rows = {
        name: [dict(row) for row in (await session.execute(statement)).mappings()]
        for name, statement in session_slots_statements().items()
    }
    states = await anchor_states(session, [row["id"] for row in rows["slots"]])
    return assemble_session_slots_block(
        bases=rows["bases"],
        slots=rows["slots"],
        states=states,
        sessions=rows["sessions"],
        traces=rows["traces"],
        now=now or datetime.now(UTC),
    )
