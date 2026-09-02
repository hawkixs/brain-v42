"""PostgreSQL persistence for explicit concurrent Brain sessions."""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.focus_history import record_focus_history
from brain_v42.db.focus_stamp import focus_stamp
from brain_v42.db.tables import (
    adrs,
    brain_session_artifacts,
    brain_session_checkpoints,
    brain_sessions,
    decisions,
    indexed_plans,
    learnings,
    project_contexts,
    runbooks,
    snippets,
)
from brain_v42.models.brain_session import (
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    MAX_CAPTURED_KNOWLEDGE_IDS,
    MAX_CHECKPOINTS_PER_SESSION,
    SESSION_STALE_AFTER,
    BrainSession,
    BrainSessionAbandonResult,
    BrainSessionCaptureConflictError,
    BrainSessionCaptureResult,
    BrainSessionCheckpointConflictError,
    BrainSessionCheckpointResult,
    BrainSessionClientKeyConflictError,
    BrainSessionEndResult,
    BrainSessionFocusOutcome,
    BrainSessionHeartbeatResult,
    BrainSessionIdentityConflictError,
    BrainSessionInputError,
    BrainSessionListResult,
    BrainSessionNotFoundError,
    BrainSessionResumeResult,
    BrainSessionStartResult,
    BrainSessionStateError,
    BrainSessionStatus,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
    BrainSessionTerminalConflictError,
)
from brain_v42.repositories.pg_base import BasePgRepository

Row = dict[str, Any]


def _observation_columns(reference: datetime) -> dict[str, datetime]:
    """The TWO clocks a tracer observation moves, and not one more.

    Written once because the two writers — ``auto_open``'s ``ON CONFLICT DO
    UPDATE`` and ``observe`` — must move EXACTLY the same set. Letting them
    diverge would give a re-identified connection a presence clock different
    from a re-observed one, and the sweep would read two regimes for a single
    gesture.
    """
    return {"last_observed_at": reference, "last_heartbeat_at": reference}


CAPTURE_TABLES = (
    (decisions, "decision"),
    (learnings, "learning"),
    (snippets, "snippet"),
    (runbooks, "runbook"),
    (adrs, "adr"),
    (indexed_plans, "indexed_plan"),
)


class PgBrainSessionRepo(BasePgRepository):
    """Own the atomic lifecycle of persistent Brain sessions."""

    table = brain_sessions
    fts_columns: list[str] = []

    async def start(self, project_key: str, client_key: str) -> BrainSessionStartResult:
        """Create or replay a session idempotently for a project/client key."""
        normalized_client_key = client_key.strip()
        if not normalized_client_key:
            raise BrainSessionInputError("client_key must not be blank")

        async with self.transaction() as session:
            focus = await self._load_focus(session, project_key)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {project_key!r} was not found")

            insert_stmt = (
                pg_insert(brain_sessions)
                .values(
                    project_key=project_key,
                    client_key=normalized_client_key,
                    started_focus=focus["current_focus"],
                    started_focus_revision=focus["focus_revision"],
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        brain_sessions.c.project_key,
                        brain_sessions.c.client_key,
                    ]
                )
                .returning(brain_sessions)
            )
            insert_result = await session.execute(insert_stmt)
            inserted_row = insert_result.mappings().one_or_none()
            row = dict(inserted_row) if inserted_row is not None else None
            replayed = inserted_row is None

            if replayed:
                row = await self._get_by_client_key(session, project_key, normalized_client_key)
            if row is None:
                raise BrainSessionClientKeyConflictError(
                    f"Could not replay client_key {normalized_client_key!r}"
                )

            model = self._to_model(row)
            if replayed and model.status != "open":
                raise BrainSessionClientKeyConflictError(
                    f"client_key {normalized_client_key!r} belongs to a "
                    f"{model.status.value} session; use a new client_key"
                )

            if replayed:
                attributed_ids = await self._load_session_artifact_ids(session, model.id)
                model = model.model_copy(update={"attributed_knowledge_ids": attributed_ids})

            open_count = await self._count_open(session, project_key)
            return BrainSessionStartResult(
                session=model,
                replayed=replayed,
                open_session_count=open_count,
            )

    async def auto_open(self, identity: Any, *, now: datetime | None = None) -> UUID | None:
        """Open — or find AND RE-OBSERVE — THE `agent` session of a connection.

        A single round trip, conflict included. Idempotence is carried by 046's
        **PARTIAL** UNIQUE index ``uq_brain_sessions_connection``
        (``WHERE status = 'open'``), not by the calling code: two concurrent
        calls on the same connection produce a conflict, not two sessions, and
        an already closed session does not block the next one. A full index
        would have burned the connection for life at the first auto-closure
        (inherited trap, `SPEC-M-G` §5).

        ``ON CONFLICT DO UPDATE`` and not ``DO NOTHING``: the conflict is the
        case "this connection already has its session", and that is **an
        observation**. The old form followed it with a ``SELECT`` to find the
        id — two round trips that dated nothing. Here the same row is found AND
        re-observed, and the ``RETURNING`` yields the id on both branches.

        ``client_key`` receives a fresh UUID, and that is not a detail: the
        ``uq_brain_sessions_project_client`` constraint is **full**, so reusing
        a key that is stable per connection would make reopening fail after a
        closure — the partial-index trap, moved one column over. On this path
        the client key no longer keeps anything anyway: ``expected_client_key``
        was removed from it (§0ter.3), identity being the connection.

        ``started_at`` is set EXPLICITLY, and only on the INSERT branch. Without
        it the column fell back to the database's ``DEFAULT now()`` — the
        TRANSACTION START stamp, hence later than the ``reference`` the
        application reads BEFORE opening the transaction. The row was born with
        ``last_heartbeat_at`` dated 1.5 ms before its own start, and the DR
        contract counted it: receipt 28/29 measured in production on 2026-08-22,
        on both variants of the asset. ``start()`` never had the defect because
        it sets NEITHER of the two columns: its clocks come from the same
        default. It was the asymmetry that cost, not the default itself.

        It stays out of ``_observation_columns()``, and that is the heart of the
        fix: re-observing is not reopening. Slipping it into the shared set
        would make the tracer young again on every tool call, and the 7 d sweep
        would never take anything.

        Returns ``None`` when the project has no context: the server does not
        manufacture one. Does not raise in that case — ``start()`` does, because
        there a user named a project that does not exist and must learn it; here
        nobody named anything.
        """
        reference = now or datetime.now(UTC)
        async with self.transaction() as session:
            focus = await self._load_focus(session, identity.project_key)
            if focus is None:
                return None

            insert_stmt = (
                pg_insert(brain_sessions)
                .values(
                    project_key=identity.project_key,
                    client_key=f"auto:{uuid4().hex}",
                    started_focus=focus["current_focus"],
                    started_focus_revision=focus["focus_revision"],
                    nature=identity.nature,
                    connection_id=identity.connection_id,
                    started_by_actor=identity.started_by_actor,
                    intent=identity.intent,
                    started_at=reference,
                    last_observed_at=reference,
                    last_heartbeat_at=reference,
                )
                .on_conflict_do_update(
                    index_elements=[
                        brain_sessions.c.project_key,
                        brain_sessions.c.connection_id,
                    ],
                    index_where=sa.text("status = 'open'"),
                    set_=_observation_columns(reference),
                    # A HARD guard on the conflict's ACTION, symmetric with
                    # ``observe``'s: without it, an `operator` row carrying a
                    # connection would have `last_heartbeat_at` re-dated on
                    # every tool call. But the sweep's 7-day eligibility reads
                    # that column WITH NO nature filter — the one written
                    # exception to the covenant would become unreachable, and
                    # the row an immortal ghost.
                    where=brain_sessions.c.nature == "agent",
                )
                .returning(brain_sessions.c.id)
            )
            inserted = (await session.execute(insert_stmt)).scalar_one_or_none()
            return UUID(str(inserted)) if inserted is not None else None

    async def attributed_knowledge_ids(self, session_id: UUID | str) -> builtins.list[UUID]:
        """This session's ledger, re-read — the SOURCE OF TRUTH, not the snapshot.

        `brain_sessions.captured_knowledge_ids` is a TERMINAL photograph: one
        writer, at closing time, and the `open` constraint forbids filling it
        earlier. Measured on 2026-08-25: across 44 open sessions, zero non-empty
        arrays, and none has ever carried one in the whole history of the table.
        Reading it on a live session would therefore always return `[]`.

        Exists for `start`, the only one of the five that cannot absorb before
        materializing: it re-reads here what its absorption just moved.
        """
        async with self.get_session() as session:
            return await self._load_session_artifact_ids(session, UUID(str(session_id)))

    async def absorb_derived_capture(
        self,
        session_id: UUID | str,
        connection_id: str,
        expected_client_key: str,
    ) -> int:
        """Have this session absorb the ledger of its connection's tracer.

        The repository decides nothing about the BOUNDS: it opens the
        transaction, finds the target session and delegates to
        ``absorb_tracer_ledger``, which aligns them with those of an EXPLICIT
        capture. The service has already settled the flag and the connection
        upstream, so that a closed flag does not cost this round trip.

        **THE IDENTITY GUARD LIVES HERE, IN THE SAME TRANSACTION AS THE
        MUTATION**, and not at the call site. `CLAUDE.md` is literal — "the
        server refuses an inconsistent pair BEFORE ANY MUTATION" — and a call
        order keeps that promise only for as long as nobody reorders: which is
        exactly what just happened when absorption was moved ahead of
        `_assert_identity`, which lived in the repository's commands. A
        mistargeted call then moved a tracer's ledger into someone else's
        session, THEN got refused — and since the ledger is EXCLUSIVE, that move
        is IRREVERSIBLE, while the caller sees nothing but a refusal.

        It RAISES, instead of returning ``0`` in silence. An inconsistent pair
        is not one refusal to absorb among others: it is a mistargeted command,
        which the repository is about to refuse two lines further down with the
        same exception anyway. Surfacing it here makes the guard impassable
        instead of leaving it dependent on an order.

        ⚠ WHAT PROTECTS IS THE TRANSACTIONAL BOUNDARY, NOT THE POSITION OF THIS
        LINE. Measured, two mutants played:

        - guard moved AFTER ``absorb_tracer_ledger`` but INSIDE this ``async
          with`` → **11 tests green**. An EQUIVALENT mutant, not a hollow test:
          ``transaction()`` opens a ``sess.begin()``, so the exception rolls the
          move back along with the rest. The position above is a READABILITY
          choice;
        - guard moved AFTER and OUTSIDE the ``async with`` → **red**. The move
          is committed before the refusal, and the bench sees it.

        Put differently: safety rests on no COMMIT interposing between the
        ledger move and the refusal. The two gestures that would break it
        VISIBLY — taking absorption out of the transaction, slipping a
        ``commit()`` into it — are therefore covered by
        ``test_a_mistargeted_absorption_moves_NOTHING_before_it_refuses``.

        WHAT NO TEST WATCHES, and it is why this paragraph exists: **giving this
        method a ``session`` parameter**. ``transaction()`` would switch to its
        ``begin_nested()`` branch and the rollback scope would become the
        CALLER's, not ours. The bench never passes a session — it would
        therefore stay GREEN while a caller that does pass one, and swallows the
        exception, commits the move. It takes none today, and that is what makes
        the guarantee unconditional.

        A test cannot express "do not give me a session". This comment, however,
        sits where the refactor will be read. The cost of the hole is recalled
        above: the ledger is EXCLUSIVE, so a mistargeted move is IRREVERSIBLE.

        Returns the number of rows moved; ``0`` covers every other refusal.
        """
        from brain_v42.db.session_derived_capture import (  # noqa: PLC0415
            absorb_tracer_ledger,
        )

        async with self.transaction() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                # Unknown session: nothing to absorb, and above all not an
                # identity error — the command is what will say "not found".
                return 0
            self._assert_identity(self._to_model(row), expected_client_key)
            target = SimpleNamespace(
                id=row["id"],
                project_key=row["project_key"],
                started_at=row["started_at"],
            )
            outcome = await absorb_tracer_ledger(session, target, connection_id)
            return outcome.total

    async def observe(self, session_id: UUID | str, *, now: datetime | None = None) -> bool:
        """Stamp the observation of an open `agent` tracer. Returns "still open".

        This is the writer 046 was waiting for: without it, ``last_observed_at``
        stays NULL across the whole table, and the sweep's 4 h rule matches
        NOTHING — M-G would ship inert. Guarantee 2 of `§0bis.3` is literal: the
        column moves on **every** tool call.

        **The false corpse, and why ``last_heartbeat_at`` moves too.** The 7 d
        sweep reads ``last_heartbeat_at``. A tracer whose connection lives eight
        days has never sent a heartbeat — there is no user to send one — and
        would be abandoned in full activity. That is exactly the false corpse of
        2026-08-06. The two clocks therefore move together on this path, and
        remain two columns: the 4 h reads observation, the 7 d reads presence,
        and a tracer inactive for more than seven days still matches BOTH
        (precedence: `sweep_open_sessions`).

        **``updated_at`` does NOT move**, and that is deliberate: observing is
        not mutating the session's declared state. Refreshing it would turn the
        last-write column into an activity signal, exactly the hollow control
        `77348350` cost elsewhere.

        The ``nature = 'agent'`` in the predicate is a HARD guard, not a
        redundancy: this path must never be able to stamp an `operator` session,
        even if a poisoned memo handed it the UUID.
        """
        reference = now or datetime.now(UTC)
        statement = (
            brain_sessions.update()
            .where(
                brain_sessions.c.id == session_id,
                brain_sessions.c.status == "open",
                brain_sessions.c.nature == "agent",
            )
            .values(**_observation_columns(reference))
            .returning(brain_sessions.c.id)
        )
        async with self.transaction() as session:
            observed = (await session.execute(statement)).scalar_one_or_none()
        return observed is not None

    async def get_by_id(  # type: ignore[override]
        self, session_id: UUID | str
    ) -> BrainSession | None:
        """Return a session by id without changing its state."""
        async with self.get_session() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                return None
            attributed_ids = await self._load_session_artifact_ids(session, row["id"])
        return self._to_model(row, attributed_ids=attributed_ids)

    async def list(
        self,
        project_key: str | None = None,
        status: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> BrainSessionListResult:
        """List project sessions in reverse chronological order."""
        if limit < 1 or limit > 100 or offset < 0:
            raise BrainSessionInputError("limit must be between 1 and 100 and offset non-negative")

        filters: list[Any] = []
        if project_key is not None:
            filters.append(brain_sessions.c.project_key == project_key)
        now = datetime.now(UTC)
        if status == "stale":
            filters.extend(
                (
                    brain_sessions.c.status == "open",
                    brain_sessions.c.last_heartbeat_at <= now - SESSION_STALE_AFTER,
                )
            )
        elif status not in (None, "all"):
            filters.append(brain_sessions.c.status == status)

        async with self.get_session() as session:
            count_stmt = sa.select(sa.func.count()).select_from(brain_sessions).where(*filters)
            total = int((await session.execute(count_stmt)).scalar_one())
            list_stmt = (
                sa.select(brain_sessions)
                .where(*filters)
                .order_by(brain_sessions.c.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(list_stmt)).mappings().all()
            attributed_by_session = await self._load_artifact_ids_by_session(
                session,
                [row["id"] for row in rows],
            )

        return BrainSessionListResult(
            sessions=[
                self._to_model(
                    row,
                    now=now,
                    attributed_ids=attributed_by_session.get(row["id"], []),
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def resume(
        self,
        session_id: UUID | str,
        expected_client_key: str,
    ) -> BrainSessionResumeResult:
        """Read an open session and current focus without mutating either."""
        async with self.get_session() as session:
            row = await self._get_row(session, session_id)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)
            if model.status != "open":
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            attributed_ids = await self._load_session_artifact_ids(session, model.id)
            model = model.model_copy(update={"attributed_knowledge_ids": attributed_ids})

            focus = await self._load_focus(session, model.project_key)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
            open_count = await self._count_open(session, model.project_key)

        return BrainSessionResumeResult(
            session=model,
            open_session_count=open_count,
            current_focus=focus["current_focus"],
            current_focus_revision=focus["focus_revision"],
        )

    async def capture(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        knowledge_ids: Sequence[UUID],
    ) -> BrainSessionCaptureResult:
        """Attach durable knowledge to exactly one explicit session."""
        capture_ids = sorted(knowledge_ids, key=str)
        self._validate_capture_ids(capture_ids, require_nonempty=True)

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                existing = await self._load_artifact_rows(session, capture_ids)
                existing_ids = self._owned_capture_ids(existing, model.id)
                if model.status in {"ended", "abandoned"} and existing_ids == set(capture_ids):
                    all_ids = await self._load_session_artifact_ids(session, model.id)
                    return BrainSessionCaptureResult(
                        session=model.model_copy(update={"attributed_knowledge_ids": all_ids}),
                        captured_knowledge_ids=all_ids,
                        newly_captured_knowledge_ids=[],
                        replayed_knowledge_ids=capture_ids,
                        replayed=True,
                    )
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            focus = await self._load_focus(session, model.project_key, for_update=True)
            if focus is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
            existing = await self._load_artifact_rows(session, capture_ids)
            # What the exclusivity rule refused to attribute must stay
            # repairable by a human who NAMES the UUID. Without this path,
            # fail-closed becomes a dead loss: the artifact stays with the
            # server and nobody can get it out.
            reclaimable = await self._tracer_held_ids(session, existing, model.id)
            existing_ids = self._owned_capture_ids(existing, model.id, reclaimable)
            if reclaimable:
                await session.execute(
                    brain_session_artifacts.update()
                    .where(brain_session_artifacts.c.knowledge_id.in_(reclaimable))
                    .values(session_id=model.id, attribution_mode="explicit")
                )
                existing_ids.update(reclaimable)
            resolved_types = await self._validate_captures(session, model, capture_ids)
            all_existing = await self._load_session_artifact_ids(session, model.id)
            all_after = sorted(set(all_existing) | set(capture_ids), key=str)
            if len(all_after) > MAX_CAPTURED_KNOWLEDGE_IDS:
                raise BrainSessionInputError(
                    "a session may capture at most "
                    f"{MAX_CAPTURED_KNOWLEDGE_IDS} knowledge artifacts"
                )

            missing = [item for item in capture_ids if item not in existing_ids]
            if missing:
                insert_stmt = (
                    pg_insert(brain_session_artifacts)
                    .values(
                        [
                            {
                                "knowledge_id": knowledge_id,
                                "session_id": model.id,
                                "knowledge_type": resolved_types[knowledge_id],
                                # A human named this UUID. It is the only mode
                                # that is a PROOF and not a deduction.
                                "attribution_mode": "explicit",
                            }
                            for knowledge_id in missing
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=[brain_session_artifacts.c.knowledge_id])
                    .returning(brain_session_artifacts.c.knowledge_id)
                )
                inserted_ids = set((await session.execute(insert_stmt)).scalars().all())
                if len(inserted_ids) != len(missing):
                    raced_rows = await self._load_artifact_rows(session, missing)
                    raced_owned_ids = self._owned_capture_ids(raced_rows, model.id)
                    unresolved = set(missing) - inserted_ids - raced_owned_ids
                    if unresolved:
                        raise BrainSessionStateError(
                            "session artifact ownership could not be resolved"
                        )
                    existing_ids.update(raced_owned_ids)
                missing = sorted(inserted_ids, key=str)

            now = datetime.now(UTC)
            heartbeat_stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(last_heartbeat_at=now, updated_at=now)
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(heartbeat_stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} could not record captures")

            return BrainSessionCaptureResult(
                session=self._to_model(
                    updated_row,
                    now=now,
                    attributed_ids=all_after,
                ),
                captured_knowledge_ids=all_after,
                newly_captured_knowledge_ids=missing,
                replayed_knowledge_ids=sorted(existing_ids, key=str),
                replayed=not missing,
            )

    async def checkpoint(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        *,
        seq: int,
        progress: str,
        next_step: str,
        blocker: str | None,
    ) -> BrainSessionCheckpointResult:
        """Append one semantic checkpoint, idempotently (SPEC-checkpoint §1.1, §2).

        `ON CONFLICT DO NOTHING … RETURNING` returns zero rows for an exact replay
        AND for a content collision — the same silence for two opposite events. So
        an empty `RETURNING` is not the answer, it is the question: the stored row
        is reread and the triple compared. Identical means the retry is absorbed
        and reported as `replayed`; different is a non-destructive conflict and
        raises, because `seq` is supplied by the CLIENT and agent retries are the
        norm (invariant C6), which makes the collision ordinary rather than exotic.

        The ceiling is checked INSIDE the row lock taken on the session, so two
        concurrent writers cannot both read 199 and both insert. It is counted
        before the insert and re-derived after, so the number returned is the
        number stored and never an optimistic guess.

        Writes NOTHING on `brain_sessions` — not `last_heartbeat_at`, not
        `updated_at`, not the focus. ADR D4 still describes a heartbeat side
        effect; §0bis.4 is later and dissolves it, and this method is where that
        resolution is either honoured or quietly undone.
        """
        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)
            if model.status != "open":
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            count_stmt = (
                sa.select(sa.func.count())
                .select_from(brain_session_checkpoints)
                .where(brain_session_checkpoints.c.session_id == model.id)
            )
            existing = int((await session.execute(count_stmt)).scalar_one())

            insert_stmt = (
                pg_insert(brain_session_checkpoints)
                .values(
                    session_id=model.id,
                    seq=seq,
                    progress=progress,
                    next_step=next_step,
                    blocker=blocker,
                )
                .on_conflict_do_nothing(constraint="uq_brain_session_checkpoints_session_seq")
                .returning(brain_session_checkpoints.c.created_at)
            )

            if existing >= MAX_CHECKPOINTS_PER_SESSION:
                # Fail-closed, but an exact REPLAY at the ceiling must still be
                # answerable: it stores nothing, and refusing it would make a
                # retry fail where the original succeeded.
                stored = await self._stored_checkpoint(session, model.id, seq)
                if stored is None:
                    raise BrainSessionInputError(
                        f"Session {session_id} already holds "
                        f"{MAX_CHECKPOINTS_PER_SESSION} checkpoints"
                    )
                return self._replayed_checkpoint(
                    model.id, seq, stored, progress, next_step, blocker, existing
                )

            created_at = (await session.execute(insert_stmt)).scalar_one_or_none()
            if created_at is not None:
                return BrainSessionCheckpointResult(
                    session_id=model.id,
                    seq=seq,
                    created_at=created_at,
                    replayed=False,
                    checkpoint_count=existing + 1,
                )

            stored = await self._stored_checkpoint(session, model.id, seq)
            if stored is None:  # pragma: no cover — the unique key just refused it
                raise BrainSessionStateError(
                    f"Session {session_id} checkpoint {seq} was neither stored nor found"
                )
            return self._replayed_checkpoint(
                model.id, seq, stored, progress, next_step, blocker, existing
            )

    @staticmethod
    async def _stored_checkpoint(session: AsyncSession, session_id: UUID, seq: int) -> Any:
        stmt = sa.select(
            brain_session_checkpoints.c.created_at,
            brain_session_checkpoints.c.progress,
            brain_session_checkpoints.c.next_step,
            brain_session_checkpoints.c.blocker,
        ).where(
            brain_session_checkpoints.c.session_id == session_id,
            brain_session_checkpoints.c.seq == seq,
        )
        return (await session.execute(stmt)).mappings().one_or_none()

    @staticmethod
    def _replayed_checkpoint(
        session_id: UUID,
        seq: int,
        stored: Any,
        progress: str,
        next_step: str,
        blocker: str | None,
        count: int,
    ) -> BrainSessionCheckpointResult:
        """Same key, same content = replay. Same key, other content = conflict.

        The comparison is the whole payload, never a prefix: two judgments that
        share an opening sentence are two judgments.
        """
        if (stored["progress"], stored["next_step"], stored["blocker"]) != (
            progress,
            next_step,
            blocker,
        ):
            raise BrainSessionCheckpointConflictError(
                f"Session {session_id} already has a checkpoint {seq} with different content"
            )
        return BrainSessionCheckpointResult(
            session_id=session_id,
            seq=seq,
            created_at=stored["created_at"],
            replayed=True,
            checkpoint_count=count,
        )

    async def heartbeat(
        self,
        session_id: UUID | str,
        expected_client_key: str,
    ) -> BrainSessionHeartbeatResult:
        """Refresh session presence without changing focus or lifecycle state."""
        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)
            if model.status != "open":
                raise BrainSessionStateError(f"Session {session_id} is {model.status}, not open")

            attributed_ids = await self._load_session_artifact_ids(session, model.id)

            now = datetime.now(UTC)
            stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(last_heartbeat_at=now, updated_at=now)
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} heartbeat was not persisted")
            return BrainSessionHeartbeatResult(
                session=self._to_model(
                    updated_row,
                    now=now,
                    attributed_ids=attributed_ids,
                )
            )

    async def _count_unattributed_in_window(
        self,
        session: AsyncSession,
        project_key: str,
        started_at: datetime,
        ended_at: datetime,
    ) -> int:
        """Count what the session produced and nobody attributed.

        The same six tables and the same window as an explicit capture, plus an
        ANTI-JOIN on the ledger. Without it, the number would rise when one does
        the right thing — it would also count what was attributed, and a figure
        that worsens as you work properly would be read by nobody.
        """
        produced = sa.union_all(
            *[
                sa.select(table.c.id).where(
                    table.c.project_key == project_key,
                    table.c.created_at >= started_at,
                    table.c.created_at <= ended_at,
                )
                for table, _knowledge_type in CAPTURE_TABLES
            ]
        ).subquery()
        # The anti-join looks at the OWNER'S NATURE, not merely at the
        # existence of a row. A row parked in an `agent` tracer does have an
        # owner, but that owner is the SERVER: counting it as attributed made
        # this receipt silent in exactly the case being repaired — measured on
        # 2026-08-24, `unattributed_in_window` read 0 while the only derived
        # artifact belonged to no human.
        owner = brain_sessions.alias("owner")
        attributed_to_a_human = sa.exists(
            sa.select(sa.literal(1))
            .select_from(
                brain_session_artifacts.join(
                    owner, owner.c.id == brain_session_artifacts.c.session_id
                )
            )
            .where(
                brain_session_artifacts.c.knowledge_id == produced.c.id,
                sa.or_(owner.c.nature.is_(None), owner.c.nature != "agent"),
            )
        )
        statement = sa.select(sa.func.count()).select_from(produced).where(~attributed_to_a_human)
        return int((await session.execute(statement)).scalar_one() or 0)

    async def end(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        nothing_to_capture_reason: str | None,
    ) -> BrainSessionEndResult:
        """End atomically while treating a stale shared focus as data."""
        normalized_summary = summary.strip()
        normalized_focus = next_focus.strip()
        normalized_reason = self._normalize_optional(nothing_to_capture_reason)
        self._validate_end_input(normalized_summary, normalized_focus, expected_focus_revision)

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                return await self._replay_end(
                    session,
                    model,
                    normalized_summary,
                    normalized_focus,
                    expected_focus_revision,
                    normalized_reason,
                )

            focus_before = await self._load_focus(
                session,
                model.project_key,
                for_update=True,
            )
            if focus_before is None:
                raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")

            capture_ids = await self._load_session_artifact_ids(session, model.id)
            if capture_ids:
                await self._validate_captures(session, model, capture_ids)

            focus, focus_outcome = await self._apply_focus_if_current(
                session,
                focus_before,
                project_key=model.project_key,
                next_focus=normalized_focus,
                expected_revision=expected_focus_revision,
            )
            ended = await self._mark_ended(
                session,
                model.id,
                normalized_summary,
                normalized_focus,
                capture_ids,
                normalized_reason,
                expected_focus_revision=expected_focus_revision,
                focus_outcome=focus_outcome,
                focus_at_end=focus["current_focus"],
                focus_revision_at_end=focus["focus_revision"],
            )
            remaining = await self._count_open(session, model.project_key)
            unattributed = await self._count_unattributed_in_window(
                session,
                model.project_key,
                model.started_at,
                ended.ended_at or model.started_at,
            )

            return BrainSessionEndResult(
                session=ended,
                replayed=False,
                remaining_open_session_count=remaining,
                unattributed_in_window=unattributed,
                current_focus=focus["current_focus"],
                current_focus_revision=focus["focus_revision"],
                focus_outcome=focus_outcome,
                focus_at_end=focus["current_focus"],
                focus_revision_at_end=focus["focus_revision"],
            )

    async def abandon(
        self,
        session_id: UUID | str,
        expected_client_key: str,
        reason: str,
    ) -> BrainSessionAbandonResult:
        """Atomically abandon an open session without touching project focus."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")

        async with self.transaction() as session:
            row = await self._get_row(session, session_id, for_update=True)
            if row is None:
                raise BrainSessionNotFoundError(f"Session {session_id} was not found")
            model = self._to_model(row)
            self._assert_identity(model, expected_client_key)

            if model.status != "open":
                if model.status == "abandoned" and model.abandonment_reason == normalized_reason:
                    attributed_ids = await self._load_session_artifact_ids(session, model.id)
                    remaining = await self._count_open(session, model.project_key)
                    return BrainSessionAbandonResult(
                        session=model.model_copy(
                            update={"attributed_knowledge_ids": attributed_ids}
                        ),
                        replayed=True,
                        remaining_open_session_count=remaining,
                    )
                raise BrainSessionTerminalConflictError(
                    f"Session {session_id} already has terminal state {model.status}"
                )

            attributed_ids = await self._load_session_artifact_ids(session, model.id)
            now = datetime.now(UTC)
            update_stmt = (
                brain_sessions.update()
                .where(brain_sessions.c.id == model.id)
                .values(
                    status="abandoned",
                    abandonment_reason=normalized_reason,
                    ended_at=now,
                    updated_at=now,
                )
                .returning(brain_sessions)
            )
            updated_row = (await session.execute(update_stmt)).mappings().one_or_none()
            if updated_row is None:
                raise BrainSessionStateError(f"Session {session_id} could not be abandoned")
            remaining = await self._count_open(session, model.project_key)
            return BrainSessionAbandonResult(
                session=self._to_model(
                    updated_row,
                    attributed_ids=attributed_ids,
                ),
                replayed=False,
                remaining_open_session_count=remaining,
            )

    async def sweep_open_sessions(
        self,
        *,
        older_than: timedelta = AUTO_STALE_AFTER,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        close_inactive_after: timedelta | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        """Drain the open sessions — TWO rules, ONE statement, one precedence.

        7 d rule (always active): any open session with no heartbeat since
        ``older_than`` goes to ``abandoned``, with its reason.

        4 h rule (``close_inactive_after``, ``None`` = closed): a
        ``nature = 'agent'`` tracer whose OBSERVATION is older than that
        threshold goes to ``closed_inactive``, with no reason and **with its
        ledger intact** — that is the whole point of 046. An ``operator``
        session, and a pre-046 ``nature IS NULL`` session, stay out of reach:
        resolution (d) refuses to judge retroactively.

        **PRECEDENCE: 7 d BEATS 4 h**, and that is not cosmetic. A tracer
        inactive for more than seven days matches BOTH predicates. The ``CASE``
        tests presence FIRST, so it goes to ``abandoned`` with its reason, never
        to a silent ``closed_inactive``. The rule is pinned by a test, not by
        this paragraph.

        **``last_observed_at IS NULL`` is NEVER taken by the 4 h rule** (S3,
        settled). ``NULL`` means "never observed", not "observed a long time
        ago": that is the regime of pre-auto-open sessions, and a SQL comparison
        would already let them out — the explicit predicate is there so the
        intent reads, and so the test guards it.

        **NEVER during an in-flight call** (guarantee 1 of §0bis.3), and the
        machinery is not here: ``observe()`` stamps the tracer BEFORE the tool
        runs, so an in-flight call carries an observation a few milliseconds
        old. The guarantee is structural and reads where it is produced; it no
        longer holds if a single tool call exceeds ``close_inactive_after``,
        which does not exist in the catalogue.

        SERVER path only: no ``expected_client_key`` guard, because no client is
        asking — the server is. The CLAUDE.md doctrinal amendment bounds this
        right to this path alone; it opens nothing for the agent nor for the
        client, whose seven commands stay explicit.

        Touches neither ``project_contexts`` nor ``brain_session_artifacts``:
        the focus and the capture ledger survive both outcomes, as for a manual
        abandonment. No focus CAS is attempted — N grouped closures would
        produce N−1 manufactured ``conflict``s (`SPEC-M-G` §3.2).
        """
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")
        if older_than <= timedelta(0):
            raise BrainSessionInputError("older_than must be a positive interval")
        if close_inactive_after is not None and close_inactive_after <= timedelta(0):
            raise BrainSessionInputError("close_inactive_after must be a positive interval")

        reference = now or datetime.now(UTC)
        cutoff = reference - older_than
        inactive_cutoff = None if close_inactive_after is None else reference - close_inactive_after

        # The PRESENCE predicate, isolated: it serves twice — for eligibility
        # and for the CASE's precedence. Duplicating it would let the two
        # diverge in silence.
        is_stale = brain_sessions.c.last_heartbeat_at < cutoff
        open_stale = sa.and_(brain_sessions.c.status == "open", is_stale)

        if inactive_cutoff is None:
            eligible: Any = open_stale
            outcome: Any = sa.literal(BrainSessionStatus.ABANDONED.value)
            status_value: Any = BrainSessionStatus.ABANDONED.value
            reason_value: Any = normalized_reason
        else:
            eligible = sa.and_(
                brain_sessions.c.status == "open",
                sa.or_(
                    is_stale,
                    sa.and_(
                        brain_sessions.c.nature == "agent",
                        brain_sessions.c.last_observed_at.is_not(None),
                        brain_sessions.c.last_observed_at < inactive_cutoff,
                    ),
                ),
            )
            # The `CASE` tests `is_stale` FIRST: this is WHERE the 7 d > 4 h
            # precedence lives, in executed SQL, not in a comment.
            outcome = sa.case(
                (is_stale, sa.literal(BrainSessionStatus.ABANDONED.value)),
                else_=sa.literal(BrainSessionStatus.CLOSED_INACTIVE.value),
            )
            status_value = outcome
            # `closed_inactive` FORBIDS `abandonment_reason` (046's CHECK): the
            # `CASE` is therefore not a convenience, it is what makes the row
            # acceptable to the database.
            reason_value = sa.case((is_stale, sa.literal(normalized_reason)), else_=sa.null())

        selection = (
            brain_sessions.c.id,
            brain_sessions.c.project_key,
            brain_sessions.c.client_key,
            brain_sessions.c.last_heartbeat_at,
            brain_sessions.c.last_observed_at,
        )

        if dry_run:
            statement: Any = sa.select(*selection, outcome.label("outcome")).where(eligible)
        else:
            # ONE statement. Not a SELECT then an UPDATE: under READ COMMITTED,
            # PostgreSQL re-evaluates `eligible` under the row lock, so a
            # heartbeat committing during the sweep pulls its row out of the
            # update instead of losing the race. That is the answer to the false
            # corpse of 2026-08-06 (a live session wrongly abandoned), and it
            # covers the new rule without one more line.
            statement = (
                brain_sessions.update()
                .where(eligible)
                .values(
                    status=status_value,
                    abandonment_reason=reason_value,
                    ended_at=reference,
                    updated_at=reference,
                )
                # `status` AFTER the write: RETURNING sees the new row, so the
                # report reads the outcome actually persisted, not one
                # recomputed in Python that could diverge from the CASE.
                .returning(*selection, brain_sessions.c.status.label("outcome"))
            )

        async with self.transaction() as session:
            rows = (await session.execute(statement)).mappings().all()

        candidates = sorted(
            (BrainSessionSweepCandidate(**dict(row)) for row in rows),
            key=lambda candidate: candidate.last_heartbeat_at,
        )
        counted = [] if dry_run else candidates
        return BrainSessionSweepResult(
            candidates=candidates,
            dry_run=dry_run,
            cutoff=cutoff,
            inactive_cutoff=inactive_cutoff,
            abandoned_count=sum(1 for c in counted if c.outcome is BrainSessionStatus.ABANDONED),
            closed_inactive_count=sum(
                1 for c in counted if c.outcome is BrainSessionStatus.CLOSED_INACTIVE
            ),
        )

    async def _get_row(
        self,
        session: AsyncSession,
        session_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> Row | None:
        stmt = sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _get_by_client_key(
        self,
        session: AsyncSession,
        project_key: str,
        client_key: str,
    ) -> Row | None:
        stmt = sa.select(brain_sessions).where(
            brain_sessions.c.project_key == project_key,
            brain_sessions.c.client_key == client_key,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _load_focus(
        self,
        session: AsyncSession,
        project_key: str,
        *,
        for_update: bool = False,
    ) -> Row | None:
        stmt = sa.select(
            project_contexts.c.current_focus,
            project_contexts.c.focus_revision,
        ).where(project_contexts.c.project_key == project_key)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _count_open(self, session: AsyncSession, project_key: str) -> int:
        """Count the sessions the EXPLICIT CYCLE governs — never the tracers.

        This number is returned on every `start`/`resume`/`end` as a measure of
        WORK concurrency. Measured on 2026-08-25 (ticket 92fe7f0f): it showed 36
        when real human concurrency was 2, `agent` tracers never dying with
        their transport. Counting them here passes them off as something they
        are not; they stay visible through `list` and are counted separately by
        the sweep. `nature IS NULL` (pre-046) is still counted: those sessions
        are human by construction.
        """
        stmt = (
            sa.select(sa.func.count())
            .select_from(brain_sessions)
            .where(
                brain_sessions.c.project_key == project_key,
                brain_sessions.c.status == "open",
                sa.or_(
                    brain_sessions.c.nature.is_(None),
                    brain_sessions.c.nature != "agent",
                ),
            )
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _validate_captures(
        self,
        session: AsyncSession,
        brain_session: BrainSession,
        capture_ids: Sequence[UUID],
    ) -> dict[UUID, str]:
        found: dict[UUID, str] = {}
        ambiguous: set[UUID] = set()
        for table, knowledge_type in CAPTURE_TABLES:
            stmt = (
                sa.select(table.c.id)
                .where(
                    table.c.id.in_(capture_ids),
                    table.c.project_key == brain_session.project_key,
                    table.c.created_at >= brain_session.started_at,
                )
                .with_for_update(read=True, key_share=True)
            )
            for knowledge_id in (await session.execute(stmt)).scalars().all():
                if knowledge_id in found:
                    ambiguous.add(knowledge_id)
                found[knowledge_id] = knowledge_type
        invalid = set(capture_ids) - set(found)
        if invalid or ambiguous:
            rejected = sorted(str(item) for item in invalid | ambiguous)
            raise BrainSessionInputError(
                "Captured knowledge must exist in the same project and have been "
                "created during the session with an unambiguous type; invalid ids: "
                + ", ".join(rejected)
            )
        return found

    async def _load_artifact_rows(
        self,
        session: AsyncSession,
        knowledge_ids: Sequence[UUID],
    ) -> builtins.list[Row]:
        if not knowledge_ids:
            return []
        stmt = sa.select(brain_session_artifacts).where(
            brain_session_artifacts.c.knowledge_id.in_(knowledge_ids)
        )
        return [dict(row) for row in (await session.execute(stmt)).mappings().all()]

    async def _tracer_held_ids(
        self,
        session: AsyncSession,
        existing: Sequence[Row],
        session_id: UUID,
    ) -> frozenset[UUID]:
        """Among these rows, those an `agent` tracer holds — and only those.

        The bound is the holder's NATURE, never its age nor its status: a swept
        tracer is still the server. A non-`agent` holder is never taken over
        here, whatever UUID is named.
        """
        foreign = {
            artifact["session_id"] for artifact in existing if artifact["session_id"] != session_id
        }
        if not foreign:
            return frozenset()
        tracers = set(
            (
                await session.execute(
                    sa.select(brain_sessions.c.id).where(
                        brain_sessions.c.id.in_(foreign),
                        brain_sessions.c.nature == "agent",
                    )
                )
            )
            .scalars()
            .all()
        )
        return frozenset(
            artifact["knowledge_id"] for artifact in existing if artifact["session_id"] in tracers
        )

    async def _load_session_artifact_ids(
        self,
        session: AsyncSession,
        session_id: UUID,
    ) -> builtins.list[UUID]:
        stmt = (
            sa.select(brain_session_artifacts.c.knowledge_id)
            .where(brain_session_artifacts.c.session_id == session_id)
            .order_by(brain_session_artifacts.c.knowledge_id)
        )
        rows = (await session.execute(stmt)).mappings().all()
        return sorted((row["knowledge_id"] for row in rows), key=str)

    async def _load_artifact_ids_by_session(
        self,
        session: AsyncSession,
        session_ids: Sequence[UUID],
    ) -> dict[UUID, builtins.list[UUID]]:
        if not session_ids:
            return {}
        stmt = (
            sa.select(
                brain_session_artifacts.c.session_id,
                brain_session_artifacts.c.knowledge_id,
            )
            .where(brain_session_artifacts.c.session_id.in_(session_ids))
            .order_by(
                brain_session_artifacts.c.session_id,
                brain_session_artifacts.c.knowledge_id,
            )
        )
        grouped: dict[UUID, builtins.list[UUID]] = {session_id: [] for session_id in session_ids}
        for row in (await session.execute(stmt)).mappings().all():
            grouped[row["session_id"]].append(row["knowledge_id"])
        return grouped

    async def _apply_focus_if_current(
        self,
        session: AsyncSession,
        current: Row,
        project_key: str,
        next_focus: str,
        expected_revision: int,
    ) -> tuple[Row, BrainSessionFocusOutcome]:
        if current["focus_revision"] != expected_revision:
            return current, BrainSessionFocusOutcome.CONFLICT

        stmt = (
            project_contexts.update()
            .where(project_contexts.c.project_key == project_key)
            .values(
                current_focus=next_focus,
                focus_revision=expected_revision + 1,
                # A session close rewrites the whole blob. Re-posting the
                # previous prose verbatim is the copy-forward this column
                # exists to expose, so it must not reset the age.
                focus_updated_at=focus_stamp(next_focus),
                updated_at=datetime.now(UTC),
            )
            .returning(
                project_contexts.c.current_focus,
                project_contexts.c.focus_revision,
            )
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise BrainSessionStateError(f"Project {project_key!r} focus could not be updated")
        # Only the APPLIED branch reaches here — a `conflict` returned above,
        # having written nothing. Re-posting the previous prose verbatim still
        # records, at its new revision: the CAS sets `expected + 1` without
        # comparing the text, and a copy-forward is exactly what this trail
        # exists to make visible.
        await record_focus_history(
            session,
            project_key=project_key,
            focus_revision=int(row["focus_revision"]),
            focus=row["current_focus"],
            source="session_end",
        )
        return dict(row), BrainSessionFocusOutcome.APPLIED

    async def _mark_ended(
        self,
        session: AsyncSession,
        session_id: UUID,
        summary: str,
        next_focus: str,
        capture_ids: Sequence[UUID],
        nothing_reason: str | None,
        *,
        expected_focus_revision: int,
        focus_outcome: BrainSessionFocusOutcome,
        focus_at_end: str | None,
        focus_revision_at_end: int,
    ) -> BrainSession:
        now = datetime.now(UTC)
        stmt = (
            brain_sessions.update()
            .where(brain_sessions.c.id == session_id)
            .values(
                status="ended",
                summary=summary,
                next_focus=next_focus,
                captured_knowledge_ids=list(capture_ids),
                nothing_to_capture_reason=nothing_reason,
                end_expected_focus_revision=expected_focus_revision,
                focus_outcome=focus_outcome.value,
                focus_at_end=focus_at_end,
                focus_revision_at_end=focus_revision_at_end,
                ended_at=now,
                updated_at=now,
            )
            .returning(brain_sessions)
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise BrainSessionStateError(f"Session {session_id} could not be ended")
        return self._to_model(row)

    async def _replay_end(
        self,
        session: AsyncSession,
        model: BrainSession,
        summary: str,
        next_focus: str,
        expected_focus_revision: int,
        nothing_reason: str | None,
    ) -> BrainSessionEndResult:
        exact = (
            model.status == "ended"
            and model.summary == summary
            and model.next_focus == next_focus
            and model.nothing_to_capture_reason == nothing_reason
            and (
                model.end_expected_focus_revision is None
                or model.end_expected_focus_revision == expected_focus_revision
            )
        )
        if not exact:
            raise BrainSessionTerminalConflictError(
                f"Session {model.id} already has a different terminal payload"
            )

        focus = await self._load_focus(session, model.project_key)
        if focus is None:
            raise BrainSessionNotFoundError(f"Project {model.project_key!r} was not found")
        if model.focus_outcome is None:
            raise BrainSessionStateError(f"Session {model.id} has no persisted focus outcome")
        remaining = await self._count_open(session, model.project_key)
        unattributed = await self._count_unattributed_in_window(
            session,
            model.project_key,
            model.started_at,
            model.ended_at or model.started_at,
        )
        return BrainSessionEndResult(
            session=model,
            replayed=True,
            remaining_open_session_count=remaining,
            unattributed_in_window=unattributed,
            current_focus=focus["current_focus"],
            current_focus_revision=focus["focus_revision"],
            focus_outcome=model.focus_outcome,
            focus_at_end=model.focus_at_end,
            focus_revision_at_end=model.focus_revision_at_end,
        )

    @staticmethod
    def _to_model(
        row: Any,
        *,
        now: datetime | None = None,
        attributed_ids: Sequence[UUID] | None = None,
    ) -> BrainSession:
        payload = dict(row)
        heartbeat = payload.get("last_heartbeat_at") or payload.get("updated_at")
        reference = now or datetime.now(UTC)
        payload["is_stale"] = bool(
            payload.get("status") == "open"
            and heartbeat is not None
            and heartbeat <= reference - SESSION_STALE_AFTER
        )
        payload["attributed_knowledge_ids"] = sorted(
            attributed_ids
            if attributed_ids is not None
            else payload.get("captured_knowledge_ids") or [],
            key=str,
        )
        return BrainSession.model_validate(payload)

    @staticmethod
    def _assert_identity(model: BrainSession, expected_client_key: str) -> None:
        normalized = expected_client_key.strip()
        if not normalized or not compare_digest(
            model.client_key.encode("utf-8"),
            normalized.encode("utf-8"),
        ):
            raise BrainSessionIdentityConflictError("session_id does not match expected_client_key")

    @staticmethod
    def _owned_capture_ids(
        existing: Sequence[Row],
        session_id: UUID,
        reclaimable: frozenset[UUID] = frozenset(),
    ) -> set[UUID]:
        """What this session already holds — and what it may take back.

        `reclaimable` contains ONLY artifacts parked in an `agent` tracer, that
        is, with the server. A conflict with another HUMAN stays an error:
        ledger exclusivity exists for that.
        """
        conflicts = [
            artifact
            for artifact in existing
            if artifact["session_id"] != session_id and artifact["knowledge_id"] not in reclaimable
        ]
        if conflicts:
            conflict_ids = sorted(str(item["knowledge_id"]) for item in conflicts)
            raise BrainSessionCaptureConflictError(
                "Knowledge is already attributed to another session: " + ", ".join(conflict_ids)
            )
        return {artifact["knowledge_id"] for artifact in existing}

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _validate_end_input(
        summary: str,
        next_focus: str,
        expected_revision: int,
    ) -> None:
        if not summary or not next_focus:
            raise BrainSessionInputError("summary and next_focus must not be blank")
        if expected_revision < 0:
            raise BrainSessionInputError("expected_focus_revision must be non-negative")

    @staticmethod
    def _validate_capture_ids(
        capture_ids: Sequence[UUID],
        *,
        require_nonempty: bool,
    ) -> None:
        if require_nonempty and not capture_ids:
            raise BrainSessionInputError("knowledge_ids must not be empty")
        if len(capture_ids) > MAX_CAPTURED_KNOWLEDGE_IDS:
            raise BrainSessionInputError(
                f"captured_knowledge_ids must contain at most {MAX_CAPTURED_KNOWLEDGE_IDS} items"
            )
        if len(set(capture_ids)) != len(capture_ids):
            raise BrainSessionInputError("captured_knowledge_ids must be unique")
