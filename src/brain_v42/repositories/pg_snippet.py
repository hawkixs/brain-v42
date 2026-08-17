"""PostgreSQL repository for Snippet entities.

Subclasses BasePgRepository (SQLAlchemy Core async).
Operates at the Pydantic model layer — accepts SnippetCreate/SnippetUpdate,
returns Snippet instances (or None/bool/list[Snippet]).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import snippets
from brain_v42.models.snippet import Snippet, SnippetCreate, SnippetUpdate
from brain_v42.repositories.pg_base import BasePgRepository, Row

logger = structlog.get_logger(__name__)


class PgSnippetRepo(BasePgRepository):
    """Repository for snippets table.

    All public methods accept/return Pydantic Snippet models.
    The base class handles raw dicts — this class wraps them.
    """

    table = snippets
    fts_columns: list[str] = []  # search_vector is DB-generated

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _row_to_model(self, row: Row) -> Snippet:
        """Convert a raw DB row dict to a Snippet Pydantic model."""
        return Snippet.model_validate(row)

    def _create_data_to_dict(
        self, data: SnippetCreate, embedding: list[float] | None = None
    ) -> dict[str, Any]:
        """Serialize SnippetCreate to a dict suitable for DB insert."""
        d = data.model_dump()
        if embedding is not None:
            d["embedding"] = embedding
        return d

    def _update_data_to_dict(
        self, data: SnippetUpdate, embedding: list[float] | None = None
    ) -> dict[str, Any]:
        """Serialize SnippetUpdate to a dict suitable for DB update (excludes None fields)."""
        d = data.model_dump(exclude_none=True)
        if embedding is not None:
            d["embedding"] = embedding
        return d

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------

    async def create(  # type: ignore[override]
        self,
        data: SnippetCreate,
        embedding: list[float] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> Snippet:
        """Insert a new snippet and return it as a Snippet model."""
        payload = self._create_data_to_dict(data, embedding)
        row = await super().create(payload, session=session)
        return self._row_to_model(row)

    async def get_by_id(  # type: ignore[override]
        self,
        id: uuid.UUID | str,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Snippet | None:
        """Fetch a snippet by primary key. Returns None if not found."""
        if project_key is None:
            row = await super().get_by_id(id, session=session)
        else:
            row = await self.find_one(
                {"id": id, "project_key": project_key},
                session=session,
            )
        return self._row_to_model(row) if row is not None else None

    async def update(  # type: ignore[override]
        self,
        id: uuid.UUID | str,
        data: SnippetUpdate,
        embedding: list[float] | None = None,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Snippet | None:
        """Partial update on a snippet. Returns updated Snippet or None if not found."""
        payload = self._update_data_to_dict(data, embedding)
        if not payload:
            # Nothing to update — just return the current row
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
                snippets.update()
                .where(
                    snippets.c.id == id,
                    snippets.c.project_key == project_key,
                )
                .values(**payload, updated_at=datetime.now(UTC))
                .returning(snippets)
            )
            async with self._maybe_session(session, write=True) as sess:
                result = await sess.execute(stmt)
                mapping = result.mappings().one_or_none()
                row = dict(mapping) if mapping is not None else None
        return self._row_to_model(row) if row is not None else None

    async def delete(
        self,
        id: uuid.UUID | str,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete a snippet by id. Returns True if deleted, False if not found."""
        if project_key is None:
            return await super().delete(id, session=session)
        stmt = (
            snippets.delete()
            .where(
                snippets.c.id == id,
                snippets.c.project_key == project_key,
            )
            .returning(snippets.c.id)
        )
        async with self._maybe_session(session, write=True) as sess:
            return (await sess.execute(stmt)).one_or_none() is not None

    # -------------------------------------------------------------------------
    # List
    # -------------------------------------------------------------------------

    async def list_all(  # type: ignore[override]
        self,
        project_key: str | None = None,
        language: str | None = None,
        limit: int = 20,
        offset: int = 0,
        order_by_use_count: bool = False,
        include_archived: bool = False,
        *,
        session: AsyncSession | None = None,
    ) -> list[Snippet]:
        """List snippets with optional project_key/language filters.

        When order_by_use_count=True, sorts by use_count DESC (most used first).
        Otherwise sorts by created_at DESC.
        When include_archived=False (default), excludes merged (merged_into IS NOT NULL)
        and archived (freshness_status = 'archived') snippets.
        """
        stmt = sa.select(snippets)
        if project_key is not None:
            stmt = stmt.where(snippets.c.project_key == project_key)
        if language is not None:
            stmt = stmt.where(snippets.c.language == language)
        if not include_archived:
            stmt = stmt.where(snippets.c.merged_into.is_(None))
            stmt = stmt.where(snippets.c.freshness_status != "archived")
        order_col = snippets.c.use_count if order_by_use_count else snippets.c.created_at
        stmt = stmt.order_by(order_col.desc()).limit(limit).offset(offset)

        async def _execute(sess: AsyncSession) -> list[Snippet]:
            result = await sess.execute(stmt)
            return [self._row_to_model(dict(row)) for row in result.mappings().all()]

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            return await _execute(sess)

    # -------------------------------------------------------------------------
    # Full-text search
    # -------------------------------------------------------------------------

    async def search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        language: str | None = None,
        limit: int = 20,
        offset: int = 0,
        *,
        session: AsyncSession | None = None,
    ) -> list[Snippet]:
        """FTS search over snippets using the DB-generated search_vector column."""
        filters: dict[str, Any] = {
            "language": language,
        }
        if project_keys is not None:
            filters["project_key"] = project_keys
        else:
            filters["project_key"] = project_key
        # skip_count=True: the total is discarded here — avoids a second full @@ scan
        rows, _total = await super().search_fts(
            query,
            filters=filters,
            offset=offset,
            limit=limit,
            session=session,
            skip_count=True,
        )
        return [self._row_to_model(r) for r in rows]

    # -------------------------------------------------------------------------
    # Vector / semantic search
    # -------------------------------------------------------------------------

    async def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        language: str | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> list[tuple[Snippet, float]]:
        """Semantic search using pgvector cosine similarity.

        Returns list of (Snippet, similarity_score) tuples ordered by similarity DESC.
        """
        filters: dict[str, Any] = {
            "language": language,
        }
        if project_keys is not None:
            filters["project_key"] = project_keys
        else:
            filters["project_key"] = project_key
        rows = await super().search_vector(
            query_embedding,
            filters=filters,
            limit=limit,
            session=session,
        )
        result: list[tuple[Snippet, float]] = []
        for row in rows:
            similarity: float = float(row.pop("similarity", 0.0))
            snippet = self._row_to_model(row)
            result.append((snippet, similarity))
        return result

    # -------------------------------------------------------------------------
    # Atomic increment use_count
    # -------------------------------------------------------------------------

    async def increment_use(
        self,
        id: uuid.UUID | str,
        *,
        session: AsyncSession | None = None,
    ) -> Snippet | None:
        """Atomically increment use_count by 1 and set last_used_at = NOW().

        Returns the updated Snippet, or None if not found.
        """

        async def _execute(sess: AsyncSession) -> Snippet | None:
            stmt = (
                self.table.update()
                .where(self.table.c.id == id)
                .values(
                    use_count=self.table.c.use_count + sa.literal(1),
                    last_used_at=sa.func.now(),
                    updated_at=datetime.now(UTC),
                )
                .returning(self.table)
            )
            result = await sess.execute(stmt)
            row = result.mappings().one_or_none()
            if row is None:
                logger.debug("repository.increment_use.not_found", table=self.table.name, id=id)
                return None
            logger.debug("repository.increment_use", table=self.table.name, id=id)
            return self._row_to_model(dict(row))

        if session is not None:
            return await _execute(session)
        async with self.get_session() as sess:
            async with sess.begin():
                return await _execute(sess)
