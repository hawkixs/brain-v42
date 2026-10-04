"""PostgreSQL repository for tickets + ticket_messages (coordination family)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_attestations,
    delivery_workflows,
    ticket_messages,
    tickets,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.ticket import (
    ExtractionStatus,
    LotShipping,
    ReleaseLot,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
)
from brain_v42.repositories.delivery_ticket_guard import guard_delivery_transition
from brain_v42.repositories.pg_base import BasePgRepository
from brain_v42.repositories.pg_delivery import lock_workflows
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY

logger = structlog.get_logger(__name__)

# Shapes of the observer's release facts (payloads written by pg_release_derivation).
_RELEASE_TAG_SHAPE = r"^v[0-9]+\.[0-9]+\.[0-9]+$"
_RELEASE_SHA_SHAPE = r"^[0-9a-f]{40}$"

_ACTIONABLE = ("open", "in_progress")
_CONFIRMABLE = ("resolved", "wontfix")


def _measures_active_merge(observed: Any) -> sa.ColumnElement[bool]:
    """Keep only observer facts about the merge the ticket currently stands on.

    A reopen deactivates the bindings and the next attempt binds a new merge, while
    the old release and deployment rows stay in the append-only ledger. Without this
    scope the view would present the first merge's tag or live release as the
    reopened ticket's own.

    The identity label is only declared: the write side also requires the executor
    project as issuer (``PgReleaseDerivationRepo``), so the read side does too.
    """
    binding = delivery_artifact_bindings.alias("observed_binding")
    executor = tickets.alias("observed_ticket")
    return sa.and_(
        observed.c.issuer_identity == OBSERVER_IDENTITY,
        sa.exists().where(
            executor.c.id == observed.c.ticket_id,
            executor.c.to_project == observed.c.issuer_project,
        ),
        sa.exists().where(
            binding.c.ticket_id == observed.c.ticket_id,
            binding.c.active.is_(True),
            binding.c.integration_sha == observed.c.payload["integration_sha"].astext,
        ),
    )


async def count_grouped_by_project(session: AsyncSession) -> list[dict[str, Any]]:
    """Per-project pending-ticket counters for the sidecar's `tickets` block (0fb857ef).

    Single place for the categorisation semantics: reuses the SAME
    ``_ACTIONABLE``/``_CONFIRMABLE`` tuples as ``list_grouped``'s
    ``a_traiter``/``a_confirmer``/``en_attente`` groups, so the sidecar's
    per-project counters can never drift from ``brain_ticket_list``'s own
    rules.

    - ``todo``: ``to_project == X AND status IN _ACTIONABLE``
      (mirrors ``a_traiter`` — no self-ticket exclusion).
    - ``to_confirm``: ``from_project == X AND status IN _CONFIRMABLE``
      (mirrors ``a_confirmer`` — grouped by the REQUESTER, so "a resolved
      ticket counts at the requester"; no self-ticket exclusion, same as
      ``a_confirmer``).
    - ``waiting``: ``from_project == X AND status IN _ACTIONABLE AND
      from_project != to_project`` (mirrors ``en_attente`` — a note-to-self
      never counts as "waiting on someone").

    Unlike ``list_grouped`` (one project, full ``Ticket`` rows, 4 queries),
    this is one aggregate query across every project, ``GROUP BY`` project,
    for the sidecar's slow-cadence collector. Rows are pre-sorted
    most-urgent-first (``todo`` desc, then ``to_confirm`` desc, then
    ``waiting`` desc, then project name for a stable tie-break) — callers
    must not re-sort.
    """
    todo = sa.select(
        tickets.c.to_project.label("project"),
        sa.literal("todo").label("category"),
    ).where(tickets.c.status.in_(_ACTIONABLE))
    to_confirm = sa.select(
        tickets.c.from_project.label("project"),
        sa.literal("to_confirm").label("category"),
    ).where(tickets.c.status.in_(_CONFIRMABLE))
    waiting = sa.select(
        tickets.c.from_project.label("project"),
        sa.literal("waiting").label("category"),
    ).where(
        tickets.c.status.in_(_ACTIONABLE),
        tickets.c.from_project != tickets.c.to_project,
    )
    unioned = sa.union_all(todo, to_confirm, waiting).subquery()
    query = (
        sa.select(
            unioned.c.project,
            sa.func.count().filter(unioned.c.category == "todo").label("todo"),
            sa.func.count().filter(unioned.c.category == "to_confirm").label("to_confirm"),
            sa.func.count().filter(unioned.c.category == "waiting").label("waiting"),
        )
        .group_by(unioned.c.project)
        .order_by(
            sa.desc("todo"),
            sa.desc("to_confirm"),
            sa.desc("waiting"),
            unioned.c.project.asc(),
        )
    )
    rows = (await session.execute(query)).mappings().all()
    return [dict(row) for row in rows]


class PgTicketRepo(BasePgRepository):
    table = tickets
    fts_columns: list[str] = []  # hors recherche — famille coordination (spec §1)

    async def release_state(self, ticket_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Read only release facts issued by the delivery observer.

        The issuer label is declared, and ``attest`` does not check payload keys:
        a row without a well-formed tag or SHA is skipped here, so it can never
        make the ticket view fail.
        """
        payload = delivery_attestations.c.payload
        tag, sha = payload["tag"].astext, payload["live_release_sha"].astext
        rows_query = (
            sa.select(delivery_attestations.c.kind, tag.label("tag"), sha.label("sha"))
            .where(
                delivery_attestations.c.ticket_id == ticket_id,
                _measures_active_merge(delivery_attestations),
                sa.or_(
                    sa.and_(
                        delivery_attestations.c.kind == "released",
                        tag.regexp_match(_RELEASE_TAG_SHAPE),
                    ),
                    sa.and_(
                        delivery_attestations.c.kind == "deployed",
                        sha.regexp_match(_RELEASE_SHA_SHAPE),
                    ),
                ),
            )
            .order_by(delivery_attestations.c.emitted_at, delivery_attestations.c.id)
        )
        async with self.get_session() as session:
            rows = (await session.execute(rows_query)).mappings().all()
        tags = tuple(row["tag"] for row in rows if row["kind"] == "released")
        shas = tuple(row["sha"] for row in rows if row["kind"] == "deployed")
        return tags, shas

    async def deployed_deliverables(self, ticket_id: UUID, running_sha: str) -> tuple[int, int]:
        """Count the ticket's active merged deliverables, and those the live release carries.

        A deliverable is live when the observer issued a ``deployed`` row for ITS
        merge at ``running_sha``: the rows of the other deliverables do not speak
        for it. Same observer scope as ``release_state``.
        """
        binding = delivery_artifact_bindings
        live = delivery_attestations.alias("live_deployed")
        carried = sa.exists().where(
            live.c.ticket_id == binding.c.ticket_id,
            live.c.kind == "deployed",
            _measures_active_merge(live),
            live.c.payload["integration_sha"].astext == binding.c.integration_sha,
            live.c.payload["live_release_sha"].astext == running_sha,
        )
        query = sa.select(sa.func.count().filter(carried), sa.func.count()).where(
            binding.c.ticket_id == ticket_id,
            binding.c.active.is_(True),
            binding.c.integration_sha.is_not(None),
        )
        async with self.get_session() as session:
            carried_count, total = (await session.execute(query)).one()
        return int(carried_count), int(total)

    async def shipped_by_release(self, project_key: str, target_release: str) -> LotShipping:
        """Compare planned tickets with observer tags; never alter either record."""
        observed = delivery_attestations.alias("observed_release")
        known_query = sa.select(
            sa.exists().where(
                observed.c.kind == "released",
                observed.c.issuer_identity == OBSERVER_IDENTITY,
                observed.c.payload["tag"].astext == f"v{target_release}",
                sa.exists().where(
                    tickets.c.id == observed.c.ticket_id,
                    tickets.c.to_project == project_key,
                    # The identity label is only declared: the executor project
                    # is the sole issuer the observer writes as.
                    tickets.c.to_project == observed.c.issuer_project,
                ),
            )
        )
        planned_observed = delivery_attestations.alias("planned_observed")
        rows_query = (
            sa.select(tickets.c.id, planned_observed.c.payload["tag"].astext.label("tag"))
            .select_from(
                tickets.outerjoin(
                    planned_observed,
                    sa.and_(
                        tickets.c.id == planned_observed.c.ticket_id,
                        planned_observed.c.kind == "released",
                        _measures_active_merge(planned_observed),
                        planned_observed.c.payload["tag"].astext.regexp_match(_RELEASE_TAG_SHAPE),
                    ),
                )
            )
            .where(tickets.c.to_project == project_key, tickets.c.target_release == target_release)
        )
        async with self.get_session() as session:
            tag_known = bool(await session.scalar(known_query))
            rows = (await session.execute(rows_query)).all()
        grouped: dict[UUID, set[str]] = {}
        for ticket_id, tag in rows:
            if tag:
                grouped.setdefault(ticket_id, set()).add(tag)
            else:
                grouped.setdefault(ticket_id, set())
        return LotShipping(
            tag_known=tag_known,
            not_shipped=tuple(
                sorted((ticket_id for ticket_id, tags in grouped.items() if not tags), key=str)
            )
            if tag_known
            else (),
            shipped_elsewhere=tuple(
                (ticket_id, tag)
                for ticket_id, tags in sorted(grouped.items(), key=lambda item: str(item[0]))
                for tag in sorted(tags)
                if f"v{target_release}" not in tags
            ),
        )

    async def release_lots(self, project_key: str) -> list[ReleaseLot]:
        """Aggregate planned tickets by release for one executor project."""
        query = (
            sa.select(
                tickets.c.target_release,
                sa.func.count().filter(tickets.c.status.in_(("open", "in_progress"))).label("open"),
                sa.func.count()
                .filter(tickets.c.status.in_(("resolved", "closed")))
                .label("resolved"),
                sa.func.count().filter(tickets.c.status == "wontfix").label("wontfix"),
            )
            .where(tickets.c.to_project == project_key, tickets.c.target_release.is_not(None))
            .group_by(tickets.c.target_release)
        )
        async with self.get_session() as session:
            rows = (await session.execute(query)).mappings().all()
        return [ReleaseLot.model_validate(dict(row)) for row in rows]

    async def update(
        self,
        id: UUID | str,
        data: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> dict[str, Any] | None:
        protected = {
            "id",
            "status",
            "kind",
            "from_project",
            "to_project",
            "resolved_at",
            "closed_at",
        }
        if not protected.intersection(data):
            return await super().update(id, data, session=session)
        async with self._maybe_session(session, write=True) as sess:
            async with lock_workflows(sess, (UUID(str(id)),)):
                contracted = await sess.scalar(
                    sa.select(delivery_workflows.c.ticket_id).where(
                        delivery_workflows.c.ticket_id == id
                    )
                )
                if contracted is not None:
                    raise DeliveryError(
                        "delivery_transition_required",
                        "use the canonical contracted ticket transition",
                    )
                return await super().update(id, data, session=sess)

    async def create(self, data: TicketCreate) -> Ticket:  # type: ignore[override]
        values = {
            "kind": data.kind.value,
            "title": data.title,
            "body": data.body,
            "from_project": data.from_project,
            "to_project": data.to_project,
            "extraction_status": (data.extraction_status.value if data.extraction_status else None),
        }
        async with self.get_session() as session:
            async with session.begin():
                stmt = tickets.insert().values(**values).returning(tickets)
                row = (await session.execute(stmt)).mappings().one()
                logger.debug("pg_ticket.create", ticket_id=str(row["id"]))
                return Ticket.model_validate(dict(row))

    async def get_by_id(self, ticket_id: UUID) -> Ticket | None:  # type: ignore[override]
        async with self.get_session() as session:
            stmt = sa.select(tickets).where(tickets.c.id == ticket_id)
            row = (await session.execute(stmt)).mappings().first()
            return Ticket.model_validate(dict(row)) if row else None

    async def get_messages(self, ticket_id: UUID) -> list[TicketMessage]:
        async with self.get_session() as session:
            stmt = (
                sa.select(ticket_messages)
                .where(ticket_messages.c.ticket_id == ticket_id)
                .order_by(ticket_messages.c.created_at.asc())
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [TicketMessage.model_validate(dict(r)) for r in rows]

    async def add_message(
        self,
        ticket_id: UUID,
        author_project: str,
        body: str,
        status_to: TicketStatus | None = None,
        new_ticket_body: str | None = None,
    ) -> TicketMessage:
        """Insert a thread message; optionally rewrite the ticket body with it.

        ``new_ticket_body`` is applied in the SAME transaction as the message.
        The two must never diverge: a body rewritten without its thread entry
        is a silent rewrite of history, and a thread entry claiming a rewrite
        that did not land is worse — it makes the record lie.
        """
        async with self.get_session() as session:
            async with session.begin():
                stmt = (
                    ticket_messages.insert()
                    .values(
                        ticket_id=ticket_id,
                        author_project=author_project,
                        body=body,
                        status_to=status_to.value if status_to else None,
                    )
                    .returning(ticket_messages)
                )
                row = (await session.execute(stmt)).mappings().one()
                # A reply is activity: bump the ticket's updated_at. An
                # amendment adds the body to it, in the same UPDATE — hence in
                # the same transaction as the message that reports it.
                ticket_values: dict[str, Any] = {"updated_at": sa.func.now()}
                if new_ticket_body is not None:
                    ticket_values["body"] = new_ticket_body
                await session.execute(
                    tickets.update().where(tickets.c.id == ticket_id).values(**ticket_values)
                )
                return TicketMessage.model_validate(dict(row))

    async def set_target_release(
        self,
        ticket_id: UUID,
        *,
        author_project: str,
        expected: str | None,
        new: str | None,
        message: str,
    ) -> TicketMessage | None:
        """Compare-and-swap a release plan with its thread message atomically."""
        async with self.get_session() as session:
            async with session.begin():
                updated = await session.execute(
                    tickets.update()
                    .where(
                        tickets.c.id == ticket_id,
                        tickets.c.target_release.is_not_distinct_from(expected),
                    )
                    .values(target_release=new, updated_at=sa.func.now())
                    .returning(tickets.c.id)
                )
                if updated.first() is None:
                    return None
                row = (
                    (
                        await session.execute(
                            ticket_messages.insert()
                            .values(
                                ticket_id=ticket_id,
                                author_project=author_project,
                                body=message,
                            )
                            .returning(ticket_messages)
                        )
                    )
                    .mappings()
                    .one()
                )
                return TicketMessage.model_validate(dict(row))

    async def apply_transition(
        self,
        ticket_id: UUID,
        new_status: TicketStatus,
        *,
        expected_status: TicketStatus,
        action: TicketAction | str | None = None,
        actor_project: str | None = None,
        resolved_at: datetime | None,
        closed_at: datetime | None,
        extraction_status: ExtractionStatus | None,
        message_author: str | None = None,
        message_body: str | None = None,
    ) -> Ticket | None:
        if (message_author is None) != (message_body is None):
            raise ValueError("message_author and message_body must be provided together")

        async with self.get_session() as session:
            async with session.begin():
                mutation = await guard_delivery_transition(
                    session,
                    ticket_id,
                    action=action,
                    actor_project=actor_project,
                    expected_status=expected_status,
                    new_status=new_status,
                    settings=DeliverySettings(),
                )
                stmt = (
                    tickets.update()
                    .where(
                        tickets.c.id == ticket_id,
                        tickets.c.status == expected_status.value,
                    )
                    .values(
                        status=new_status.value,
                        resolved_at=resolved_at,
                        closed_at=closed_at,
                        extraction_status=(extraction_status.value if extraction_status else None),
                        updated_at=sa.func.now(),
                    )
                    .returning(tickets)
                )
                row = (await session.execute(stmt)).mappings().one_or_none()
                if row is None:
                    return None
                if mutation is not None:
                    await mutation.apply(session, ticket_id)
                if message_author is not None and message_body is not None:
                    await session.execute(
                        ticket_messages.insert().values(
                            ticket_id=ticket_id,
                            author_project=message_author,
                            body=message_body,
                            status_to=new_status.value,
                        )
                    )
                logger.info(
                    "pg_ticket.transition",
                    ticket_id=str(ticket_id),
                    new_status=new_status.value,
                )
                return Ticket.model_validate(dict(row))

    async def list_grouped(
        self, project_key: str, target_release: str | None = None
    ) -> TicketGroups:
        async with self.get_session() as session:

            def _q(col: sa.Column, statuses: tuple[str, ...]) -> sa.Select:
                stmt = sa.select(tickets).where(col == project_key, tickets.c.status.in_(statuses))
                if target_release is not None:
                    stmt = stmt.where(tickets.c.target_release == target_release)
                return stmt.order_by(
                    tickets.c.updated_at.desc(),
                    tickets.c.created_at.desc(),
                    tickets.c.id.asc(),
                )

            a_traiter = (
                (await session.execute(_q(tickets.c.to_project, _ACTIONABLE))).mappings().all()
            )
            a_confirmer = (
                (await session.execute(_q(tickets.c.from_project, _CONFIRMABLE))).mappings().all()
            )
            en_attente = (
                (
                    await session.execute(
                        _q(tickets.c.from_project, _ACTIONABLE).where(
                            tickets.c.from_project != tickets.c.to_project
                        )
                    )
                )
                .mappings()
                .all()
            )
            # Mirror of en_attente: we delivered (resolved/wontfix), the
            # requester has not confirmed. The self-ticket exclusion is not
            # cosmetic — without it, a resolved self-ticket would appear twice,
            # in a_confirmer AND here (spec 2026-08-03 §2.1).
            awaiting_requester_confirmation = (
                (
                    await session.execute(
                        _q(tickets.c.to_project, _CONFIRMABLE).where(
                            tickets.c.from_project != tickets.c.to_project
                        )
                    )
                )
                .mappings()
                .all()
            )
            return TicketGroups(
                a_traiter=[Ticket.model_validate(dict(r)) for r in a_traiter],
                a_confirmer=[Ticket.model_validate(dict(r)) for r in a_confirmer],
                en_attente=[Ticket.model_validate(dict(r)) for r in en_attente],
                awaiting_requester_confirmation=[
                    Ticket.model_validate(dict(r)) for r in awaiting_requester_confirmation
                ],
            )
