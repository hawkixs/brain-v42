"""PostgreSQL repository for learnings — CRUD, FTS, pgvector.

Re-platformed on BasePgRepository (vague 3).

Design decisions:
- create/get_by_id/update/delete/list_all/search_fts/search_vector all delegate
  to super() — SQL is built once in pg_base; public signatures are unchanged.
- validate() stays local: it must update validated_at WITHOUT bumping updated_at.
  Using super().update() would set updated_at = NOW() (correct for generic updates),
  which is undesirable here.  A dedicated UPDATE is issued instead.
- __init__/_session removed — BasePgRepository absorbs session_factory injection.
- _row_to_model strips {rank, similarity, distance, search_vector} keys added by
  search helpers (pg_base) before handing the dict to Learning.model_validate.
  ``embedding`` is NOT in the strip set — create/get_by_id/update paths project
  the full row (RETURNING *) so Learning.embedding must be populated from the DB.
  Search paths use slim columns and never project embedding, so stripping it there
  is a no-op; the contract strip set = {rank, similarity, distance, search_vector}
  is consistent with pg_decision and pg_adr (vague 3 convention (e)).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.project_group_scope import project_key_in_group
from brain_v42.db.tables import learnings
from brain_v42.models.learning import Learning, LearningCreate, LearningUpdate
from brain_v42.repositories.pg_base import BasePgRepository, Row, project_scope

logger = structlog.get_logger(__name__)

# Keys added by search helpers (pg_base) that are not Learning model fields.
# NOTE: ``embedding`` is intentionally absent — create/get_by_id/update project
# the full row (RETURNING *) and must populate Learning.embedding from the DB.
# Search paths use slim columns and never project embedding, so it is never
# present in those rows; strip set aligns with vague-3 convention (e).
_SEARCH_STRIP_KEYS = frozenset({"rank", "similarity", "distance", "search_vector"})


class PgLearningRepo(BasePgRepository):
    """Async PostgreSQL repository for the learnings table.

    Inherits session_factory injection (optional) and all generic CRUD/search
    paths from BasePgRepository. Provides CRUD, FTS, pgvector semantic search,
    confidence/project_key/tags filters, and validate() which sets validated_at
    WITHOUT bumping updated_at (special-cased; cannot delegate to super().update).

    Public signatures are unchanged from the pre-vague-3 implementation — the
    service layer calls this repo without modification.
    """

    table = learnings
    fts_columns: list[str] = []  # search_vector is DB-generated STORED column

    # -------------------------------------------------------------------------
    # Row → Model conversion
    # -------------------------------------------------------------------------

    def _row_to_model(self, row: Row) -> Learning:
        """Strip search-path extras and convert a raw DB dict to a Learning model.

        Strips: rank, similarity, distance, search_vector.
        These keys are added by list_all / search_fts / search_vector helpers
        in pg_base and are not Learning model fields (Pydantic would ignore
        unknown fields, but explicit stripping avoids relying on model config).

        ``embedding`` is NOT stripped: create/get_by_id/update project the full
        row (RETURNING *) so Learning.embedding must be populated from the DB.
        Search paths use slim columns and never include embedding, so the strip
        set not containing it is a no-op on those paths.
        """
        return Learning.model_validate(
            {k: v for k, v in row.items() if k not in _SEARCH_STRIP_KEYS}
        )

    # -------------------------------------------------------------------------
    # CREATE
    # -------------------------------------------------------------------------

    async def create(  # type: ignore[override]
        self,
        data: LearningCreate,
        embedding: list[float] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> Learning:
        """Insert a new learning. embedding is optional (set by service layer)."""
        values: dict = {
            "topic": data.topic,
            "insight": data.insight,
            "source": data.source,
            "source_type": data.source_type,
            "confidence": data.confidence,
            "project_key": data.project_key,
            "tags": data.tags,
            "metadata": data.metadata,
        }
        if embedding is not None:
            values["embedding"] = embedding
        row = await super().create(values, session=session)
        learning = self._row_to_model(row)
        logger.info("learning created", id=str(learning.id))
        return learning

    # -------------------------------------------------------------------------
    # GET BY ID
    # -------------------------------------------------------------------------

    async def get_by_id(  # type: ignore[override]
        self,
        learning_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Learning | None:
        """Fetch a single learning by primary key. Returns None if not found."""
        if project_key is None:
            row = await super().get_by_id(learning_id, session=session)
        else:
            row = await self.find_one(
                {"id": learning_id, "project_key": project_key},
                session=session,
            )
        return self._row_to_model(row) if row is not None else None

    # -------------------------------------------------------------------------
    # UPDATE
    # -------------------------------------------------------------------------

    async def update(  # type: ignore[override]
        self,
        learning_id: UUID,
        data: LearningUpdate,
        embedding: list[float] | None = None,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Learning | None:
        """Partial update (PATCH semantics). Only non-None fields are updated.

        updated_at is always bumped by super().update (datetime.now(UTC) client-side,
        aligned with the vague-3 contract for snippet/runbook).
        """
        values: dict = {
            k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None
        }
        if embedding is not None:
            values["embedding"] = embedding
        if not values:
            # Nothing to update — return current state via get_by_id
            if project_key is None:
                return await self.get_by_id(learning_id, session=session)
            return await self.get_by_id(
                learning_id,
                project_key=project_key,
                session=session,
            )
        if project_key is None:
            row = await super().update(learning_id, values, session=session)
        else:
            stmt = (
                learnings.update()
                .where(
                    learnings.c.id == learning_id,
                    learnings.c.project_key == project_key,
                )
                .values(**values, updated_at=datetime.now(UTC))
                .returning(learnings)
            )
            async with self._maybe_session(session, write=True) as sess:
                result = await sess.execute(stmt)
                mapping = result.mappings().one_or_none()
                row = dict(mapping) if mapping is not None else None
        return self._row_to_model(row) if row is not None else None

    # -------------------------------------------------------------------------
    # DELETE
    # -------------------------------------------------------------------------

    async def delete(  # type: ignore[override]
        self,
        learning_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete a learning by id. Returns True if deleted, False if not found."""
        if project_key is None:
            deleted = await super().delete(learning_id, session=session)
        else:
            stmt = (
                learnings.delete()
                .where(
                    learnings.c.id == learning_id,
                    learnings.c.project_key == project_key,
                )
                .returning(learnings.c.id)
            )
            async with self._maybe_session(session, write=True) as sess:
                deleted = (await sess.execute(stmt)).one_or_none() is not None
        if deleted:
            logger.info("learning deleted", id=str(learning_id))
        return deleted

    # -------------------------------------------------------------------------
    # LIST
    # -------------------------------------------------------------------------

    async def list_all(  # type: ignore[override]
        self,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        confidence: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
        *,
        session: AsyncSession | None = None,
    ) -> list[Learning]:
        """List learnings with optional filters. Ordered by created_at DESC.

        When include_archived=False (default), excludes merged (merged_into IS NOT NULL)
        and archived (freshness_status = 'archived') learnings.

        Delegates SELECT slim (no embedding/search_vector), pagination, archive
        filter, and ORDER BY to super().list_all via extra_clauses for tags and
        confidence, and filters dict for project scope.
        """
        filters: dict = {"project_key": project_scope(project_key, project_keys)}
        if confidence is not None:
            filters["confidence"] = confidence

        tags_clause = self._tags_clause(self.table.c.tags, tags)
        extra = [tags_clause] if tags_clause is not None else None

        rows, _total = await super().list_all(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by="created_at",
            order_desc=True,
            skip_count=True,
            include_archived=False if not include_archived else None,
            extra_clauses=extra,
            session=session,
        )
        return [self._row_to_model(r) for r in rows]

    # -------------------------------------------------------------------------
    # FTS
    # -------------------------------------------------------------------------

    async def search_fts(  # type: ignore[override]
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        confidence: str | None = None,
        limit: int = 20,
        offset: int = 0,
        *,
        session: AsyncSession | None = None,
    ) -> list[Learning]:
        """Full-text search using PostgreSQL tsvector, ordered by ts_rank DESC.

        NOTE: search paths do NOT filter archived rows (only list_all does).
        tags filter is absent on FTS — iso-behaviour with pre-vague-3 implementation.
        """
        filters: dict = {"project_key": project_scope(project_key, project_keys)}
        if confidence is not None:
            filters["confidence"] = confidence

        rows, _total = await super().search_fts(
            query,
            filters=filters,
            offset=offset,
            limit=limit,
            skip_count=True,
            session=session,
        )
        return [self._row_to_model(r) for r in rows]

    # -------------------------------------------------------------------------
    # VECTOR SEARCH
    # -------------------------------------------------------------------------

    async def search_vector(  # type: ignore[override]
        self,
        query_embedding: list[float],
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        confidence: str | None = None,
        limit: int = 10,
        *,
        session: AsyncSession | None = None,
    ) -> list[tuple[Learning, float]]:
        """Semantic search via pgvector cosine distance. Returns (Learning, similarity).

        Delegates to super().search_vector which uses op('<=>',return_type=Float)
        canonical cast — no CAST() in SQL, HNSW plan intact.
        Each row includes 'similarity' = 1 - cosine_distance (epsilon ~1e-17 from
        float arithmetic; no consumer thresholds on this).
        """
        filters: dict = {"project_key": project_scope(project_key, project_keys)}
        if confidence is not None:
            filters["confidence"] = confidence

        rows = await super().search_vector(
            query_embedding,
            filters=filters,
            limit=limit,
            session=session,
        )
        out: list[tuple[Learning, float]] = []
        for row in rows:
            similarity = float(row.pop("similarity", 0.0))
            learning = self._row_to_model(row)
            out.append((learning, similarity))
        return out

    # -------------------------------------------------------------------------
    # VALIDATE  (KEEP — must NOT bump updated_at)
    # -------------------------------------------------------------------------

    async def validate(
        self,
        learning_id: UUID,
        *,
        project_group: str | None = None,
    ) -> Learning | None:
        """Set validated_at to current UTC time. Returns None if not found.

        INTENTIONALLY does NOT delegate to super().update(): that helper always
        sets updated_at = datetime.now(UTC), which is undesirable here — we only
        want to stamp validated_at without affecting the updated_at audit field.
        """
        conditions = [learnings.c.id == learning_id]
        if project_group is not None:
            conditions.append(project_key_in_group(learnings.c.project_key, project_group))
        stmt = (
            learnings.update()
            .where(*conditions)
            .values(validated_at=datetime.now(UTC))
            .returning(*learnings.c)
        )
        async with self._maybe_session(None, write=True) as sess:
            result = await sess.execute(stmt)
            row = result.mappings().first()
            if row is None:
                return None
            learning = Learning.model_validate(dict(row))
            logger.info("learning validated", id=str(learning_id))
            return learning
