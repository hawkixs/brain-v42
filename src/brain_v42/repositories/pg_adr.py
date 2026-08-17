"""PostgreSQL async repository for ADR (Architecture Decision Record) entities.

Subclasses BasePgRepository (SQLAlchemy Core async).

Capabilities:
- CRUD: create, get_by_id, get_by_number, update, delete
- Auto-number: per project_key, using COALESCE(MAX(number), 0)+1 within the same
  transaction as the INSERT (advisory lock prevents duplicate-number races).
- FTS: full-text search using tsvector (ts_rank + plainto_tsquery)
- Vector search: pgvector cosine similarity via <=> operator (base); public
  contract exposes distance = 1.0 - similarity (epsilon ~1e-17, no consumer
  thresholds on this).
- Filters: by status, project_key, tags (overlap &&)
- accept(): sets status='accepted' + decided_at=now() atomically via super().update()
- create_with_promotion(): local transaction (statement order is load-bearing);
  stamp + audit delegated to the promotion helpers (stamp_learning() checked
  eagerly for SourceLearningNotFound, then insert_promotion_audit()).

Design rules (vague 3):
  - _row_to_model uses an allowlist (known_fields) to strip computed columns
    (rank, similarity, distance, search_vector, embedding).
  - list_all ORDER BY number DESC (not created_at).
  - search() without query DOES NOT filter archived rows — brain_service refilters
    in Python; filtering in SQL would break include_archived=True use-cases.
  - vector_search maps distance = 1.0 - row["similarity"] so the public
    contract (ADR, cosine-distance) is preserved after the base move.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import adrs
from brain_v42.models.adr import ADR, ADRCreate, ADRUpdate
from brain_v42.repositories.pg_base import BasePgRepository, project_scope
from brain_v42.repositories.promotion import (
    SourceLearningNotFound,
    insert_promotion_audit,
    lock_source_learning,
    stamp_learning,
)

logger = structlog.get_logger(__name__)


class PgADRRepo(BasePgRepository):
    """PostgreSQL async repository for ADR (Architecture Decision Record) entities.

    Subclasses BasePgRepository — CRUD, list, FTS and vector search delegate
    to the base; this class handles ADR-specific logic: auto-numbering with an
    advisory lock, status transitions (accept/supersede), and the three-step
    create_with_promotion transaction.

    Session management inherited from BasePgRepository:
        __init__(session_factory=None) — optional DI, falls back to global singleton.
        transaction() / get_session() / _maybe_session() from base.
    """

    table = adrs
    fts_columns: list[str] = []  # search_vector is DB-generated STORED column

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _row_to_model(row: sa.engine.Row | dict) -> ADR:
        """Convert a SQLAlchemy Row or dict to an ADR Pydantic model.

        Uses an allowlist to strip computed columns (rank, similarity, distance,
        search_vector, embedding) before passing to Pydantic validation.
        """
        if isinstance(row, dict):
            data = row
        else:
            data = dict(row._mapping)
        known_fields = {
            "id",
            "number",
            "title",
            "context",
            "decision",
            "consequences",
            "alternatives_considered",
            "project_key",
            "tags",
            "status",
            "decided_at",
            "superseded_by",
            "embedding",
            "metadata",
            "search_vector",
            "created_at",
            "updated_at",
        }
        clean = {k: v for k, v in data.items() if k in known_fields}
        return ADR.model_validate(clean)

    # Prefix for the advisory lock key — scopes locks to brain_v42 ADR numbering.
    _ADR_LOCK_PREFIX = "brain_v42_adr_number"

    async def _next_number(self, session: AsyncSession, project_key: str) -> int:
        """Return the next ADR number for the given project_key.

        Uses COALESCE(MAX(number), 0) + 1 to handle the empty-table case.

        CRITICAL: Must be called within the same transaction as the INSERT.

        Concurrency: Acquires pg_advisory_xact_lock(hashtext(prefix:project_key))
        before the MAX read to prevent two concurrent READ COMMITTED transactions
        from reading the same MAX and inserting duplicate numbers. The lock is
        scoped per-project so concurrent ADR creation on different projects does
        not block each other. The lock is released automatically at transaction end.
        """
        lock_key = f"{self._ADR_LOCK_PREFIX}:{project_key}"
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": lock_key},
        )
        result = await session.execute(
            sa.text(
                "SELECT COALESCE(MAX(number), 0) + 1 AS next_number"
                " FROM adrs WHERE project_key = :pk"
            ),
            {"pk": project_key},
        )
        return int(result.scalar_one())

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------

    async def create(  # type: ignore[override]
        self,
        data: ADRCreate,
        embedding: list[float] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> ADR:
        """Insert a new ADR with auto-assigned number.

        The number is computed inside the same transaction as the INSERT to
        prevent race conditions (advisory lock in _next_number).

        Args:
            data: ADRCreate payload (Pydantic model).
            embedding: Optional embedding vector (computed by service layer).
            session: Optional shared session (caller owns the transaction).

        Returns:
            The created ADR model.
        """
        async with self.transaction(session) as s:
            number = await self._next_number(s, data.project_key)
            payload: dict[str, Any] = {
                "number": number,
                "title": data.title,
                "context": data.context,
                "decision": data.decision,
                "consequences": data.consequences,
                "alternatives_considered": [
                    alt.model_dump() for alt in data.alternatives_considered
                ],
                "project_key": data.project_key,
                "tags": data.tags,
                "status": data.status,
                "embedding": embedding,
                "metadata": data.metadata,
            }
            row = await super().create(payload, session=s)
            logger.info("adr.created", number=number, project_key=data.project_key)
            return self._row_to_model(row)

    async def get_by_id(  # type: ignore[override]
        self,
        adr_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> ADR | None:
        """Fetch a single ADR by its UUID primary key.

        Returns None if not found.
        """
        if project_key is None:
            row = await super().get_by_id(adr_id, session=session)
        else:
            row = await self.find_one(
                {"id": adr_id, "project_key": project_key},
                session=session,
            )
        return self._row_to_model(row) if row is not None else None

    async def get_by_number(
        self,
        number: int,
        project_key: str,
        *,
        session: AsyncSession | None = None,
    ) -> ADR | None:
        """Fetch a single ADR by its project-scoped number.

        Delegates to find_one() for a clean equality-filter lookup.
        Returns None if not found.
        """
        row = await self.find_one(
            {"number": number, "project_key": project_key},
            session=session,
        )
        return self._row_to_model(row) if row is not None else None

    async def update(  # type: ignore[override]
        self,
        adr_id: UUID,
        data: ADRUpdate,
        embedding: list[float] | None = None,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> ADR | None:
        """Partially update an ADR (PATCH semantics — only non-None fields).

        alternatives_considered is serialised to a list[dict] before delegation
        so the base receives a plain JSON-serialisable payload.

        Args:
            adr_id: UUID of the ADR to update.
            data: ADRUpdate payload; only non-None fields are applied.
            embedding: Optional new embedding vector.
            session: Optional shared session (caller owns the transaction).

        Returns:
            Updated ADR model or None if not found.
        """
        payload: dict[str, Any] = {k: v for k, v in data.model_dump().items() if v is not None}
        if embedding is not None:
            payload["embedding"] = embedding
        if "alternatives_considered" in payload:
            payload["alternatives_considered"] = [
                alt.model_dump() if hasattr(alt, "model_dump") else alt
                for alt in payload["alternatives_considered"]
            ]
        if project_key is None:
            # base.update() always appends updated_at = datetime.now(UTC)
            row = await super().update(adr_id, payload, session=session)
        else:
            stmt = (
                adrs.update()
                .where(
                    adrs.c.id == adr_id,
                    adrs.c.project_key == project_key,
                )
                .values(**payload, updated_at=datetime.now(UTC))
                .returning(adrs)
            )
            async with self._maybe_session(session, write=True) as sess:
                result = await sess.execute(stmt)
                mapping = result.mappings().one_or_none()
                row = dict(mapping) if mapping is not None else None
        if row is None:
            return None
        logger.info("adr.updated", adr_id=str(adr_id))
        return self._row_to_model(row)

    async def accept(
        self,
        adr_id: UUID,
        *,
        session: AsyncSession | None = None,
    ) -> ADR | None:
        """Atomically set status='accepted' and decided_at=now().

        Delegates to super().update() which also bumps updated_at.

        Args:
            adr_id: UUID of the ADR to accept.
            session: Optional shared session (caller owns the transaction).

        Returns:
            Updated ADR model or None if not found.
        """
        now = datetime.now(UTC)
        row = await super().update(
            adr_id,
            {"status": "accepted", "decided_at": now},
            session=session,
        )
        if row is None:
            return None
        logger.info("adr.accepted", adr_id=str(adr_id))
        return self._row_to_model(row)

    async def delete(  # type: ignore[override]
        self,
        adr_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete an ADR by UUID.

        Returns:
            True if deleted, False if not found.
        """
        if project_key is None:
            deleted = await super().delete(adr_id, session=session)
        else:
            stmt = (
                adrs.delete()
                .where(
                    adrs.c.id == adr_id,
                    adrs.c.project_key == project_key,
                )
                .returning(adrs.c.id)
            )
            async with self._maybe_session(session, write=True) as sess:
                deleted = (await sess.execute(stmt)).one_or_none() is not None
        logger.info("adr.deleted", adr_id=str(adr_id), found=deleted)
        return deleted

    async def create_with_promotion(
        self,
        data: ADRCreate,
        embedding: list[float] | None,
        source_learning_id: UUID,
        auto_accept: bool,
        dream_run_id: int | None,
        *,
        project_key: str | None = None,
    ) -> ADR:
        """Insert an ADR + update learning.metadata + insert dream_promotions row,
        all in ONE transaction.

        Statement order is load-bearing. Both paths first take the existing ADR
        number advisory lock. Scoped calls then lock the owned source before the
        target INSERT; admin calls preserve their historical target-first shape.
        Both paths stamp the source before inserting the promotion audit row.

        Steps 2 and 4 are intentionally split (not delegated to record_promotion())
        so the rowcount check at step 3 happens BEFORE the dream_promotions INSERT.
        This is critical because dream_promotions.source_learning_id carries a
        non-deferrable FK to learnings.id: if the learning is absent and the INSERT
        were reached, PostgreSQL would raise ForeignKeyViolationError instead of
        allowing the caller to detect a missing learning via rowcount.

        Raises:
            SourceLearningNotFound: source_learning_id doesn't exist in learnings.
            IntegrityError: learning already materialized (partial unique index on
                dream_promotions).  Caller translates into a typed message (T6).
        """
        async with self.get_session() as session:
            async with session.begin():
                number = await self._next_number(session, data.project_key)
                status = "accepted" if auto_accept else "proposed"

                if project_key is not None:
                    source_exists = await lock_source_learning(
                        session,
                        source_learning_id=source_learning_id,
                        project_key=project_key,
                    )
                    if not source_exists:
                        raise SourceLearningNotFound("source learning not found")

                # 1) Insert the ADR — numbered, statused, embedded.
                result = await session.execute(
                    adrs.insert()
                    .values(
                        number=number,
                        title=data.title,
                        context=data.context,
                        decision=data.decision,
                        consequences=data.consequences,
                        alternatives_considered=[
                            alt.model_dump() for alt in data.alternatives_considered
                        ],
                        project_key=data.project_key,
                        tags=data.tags,
                        status=status,
                        decided_at=sa.func.now() if auto_accept else None,
                        embedding=embedding,
                        metadata=data.metadata,
                    )
                    .returning(*adrs.c)
                )
                adr_row = result.fetchone()
                assert adr_row is not None
                adr = self._row_to_model(adr_row)

                # 2) Stamp learning metadata — returns rowcount (1 if found, 0 if not).
                if project_key is None:
                    rowcount = await stamp_learning(
                        session,
                        source_learning_id=source_learning_id,
                        target_entity_id=adr.id,
                    )
                else:
                    rowcount = await stamp_learning(
                        session,
                        source_learning_id=source_learning_id,
                        target_entity_id=adr.id,
                        project_key=project_key,
                    )

                # 3) Eagerly raise BEFORE the dream_promotions INSERT so the FK
                #    (source_learning_id → learnings.id, non-deferrable) cannot
                #    obscure the "missing learning" failure mode as IntegrityError.
                if rowcount != 1:
                    if project_key is not None:
                        raise SourceLearningNotFound("source learning not found")
                    raise SourceLearningNotFound(
                        f"learning {source_learning_id} not found; cannot promote"
                    )

                # 4) Insert the dream_promotions audit row.
                await insert_promotion_audit(
                    session,
                    source_learning_id=source_learning_id,
                    target_type="adr",
                    dream_run_id=dream_run_id,
                    target_adr_id=adr.id,
                    target_runbook_id=None,
                )

                logger.info(
                    "adr.created_with_promotion",
                    adr_id=str(adr.id),
                    source_learning_id=str(source_learning_id),
                    auto_accept=auto_accept,
                )
                return adr

    # -------------------------------------------------------------------------
    # List / Search
    # -------------------------------------------------------------------------

    async def list_all(  # type: ignore[override]
        self,
        project_key: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
        *,
        session: AsyncSession | None = None,
    ) -> list[ADR]:
        """List ADRs with optional filters and pagination.

        Args:
            project_key: Filter by project (optional).
            status: Filter by status e.g. 'proposed', 'accepted' (optional).
            limit: Maximum number of results (default 20).
            offset: Number of rows to skip (default 0).
            include_archived: When False (default), excludes merged/archived ADRs.
            session: Optional shared session.

        Returns:
            List of ADR models ordered by number DESC.
        """
        filters: dict[str, Any] = {}
        if project_key is not None:
            filters["project_key"] = project_key
        if status is not None:
            filters["status"] = status

        rows, _total = await super().list_all(
            filters=filters,
            offset=offset,
            limit=limit,
            order_by="number",
            order_desc=True,
            skip_count=True,
            include_archived=False if not include_archived else None,
            session=session,
        )
        return [self._row_to_model(r) for r in rows]

    async def search(
        self,
        query: str | None = None,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        *,
        session: AsyncSession | None = None,
    ) -> list[ADR]:
        """Search ADRs with optional FTS, filters, and pagination.

        When `query` is provided, uses PostgreSQL FTS (plainto_tsquery + ts_rank)
        with optional tags filter (&&).
        Without `query`, returns all ADRs matching the optional filters ordered by
        number DESC.  The no-query branch DOES NOT filter archived rows — the service
        layer refilters in Python (filtering in SQL would break include_archived=True).

        Args:
            query: Free-text search string (FTS via search_vector, optional).
            project_key: Filter by project (optional).
            project_keys: Multi-project filter — takes precedence over project_key.
            status: Filter by status (optional).
            tags: Filter by tags overlap (optional, uses && operator).
            limit: Maximum number of results (default 20).
            offset: Number of rows to skip (default 0).
            session: Optional shared session.

        Returns:
            List of ADR models.
        """
        # Build project scope (project_keys overrides project_key)
        scope = project_scope(project_key, project_keys)
        filters: dict[str, Any] = {}
        if scope is not None:
            filters["project_key"] = scope
        if status is not None:
            filters["status"] = status

        if query is not None:
            # FTS branch — optionally filtered by tags via extra_clauses
            extra_clauses = None
            tags_clause = self._tags_clause(self.table.c.tags, tags)
            if tags_clause is not None:
                extra_clauses = [tags_clause]

            rows, _total = await super().search_fts(
                query,
                filters=filters if filters else None,
                offset=offset,
                limit=limit,
                skip_count=True,
                extra_clauses=extra_clauses,
                session=session,
            )
        else:
            # No-query branch: list all matching filters, no archived filtering.
            # Preserve: tags filter via extra_clauses (no archived exclusion).
            extra_clauses = None
            tags_clause = self._tags_clause(self.table.c.tags, tags)
            if tags_clause is not None:
                extra_clauses = [tags_clause]

            rows, _total = await super().list_all(
                filters=filters if filters else None,
                offset=offset,
                limit=limit,
                order_by="number",
                order_desc=True,
                skip_count=True,
                include_archived=None,  # no archive filter — preserve existing behaviour
                extra_clauses=extra_clauses,
                session=session,
            )
        return [self._row_to_model(r) for r in rows]

    async def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> list[tuple[ADR, float]]:
        """Semantic search using pgvector cosine distance (<=> operator).

        Delegates to base search_vector() which returns rows with a 'similarity'
        key (= 1 - cosine_distance).  This wrapper maps:
            distance = 1.0 - row["similarity"]
        so the public contract (ADR, cosine-distance float) is preserved.

        The epsilon difference (~1e-17) from the previous direct distance
        computation is assumed-acceptable — no consumer thresholds on this value.

        Args:
            query_embedding: Query vector (1536 floats, L2-normalized).
            limit: Maximum number of results (default 10).
            project_key: Optional filter by project.
            project_keys: Multi-project filter — takes precedence over project_key.
            session: Optional shared session.

        Returns:
            List of (ADR, distance) tuples where distance is cosine distance [0, 1].
        """
        scope = project_scope(project_key, project_keys)
        filters: dict[str, Any] = {}
        if scope is not None:
            filters["project_key"] = scope

        rows = await super().search_vector(
            query_embedding,
            filters=filters if filters else None,
            limit=limit,
            session=session,
        )
        output: list[tuple[ADR, float]] = []
        for row in rows:
            similarity = float(row.get("similarity", 0.0))
            distance = 1.0 - similarity
            adr_obj = self._row_to_model(row)
            output.append((adr_obj, distance))
        return output
