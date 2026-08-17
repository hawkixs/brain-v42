"""Search service over indexed_plan_chunks.

Provides `search` (FTS) and `semantic_search` (vector) methods compatible
with HybridSearcher's pluggable search_fn parameters.

- search(): returns list[IndexedPlanChunk] (plain entities, ranked by FTS score)
  matching the fts_search_fn interface expected by HybridSearcher.
- semantic_search(): returns list[tuple[IndexedPlanChunk, float]]
  matching the vector_search_fn interface expected by HybridSearcher.

Filters status='active' by default (drafts excluded unless include_drafts=True).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk


class IndexedPlanSearchService:
    """Search over indexed_plan_chunks via FTS and pgvector cosine similarity."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sf = session_factory

    async def search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 10,
        tags: list[str] | None = None,
        include_drafts: bool = False,
        include_archived: bool = False,
        **kwargs: Any,  # absorb extra kwargs passed by HybridSearcher
    ) -> list[IndexedPlanChunk]:
        """Full-text search over indexed_plan_chunks.

        Returns plain entities (list[IndexedPlanChunk]) ranked by ts_rank,
        matching the fts_search_fn interface expected by HybridSearcher.

        Filters status='active' by default (drafts excluded).
        """
        pairs = await self._run_fts(
            query=query,
            project_key=project_key,
            project_keys=project_keys,
            limit=limit,
            tags=tags,
            include_drafts=include_drafts,
            include_archived=include_archived,
        )
        return [chunk for chunk, _score in pairs]

    async def semantic_search(
        self,
        query: str | None = None,
        embedding: list[float] | None = None,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 10,
        tags: list[str] | None = None,
        include_drafts: bool = False,
        include_archived: bool = False,
        **kwargs: Any,
    ) -> list[tuple[IndexedPlanChunk, float]]:
        """Vector search over indexed_plan_chunks via pgvector cosine similarity.

        Returns list[tuple[IndexedPlanChunk, float]] matching the
        vector_search_fn interface expected by HybridSearcher.

        Returns empty list if no embedding is provided.
        """
        if embedding is None:
            return []
        return await self._run_vector(
            embedding=embedding,
            project_key=project_key,
            project_keys=project_keys,
            limit=limit,
            tags=tags,
            include_drafts=include_drafts,
            include_archived=include_archived,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_where(
        self,
        params: dict[str, Any],
        project_key: str | None,
        project_keys: list[str] | None,
        tags: list[str] | None,
        include_drafts: bool,
        include_archived: bool,
    ) -> list[str]:
        """Build WHERE clause fragments and populate params dict."""
        clauses: list[str] = ["1=1"]

        if not include_drafts:
            clauses.append("c.status = 'active'")

        if not include_archived:
            clauses.append("p.freshness_status != 'archived'")

        if project_key is not None:
            clauses.append("c.project_key = :project_key")
            params["project_key"] = project_key
        elif project_keys:
            clauses.append("c.project_key = ANY(CAST(:pks AS VARCHAR[]))")
            params["pks"] = project_keys

        if tags:
            clauses.append("c.tags && CAST(:tags AS VARCHAR[])")
            params["tags"] = tags

        return clauses

    async def _run_fts(
        self,
        *,
        query: str,
        project_key: str | None,
        project_keys: list[str] | None,
        limit: int,
        tags: list[str] | None,
        include_drafts: bool,
        include_archived: bool,
    ) -> list[tuple[IndexedPlanChunk, float]]:
        params: dict[str, Any] = {"limit": limit, "q": query}
        clauses = self._build_where(
            params,
            project_key,
            project_keys,
            tags,
            include_drafts,
            include_archived,
        )
        clauses.append("c.search_vector @@ plainto_tsquery('english', :q)")
        where = " AND ".join(clauses)

        sql = f"""
            SELECT c.id, c.plan_id, c.section_title, c.section_path, c.content,
                   c.section_order, c.word_count, c.tags, c.project_key,
                   c.plan_type, c.status, c.access_count, c.last_accessed_at,
                   c.created_at,
                   p.access_count AS parent_access_count,
                   p.last_accessed_at AS parent_last_accessed_at,
                   p.freshness_status AS parent_freshness_status,
                   p.created_at AS parent_created_at,
                   ts_rank(c.search_vector, plainto_tsquery('english', :q)) AS score
            FROM indexed_plan_chunks c
            JOIN indexed_plans p ON p.id = c.plan_id AND p.project_key = c.project_key
            WHERE {where}
            ORDER BY score DESC
            LIMIT :limit
        """  # nosec B608 - fragment = `where`, join des seuls littéraux retournés par _build_where (aucune valeur d'appel n'y entre) ; le texte de recherche utilisateur passe par le bind :q, project_key/:pks/:tags idem ; exception revue le 2026-08-16, à réexaminer avant le 2026-09-30

        return await self._execute_query(sql, params)

    async def _run_vector(
        self,
        *,
        embedding: list[float],
        project_key: str | None,
        project_keys: list[str] | None,
        limit: int,
        tags: list[str] | None,
        include_drafts: bool,
        include_archived: bool,
    ) -> list[tuple[IndexedPlanChunk, float]]:
        params: dict[str, Any] = {"limit": limit, "emb": str(embedding)}
        clauses = self._build_where(
            params,
            project_key,
            project_keys,
            tags,
            include_drafts,
            include_archived,
        )
        where = " AND ".join(clauses)

        sql = f"""
            SELECT c.id, c.plan_id, c.section_title, c.section_path, c.content,
                   c.section_order, c.word_count, c.tags, c.project_key,
                   c.plan_type, c.status, c.access_count, c.last_accessed_at,
                   c.created_at,
                   p.access_count AS parent_access_count,
                   p.last_accessed_at AS parent_last_accessed_at,
                   p.freshness_status AS parent_freshness_status,
                   p.created_at AS parent_created_at,
                   1 - (c.embedding <=> CAST(:emb AS VECTOR)) AS score
            FROM indexed_plan_chunks c
            JOIN indexed_plans p ON p.id = c.plan_id AND p.project_key = c.project_key
            WHERE {where}
            ORDER BY c.embedding <=> CAST(:emb AS VECTOR)
            LIMIT :limit
        """  # nosec B608 - fragment = `where`, join des seuls littéraux retournés par _build_where (aucune valeur d'appel n'y entre) ; l'embedding passe par le bind :emb, project_key/:pks/:tags idem ; exception revue le 2026-08-16, à réexaminer avant le 2026-09-30

        return await self._execute_query(sql, params)

    async def _execute_query(
        self,
        sql: str,
        params: dict[str, Any],
    ) -> list[tuple[IndexedPlanChunk, float]]:
        async with self._sf() as session:
            result = await session.execute(text(sql), params)
            rows = result.mappings().all()

        out: list[tuple[IndexedPlanChunk, float]] = []
        for row in rows:
            row_dict = dict(row)
            score = float(row_dict.pop("score"))
            chunk = IndexedPlanChunk(**row_dict)
            out.append((chunk, score))
        return out
