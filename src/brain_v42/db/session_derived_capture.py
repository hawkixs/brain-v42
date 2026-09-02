"""DERIVED capture: deposit the artifact into its connection's tracer.

The chosen shape is **ABSORPTION**, not adoption. The server never promotes a
tracer into a user session: it deposits the artifact into the connection's
`agent` tracer at creation time, and the user's session absorbs that ledger on
its next command (``absorb_tracer_ledger`` below).

**Why adoption is forbidden.** ``auto_open`` re-dates a row that conflicts on
(project, connection). An `operator` row carrying a connection would therefore
be re-dated on every tool call; and since the sweep's 7-day eligibility reads
``last_heartbeat_at`` **with no nature filter**, the one WRITTEN exception to the
covenant would become unreachable — an immortal ghost. ``connection_id`` is
never set on an `operator` row.

**A LEAF module, and that is structural.** ``pg_brain_session`` imports
``pg_base``; ``pg_base`` calls this module. Making it import
``pg_brain_session`` — if only for ``CAPTURE_TABLES`` — would close the cycle.
The two lists therefore live separately, and
``test_the_table_map_agrees_with_the_repository_capture_tables`` is what keeps
them from diverging in silence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.config import get_settings
from brain_v42.db.tables import (
    adrs,
    brain_session_artifacts,
    brain_sessions,
    decisions,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.provenance import SYSTEM_ACTOR_NAMES, SYSTEM_ACTOR_PREFIXES

logger = structlog.get_logger(__name__)


def _capture_cap() -> int:
    """The explicit-capture ceiling, imported LATE and for a reason.

    ``pg_base`` calls this module and carries a written rule: "Never import from
    brain_v42.models here — stay at the DB Core dict layer." A module-level
    import would sidestep it, pulling the model layer into the DB core's import
    graph. Deferring it keeps the rule true without duplicating the constant,
    which would make it drift.
    """
    from brain_v42.models.brain_session import (  # noqa: PLC0415
        MAX_CAPTURED_KNOWLEDGE_IDS,
    )

    return MAX_CAPTURED_KNOWLEDGE_IDS


#: The tables whose creations can be attributed. A deliberate mirror of
#: ``pg_brain_session.CAPTURE_TABLES``: see the import cycle above.
_CAPTURE_TABLES: Final[tuple[tuple[sa.Table, str], ...]] = (
    (decisions, "decision"),
    (learnings, "learning"),
    (snippets, "snippet"),
    (runbooks, "runbook"),
    (adrs, "adr"),
    (indexed_plans, "indexed_plan"),
)

#: By table NAME — the only thing ``BasePgRepository`` knows about its own.
CAPTURE_TABLES: Final[Mapping[str, str]] = {
    table.name: knowledge_type for table, knowledge_type in _CAPTURE_TABLES
}


#: The four attribution modes, mirroring the 048 CHECK and `tables.py`. Naming
#: them here rather than writing them inline is what makes an invented mode fail
#: at the INSERT rather than in production, on a constraint.
EXPLICIT: Final = "explicit"
DEPOSIT: Final = "derived_deposit"
BY_CONNECTION: Final = "derived_connection"
BY_WINDOW: Final = "derived_window"

#: Statuses of a tracer whose ledger stays TAKEABLE. `closed_inactive` is one
#: of them, and that is not generosity: the 4 h sweep exists to move a tracer
#: out of `open` WHILE KEEPING its ledger. Sticking to `open` would work today —
#: the sweep is inert through a drop-in placement accident — and would go silent
#: again the day someone fixes that placement, without a single test failing.
_DONOR_STATUSES: Final = ("open", "closed_inactive")

#: Actors whose tracer is NEVER absorbed by the window stage. SQL mirror of
#: `provenance.is_human_actor` — the dream is not an "unknown creator", it is
#: identified, and leaving it in the common pool would make the failure mode
#: daily (the 03:00 `promote` falls inside the window of any session open that
#: night) instead of marginal.
#:
#: IMPORTED, never redeclared: a mirror that copies its constants stops being
#: one the first time a single side is widened — pinned by
#: `test_the_sql_mirror_shares_the_same_constants`.
_SYSTEM_ACTOR_PREFIXES: Final = SYSTEM_ACTOR_PREFIXES
_SYSTEM_ACTOR_NAMES: Final = SYSTEM_ACTOR_NAMES
_NON_HUMAN_ACTORS: Final = ("unknown", "_unexpanded", "")


@dataclass(frozen=True)
class AbsorptionOutcome:
    """What the absorption did, AND by which key — never a bare total.

    Three `0` returns were indistinguishable until now: flag closed, no
    connection, and "nothing to absorb". A fourth joins them with the window
    stage — the REFUSAL for ambiguity — and it is the only one that means "the
    rule ran and said no". Confusing them reproduces exactly this project's
    failure mode: a capability armed, green, and silent where it fails.

    `total` is what the repository returns to the caller; the rest is what one
    reads in the log when looking for why nothing moved.
    """

    reason: str
    moved_by_connection: int = 0
    moved_by_window: int = 0
    #: Artifacts the window stage refused because ANOTHER non-`agent` session
    #: covered their creation instant. Without this count, a systematic refusal
    #: is indistinguishable from a dead path.
    rivals: int = 0
    moved_ids: tuple[UUID, ...] = field(default_factory=tuple)
    donors: tuple[UUID, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.moved_by_connection + self.moved_by_window


def _enabled() -> bool:
    """Flag closed by default. A resolution that fails counts as "closed"."""
    try:
        return bool(get_settings().brain_session_derived_capture_enabled)
    except Exception:
        return False


def _current_connection_id() -> str:
    from brain_v42.provenance import get_current_transport  # noqa: PLC0415

    return (get_current_transport() or "").strip()


def _tracer_query(project_key: str, connection_id: str) -> sa.Select[Any]:
    """This connection's tracer, and nothing else.

    Four bounds, none decorative. ``nature = 'agent'`` in particular: without
    it, derivation could deposit into a human's session without them asking,
    which is precisely what absorption must remain alone in doing — and on an
    explicit command.
    """
    return sa.select(brain_sessions.c.id).where(
        brain_sessions.c.project_key == project_key,
        brain_sessions.c.connection_id == connection_id,
        brain_sessions.c.status == "open",
        brain_sessions.c.nature == "agent",
    )


async def derive_capture(
    session: AsyncSession,
    table_name: str,
    row: Mapping[str, Any],
) -> UUID | None:
    """Deposit the freshly created artifact into its connection's tracer.

    Returns the deposited identifier, or ``None`` — and ``None`` is never an
    error: no flag, no connection, no tracer, table out of scope, artifact
    already attributed, ledger full. These are refusals, not failures, and none
    of them must be visible from the call that creates.

    **It never steals**: ``ON CONFLICT DO NOTHING`` is on ``knowledge_id``,
    which IS the ledger's primary key. An already attributed artifact stays
    where it is, whether with an explicit session or another tracer.

    **It does not break the creation it observes**: everything lives inside a
    ``begin_nested()`` and every ``Exception`` is swallowed. "Does not" and not
    "never", because ``except Exception`` does not catch ``BaseException``: a
    ``CancelledError`` received during ``__aexit__``'s ``ROLLBACK TO
    SAVEPOINT`` can leave the calling transaction in an undetermined state. The
    window is narrow, and writing it down is cheaper than letting anyone believe
    in a total guarantee.

    Resolving the connection lives INSIDE the ``try``, not above it: its import
    is deferred, so an ``ImportError`` from ``brain_v42.provenance`` would
    surface in the observed call — exactly what these guards claim to prevent.
    """
    knowledge_type = CAPTURE_TABLES.get(table_name)
    if knowledge_type is None or not _enabled():
        return None

    try:
        connection_id = _current_connection_id()
        knowledge_id = row.get("id")
        project_key = row.get("project_key")
        if not connection_id or knowledge_id is None or not project_key:
            return None

        async with session.begin_nested():
            tracer = (
                await session.execute(_tracer_query(str(project_key), connection_id))
            ).scalar_one_or_none()
            if tracer is None:
                return None

            occupied = await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_session_artifacts)
                .where(brain_session_artifacts.c.session_id == tracer)
            )
            if int(occupied.scalar_one() or 0) >= _capture_cap():
                return None

            inserted = (
                await session.execute(
                    pg_insert(brain_session_artifacts)
                    .values(
                        knowledge_id=knowledge_id,
                        session_id=tracer,
                        knowledge_type=knowledge_type,
                        attribution_mode=DEPOSIT,
                    )
                    .on_conflict_do_nothing(index_elements=[brain_session_artifacts.c.knowledge_id])
                    .returning(brain_session_artifacts.c.knowledge_id)
                )
            ).scalar_one_or_none()
    except Exception:
        # A TOTAL, tightly scoped ``except``, same posture as auto-open: this
        # path accompanies EVERY knowledge creation. The savepoint has already
        # made the creating transaction sound; what remains is not to
        # propagate.
        logger.warning("session_derived_capture.failed", table=table_name, exc_info=True)
        return None

    return UUID(str(inserted)) if inserted is not None else None


def _eligible_ids(project_key: str, started_at: datetime, limit: int) -> sa.CompoundSelect[Any]:
    """What an EXPLICIT capture would have accepted, and nothing more.

    ``_validate_captures`` bounds a requested capture to "same project AND
    ``created_at >= started_at``", on these six tables. Absorption carries the
    SAME bounds, and that is this project's invariant: without it, derivation
    would attribute artifacts the user could not have captured themselves, and
    so would become a path more permissive than the command it replaces. A
    special dispensation, not a convenience.
    """
    branches = [
        sa.select(table.c.id).where(
            table.c.project_key == project_key,
            table.c.created_at >= started_at,
        )
        for table, _knowledge_type in _CAPTURE_TABLES
    ]
    return sa.union_all(*branches).limit(limit)


def _eligible_rows(project_key: str, started_at: datetime) -> sa.CompoundSelect[Any]:
    """The same bounds as ``_eligible_ids``, but the INSTANT travels with the id.

    The window stage does not judge an artifact in the abstract: it judges who
    covered the instant of its creation. That instant must therefore be
    correlatable row by row, which a list of bare identifiers does not allow.

    **NO ``LIMIT`` here, and that is fix A1.** The previous version bounded this
    ``UNION ALL`` before the rivalry filter applied: Postgres then returned an
    ARBITRARY batch (no ``ORDER BY``), which could be entirely contested, and
    the legitimate rows were never absorbed — silently, and differently from one
    call to the next. The ceiling is not 100 but ``100 - taken``, which shrinks
    as a session accumulates: the case happened at the end of a long session,
    exactly when absorption is worth the most. So we bound AFTER filtering.
    """
    branches = [
        sa.select(table.c.id.label("id"), table.c.created_at.label("created_at")).where(
            table.c.project_key == project_key,
            table.c.created_at >= started_at,
        )
        for table, _knowledge_type in _CAPTURE_TABLES
    ]
    return sa.union_all(*branches)


def _window_donors(project_key: str) -> sa.Select[Any]:
    """The project's tracers whose ledger the window may take.

    Three bounds, none decorative. ``nature='agent'``: we never take from a
    human. The status includes ``closed_inactive``, otherwise the fix would go
    silent again the day the 4 h sweep stops being inert. And the actor must be
    human: a tracer opened by the dream is identified as such, and leaving it in
    the common pool would drop the 03:00 `promote` into the window of every
    session open that night.
    """
    actor = brain_sessions.c.started_by_actor
    return sa.select(brain_sessions.c.id).where(
        brain_sessions.c.project_key == project_key,
        brain_sessions.c.nature == "agent",
        brain_sessions.c.status.in_(_DONOR_STATUSES),
        actor.is_not(None),
        actor.not_in(_NON_HUMAN_ACTORS),
        actor.not_in(_SYSTEM_ACTOR_NAMES),
        *[sa.not_(actor.startswith(prefix)) for prefix in _SYSTEM_ACTOR_PREFIXES],
    )


def _covered_by_a_rival(
    project_key: str, target_id: Any, created_at: Any
) -> sa.ColumnElement[bool]:
    """Did ANOTHER non-`agent` session cover this instant?

    Rivalry is SYMMETRIC: no recency clause, no sibling clause. Two claimants
    mean an abstention — never a coin toss, and never "the most recent wins",
    which would make attribution depend on the order of closing.

    Coverage is judged at the INSTANT, not at command time:
    ``started_at <= t <= coalesce(ended_at, now())``. A session closed AFTER the
    instant did cover it and therefore stays a rival; a session closed BEFORE
    covers nothing. Judging "open NOW" would let the last one to close take
    everything.
    """
    rival = brain_sessions.alias("rival")
    return sa.exists().where(
        sa.and_(
            rival.c.project_key == project_key,
            rival.c.id != target_id,
            sa.or_(rival.c.nature.is_(None), rival.c.nature != "agent"),
            rival.c.started_at <= created_at,
            sa.func.coalesce(rival.c.ended_at, sa.func.now()) >= created_at,
        )
    )


async def absorb_tracer_ledger(
    session: AsyncSession,
    target: Any,
    connection_id: str,
) -> AbsorptionOutcome:
    """Give ``target`` what the tracers collected for it. TWO stages.

    This is ABSORPTION: the user's session takes what a tracer collected,
    without the tracer ever being promoted.

    **Stage 1 — the current connection.** The EXACT match, evaluated first and
    unchanged. When it answers, there is nothing to infer.

    **Stage 2 — temporal exclusivity.** It exists only because stage 1 is
    structurally insufficient: ``connection_id`` is the ``Mcp-Session-Id``, a
    TRANSPORT identifier that the 900 s idle timeout kills long before the user
    closes their session — measured ~26 times a day, against 3 restarts in three
    days. A 16 h session facing transports whose median lifetime is under 2
    minutes cannot be matched by the connection of its single closing call.

    Stage 2 is a DEDUCTION, not a proof, and the code must say so: it attributes
    only if ``target`` was, at the creation instant, the ONLY non-`agent`
    session of the project covering that instant. Under ambiguity it refuses —
    the artifact stays with the tracer, visible and not lost.

    The donor stays `agent` ONLY, at both stages. Absorbing an `operator`
    session would move a human's ledger to another human, which is what ledger
    exclusivity exists to prevent.

    The explicit-capture ceiling of 100 is respected and DECREMENTED between
    stages: crossing it would make ``brain_session_capture`` refusable for a
    reason the user did not cause.
    """
    if not _enabled():
        return AbsorptionOutcome(reason="disabled")
    if not connection_id:
        # `stdio` and the stateless mode have no (project, connection) pair.
        # That is not the same "nothing" as a closed flag, and confusing the two
        # is exactly what kept this failure silent for ten days.
        return AbsorptionOutcome(reason="no_connection")

    moved_connection: list[UUID] = []
    moved_window: list[UUID] = []
    donors: list[UUID] = []
    rivals = 0

    try:
        async with session.begin_nested():
            tracer = (
                await session.execute(_tracer_query(target.project_key, connection_id))
            ).scalar_one_or_none()

            occupied = int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(brain_session_artifacts)
                        .where(brain_session_artifacts.c.session_id == target.id)
                    )
                ).scalar_one()
                or 0
            )
            remaining = _capture_cap() - occupied
            if remaining <= 0:
                # A2c: this refusal reaches the LOG, not just the API. The
                # previous version returned from here emitting nothing, while
                # the batch promised reasons distinguishable "in the API as in
                # the log".
                full = AbsorptionOutcome(reason="ledger_full")
                _log_absorption(target, connection_id, full)
                return full

            if tracer is not None and tracer != target.id:
                donors.append(UUID(str(tracer)))
                moved_connection = list(
                    (
                        await session.execute(
                            brain_session_artifacts.update()
                            .where(
                                brain_session_artifacts.c.session_id == tracer,
                                brain_session_artifacts.c.knowledge_id.in_(
                                    _eligible_ids(target.project_key, target.started_at, remaining)
                                ),
                            )
                            .values(session_id=target.id, attribution_mode=BY_CONNECTION)
                            .returning(brain_session_artifacts.c.knowledge_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                remaining -= len(moved_connection)

            if remaining > 0:
                eligible = _eligible_rows(target.project_key, target.started_at).subquery()
                contested = _covered_by_a_rival(
                    target.project_key, target.id, eligible.c.created_at
                )
                parked = brain_session_artifacts.join(
                    eligible, eligible.c.id == brain_session_artifacts.c.knowledge_id
                )
                in_a_tracer = brain_session_artifacts.c.session_id.in_(
                    _window_donors(target.project_key)
                )

                # A1: FILTER first, then bound. And SELECT before updating,
                # because an UPDATE's `RETURNING` yields the NEW value of
                # `session_id`: the donor would be lost at the precise moment it
                # matters — the inferred stage is the one we will want to undo
                # (A2b).
                candidates = (
                    await session.execute(
                        sa.select(
                            brain_session_artifacts.c.knowledge_id,
                            brain_session_artifacts.c.session_id,
                        )
                        .select_from(parked)
                        .where(in_a_tracer, sa.not_(contested))
                        .limit(remaining)
                    )
                ).all()

                if candidates:
                    taken = [row[0] for row in candidates]
                    donors.extend(dict.fromkeys(UUID(str(row[1])) for row in candidates))
                    moved_window = list(
                        (
                            await session.execute(
                                brain_session_artifacts.update()
                                .where(brain_session_artifacts.c.knowledge_id.in_(taken))
                                .values(session_id=target.id, attribution_mode=BY_WINDOW)
                                .returning(brain_session_artifacts.c.knowledge_id)
                            )
                        )
                        .scalars()
                        .all()
                    )

                # A2a: counted ON EVERY PASS of the window stage, not only when
                # nothing moved. An absorption taking 1 row by connection and
                # refusing 5 for ambiguity used to log `rivals_blocked=0` — the
                # "total that masks", which this batch exists to prevent,
                # reintroduced one level down.
                rivals = int(
                    (
                        await session.execute(
                            sa.select(sa.func.count())
                            .select_from(parked)
                            .where(in_a_tracer, contested)
                        )
                    ).scalar_one()
                    or 0
                )
    except Exception:
        logger.warning("session_derived_capture.absorb_failed", exc_info=True)
        return AbsorptionOutcome(reason="failed")

    moved_ids = tuple(UUID(str(item)) for item in (*moved_connection, *moved_window))
    if moved_ids:
        reason = "absorbed"
    elif rivals:
        reason = "ambiguous"
    else:
        reason = "nothing_to_absorb"

    outcome = AbsorptionOutcome(
        reason=reason,
        moved_by_connection=len(moved_connection),
        moved_by_window=len(moved_window),
        rivals=rivals,
        moved_ids=moved_ids,
        donors=tuple(donors),
    )
    _log_absorption(target, connection_id, outcome)
    return outcome


def _log_absorption(target: Any, connection_id: str, outcome: AbsorptionOutcome) -> None:
    """The production observable: BY WHICH KEY, and on WHICH artifacts.

    Without the UUIDs, a bad attribution is not undoable — one would know there
    had been one, never which. Without the rival count, a systematic refusal is
    indistinguishable from a dead path, which is the failure mode this batch
    repairs.
    """
    if outcome.reason in {"disabled", "no_connection"}:
        return
    logger.info(
        "session_derived_capture.absorbed",
        reason=outcome.reason,
        session_id=str(target.id),
        project_key=target.project_key,
        connection_id=connection_id,
        moved_by_connection=outcome.moved_by_connection,
        moved_by_window=outcome.moved_by_window,
        rivals_blocked=outcome.rivals,
        moved_ids=[str(item) for item in outcome.moved_ids],
        donors=[str(item) for item in outcome.donors],
    )
