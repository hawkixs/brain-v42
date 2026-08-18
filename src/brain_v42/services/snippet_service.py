"""SnippetService — business logic layer for snippet management.

Sits between MCP tools and PgSnippetRepo. New snippets commit before bounded
embedding enrichment. No SQL, no SQLAlchemy, no session management here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from brain_v42.models.snippet import Snippet, SnippetCreate, SnippetUpdate
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.embedding_text import snippet_embedding_text
from brain_v42.services.graph_helpers import (
    RelationAuthorization,
    auto_link_if_enabled,
    graph_delete_entity,
    graph_upsert_entity,
    link_artifact_if_enabled,
)
from brain_v42.services.project_guard import require_known_project

if TYPE_CHECKING:
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo

logger = structlog.get_logger(__name__)


class SnippetService:
    """Service layer for snippet CRUD, semantic search, and use tracking.

    Design:
    - Thin orchestrator: no SQL, no SA, no session management.
    - Persists create before best-effort embedding enrichment.
    - Embeds snippet.intention on update via embedding_svc.embed().
    - Delegates all persistence to PgSnippetRepo.
    - Embedding regenerated on update only when intention field changes.
    """

    def __init__(
        self,
        repo: PgSnippetRepo,
        embedding_svc: Any | None = None,
        feature_linker: Any | None = None,
        graph: Any | None = None,
        auto_linker: Any | None = None,
        embedding_enricher: EmbeddingEnrichmentService | None = None,
        project_context_repo: PgProjectContextRepo | None = None,
    ) -> None:
        """Initialize SnippetService with injected dependencies.

        Args:
            repo: PgSnippetRepo instance for DB operations.
            embedding_svc: Duck-typed embedding service — `embed(text)` (documents,
                on write) and `embed_query(text)` (searches), both async and
                returning `list[float]`.
            feature_linker: Optional FeatureLinker for auto-linking artifacts to features.
            graph: Optional GraphService for Neo4j write-through. When provided, create/delete
                   operations are mirrored to Neo4j. Graph failures are caught and logged.
        """
        self._repo = repo
        self._embedding_svc = embedding_svc
        self._embedding_enricher = embedding_enricher
        if self._embedding_enricher is None and embedding_svc is not None:
            self._embedding_enricher = EmbeddingEnrichmentService(embedding_svc)
        self._feature_linker = feature_linker
        self._graph = graph
        self._auto_linker = auto_linker
        self._project_context_repo = project_context_repo

    async def create(
        self,
        data: SnippetCreate,
        related_to: list[dict] | None = None,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> Snippet:
        """Create a durable snippet, then enrich its embedding when possible.

        Args:
            data: SnippetCreate payload.
            related_to: Optional list of dicts with keys ``id`` (UUID string)
                and ``type`` (relation label, e.g. ``"IMPLEMENTS"``).
                When provided and a graph is configured, relations are created
                in Neo4j after the PG write.

        Returns:
            The durable Snippet, enriched when the bounded attempt succeeds.
        """
        logger.debug(
            "snippet_service.create",
            title_length=len(data.title),
            language_supplied=bool(data.language),
        )
        await require_known_project(self._project_context_repo, data.project_key)

        result = await self._repo.create(data, embedding=None)

        await graph_upsert_entity(
            self._graph,
            "Snippet",
            result.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            related_to=related_to,
            authorization=authorization,
        )

        if self._embedding_enricher is None:
            return result

        enrichment = await self._embedding_enricher.enrich(
            repo=self._repo,
            entity_type="snippet",
            entity_id=result.id,
            text=snippet_embedding_text(data.intention),
            expected_updated_at=result.updated_at,
        )
        if enrichment.status is EnrichmentStatus.STORED and enrichment.embedding is not None:
            await link_artifact_if_enabled(
                self._feature_linker,
                enrichment.embedding,
                "snippet",
                result.id,
                data.project_key,
                data.title,
            )
            await auto_link_if_enabled(
                self._auto_linker,
                "Snippet",
                result.id,
                enrichment.embedding,
                authorization=authorization,
            )
            if enrichment.row is not None:
                return Snippet.model_validate(enrichment.row)

        return result

    async def get_by_id(
        self,
        id: uuid.UUID,
        *,
        project_key: str | None = None,
    ) -> Snippet | None:
        """Fetch a snippet by primary key.

        Args:
            id: Snippet UUID.

        Returns:
            Snippet if found, None otherwise.
        """
        logger.debug("snippet_service.get_by_id", id=str(id))
        if project_key is None:
            return await self._repo.get_by_id(id)
        return await self._repo.get_by_id(id, project_key=project_key)

    async def resolve_id_prefix(self, prefix_hex: str) -> list[uuid.UUID]:
        """Resolve a git-style short id prefix to matching snippet ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def update(
        self,
        id: uuid.UUID,
        data: SnippetUpdate,
        *,
        project_key: str | None = None,
    ) -> Snippet | None:
        """Partial update a snippet, regenerating embedding only if intention changed.

        Args:
            id: UUID of the snippet to update.
            data: SnippetUpdate payload (None fields are not updated).

        Returns:
            Updated Snippet, or None if not found.
        """
        logger.debug("snippet_service.update", id=str(id))
        embedding: list[float] | None = None
        if data.intention is not None and self._embedding_svc is not None:
            embedding = await self._embedding_svc.embed(snippet_embedding_text(data.intention))
        if project_key is None:
            return await self._repo.update(id, data, embedding=embedding)
        return await self._repo.update(
            id,
            data,
            embedding=embedding,
            project_key=project_key,
        )

    async def delete(
        self,
        id: uuid.UUID,
        *,
        project_key: str | None = None,
    ) -> bool:
        """Delete a snippet by id.

        Args:
            id: UUID of the snippet to delete.

        Returns:
            True if deleted, False if not found.
        """
        logger.debug("snippet_service.delete", id=str(id))
        if project_key is None:
            result = await self._repo.delete(id)
            await graph_delete_entity(self._graph, "Snippet", id)
            return result
        result = await self._repo.delete(id, project_key=project_key)
        if result:
            await graph_delete_entity(
                self._graph,
                "Snippet",
                id,
                project_key=project_key,
            )
        return result

    async def list_snippets(
        self,
        project_key: str | None = None,
        language: str | None = None,
        limit: int = 20,
        offset: int = 0,
        order_by_use_count: bool = False,
        include_archived: bool = False,
    ) -> list[Snippet]:
        """List snippets with optional filters.

        Args:
            project_key: Filter by project key.
            language: Filter by programming language.
            limit: Maximum number of results.
            offset: Pagination offset.
            order_by_use_count: If True, sort by use_count DESC; otherwise by created_at DESC.
            include_archived: When False (default), exclude merged/archived snippets.

        Returns:
            List of matching Snippet instances.
        """
        logger.debug(
            "snippet_service.list_snippets",
            project_key=project_key,
            language_filter=language is not None,
            limit=limit,
            offset=offset,
        )
        return await self._repo.list_all(
            project_key=project_key,
            language=language,
            limit=limit,
            offset=offset,
            order_by_use_count=order_by_use_count,
            include_archived=include_archived,
        )

    async def search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        language: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Snippet]:
        """Full-text search snippets via ts_rank.

        Args:
            query: Search query string.
            project_key: Optional project scope filter.
            project_keys: Optional list of project keys for group filtering.
            language: Optional programming language filter.
            limit: Maximum number of results.
            offset: Pagination offset.

        Returns:
            List of matching Snippet instances ordered by FTS rank.
        """
        logger.debug("snippet_service.search", query_length=len(query), limit=limit)
        return await self._repo.search(
            query=query,
            project_key=project_key,
            project_keys=project_keys,
            language=language,
            limit=limit,
            offset=offset,
        )

    async def semantic_search(
        self,
        query: str,
        limit: int = 10,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        language: str | None = None,
        embedding: list[float] | None = None,
    ) -> list[tuple[Snippet, float]]:
        """Semantic search over snippets by intention similarity.

        When a pre-computed ``embedding`` is provided, skips the embed() call.
        The language filter is applied as a SQL WHERE clause (not post-filter).

        Args:
            query: Natural language query to find relevant snippets.
            limit: Maximum number of results.
            project_key: Optional project key filter.
            language: Optional programming language filter (SQL-level, not post-filter).
            embedding: Optional pre-computed embedding vector (skips embed() call).

        Returns:
            List of (Snippet, similarity_score) tuples ordered by similarity DESC.
        """
        logger.debug(
            "snippet_service.semantic_search",
            query_length=len(query),
            limit=limit,
            language_filter=language is not None,
        )
        if embedding is None:
            if self._embedding_svc is None:
                raise ValueError("embedding_svc is required for semantic search")
            embedding = await self._embedding_svc.embed_query(query)
        return await self._repo.vector_search(
            embedding,
            limit=limit,
            project_key=project_key,
            project_keys=project_keys,
            language=language,
        )

    async def increment_use(self, id: uuid.UUID) -> Snippet | None:
        """Atomically increment the use_count of a snippet.

        Args:
            id: UUID of the snippet to increment.

        Returns:
            Updated Snippet with new use_count and last_used_at, or None if not found.
        """
        logger.debug("snippet_service.increment_use", id=str(id))
        return await self._repo.increment_use(id)
