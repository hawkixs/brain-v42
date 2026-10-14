"""Persist lifecycle evidence without granting the pipeline history-edit rights.

Methods accept an AsyncConnection and never commit: the caller checkpoints each
run phase, groups event/cache/counters in one entry transaction, and checks the
UTC apply window immediately before committing. Savepoints keep multi-row
writes atomic even if the caller catches an error and continues its transaction.
The whole-run session lock alone owns a dedicated connection across commits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.dml import Insert, Update

from brain_v42.db import tables as t

# Separate namespace from delivery and graph locks; one lifecycle run globally.
LIFECYCLE_RUN_LOCK = 7_311_066_001


class RunStatus(StrEnum):
    CREATED = "created"
    MANIFEST_FROZEN = "manifest_frozen"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RESULTS_STORED = "results_stored"
    EVALUATED = "evaluated"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    INVALIDATED = "invalidated"


_NEXT = {
    RunStatus.CREATED: RunStatus.MANIFEST_FROZEN,
    RunStatus.MANIFEST_FROZEN: RunStatus.SUBMITTING,
    RunStatus.SUBMITTING: RunStatus.SUBMITTED,
    RunStatus.SUBMITTED: RunStatus.RESULTS_STORED,
    RunStatus.RESULTS_STORED: RunStatus.EVALUATED,
    RunStatus.EVALUATED: RunStatus.APPLYING,
    RunStatus.APPLYING: RunStatus.APPLIED,
}


def transition_allowed(
    source: RunStatus | str, target: RunStatus | str, *, invalidation: bool = False
) -> bool:
    """Keep the single edge table independent of SQL and stale-state detection."""
    try:
        source, target = RunStatus(source), RunStatus(target)
    except ValueError:
        return False
    if target == RunStatus.INVALIDATED:
        return invalidation and source in {RunStatus.APPLIED, RunStatus.FAILED}
    return _NEXT.get(source) == target or (source in _NEXT and target == RunStatus.FAILED)


class InvalidRunTransition(ValueError):
    """A requested edge is outside the persisted run protocol."""


class StaleRunState(RuntimeError):
    """Another checkpoint has already consumed the expected from-state."""


class StaleAtApply(RuntimeError):
    """The entry or cache changed; leave this entry for a later evaluation."""


class InvalidApply(RuntimeError):
    """A new event requires a complete evaluation and an eligible apply run."""


class ApplyDeferred(RuntimeError):
    """Lock contention or query cancellation requires retrying this entry later.

    Roll back the caller's transaction (or enclosing savepoint) before continuing:
    PostgreSQL aborts that scope on either error. The original DBAPI error is
    retained as the cause. SQLSTATE 57014 covers both deadlines and manual cancels.
    """

    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        super().__init__("lock_timeout" if sqlstate == "55P03" else "query_canceled")


@asynccontextmanager
async def _defer_apply_timeouts() -> AsyncIterator[None]:
    """Translate contention and cancellation without hiding other database failures."""
    try:
        yield
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate in {"55P03", "57014"}:
            raise ApplyDeferred(sqlstate) from exc
        raise


class ApplyWindowClosed(RuntimeError):
    """Nightly writers own the interval from 00:50Z through 03:59:59Z."""


def apply_window_open(utc_time: time) -> bool:
    """Require a naive UTC wall-clock time; the opening boundary includes 04:00Z."""
    if utc_time.tzinfo is not None:
        raise ValueError("Apply window requires a naive UTC time")
    return not time(0, 50) <= utc_time < time(4)


async def check_apply_window(conn: AsyncConnection) -> None:
    """Sample the server clock, not the transaction's possibly old start time."""
    utc_time = await conn.scalar(sa.text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::time"))
    if not apply_window_open(utc_time):
        raise ApplyWindowClosed("apply_window_closed")


@asynccontextmanager
async def whole_run_lock(engine: AsyncEngine) -> AsyncIterator[bool]:
    """Hold a session lock without an idle transaction or a retained xmin horizon."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = bool(
                await conn.scalar(sa.select(sa.func.pg_try_advisory_lock(LIFECYCLE_RUN_LOCK)))
            )
            yield acquired
            if acquired:
                # An aborted transaction must not prevent session-level unlock.
                await conn.rollback()
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(LIFECYCLE_RUN_LOCK)))
                await conn.commit()
        except BaseException:
            # A failed acquire may have granted the lock before losing its reply.
            # Never return a possibly locked physical session to the pool.
            await conn.invalidate()
            raise


_TABLES = {
    "decisions": t.decisions,
    "learnings": t.learnings,
    "snippets": t.snippets,
    "runbooks": t.runbooks,
    "adrs": t.adrs,
}
_GRAPH_TYPES = {
    "decisions": "decision",
    "learnings": "learning",
    "snippets": "snippet",
    "runbooks": "runbook",
    "adrs": "adr",
}
_TEXT_FIELDS = {
    "decisions": ("title", "description", "reasoning", "alternatives", "consequences"),
    "learnings": ("topic", "insight", "source"),
    "snippets": (
        "title",
        "intention",
        "code",
        "language",
        "dependencies",
        "usage_example",
        "gotchas",
    ),
    "runbooks": (
        "title",
        "description",
        "trigger",
        "prerequisites",
        "steps",
        "rollback_steps",
        "estimated_duration",
    ),
    "adrs": ("title", "context", "decision", "consequences", "alternatives_considered"),
}
_REQUEST_STATUSES = frozenset({"succeeded", "errored", "expired", "canceled", "missing", "invalid"})


@dataclass(frozen=True)
class LockedApplyInputs:
    """Return current content and structure so callers can compare their snapshot.

    Recompute the digest with the same text serialization used at extraction,
    and compare the structural inputs retained by the caller, before appending.
    Migration 066 stores no structural snapshot in an evaluation.
    content_updated_at is COALESCE(content_updated_at, created_at): for legacy
    unmeasured rows, creation is a lower bound, not an invented edit. Read-side
    cache comparisons must use the same coalesced value; later edits advance it.
    """

    entity_type: str
    entity_id: UUID
    entry: dict[str, Any]
    cache: dict[str, Any] | None

    @property
    def revision(self) -> int:
        return 0 if self.cache is None else int(self.cache["revision"])


def _identity(table: sa.Table, entity_type: str, entity_id: UUID) -> sa.ColumnElement[bool]:
    return sa.and_(table.c.entity_type == entity_type, table.c.entity_id == entity_id)


def _entry_columns(entity_type: str) -> list[str]:
    """Exclude mutable access metadata and vector arrays from snapshot comparison."""
    columns = list(_TEXT_FIELDS[entity_type]) + [
        "id",
        "project_key",
        "created_at",
        "content_updated_at",
        "merged_into",
        "freshness_status",
        "freshness_status_updated_at",
    ]
    if entity_type in {"decisions", "adrs"}:
        columns.extend(["status", "superseded_by"])
    return columns


async def _row(conn: AsyncConnection, statement: sa.Executable) -> dict[str, Any] | None:
    row = (await conn.execute(statement)).mappings().one_or_none()
    return None if row is None else dict(row)


class PgKnowledgeLifecycleRepo:
    """Use caller-owned connections so persistence cannot commit partial phases."""

    async def create_run(
        self, conn: AsyncConnection, run_id: str, mode: str, scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Use an empty digest only until the NOT NULL manifest column is frozen."""
        row = await _row(
            conn,
            t.knowledge_lifecycle_runs.insert()
            .values(
                run_id=run_id,
                mode=mode,
                scope=dict(scope),
                status=RunStatus.CREATED,
                manifest_digest="",
            )
            .returning(t.knowledge_lifecycle_runs),
        )
        assert row is not None
        return row

    async def read_run(self, conn: AsyncConnection, run_id: str) -> dict[str, Any]:
        row = await _row(
            conn,
            sa.select(t.knowledge_lifecycle_runs).where(
                t.knowledge_lifecycle_runs.c.run_id == run_id
            ),
        )
        if row is None:
            raise KeyError(run_id)
        return row

    async def transition_run(
        self,
        conn: AsyncConnection,
        run_id: str,
        from_status: RunStatus | str,
        to_status: RunStatus | str,
        *,
        reason: str | None = None,
        manifest_digest: str | None = None,
    ) -> dict[str, Any]:
        """Persist the edge before side effects; invalidation has a separate method.

        The frozen schema has no failure_reason column. A failed run stores that
        reason at counts.failure_reason, alongside its report counters.
        """
        if not transition_allowed(from_status, to_status):
            raise InvalidRunTransition(f"{from_status} -> {to_status}")
        values: dict[str, Any] = {"status": to_status}
        if to_status == RunStatus.MANIFEST_FROZEN:
            if not manifest_digest or not manifest_digest.strip():
                raise ValueError("A frozen manifest needs its digest")
            values["manifest_digest"] = manifest_digest
        elif manifest_digest is not None:
            raise ValueError("The manifest digest can only be set when freezing")
        if to_status == RunStatus.FAILED:
            if not reason or not reason.strip():
                raise ValueError("A failed run needs a reason")
            values["counts"] = t.knowledge_lifecycle_runs.c.counts.op("||")(
                sa.cast({"failure_reason": reason}, JSONB)
            )
        if to_status in {RunStatus.FAILED, RunStatus.APPLIED}:
            values["ended_at"] = sa.func.clock_timestamp()
        row = await _row(
            conn,
            t.knowledge_lifecycle_runs.update()
            .where(
                t.knowledge_lifecycle_runs.c.run_id == run_id,
                t.knowledge_lifecycle_runs.c.status == from_status,
            )
            .values(**values)
            .returning(t.knowledge_lifecycle_runs),
        )
        if row is None:
            raise StaleRunState(run_id)
        return row

    async def record_run(
        self,
        conn: AsyncConnection,
        run_id: str,
        *,
        batch_id: str | None = None,
        usage: Mapping[str, Any] | None = None,
        real_cost: Decimal | None = None,
        counts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge metadata atomically so checkpoints do not lose concurrent counters."""
        table = t.knowledge_lifecycle_runs
        values: dict[str, Any] = {}
        if batch_id is not None:
            batch = sa.cast([batch_id], JSONB)
            values["batch_ids"] = sa.case(
                (table.c.batch_ids.contains([batch_id]), table.c.batch_ids),
                else_=table.c.batch_ids.op("||")(batch),
            )
        if usage is not None:
            values["usage"] = table.c.usage.op("||")(sa.cast(dict(usage), JSONB))
        if counts is not None:
            values["counts"] = table.c.counts.op("||")(sa.cast(dict(counts), JSONB))
        if real_cost is not None:
            values["real_cost"] = real_cost
        if not values:
            return await self.read_run(conn, run_id)
        row = await _row(
            conn, table.update().where(table.c.run_id == run_id).values(**values).returning(table)
        )
        if row is None:
            raise KeyError(run_id)
        return row

    async def set_count_key(
        self,
        conn: AsyncConnection,
        run_id: str,
        section: str,
        key: str,
        value: Any,
    ) -> None:
        """Update one nested counter without sending its growing siblings back."""
        table = t.knowledge_lifecycle_runs
        current = sa.func.coalesce(table.c.counts[section], sa.cast({}, JSONB))
        nested = (
            current.op("-")(key)
            if value is None
            else current.op("||")(sa.cast({key: value}, JSONB))
        )
        updated = await conn.scalar(
            table.update()
            .where(table.c.run_id == run_id)
            .values(
                counts=sa.func.jsonb_set(
                    table.c.counts, sa.cast([section], sa.ARRAY(sa.Text)), nested
                )
            )
            .returning(table.c.run_id)
        )
        if updated is None:
            raise KeyError(run_id)

    async def insert_requests(
        self, conn: AsyncConnection, run_id: str, requests: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """Freeze attribution once; later resumes may only replay identical requests."""
        table = t.knowledge_lifecycle_requests
        async with conn.begin_nested():
            run = await _row(
                conn,
                sa.select(t.knowledge_lifecycle_runs)
                .where(t.knowledge_lifecycle_runs.c.run_id == run_id)
                .with_for_update(),
            )
            if run is None:
                raise KeyError(run_id)
            for custom_id, manifest_entry in requests.items():
                suffix = custom_id.removeprefix(run_id + "-")
                if (
                    not custom_id.startswith(run_id + "-")
                    or not suffix.isascii()
                    or not suffix.isdigit()
                ):
                    raise ValueError("custom_id must identify its run and sequence")
                predicate = sa.and_(table.c.run_id == run_id, table.c.custom_id == custom_id)
                existing = await _row(conn, sa.select(table).where(predicate))
                if existing is not None:
                    if existing["manifest_entry"] != dict(manifest_entry):
                        raise ValueError("A frozen request cannot change")
                    continue
                if run["status"] != RunStatus.CREATED:
                    raise InvalidRunTransition("Requests must be inserted before manifest_frozen")
                await conn.execute(
                    table.insert().values(
                        run_id=run_id, custom_id=custom_id, manifest_entry=dict(manifest_entry)
                    )
                )

    async def checkpoint_request(
        self, conn: AsyncConnection, run_id: str, custom_id: str, status: str
    ) -> bool:
        """True stores the first result; False lets resume skip an existing checkpoint."""
        if status not in _REQUEST_STATUSES:
            raise ValueError("A checkpoint needs a terminal request status")
        table = t.knowledge_lifecycle_requests
        predicate = sa.and_(table.c.run_id == run_id, table.c.custom_id == custom_id)
        stored = await conn.scalar(
            table.update()
            .where(predicate, table.c.result_stored_at.is_(None))
            .values(status=status, result_stored_at=sa.func.clock_timestamp())
            .returning(table.c.custom_id)
        )
        if stored is not None:
            return True
        if await conn.scalar(sa.select(table.c.custom_id).where(predicate)) is None:
            raise KeyError((run_id, custom_id))
        return False

    async def pending_requests(self, conn: AsyncConnection, run_id: str) -> list[dict[str, Any]]:
        table = t.knowledge_lifecycle_requests
        rows = (
            await conn.execute(
                sa.select(table)
                .where(
                    table.c.run_id == run_id,
                    table.c.result_stored_at.is_(None),
                )
                .order_by(table.c.custom_id)
            )
        ).mappings()
        return [dict(row) for row in rows]

    async def insert_extraction(
        self,
        conn: AsyncConnection,
        *,
        run_id: str,
        entity_type: str,
        entity_id: UUID,
        entry_digest: str,
        model: str,
        prompt_version: str,
        assertions: Sequence[Mapping[str, Any]],
    ) -> int:
        """Accept version-validated assertions; DO NOTHING requires no history UPDATE."""
        table = t.knowledge_extractions
        inserted = await conn.scalar(
            pg_insert(table)
            .values(
                run_id=run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                entry_digest=entry_digest,
                model=model,
                prompt_version=prompt_version,
                assertions=[dict(a) for a in assertions],
            )
            .on_conflict_do_nothing(constraint="knowledge_extractions_revision_key")
            .returning(table.c.id)
        )
        if inserted is not None:
            return int(inserted)
        existing = await self.read_extraction(
            conn, entity_type, entity_id, entry_digest, model=model, prompt_version=prompt_version
        )
        assert existing is not None
        return int(existing["id"])

    async def read_extraction(
        self,
        conn: AsyncConnection,
        entity_type: str,
        entity_id: UUID,
        entry_digest: str,
        *,
        model: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        """Match the model and prompt as well as content to avoid stale schema reuse."""
        table = t.knowledge_extractions
        return await _row(
            conn,
            sa.select(table).where(
                _identity(table, entity_type, entity_id),
                table.c.entry_digest == entry_digest,
                table.c.model == model,
                table.c.prompt_version == prompt_version,
            ),
        )

    async def insert_evaluation(
        self,
        conn: AsyncConnection,
        values: Mapping[str, Any],
        justifications: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Insert one schema-shaped evaluation and its observations, or replay both.

        Include all observations, not just decide's winning ids: their kinds
        preserve the propagation layer even when its marker is being lifted.
        """
        table = t.knowledge_evaluations
        async with conn.begin_nested():
            inserted = await _row(
                conn,
                pg_insert(table)
                .values(**dict(values))
                .on_conflict_do_nothing(constraint="knowledge_evaluations_run_entry_key")
                .returning(table),
            )
            if inserted is None:
                existing = await _row(
                    conn,
                    sa.select(table).where(
                        table.c.run_id == values["run_id"],
                        _identity(table, values["entity_type"], values["entity_id"]),
                    ),
                )
                assert existing is not None
                return existing
            for justification in justifications:
                payload = dict(justification)
                payload.update(
                    evaluation_id=inserted["id"],
                    entity_type=inserted["entity_type"],
                    entity_id=inserted["entity_id"],
                )
                await conn.execute(t.knowledge_justifications.insert().values(**payload))
            return inserted

    async def read_evaluation(self, conn: AsyncConnection, evaluation_id: int) -> dict[str, Any]:
        row = await _row(
            conn,
            sa.select(t.knowledge_evaluations).where(t.knowledge_evaluations.c.id == evaluation_id),
        )
        if row is None:
            raise KeyError(evaluation_id)
        return row

    async def evaluations_for_entry(
        self, conn: AsyncConnection, entity_type: str, entity_id: UUID
    ) -> list[dict[str, Any]]:
        """Supply candidate identities for resume and invalidation replay.

        Keep incomplete and invalidated records visible: the orchestrator must
        replay hysteresis rather than infer a state from the last event alone.
        """
        evaluations, runs = t.knowledge_evaluations, t.knowledge_lifecycle_runs
        rows = await conn.execute(
            sa.select(
                evaluations,
                runs.c.mode,
                runs.c.status.label("run_status"),
                sa.or_(
                    runs.c.invalidated_at.is_not(None), runs.c.status == RunStatus.INVALIDATED
                ).label("invalidated"),
            )
            .join(runs, runs.c.run_id == evaluations.c.run_id)
            .where(_identity(evaluations, entity_type, entity_id))
            .order_by(evaluations.c.id)
        )
        return [dict(row) for row in rows.mappings()]

    async def read_justifications(
        self, conn: AsyncConnection, evaluation_id: int
    ) -> list[dict[str, Any]]:
        table = t.knowledge_justifications
        return [
            dict(row)
            for row in (
                await conn.execute(
                    sa.select(table)
                    .where(table.c.evaluation_id == evaluation_id)
                    .order_by(table.c.id)
                )
            ).mappings()
        ]

    async def read_cache(
        self, conn: AsyncConnection, entity_type: str, entity_id: UUID
    ) -> dict[str, Any] | None:
        table = t.knowledge_lifecycle_state
        return await _row(conn, sa.select(table).where(_identity(table, entity_type, entity_id)))

    async def _entry_inputs(
        self,
        conn: AsyncConnection,
        entity_type: str,
        row: Mapping[str, Any],
        *,
        lock_successor: bool = False,
    ) -> dict[str, Any]:
        result = dict(row)
        result.update(
            entity_type=entity_type,
            text_fields={field: row[field] for field in _TEXT_FIELDS[entity_type]},
            content_updated_at=(
                row["created_at"]
                if row["content_updated_at"] is None
                else row["content_updated_at"]
            ),
            successor_exists=row.get("successor_exists", False),
            successor_created_at=row.get("successor_created_at"),
        )
        successor = row.get("superseded_by")
        if (
            successor is not None
            and entity_type in {"decisions", "adrs"}
            and "successor_exists" not in row
        ):
            entry_table = _TABLES[entity_type]
            predicate = (
                entry_table.c.id == successor
                if entity_type == "decisions"
                else sa.and_(
                    entry_table.c.number == successor,
                    entry_table.c.project_key == row["project_key"],
                )
            )
            query = sa.select(entry_table.c.created_at).where(predicate)
            if lock_successor:
                query = query.with_for_update(read=True)
            successor_row = (await conn.execute(query)).one_or_none()
            if successor_row is not None:
                result.update(successor_exists=True, successor_created_at=successor_row.created_at)
        return result

    async def lock_apply_inputs(
        self,
        conn: AsyncConnection,
        entity_type: str,
        entity_id: UUID,
        *,
        lock_timeout_ms: int = 5000,
        statement_timeout_ms: int = 30000,
        _set_timeouts: bool = True,
    ) -> LockedApplyInputs:
        """Lock entry before cache, including the absent-cache case via the entry.

        The caller compares digest and structural snapshot before appending.
        Successors are SHARE-locked so deletion cannot change structural facts
        after this read. The timeouts last only for the caller's transaction.
        Timeout overrides allow bounded waits; ApplyDeferred requires rollback
        of the caller's transaction or enclosing savepoint before continuing.
        """
        entry_table = _TABLES[entity_type]
        async with _defer_apply_timeouts():
            if _set_timeouts:
                await conn.execute(
                    sa.select(sa.func.set_config("lock_timeout", f"{lock_timeout_ms}ms", True))
                )
                await conn.execute(
                    sa.select(
                        sa.func.set_config("statement_timeout", f"{statement_timeout_ms}ms", True)
                    )
                )
            row = await _row(
                conn,
                sa.select(*(entry_table.c[name] for name in _entry_columns(entity_type)))
                .where(entry_table.c.id == entity_id)
                .with_for_update(),
            )
            if row is None:
                raise StaleAtApply("entry_missing")
            inputs = await self._entry_inputs(conn, entity_type, row, lock_successor=True)
            cache_table = t.knowledge_lifecycle_state
            cache = await _row(
                conn,
                sa.select(cache_table)
                .where(_identity(cache_table, entity_type, entity_id))
                .with_for_update(),
            )
            return LockedApplyInputs(entity_type, entity_id, inputs, cache)

    async def append_transition(
        self,
        conn: AsyncConnection,
        evaluation_id: int,
        inputs: LockedApplyInputs,
        *,
        to_state: str,
        reason: str,
        valid_from: date | None = None,
        valid_from_precision: str = "unknown",
    ) -> dict[str, Any]:
        """Replay a prior transition before checking its now-consumed revision.

        The caller decides eligibility with confirmed() and compares the locked
        text/structure to the evaluation snapshot. This method does no decide.
        Its second locked read only detects changes made by the same transaction;
        it is not a digest/structure comparison against the earlier evaluation.
        New events require mode=apply, status=applying, no invalidation and a
        complete evaluation. A persisted event remains replayable after completion.
        """
        evaluation = await self.read_evaluation(conn, evaluation_id)
        return await self._append_event(
            conn,
            evaluation,
            inputs,
            expected_revision=evaluation["expected_cache_revision"],
            kind="transition",
            to_state=to_state,
            reason=reason,
            valid_from=valid_from,
            valid_from_precision=valid_from_precision,
        )

    async def append_compensation(
        self,
        conn: AsyncConnection,
        *,
        evaluation_id: int,
        inputs: LockedApplyInputs,
        invalidated_run_id: str,
        compensates_event_id: int,
        expected_cache_revision: int,
        to_state: str,
        reason: str,
        valid_from: date | None = None,
        valid_from_precision: str = "unknown",
    ) -> dict[str, Any]:
        """Use the recomputation's current revision, not the old evaluation's CAS.

        Evidence must be complete and come from an apply-mode run. Historical
        evidence may belong to a completed run, so applying status is not required.
        """
        evaluation = await self.read_evaluation(conn, evaluation_id)
        return await self._append_event(
            conn,
            evaluation,
            inputs,
            expected_revision=expected_cache_revision,
            kind="compensation",
            to_state=to_state,
            reason=reason,
            valid_from=valid_from,
            valid_from_precision=valid_from_precision,
            invalidated_run_id=invalidated_run_id,
            compensates_event_id=compensates_event_id,
        )

    async def _append_event(
        self,
        conn: AsyncConnection,
        evaluation: Mapping[str, Any],
        inputs: LockedApplyInputs,
        *,
        expected_revision: int,
        kind: str,
        to_state: str,
        reason: str,
        valid_from: date | None,
        valid_from_precision: str,
        invalidated_run_id: str | None = None,
        compensates_event_id: int | None = None,
    ) -> dict[str, Any]:
        events, cache = t.knowledge_lifecycle_events, t.knowledge_lifecycle_state
        entity_type, entity_id = inputs.entity_type, inputs.entity_id
        if (evaluation["entity_type"], evaluation["entity_id"]) != (entity_type, entity_id):
            raise StaleAtApply("evaluation_entry_mismatch")
        predicate = (
            sa.and_(events.c.evaluation_id == evaluation["id"], events.c.kind == "transition")
            if kind == "transition"
            else sa.and_(
                events.c.invalidated_run_id == invalidated_run_id,
                _identity(events, entity_type, entity_id),
            )
        )
        replay = await _row(conn, sa.select(events).where(predicate))
        if replay is not None:
            return replay
        async with _defer_apply_timeouts(), conn.begin_nested():
            runs = t.knowledge_lifecycle_runs
            run = await _row(
                conn,
                sa.select(runs).where(runs.c.run_id == evaluation["run_id"]).with_for_update(),
            )
            if kind == "transition":
                if (
                    run is None
                    or run["mode"] != "apply"
                    or run["status"] != RunStatus.APPLYING
                    or run["invalidated_at"] is not None
                    or not evaluation["complete"]
                ):
                    raise InvalidApply("transition_requires_complete_evaluation_and_applying_run")
            elif run is None or run["mode"] != "apply" or not evaluation["complete"]:
                raise InvalidApply("compensation_requires_complete_evaluation_and_apply_run")
            # The inner re-lock must retain the caller's transaction-local limits.
            current = await self.lock_apply_inputs(
                conn, entity_type, entity_id, _set_timeouts=False
            )
            replay = await _row(conn, sa.select(events).where(predicate))
            if replay is not None:
                return replay
            if current.revision != expected_revision or current.entry != inputs.entry:
                raise StaleAtApply("stale_at_apply")
            content_at = current.entry["content_updated_at"]
            if kind == "compensation":
                run = await self.read_run(conn, str(invalidated_run_id))
                if run["status"] != RunStatus.INVALIDATED:
                    raise InvalidRunTransition("Compensation requires an invalidated run")
            event = await _row(
                conn,
                pg_insert(events)
                .values(
                    evaluation_id=evaluation["id"],
                    entity_type=entity_type,
                    entity_id=entity_id,
                    kind=kind,
                    from_state=None if current.cache is None else current.cache["state"],
                    to_state=to_state,
                    reason=reason,
                    valid_from=valid_from,
                    valid_from_precision=valid_from_precision,
                    invalidated_run_id=invalidated_run_id,
                    compensates_event_id=compensates_event_id,
                )
                .on_conflict_do_nothing()
                .returning(events),
            )
            if event is None:
                replay = await _row(conn, sa.select(events).where(predicate))
                assert replay is not None
                return replay
            values = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "state": to_state,
                "reason": reason,
                "since": event["recorded_at"],
                "entry_content_updated_at": content_at,
                "event_id": event["id"],
                "revision": expected_revision + 1,
            }
            statement: Insert | Update
            if current.cache is None:
                statement = pg_insert(cache).values(**values).on_conflict_do_nothing()
            else:
                statement = (
                    cache.update()
                    .where(
                        _identity(cache, entity_type, entity_id),
                        cache.c.revision == expected_revision,
                    )
                    .values(**values)
                )
            if await conn.scalar(statement.returning(cache.c.revision)) is None:
                raise StaleAtApply("stale_at_apply")
            run_id = invalidated_run_id if kind == "compensation" else evaluation["run_id"]
            counter = "compensation_events" if kind == "compensation" else "applied_events"
            runs = t.knowledge_lifecycle_runs
            await conn.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(
                    counts=runs.c.counts.op("||")(
                        sa.func.jsonb_build_object(
                            counter,
                            sa.func.coalesce(runs.c.counts[counter].astext.cast(sa.BigInteger), 0)
                            + 1,
                        )
                    )
                )
            )
            return event

    async def invalidate_run(self, conn: AsyncConnection, run_id: str, reason: str) -> bool:
        """Mark operational metadata once; never edit any evaluation or event."""
        if not reason.strip():
            raise ValueError("Invalidation needs a reason")
        table = t.knowledge_lifecycle_runs
        row = await _row(conn, sa.select(table).where(table.c.run_id == run_id).with_for_update())
        if row is None:
            raise KeyError(run_id)
        if row["status"] == RunStatus.INVALIDATED:
            return False
        if not transition_allowed(row["status"], RunStatus.INVALIDATED, invalidation=True):
            raise InvalidRunTransition(f"{row['status']} -> invalidated")
        await conn.execute(
            table.update()
            .where(table.c.run_id == run_id, table.c.status == row["status"])
            .values(
                status=RunStatus.INVALIDATED,
                invalidated_at=sa.func.clock_timestamp(),
                invalidation_reason=reason,
            )
        )
        return True

    async def entries_for_run(self, conn: AsyncConnection, run_id: str) -> list[tuple[str, UUID]]:
        table = t.knowledge_evaluations
        return [
            (row.entity_type, row.entity_id)
            for row in (
                await conn.execute(
                    sa.select(table.c.entity_type, table.c.entity_id)
                    .where(table.c.run_id == run_id)
                    .order_by(table.c.entity_type, table.c.entity_id)
                )
            ).all()
        ]

    async def events_for_entry(
        self, conn: AsyncConnection, entity_type: str, entity_id: UUID
    ) -> list[dict[str, Any]]:
        table = t.knowledge_lifecycle_events
        return [
            dict(row)
            for row in (
                await conn.execute(
                    sa.select(table)
                    .where(_identity(table, entity_type, entity_id))
                    .order_by(table.c.id)
                )
            ).mappings()
        ]

    async def replay_cache(self, conn: AsyncConnection) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Rebuild lifecycle state in event-id order, retaining compensations.

        Project state/reason/since/event_id/revision without filtering invalidated
        runs: compensations, not erasure, repair the cache. The historical entry
        content timestamp cannot be reconstructed: 066 events do not store it.
        This read-only projection deliberately does not invent that metadata.
        """
        table = t.knowledge_lifecycle_events
        result: dict[tuple[str, UUID], dict[str, Any]] = {}
        for row in (await conn.execute(sa.select(table).order_by(table.c.id))).mappings():
            key = (row["entity_type"], row["entity_id"])
            revision = result.get(key, {}).get("revision", 0) + 1
            result[key] = {
                "state": row["to_state"],
                "reason": row["reason"],
                "since": row["recorded_at"],
                "event_id": row["id"],
                "revision": revision,
            }
        return result

    async def cache_mismatches(self, conn: AsyncConnection) -> list[tuple[str, UUID]]:
        """Compare one database snapshot so concurrent commits cannot fake drift."""
        events, cache = t.knowledge_lifecycle_events, t.knowledge_lifecycle_state
        ranked = sa.select(
            events.c.entity_type,
            events.c.entity_id,
            events.c.to_state.label("state"),
            events.c.reason,
            events.c.recorded_at.label("since"),
            events.c.id.label("event_id"),
            sa.func.count()
            .over(partition_by=(events.c.entity_type, events.c.entity_id))
            .label("revision"),
            sa.func.row_number()
            .over(
                partition_by=(events.c.entity_type, events.c.entity_id), order_by=events.c.id.desc()
            )
            .label("position"),
        ).subquery()
        latest = sa.select(ranked).where(ranked.c.position == 1).subquery()
        joined = latest.join(
            cache,
            sa.and_(
                latest.c.entity_type == cache.c.entity_type, latest.c.entity_id == cache.c.entity_id
            ),
            full=True,
        )
        mismatch = sa.or_(
            latest.c.event_id.is_(None),
            cache.c.event_id.is_(None),
            *(
                latest.c[field].is_distinct_from(cache.c[field])
                for field in ("state", "reason", "since", "event_id", "revision")
            ),
        )
        entity_type = sa.func.coalesce(latest.c.entity_type, cache.c.entity_type)
        entity_id = sa.func.coalesce(latest.c.entity_id, cache.c.entity_id)
        rows = await conn.execute(
            sa.select(entity_type, entity_id)
            .select_from(joined)
            .where(mismatch)
            .order_by(entity_type, entity_id)
        )
        return [(row[0], row[1]) for row in rows]

    async def list_entries(
        self, conn: AsyncConnection, project_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Supply structural rows and digest content for all projects or one key.

        text_fields includes decisions' title/description/reasoning/alternatives/
        consequences; learnings' topic/insight/source; snippets' title/intention/
        code/language/dependencies/usage_example/gotchas; runbooks' title/
        description/trigger/prerequisites/steps/rollback_steps/estimated_duration;
        ADRs' title/context/decision/consequences/alternatives_considered.
        These substantive fields include claims in code, arrays and procedures;
        access counters, embeddings, tags and lifecycle metadata are excluded.
        The caller serializes this mapping consistently for EntryText; this
        repository returns full content and never truncates or computes a digest.
        ADR successors resolve by number AND project; decisions resolve by UUID.
        One query per table joins successors, independent of corpus size. The
        returned content_updated_at uses the same creation lower bound as apply;
        read-side cache comparisons must compare this coalesced value.
        """
        result = []
        for entity_type, table in _TABLES.items():
            columns = _entry_columns(entity_type)
            query = sa.select(*(table.c[name] for name in columns)).order_by(table.c.id)
            if entity_type in {"decisions", "adrs"}:
                successor = table.alias("successor")
                predicate = (
                    successor.c.id == table.c.superseded_by
                    if entity_type == "decisions"
                    else sa.and_(
                        successor.c.number == table.c.superseded_by,
                        successor.c.project_key == table.c.project_key,
                    )
                )
                query = query.add_columns(
                    successor.c.id.is_not(None).label("successor_exists"),
                    successor.c.created_at.label("successor_created_at"),
                ).select_from(table.outerjoin(successor, predicate))
            if project_key is not None:
                query = query.where(table.c.project_key == project_key)
            for row in (await conn.execute(query)).mappings():
                result.append(await self._entry_inputs(conn, entity_type, dict(row)))
        return result

    async def compensations_for_run(
        self, conn: AsyncConnection, run_id: str
    ) -> list[dict[str, Any]]:
        """Attribute compensations to the invalidation, including out-of-scope entries."""
        events = t.knowledge_lifecycle_events
        rows = await conn.execute(
            sa.select(events).where(events.c.invalidated_run_id == run_id).order_by(events.c.id)
        )
        return [dict(row) for row in rows.mappings()]

    async def direct_dependents(
        self, conn: AsyncConnection, entity_type: str, entity_id: UUID
    ) -> list[tuple[str, UUID]]:
        """Read one hop from dependent SOURCE to relied-on TARGET, never recurse.

        pg_graph_ledger.canonicalize_relation_endpoints preserves directed
        endpoints (only RELATED_TO is symmetric). Legacy graph import and
        graph_helpers.graph_upsert_entity's related_to writes preserve the
        relying entry as source. Graph ids
        belong to brain_entities; source_uuid maps back to the knowledge row.
        Deleted relations/endpoints and non-knowledge sources are excluded.
        """
        source, target = t.brain_entities.alias("dependent"), t.brain_entities.alias("parent")
        relations = t.entity_relations
        graph_type = _GRAPH_TYPES[entity_type]
        rows = await conn.execute(
            sa.select(source.c.entity_type, source.c.source_uuid)
            .select_from(
                relations.join(source, source.c.id == relations.c.source_entity_id).join(
                    target, target.c.id == relations.c.target_entity_id
                )
            )
            .where(
                target.c.entity_type == graph_type,
                target.c.source_uuid == entity_id,
                source.c.entity_type.in_(tuple(_GRAPH_TYPES.values())),
                source.c.source_uuid.is_not(None),
                relations.c.relation_type.in_(("MOTIVATED_BY", "IMPLEMENTS", "DEPENDS_ON")),
                relations.c.lifecycle == "active",
                relations.c.deleted_at.is_(None),
                source.c.lifecycle != "deleted",
                source.c.deleted_at.is_(None),
                target.c.lifecycle != "deleted",
                target.c.deleted_at.is_(None),
            )
            .distinct()
            .order_by(source.c.entity_type, source.c.source_uuid)
        )
        reverse = {value: key for key, value in _GRAPH_TYPES.items()}
        return [(reverse[row[0]], row[1]) for row in rows]
