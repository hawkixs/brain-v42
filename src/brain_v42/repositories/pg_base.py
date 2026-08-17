"""Generic async PostgreSQL repository using SQLAlchemy Core.

All 6 specialized repos (pg_decision, pg_learning, etc.) subclass BasePgRepository.
Never import from brain_v42.models here — stay at the DB Core dict layer.

Design rules (vague 3 contract):
  - NEVER put a Vector-typed expression in a SELECT projection. Use
    op("<=>", return_type=sa.Float) for cosine distance so the result
    processor is Float, not Vector.  CAST() must not appear in the SQL for
    the distance column (680c51b fixed at the source).
  - extra_clauses: Sequence[ColumnElement] | None is the generic extension
    mechanism on list/search — never add per-business-domain filter params.
  - include_archived on list_all ONLY; searches do NOT filter archived rows
    (pg_service refilters in Python, filtering in SQL would break
    include_archived=True).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

import sqlalchemy as sa
import structlog
from sqlalchemy import ColumnElement, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.engine import get_session_factory
from brain_v42.db.tables import MIN_COMPARABLE_EMBEDDING_NORM

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=dict[str, Any])
Row = dict[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddingBacklogStats:
    """Durable pending-vector state for one repository table."""

    count: int
    oldest_created_at: datetime | None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def project_scope(
    project_key: str | None,
    project_keys: list[str] | None,
) -> str | list[str] | None:
    """Return project_keys if not None, else project_key.

    Usage in subclass wrappers::

        scope = project_scope(project_key, project_keys)
        filters["project_key"] = scope  # _build_filter_clauses handles scalar == / list IN

    This consolidates the (project_key, project_keys) → single value pattern that
    all repos share, without leaking business logic into pg_base.
    """
    return project_keys if project_keys is not None else project_key


class BasePgRepository:
    """Generic PostgreSQL repository base class (SQLAlchemy Core async).

    Subclasses must set:
        table: sa.Table  — the SQLAlchemy Core Table object
        fts_columns: list[str]  — column names used to build FTS query
          (for tables that don't have a DB-generated search_vector, this can be empty)

    Usage:
        class DecisionRepo(BasePgRepository):
            table = decisions
            fts_columns = []  # search_vector is DB-generated STORED column

    Session management:
        - __init__(session_factory=None): DI constructor shared by all 6 repos.
          Pass session_factory explicitly at every call site for consistent wiring
          (server.py, benchmark.py, dream scripts). Passing None is valid only in
          tests that prime the global singleton before instantiation.
        - get_session() yields an AsyncSession from the injected or global factory.
        - transaction() is a context manager that wraps operations in BEGIN/COMMIT/ROLLBACK.
        - _maybe_session(session, *, write) is an internal helper consolidating the
          "if session is not None / else create one" pattern.

    All public methods accept an optional `session` kwarg. If None, they create
    their own session via get_session(). This allows callers to share a session
    for multi-step operations (e.g. supersede = insert + update in one transaction).
    """

    table: Table  # Must be overridden by subclass
    fts_columns: list[str] = []  # Override if table lacks DB-generated search_vector

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        """Initialise with an optional injected session factory.

        When session_factory is provided, get_session() uses it exclusively.
        When None (default), get_session() falls back to the global
        get_session_factory() singleton — preserving full rétro-compatibility.

        Design note: the optional factory is stored via the ``_session_factory``
        property setter (backed by ``_session_factory_opt``).  The property
        getter always returns a non-None factory (falling back to the global
        singleton when the injected one is None), which lets subclasses call
        ``self._session_factory()`` without a None-check — mypy sees the
        property return type as ``async_sessionmaker[AsyncSession]``.
        """
        self._session_factory = session_factory  # calls property setter below

    @property
    def _session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the injected factory, or the global singleton as fallback.

        Always returns a non-None callable — callers (including subclass
        _session() methods like PgLearningRepo) can safely call it without
        a None-check.
        """
        opt = self._session_factory_opt
        return opt if opt is not None else get_session_factory()

    @_session_factory.setter
    def _session_factory(self, value: async_sessionmaker[AsyncSession] | None) -> None:
        """Store the optional injected factory (None → fallback to global)."""
        self._session_factory_opt: async_sessionmaker[AsyncSession] | None = value

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Yield a fresh AsyncSession from the injected or global session factory."""
        factory = self._session_factory
        async with factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(
        self, session: AsyncSession | None = None
    ) -> AsyncGenerator[AsyncSession, None]:
        """Yield a session with an explicit BEGIN/COMMIT/ROLLBACK transaction.

        If a session is provided, wraps it in a savepoint (nested transaction).
        If no session is provided, creates a new session and begins a transaction.
        """
        if session is not None:
            async with session.begin_nested():
                yield session
        else:
            async with self.get_session() as sess:
                async with sess.begin():
                    yield sess

    @asynccontextmanager
    async def _maybe_session(
        self,
        session: AsyncSession | None,
        *,
        write: bool,
    ) -> AsyncGenerator[AsyncSession, None]:
        """Internal helper that consolidates the session-or-create pattern.

        - session provided → yield it directly (caller owns the transaction).
        - session is None + write=True → create session + begin transaction.
        - session is None + write=False → create session without transaction.
        """
        if session is not None:
            yield session
        elif write:
            async with self.get_session() as sess:
                async with sess.begin():
                    yield sess
        else:
            async with self.get_session() as sess:
                yield sess

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------

    async def create(
        self,
        data: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> Row:
        """Insert a new row and return it as a dict (from RETURNING *)."""

        async def _execute(sess: AsyncSession) -> Row:
            stmt = self.table.insert().values(**data).returning(self.table)
            result = await sess.execute(stmt)
            row = result.mappings().one()
            logger.debug("repository.create", table=self.table.name, id=row.get("id"))
            return dict(row)

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)

    async def get_by_id(
        self,
        id: uuid.UUID | str,
        *,
        session: AsyncSession | None = None,
    ) -> Row | None:
        """Fetch a single row by primary key (UUID). Returns None if not found."""

        async def _execute(sess: AsyncSession) -> Row | None:
            stmt = sa.select(self.table).where(self.table.c.id == id)
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            return dict(row) if row is not None else None

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    async def resolve_id_prefix(
        self,
        prefix_hex: str,
        *,
        limit: int = 6,
        session: AsyncSession | None = None,
    ) -> list[uuid.UUID]:
        """Resolve a git-style short id (bare-hex prefix) to matching row ids.

        Callers pass a normalized bare-hex prefix (see
        brain_v42.entity_ids.normalize_uuid_prefix); comparison strips
        hyphens from id::text so hyphen placement never matters. Non-hex or
        empty input returns [] without querying — LIKE wildcards can't reach
        the database. Results are ORDER BY id LIMIT *limit* so ambiguity
        messages upstream stay deterministic and bounded.
        """
        if not prefix_hex or not set(prefix_hex) <= set("0123456789abcdef"):
            return []

        async def _execute(sess: AsyncSession) -> list[uuid.UUID]:
            bare_id = sa.func.replace(sa.cast(self.table.c.id, sa.Text), "-", "")
            stmt = (
                sa.select(self.table.c.id)
                .where(bare_id.like(prefix_hex + "%"))
                .order_by(self.table.c.id)
                .limit(limit)
            )
            result = await sess.execute(stmt)
            return list(result.scalars().all())

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    async def update(
        self,
        id: uuid.UUID | str,
        data: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> Row | None:
        """Update a row by id (partial update). Always sets updated_at = NOW()."""

        async def _execute(sess: AsyncSession) -> Row | None:
            payload = {**data, "updated_at": datetime.now(UTC)}
            stmt = (
                self.table.update()
                .where(self.table.c.id == id)
                .values(**payload)
                .returning(self.table)
            )
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            if row is None:
                return None
            logger.debug("repository.update", table=self.table.name, id=id)
            return dict(row)

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)

    async def set_embedding_if_current(
        self,
        id: uuid.UUID | str,
        embedding: list[float],
        *,
        expected_updated_at: datetime,
        session: AsyncSession | None = None,
    ) -> Row | None:
        """Store an embedding only while an unusable row is unchanged."""
        if "embedding" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'embedding' column.")

        async def _execute(sess: AsyncSession) -> Row | None:
            stmt = (
                self.table.update()
                .where(
                    self.table.c.id == id,
                    sa.or_(
                        self.table.c.embedding.is_(None),
                        sa.func.vector_norm(self.table.c.embedding)
                        <= MIN_COMPARABLE_EMBEDDING_NORM,
                    ),
                    self.table.c.updated_at == expected_updated_at,
                )
                .values(embedding=embedding, updated_at=datetime.now(UTC))
                .returning(self.table)
            )
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            if row is None:
                return None
            logger.debug("repository.embedding_stored", table=self.table.name, id=id)
            return dict(row)

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)

    async def list_embedding_backlog(
        self,
        *,
        limit: int = 100,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> list[Row]:
        """Return the oldest rows whose derived embedding is not comparable."""
        if "embedding" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'embedding' column.")
        if limit < 1:
            raise ValueError("limit must be positive")
        if project_key is not None and "project_key" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'project_key' column.")

        async def _execute(sess: AsyncSession) -> list[Row]:
            clauses: list[ColumnElement[bool]] = [
                sa.or_(
                    self.table.c.embedding.is_(None),
                    sa.func.vector_norm(self.table.c.embedding) <= MIN_COMPARABLE_EMBEDDING_NORM,
                )
            ]
            if project_key is not None:
                clauses.append(self.table.c.project_key == project_key)
            stmt = sa.select(self.table).where(*clauses)
            stmt = stmt.order_by(self.table.c.created_at, self.table.c.id).limit(limit)
            result = await sess.execute(stmt)
            return [dict(row) for row in result.mappings().all()]

        async with self._maybe_session(session, write=False) as sess:
            return await _execute(sess)

    async def embedding_backlog_stats(
        self,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> EmbeddingBacklogStats:
        """Count non-comparable embeddings and report the oldest row timestamp."""
        if "embedding" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'embedding' column.")
        if project_key is not None and "project_key" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'project_key' column.")

        async def _execute(sess: AsyncSession) -> EmbeddingBacklogStats:
            clauses: list[ColumnElement[bool]] = [
                sa.or_(
                    self.table.c.embedding.is_(None),
                    sa.func.vector_norm(self.table.c.embedding) <= MIN_COMPARABLE_EMBEDDING_NORM,
                )
            ]
            if project_key is not None:
                clauses.append(self.table.c.project_key == project_key)
            stmt = sa.select(
                sa.func.count().label("pending_count"),
                sa.func.min(self.table.c.created_at).label("oldest_created_at"),
            ).where(*clauses)
            result = await sess.execute(stmt)
            row = result.mappings().one()
            return EmbeddingBacklogStats(
                count=int(row["pending_count"]),
                oldest_created_at=row["oldest_created_at"],
            )

        async with self._maybe_session(session, write=False) as sess:
            return await _execute(sess)

    async def delete(
        self,
        id: uuid.UUID | str,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete a row by id. Returns True if deleted, False if not found."""

        async def _execute(sess: AsyncSession) -> bool:
            stmt = self.table.delete().where(self.table.c.id == id).returning(self.table.c.id)
            result = await sess.execute(stmt)
            deleted = result.one_or_none()
            logger.debug(
                "repository.delete",
                table=self.table.name,
                id=id,
                found=deleted is not None,
            )
            return deleted is not None

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)

    async def find_one(
        self,
        filters: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> Row | None:
        """Fetch the first row matching the given equality filters.

        Uses _build_filter_clauses — raises ValueError for unknown columns.
        Returns None if no row matches.

        Typical consumers: get_by_number (adr), get_by_title (runbook).
        """

        async def _execute(sess: AsyncSession) -> Row | None:
            where_clauses = self._build_filter_clauses(filters)
            stmt = sa.select(self.table)
            if where_clauses:
                stmt = stmt.where(sa.and_(*where_clauses))
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            return dict(row) if row is not None else None

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    # -------------------------------------------------------------------------
    # List / Pagination
    # -------------------------------------------------------------------------

    async def list_all(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        order_by: str = "created_at",
        order_desc: bool = True,
        skip_count: bool = False,
        include_archived: bool | None = None,
        extra_clauses: Sequence[ColumnElement[Any]] | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[list[Row], int]:
        """List rows with OFFSET/LIMIT pagination and optional equality filters.

        Args:
            include_archived: When False, adds ``merged_into IS NULL`` and
                ``freshness_status != 'archived'`` filters (if the table has those
                columns).  True or None (default) adds no archive filter.
                NOTE: searches do NOT filter archived rows — only list_all does.
            extra_clauses: Additional SQLAlchemy WHERE expressions appended to the
                query.  This is the generic extension point for subclasses — never
                add per-business-domain kwargs here.

        Returns: Tuple of (items: list[dict], total: int).
        """

        async def _execute(sess: AsyncSession) -> tuple[list[Row], int]:
            where_clauses: list[Any] = self._build_filter_clauses(filters or {})

            # include_archived=False: add archive-exclusion filters (if columns exist)
            if include_archived is False:
                if "merged_into" in self.table.c:
                    where_clauses.append(self.table.c.merged_into.is_(None))
                if "freshness_status" in self.table.c:
                    where_clauses.append(self.table.c.freshness_status != "archived")

            # Append any caller-supplied extra clauses
            if extra_clauses:
                where_clauses.extend(extra_clauses)

            if skip_count:
                total = 0
            else:
                count_stmt = sa.select(sa.func.count()).select_from(self.table)
                if where_clauses:
                    count_stmt = count_stmt.where(sa.and_(*where_clauses))
                count_result = await sess.execute(count_stmt)
                total = count_result.scalar_one()

            order_col = self.table.c[order_by]
            order_expr = order_col.desc() if order_desc else order_col.asc()
            stmt = sa.select(*self._list_columns())
            if where_clauses:
                stmt = stmt.where(sa.and_(*where_clauses))
            stmt = stmt.order_by(order_expr).offset(offset).limit(limit)

            result = await sess.execute(stmt)
            rows = [dict(r) for r in result.mappings().all()]
            return rows, total

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    # Columns to exclude from list and search results (heavy, not needed for display)
    _SEARCH_EXCLUDE_COLS: frozenset[str] = frozenset({"embedding", "search_vector"})

    def _search_columns(self) -> list[sa.Column]:
        """Return table columns suitable for search/list results (excludes embedding, search_vector).

        Saves ~12-19 KB per row (embedding vector 1536) and tsvector marshalling
        overhead that Pydantic models discard anyway. Used by list_all() and all
        search paths in subclasses.
        """
        return [c for c in self.table.c if c.name not in self._SEARCH_EXCLUDE_COLS]

    # Alias — list and search paths use the same slim column set.
    _list_columns = _search_columns

    def _build_filter_clauses(self, filters: dict[str, Any]) -> list[Any]:
        """Build SQLAlchemy WHERE clauses from a {column: value} dict. Skips None values.

        When value is a list, uses IN instead of equality.
        """
        clauses = []
        for col_name, value in filters.items():
            if value is None:
                continue
            if col_name not in self.table.c:
                raise ValueError(f"Unknown column '{col_name}' on table '{self.table.name}'")
            if isinstance(value, list):
                clauses.append(self.table.c[col_name].in_(value))
            else:
                clauses.append(self.table.c[col_name] == value)
        return clauses

    def _tags_clause(
        self,
        tags_col: sa.Column,
        tags: list[str] | None,
    ) -> sa.ColumnElement[bool] | None:
        """Return an overlap (&&) clause for an ARRAY column, or None if tags is empty/None.

        Usage in subclass wrappers::

            clause = self._tags_clause(self.table.c.tags, tags)
            if clause is not None:
                extra_clauses = [clause]

        The right-hand side is cast to ARRAY(Text) so PostgreSQL can resolve the
        && operator without an explicit column type declaration.
        """
        if not tags:
            return None
        return tags_col.op("&&")(sa.cast(tags, sa.ARRAY(sa.Text)))

    # -------------------------------------------------------------------------
    # Full-text search
    # -------------------------------------------------------------------------

    async def search_fts(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        offset: int = 0,
        limit: int = 20,
        language: str = "english",
        skip_count: bool = False,
        extra_clauses: Sequence[ColumnElement[Any]] | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[list[Row], int]:
        """FTS search using DB-generated search_vector column + ts_rank scoring.

        Returns (rows, total) where total=0 when skip_count=True.
        skip_count=True avoids a second full-table @@ scan just for COUNT — pass it
        from callers that discard the total (e.g. pg_snippet, pg_runbook).

        extra_clauses: Additional WHERE expressions (generic extension point).
        NOTE: search paths do NOT filter archived rows — only list_all does.

        Raises AttributeError if table has no 'search_vector' column.
        """
        if "search_vector" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'search_vector' column.")

        async def _execute(sess: AsyncSession) -> tuple[list[Row], int]:
            tsquery = sa.func.plainto_tsquery(language, query)
            rank_expr = sa.func.ts_rank(self.table.c.search_vector, tsquery).label("rank")
            fts_condition = self.table.c.search_vector.op("@@")(tsquery)
            where_clauses: list[Any] = [fts_condition] + self._build_filter_clauses(filters or {})
            if extra_clauses:
                where_clauses.extend(extra_clauses)

            if skip_count:
                total = 0
            else:
                count_stmt = (
                    sa.select(sa.func.count())
                    .select_from(self.table)
                    .where(sa.and_(*where_clauses))
                )
                count_result = await sess.execute(count_stmt)
                total = count_result.scalar_one()

            stmt = (
                sa.select(*self._search_columns(), rank_expr)
                .where(sa.and_(*where_clauses))
                .order_by(sa.desc("rank"))
                .offset(offset)
                .limit(limit)
            )
            result = await sess.execute(stmt)
            rows = [dict(r) for r in result.mappings().all()]
            return rows, total

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    # -------------------------------------------------------------------------
    # Vector search
    # -------------------------------------------------------------------------

    async def search_vector(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        extra_clauses: Sequence[ColumnElement[Any]] | None = None,
        session: AsyncSession | None = None,
    ) -> list[Row]:
        """Semantic search using pgvector cosine distance (<=> operator).

        Returns rows ordered by cosine similarity (closest first).
        Each row includes a 'similarity' key = 1 - cosine_distance.
        Raises AttributeError if table has no 'embedding' column.

        Design: distance is computed via op('<=>',return_type=sa.Float) so the
        SA result processor returns a Python float directly — no CAST() in SQL
        (which would confuse the pgvector type system and break HNSW plans).
        Rule: NEVER put a Vector-typed expression in a SELECT projection.
        """
        from pgvector.sqlalchemy import Vector

        from brain_v42.db.tables import _EMBEDDING_DIM

        if "embedding" not in self.table.c:
            raise AttributeError(f"Table '{self.table.name}' has no 'embedding' column.")

        async def _execute(sess: AsyncSession) -> list[Row]:
            embedding_col = self.table.c.embedding
            query_vec = sa.cast(embedding, Vector(_EMBEDDING_DIM))

            # op("<=>", return_type=Float): changes only the Python result processor,
            # NOT the emitted SQL — no CAST() added, HNSW plan intact.
            # Rule: NEVER use .cast() on a Vector-typed expression in a projection.
            dist = embedding_col.op("<=>", return_type=sa.Float)(query_vec)
            similarity_expr = (sa.literal(1.0) - dist).label("similarity")

            where_clauses: list[Any] = [embedding_col.isnot(None)] + self._build_filter_clauses(
                filters or {}
            )
            if extra_clauses:
                where_clauses.extend(extra_clauses)

            stmt = (
                sa.select(*self._search_columns(), similarity_expr)
                .where(sa.and_(*where_clauses))
                .order_by(dist)
                .limit(limit)
            )
            result = await sess.execute(stmt)
            return [dict(r) for r in result.mappings().all()]

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)
