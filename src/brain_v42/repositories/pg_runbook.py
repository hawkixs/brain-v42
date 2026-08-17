"""PostgreSQL repository for Runbook entities.

Subclasses BasePgRepository and adds:
- Pydantic model conversion (RunbookCreate / RunbookUpdate -> dict, dict -> Runbook)
- record_execution: atomic increment execution_count + update timestamps/status
- get_by_title: unique lookup by (title, project_key) via base.find_one()
- list_by_project: paginated list for a given project_key
- search_fts: delegates to base class with optional project_key filter
- vector_search: delegates to base class, returns (Runbook, similarity) tuples
- create_with_promotion(): local transaction; stamp + audit via record_promotion()
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from brain_v42.db.tables import runbooks
from brain_v42.models.runbook import (
    ExecutionStatus,
    Runbook,
    RunbookCreate,
    RunbookStep,
    RunbookUpdate,
)
from brain_v42.repositories.pg_base import BasePgRepository, Row
from brain_v42.repositories.promotion import (
    SourceLearningNotFound,
    lock_runbook_promotion_target,
    lock_source_learning,
    record_promotion,
)

logger = structlog.get_logger(__name__)


def _row_to_runbook(row: Row) -> Runbook:
    """Convert a raw DB dict to a Runbook Pydantic model.

    Handles JSON-stored steps/rollback_steps (list[dict] -> list[RunbookStep]).
    """
    data = dict(row)

    # steps and rollback_steps are stored as JSONB (list[dict])
    raw_steps = data.get("steps") or []
    raw_rollback = data.get("rollback_steps") or []

    data["steps"] = [RunbookStep.model_validate(s) if isinstance(s, dict) else s for s in raw_steps]
    data["rollback_steps"] = [
        RunbookStep.model_validate(s) if isinstance(s, dict) else s for s in raw_rollback
    ]

    # Remove computed columns not in Pydantic model (e.g. 'rank', 'similarity', 'search_vector')
    for extra in ("rank", "similarity", "search_vector"):
        data.pop(extra, None)

    return Runbook.model_validate(data)


def _runbook_create_to_dict(data: RunbookCreate, embedding: list[float] | None) -> dict[str, Any]:
    """Convert RunbookCreate to a dict suitable for INSERT."""
    payload: dict[str, Any] = {
        "title": data.title,
        "description": data.description,
        "project_key": data.project_key,
        "trigger": data.trigger,
        "prerequisites": data.prerequisites,
        "steps": [s.model_dump() for s in data.steps],
        "rollback_steps": [s.model_dump() for s in data.rollback_steps],
        "estimated_duration": data.estimated_duration,
        "tags": data.tags,
        "metadata": data.metadata,
    }
    if embedding is not None:
        payload["embedding"] = embedding
    return payload


def _runbook_update_to_dict(data: RunbookUpdate, embedding: list[float] | None) -> dict[str, Any]:
    """Convert RunbookUpdate to a partial update dict (only set fields)."""
    payload = data.model_dump(exclude_none=True)
    # steps and rollback_steps contain RunbookStep models — serialize to dicts
    if "steps" in payload:
        payload["steps"] = [s.model_dump() for s in data.steps]  # type: ignore[union-attr]
    if "rollback_steps" in payload:
        payload["rollback_steps"] = [s.model_dump() for s in data.rollback_steps]  # type: ignore[union-attr]
    if embedding is not None:
        payload["embedding"] = embedding
    return payload


class PgRunbookRepo(BasePgRepository):
    """Repository for the `runbooks` table.

    All methods accept an optional ``session`` kwarg to allow callers to share
    a transaction across multiple operations.
    """

    table = runbooks

    # -------------------------------------------------------------------------
    # CRUD — Pydantic-level API
    # -------------------------------------------------------------------------

    async def create(  # type: ignore[override]
        self,
        data: RunbookCreate,
        embedding: list[float] | None = None,
        *,
        session: Any = None,
    ) -> Runbook:
        """Insert a new Runbook and return it as a Pydantic model."""
        payload = _runbook_create_to_dict(data, embedding)
        row = await super().create(payload, session=session)
        logger.info("runbook.create", title=data.title, project_key=data.project_key)
        return _row_to_runbook(row)

    async def create_with_promotion(
        self,
        data: RunbookCreate,
        embedding: list[float] | None,
        source_learning_id: UUID,
        dream_run_id: int | None,
        *,
        project_key: str | None = None,
    ) -> Runbook:
        """Insert a Runbook + update learning.metadata + insert dream_promotions
        row, all in ONE transaction.

        Statement order is load-bearing: both paths first serialize on the unique
        ``(title, project_key)`` target identity. Scoped calls then lock the owned
        source before the target INSERT; admin calls preserve their historical
        target-first shape. The runbook is always inserted before the audit FK.

        Raises IntegrityError if the learning is already materialized (partial
        unique index on dream_promotions). Callers translate the error into a
        typed message at the tool layer (T7).

        The stamp + audit INSERT are delegated to record_promotion() which emits
        them in the canonical order within the same transaction.
        """
        payload = _runbook_create_to_dict(data, embedding)
        async with self.get_session() as session:
            async with session.begin():
                await lock_runbook_promotion_target(
                    session,
                    title=data.title,
                    project_key=data.project_key,
                )

                if project_key is not None:
                    source_exists = await lock_source_learning(
                        session,
                        source_learning_id=source_learning_id,
                        project_key=project_key,
                    )
                    if not source_exists:
                        raise SourceLearningNotFound("source learning not found")

                # 1) Insert the runbook.
                result = await session.execute(
                    runbooks.insert().values(**payload).returning(*runbooks.c)
                )
                row = result.mappings().one()
                runbook = _row_to_runbook(dict(row))

                # 2+3) Stamp learning metadata + insert dream_promotions audit row.
                if project_key is None:
                    await record_promotion(
                        session,
                        source_learning_id=source_learning_id,
                        target_entity_id=runbook.id,
                        target_type="runbook",
                        dream_run_id=dream_run_id,
                        target_adr_id=None,
                        target_runbook_id=runbook.id,
                    )
                else:
                    await record_promotion(
                        session,
                        source_learning_id=source_learning_id,
                        target_entity_id=runbook.id,
                        target_type="runbook",
                        dream_run_id=dream_run_id,
                        target_adr_id=None,
                        target_runbook_id=runbook.id,
                        project_key=project_key,
                    )

                logger.info(
                    "runbook.created_with_promotion",
                    runbook_id=str(runbook.id),
                    source_learning_id=str(source_learning_id),
                )
                return runbook

    async def get_by_id(  # type: ignore[override]
        self,
        id: uuid.UUID | str,
        *,
        project_key: str | None = None,
        session: Any = None,
    ) -> Runbook | None:
        """Fetch a Runbook by primary key. Returns None if not found."""
        if project_key is None:
            row = await super().get_by_id(id, session=session)
        else:
            row = await self.find_one(
                {"id": id, "project_key": project_key},
                session=session,
            )
        return _row_to_runbook(row) if row is not None else None

    async def update(  # type: ignore[override]
        self,
        id: uuid.UUID | str,
        data: RunbookUpdate,
        embedding: list[float] | None = None,
        *,
        project_key: str | None = None,
        session: Any = None,
    ) -> Runbook | None:
        """Partially update a Runbook. Returns None if not found."""
        payload = _runbook_update_to_dict(data, embedding)
        if not payload:
            # Nothing to update — return current record
            if project_key is None:
                return await self.get_by_id(id, session=session)
            return await self.get_by_id(
                id,
                project_key=project_key,
                session=session,
            )
        if project_key is None:
            row = await super().update(id, payload, session=session)
        else:
            stmt = (
                runbooks.update()
                .where(
                    runbooks.c.id == id,
                    runbooks.c.project_key == project_key,
                )
                .values(**payload, updated_at=datetime.now(UTC))
                .returning(runbooks)
            )
            async with self._maybe_session(session, write=True) as sess:
                result = await sess.execute(stmt)
                mapping = result.mappings().one_or_none()
                row = dict(mapping) if mapping is not None else None
        return _row_to_runbook(row) if row is not None else None

    async def delete(
        self,
        id: uuid.UUID | str,
        *,
        project_key: str | None = None,
        session: Any = None,
    ) -> bool:
        """Delete a Runbook by id. Returns True if deleted, False if not found."""
        if project_key is None:
            return await super().delete(id, session=session)
        stmt = (
            runbooks.delete()
            .where(
                runbooks.c.id == id,
                runbooks.c.project_key == project_key,
            )
            .returning(runbooks.c.id)
        )
        async with self._maybe_session(session, write=True) as sess:
            return (await sess.execute(stmt)).one_or_none() is not None

    # -------------------------------------------------------------------------
    # Full-text search
    # -------------------------------------------------------------------------

    async def search_fts(  # type: ignore[override]
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        *,
        session: Any = None,
    ) -> list[Runbook]:
        """FTS search on title/description/trigger/tags via DB-generated search_vector.

        Returns a list of Runbook models ordered by ts_rank (best match first).
        """
        filters: dict[str, Any] = {}
        if project_keys is not None:
            filters["project_key"] = project_keys
        elif project_key is not None:
            filters["project_key"] = project_key

        # skip_count=True: the total is discarded here — avoids a second full @@ scan
        rows, _total = await super().search_fts(
            query,
            filters=filters if filters else None,
            offset=offset,
            limit=limit,
            session=session,
            skip_count=True,
        )
        return [_row_to_runbook(r) for r in rows]

    # -------------------------------------------------------------------------
    # Vector search
    # -------------------------------------------------------------------------

    async def vector_search(
        self,
        embedding: list[float],
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 10,
        *,
        session: Any = None,
    ) -> list[tuple[Runbook, float]]:
        """Semantic search using pgvector cosine similarity.

        Returns a list of (Runbook, similarity_score) tuples ordered by
        descending similarity (closest first).
        """
        filters: dict[str, Any] = {}
        if project_keys is not None:
            filters["project_key"] = project_keys
        elif project_key is not None:
            filters["project_key"] = project_key

        rows = await super().search_vector(
            embedding,
            filters=filters if filters else None,
            limit=limit,
            session=session,
        )
        results: list[tuple[Runbook, float]] = []
        for row in rows:
            similarity = float(row.get("similarity", 0.0))
            results.append((_row_to_runbook(row), similarity))
        return results

    # -------------------------------------------------------------------------
    # Execution tracking (atomic)
    # -------------------------------------------------------------------------

    async def record_execution(
        self,
        id: uuid.UUID | str,
        status: ExecutionStatus,
        *,
        session: Any = None,
    ) -> Runbook | None:
        """Atomically increment execution_count, set last_executed_at and status.

        Uses a single UPDATE...RETURNING to avoid a read-modify-write race.
        Returns the updated Runbook or None if not found.
        """

        async def _execute(sess: Any) -> Runbook | None:
            stmt = (
                self.table.update()
                .where(self.table.c.id == id)
                .values(
                    execution_count=self.table.c.execution_count + 1,
                    last_executed_at=datetime.now(UTC),
                    last_execution_status=status,
                    updated_at=datetime.now(UTC),
                )
                .returning(self.table)
            )
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            if row is None:
                logger.warning("runbook.record_execution.not_found", id=id)
                return None
            logger.info(
                "runbook.record_execution",
                id=id,
                status=status,
                execution_count=row["execution_count"],
            )
            return _row_to_runbook(dict(row))

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)

    # -------------------------------------------------------------------------
    # Lookups
    # -------------------------------------------------------------------------

    async def get_by_title(
        self,
        title: str,
        project_key: str,
        *,
        session: Any = None,
    ) -> Runbook | None:
        """Fetch a Runbook by (title, project_key) unique constraint.

        Delegates to find_one() for a clean equality-filter lookup.
        Returns None if not found.
        """
        row = await self.find_one(
            {"title": title, "project_key": project_key},
            session=session,
        )
        return _row_to_runbook(row) if row is not None else None

    async def list_by_project(
        self,
        project_key: str,
        limit: int = 50,
        offset: int = 0,
        *,
        session: Any = None,
    ) -> list[Runbook]:
        """List Runbooks for a given project_key, ordered by created_at DESC.

        Returns a list of Runbook models (no total count).
        """
        rows, _total = await self.list_all(
            filters={"project_key": project_key},
            limit=limit,
            offset=offset,
            order_by="created_at",
            order_desc=True,
            skip_count=True,
            session=session,
        )
        return [_row_to_runbook(r) for r in rows]
