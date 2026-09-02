"""Repository for IndexedPlan and IndexedPlanChunk.

Raw SQL via SQLAlchemy async. Upserts plans and their chunks atomically.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.indexed_plan import IndexedPlan, IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import (
    IndexedPlanChunk,
    IndexedPlanChunkCreate,
)

# Max character count fed into ``to_tsvector``. PostgreSQL drops lexemes
# past the 1 MB ``tsvector`` limit and long inputs cause CPU spikes during
# indexing. 50 000 chars ~= 8000 words, more than any reasonable spec.
_FTS_INPUT_MAX_CHARS = 50_000


def _truncate_for_fts(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= _FTS_INPUT_MAX_CHARS:
        return value
    return value[:_FTS_INPUT_MAX_CHARS]


class PgIndexedPlanRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_plan_with_chunks(
        self,
        plan: IndexedPlanCreate,
        plan_embedding: list[float],
        chunks: list[IndexedPlanChunkCreate],
        chunk_embeddings: list[list[float]],
    ) -> UUID:
        """Upsert the plan by file_path and replace its chunks atomically.

        All three operations (plan upsert, chunk delete, chunk insert) run in a
        single logical transaction.  Any exception triggers ``session.rollback()``
        so the caller always receives a clean session state.

        Chunk inserts use a single ``executemany`` call (one round-trip regardless
        of chunk count) by passing a list of param-dicts to ``session.execute()``.
        SQLAlchemy routes this to asyncpg's native executemany, eliminating the
        N-round-trip loop that existed in the prior implementation.
        """
        assert len(chunks) == len(chunk_embeddings), "chunks and embeddings must align"

        upsert_sql = text("""
            INSERT INTO indexed_plans (
                file_path, title, plan_type, project_key, content_hash,
                embedding, content, summary, status, tags, metadata,
                chunk_count, word_count, freshness_status, freshness_source,
                indexed_at, search_vector
            ) VALUES (
                :file_path, :title, :plan_type, :project_key, :content_hash,
                :embedding, :content, :summary, :status, :tags,
                CAST(:metadata AS JSONB),
                :chunk_count, :word_count, 'fresh', 'plan_reindex', NOW(),
                to_tsvector('english', CAST(:title_fts AS TEXT) || ' ' || CAST(:content_fts AS TEXT))
            )
            ON CONFLICT (file_path) DO UPDATE SET
                title = EXCLUDED.title,
                plan_type = EXCLUDED.plan_type,
                project_key = EXCLUDED.project_key,
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding,
                content = EXCLUDED.content,
                summary = EXCLUDED.summary,
                status = EXCLUDED.status,
                tags = EXCLUDED.tags,
                metadata = EXCLUDED.metadata,
                chunk_count = EXCLUDED.chunk_count,
                word_count = EXCLUDED.word_count,
                freshness_status = 'fresh',
                -- Déclarée à CHAQUE écriture : le trigger de la 043 remet la
                -- provenance à NULL sinon. Un fichier archivé réédité repasse
                -- fresh par ICI — désarchivage légitime, désormais visible
                -- (ticket 55a21fb8 ; vocabulaire posé par la 049).
                freshness_source = 'plan_reindex',
                indexed_at = NOW(),
                search_vector = EXCLUDED.search_vector,
                updated_at = NOW()
            RETURNING id
        """)

        insert_chunk_sql = text("""
            INSERT INTO indexed_plan_chunks (
                plan_id, section_title, section_path, content,
                section_order, word_count, embedding, search_vector,
                tags, project_key, plan_type, status
            ) VALUES (
                :plan_id, :section_title, :section_path, :content,
                :section_order, :word_count, :embedding,
                to_tsvector('english',
                    CAST(:title_fts AS TEXT) || ' ' || CAST(:content_fts AS TEXT)
                ),
                :tags, :project_key, :plan_type, :status
            )
        """)

        try:
            result = await self._session.execute(
                upsert_sql,
                {
                    "file_path": plan.file_path,
                    "title": plan.title,
                    "plan_type": plan.plan_type,
                    "project_key": plan.project_key,
                    "content_hash": plan.content_hash,
                    "embedding": str(plan_embedding),
                    "content": plan.content,
                    "summary": plan.summary,
                    "status": plan.status,
                    "tags": plan.tags,
                    "metadata": json.dumps(plan.metadata),
                    "chunk_count": plan.chunk_count,
                    "word_count": plan.word_count,
                    "title_fts": _truncate_for_fts(plan.title),
                    "content_fts": _truncate_for_fts(plan.content),
                },
            )
            plan_id: UUID = result.scalar_one()

            # Replace chunks: delete existing ones first, then batch-insert new ones.
            await self._session.execute(
                text("DELETE FROM indexed_plan_chunks WHERE plan_id = :plan_id"),
                {"plan_id": plan_id},
            )

            if chunks:
                # Build one list of param-dicts and issue a single executemany call.
                # asyncpg (via SQLAlchemy) pipelines all rows in one round-trip.
                chunk_params: list[dict[str, Any]] = [
                    {
                        "plan_id": plan_id,
                        "section_title": chunk.section_title,
                        "section_path": chunk.section_path,
                        "content": chunk.content,
                        "section_order": chunk.section_order,
                        "word_count": chunk.word_count,
                        "embedding": str(emb),
                        "tags": chunk.tags,
                        "project_key": chunk.project_key,
                        "plan_type": chunk.plan_type,
                        "status": chunk.status,
                        "title_fts": _truncate_for_fts(chunk.section_title),
                        "content_fts": _truncate_for_fts(chunk.content),
                    }
                    for chunk, emb in zip(chunks, chunk_embeddings, strict=True)
                ]
                await self._session.execute(insert_chunk_sql, chunk_params)

        except Exception:
            await self._session.rollback()
            raise

        await self._session.commit()
        return plan_id

    async def get_with_chunks(
        self, plan_id: UUID, *, project_key: str | None = None
    ) -> tuple[IndexedPlan, list[IndexedPlanChunk]] | None:
        """Fetch a plan and its ordered chunks. Returns None if plan not found."""
        plan_sql = "SELECT * FROM indexed_plans WHERE id = :id"
        plan_params: dict[str, Any] = {"id": plan_id}
        if project_key is not None:
            plan_sql += " AND project_key = :project_key"
            plan_params["project_key"] = project_key
        plan_row = (
            (
                await self._session.execute(
                    text(plan_sql),
                    plan_params,
                )
            )
            .mappings()
            .first()
        )

        if plan_row is None:
            return None

        chunks_sql = "SELECT * FROM indexed_plan_chunks WHERE plan_id = :id"
        chunks_params: dict[str, Any] = {"id": plan_id}
        if project_key is not None:
            chunks_sql += " AND project_key = :project_key"
            chunks_params["project_key"] = project_key
        chunks_sql += " ORDER BY section_order ASC"
        chunk_rows = (
            (
                await self._session.execute(
                    text(chunks_sql),
                    chunks_params,
                )
            )
            .mappings()
            .all()
        )

        plan = IndexedPlan(**dict(plan_row))
        chunks = [IndexedPlanChunk(**dict(row)) for row in chunk_rows]
        return plan, chunks

    async def delete(self, plan_id: UUID, *, project_key: str | None = None) -> bool:
        """Delete a plan by id. Chunks are removed via ON DELETE CASCADE.

        Returns True if a row was deleted, False if not found.
        """
        if project_key is None:
            result = await self._session.execute(
                text("DELETE FROM indexed_plans WHERE id = :id"),
                {"id": plan_id},
            )
            await self._session.commit()
            rowcount: int = result.rowcount  # type: ignore[attr-defined]
            return rowcount > 0

        params = {"id": plan_id, "project_key": project_key}
        try:
            target = await self._session.execute(
                text(
                    "SELECT id FROM indexed_plans "
                    "WHERE id = :id AND project_key = :project_key FOR UPDATE"
                ),
                params,
            )
            if target.scalar_one_or_none() is None:
                await self._session.rollback()
                return False

            chunk_locks = await self._session.execute(
                text(
                    "SELECT id, project_key FROM indexed_plan_chunks WHERE plan_id = :id FOR UPDATE"
                ),
                {"id": plan_id},
            )
            chunk_rows = chunk_locks.mappings().all()
            if any(row["project_key"] != project_key for row in chunk_rows):
                await self._session.rollback()
                return False

            deleted = await self._session.execute(
                text(
                    "DELETE FROM indexed_plans "
                    "WHERE id = :id AND project_key = :project_key RETURNING id"
                ),
                params,
            )
            await self._session.commit()
            return deleted.scalar_one_or_none() is not None
        except Exception:
            await self._session.rollback()
            raise

    async def list_plans(
        self,
        project_key: str | None = None,
        plan_type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[IndexedPlan]:
        """List indexed plans with optional filters, ordered by updated_at DESC."""
        clauses = ["1=1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if project_key is not None:
            clauses.append("project_key = :project_key")
            params["project_key"] = project_key
        if plan_type is not None:
            clauses.append("plan_type = :plan_type")
            params["plan_type"] = plan_type
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        where = " AND ".join(clauses)
        sql = text(f"""
            SELECT * FROM indexed_plans
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT :limit OFFSET :offset
        """)  # noqa: S608  # nosec B608 - fragment = `where`, a join of the only literals appended to `clauses` 12 lines above in this same function; project_key/plan_type/status/limit/offset reach the SQL only through the :project_key/:plan_type/:status/:limit/:offset binds; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
        rows = (await self._session.execute(sql, params)).mappings().all()
        return [IndexedPlan(**dict(row)) for row in rows]
