"""Service layer for ADR (Architecture Decision Record) management.

Business logic:
- create(): optional embedding via embedding service, delegates to repo.create()
- accept(): delegates to repo.accept() — sets status='accepted' + decided_at=now()
- search(): FTS via repo.search() with optional filters
- semantic_search(): pgvector via repo.vector_search() — requires embedding_svc
- list_all(): filtered listing with status/project_key
- get_by_id(): direct fetch by UUID
- get_by_number(): fetch by project-scoped number

Design:
- Thin orchestrator: no SQL, no SQLAlchemy, no session management.
- embedding_svc is optional (None by default) for use without ONNX runtime.
- Delegates all persistence to PgADRRepo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from brain_v42.models.adr import ADR, ADRCreate, ADRUpdate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.embedding_text import adr_embedding_text
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


class ADRService:
    """Service layer for ADR lifecycle management.

    Orchestrates between MCP tools and PgADRRepo, optionally generating
    embeddings via the embedding service when provided.

    Args:
        pg_repo: PgADRRepo instance for all DB operations.
        embedding_svc: Optional embedding service for semantic search support.
                       When None, semantic_search() returns [] and create() stores no embedding.
    """

    def __init__(
        self,
        pg_repo: PgADRRepo,
        embedding_svc: Any = None,
        feature_linker: Any | None = None,
        graph: Any | None = None,
        auto_linker: Any | None = None,
        embedding_enricher: EmbeddingEnrichmentService | None = None,
        project_context_repo: PgProjectContextRepo | None = None,
    ) -> None:
        self._repo = pg_repo
        self._embedding_svc = embedding_svc
        self._embedding_enricher = embedding_enricher
        if self._embedding_enricher is None and embedding_svc is not None:
            self._embedding_enricher = EmbeddingEnrichmentService(embedding_svc)
        self._feature_linker = feature_linker
        self._graph = graph
        self._auto_linker = auto_linker
        self._project_context_repo = project_context_repo

    async def _maybe_embed(self, text: str) -> list[float] | None:
        """Generate embedding if embedding_svc is available, else return None."""
        if self._embedding_svc is None:
            return None
        result: list[float] = await self._embedding_svc.embed(text)
        return result

    async def create(
        self,
        data: ADRCreate,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> ADR:
        """Create a durable ADR, then attempt bounded embedding enrichment.

        Auto-number assignment and the null-vector insert are handled by the
        repo before derived work starts.

        Args:
            data: ADRCreate payload.

        Returns:
            The created ADR (with id, number, timestamps).
        """
        await require_known_project(self._project_context_repo, data.project_key)

        embed_text = adr_embedding_text(data.title, data.context, data.decision)
        adr = await self._repo.create(data, embedding=None)
        logger.info(
            "adr_service.create",
            adr_id=str(adr.id),
            number=adr.number,
            project_key=data.project_key,
        )

        await graph_upsert_entity(
            self._graph,
            "ADR",
            adr.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            authorization=authorization,
        )
        return await self._enrich_created_adr(
            adr,
            data,
            embed_text,
            authorization=authorization,
        )

    async def create_with_promotion(
        self,
        data: ADRCreate,
        source_learning_id: UUID,
        auto_accept: bool,
        dream_run_id: int | None = None,
        *,
        project_key: str | None = None,
        authorization: RelationAuthorization | None = None,
    ) -> ADR:
        """Create an ADR + atomically record promotion from a source learning.

        Repo owns the PG transaction (adr + learnings.metadata + dream_promotions
        rows commit together). Graph upsert + feature-link + auto-link run
        post-commit via graph_helpers, which swallow their own exceptions — a
        Neo4j outage never rolls back the PG writes.
        """
        embed_text = adr_embedding_text(data.title, data.context, data.decision)

        if project_key is None:
            adr = await self._repo.create_with_promotion(
                data=data,
                embedding=None,
                source_learning_id=source_learning_id,
                auto_accept=auto_accept,
                dream_run_id=dream_run_id,
            )
        else:
            adr = await self._repo.create_with_promotion(
                data=data,
                embedding=None,
                source_learning_id=source_learning_id,
                auto_accept=auto_accept,
                dream_run_id=dream_run_id,
                project_key=project_key,
            )
        logger.info(
            "adr_service.create_with_promotion",
            adr_id=str(adr.id),
            source_learning_id=str(source_learning_id),
            auto_accept=auto_accept,
        )

        await graph_upsert_entity(
            self._graph,
            "ADR",
            adr.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            authorization=authorization,
        )
        return await self._enrich_created_adr(
            adr,
            data,
            embed_text,
            authorization=authorization,
        )

    async def _enrich_created_adr(
        self,
        adr: ADR,
        data: ADRCreate,
        embed_text: str,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> ADR:
        """Enrich and link an ADR whose authoritative transaction committed."""
        if self._embedding_enricher is None:
            return adr

        enrichment = await self._embedding_enricher.enrich(
            repo=self._repo,
            entity_type="adr",
            entity_id=adr.id,
            text=embed_text,
            expected_updated_at=adr.updated_at,
        )
        if enrichment.status is not EnrichmentStatus.STORED or enrichment.embedding is None:
            return adr

        await link_artifact_if_enabled(
            self._feature_linker,
            enrichment.embedding,
            "adr",
            adr.id,
            data.project_key,
            data.title,
        )
        await auto_link_if_enabled(
            self._auto_linker,
            "ADR",
            adr.id,
            enrichment.embedding,
            authorization=authorization,
        )
        if enrichment.row is not None:
            return ADR.model_validate(enrichment.row)
        return adr

    async def accept(self, adr_id: UUID) -> ADR | None:
        """Accept an ADR by setting status='accepted' and decided_at=now().

        Delegates atomically to repo.accept().

        Args:
            adr_id: UUID of the ADR to accept.

        Returns:
            Updated ADR if found, None otherwise.
        """
        adr = await self._repo.accept(adr_id)
        if adr is not None:
            logger.info("adr_service.accept", adr_id=str(adr_id))
        return adr

    async def search(
        self,
        query: str | None = None,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ADR]:
        """Search ADRs using FTS and optional filters.

        Args:
            query: Free-text search string (FTS, optional).
            project_key: Filter by project (optional).
            project_keys: Optional list of project keys for group filtering.
            status: Filter by status e.g. 'proposed', 'accepted' (optional).
            tags: Filter by tags overlap (optional).
            limit: Maximum number of results (default 20).
            offset: Pagination offset (default 0).

        Returns:
            List of ADR models.
        """
        return await self._repo.search(
            query=query,
            project_key=project_key,
            project_keys=project_keys,
            status=status,
            tags=tags,
            limit=limit,
            offset=offset,
        )

    async def semantic_search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 10,
        embedding: list[float] | None = None,
    ) -> list[tuple[ADR, float]]:
        """Semantic search using pgvector cosine similarity.

        When a pre-computed ``embedding`` is provided, skips the embed() call.
        Returns [] if no embedding_svc configured and no embedding provided.

        Args:
            query: Natural language query string.
            project_key: Optional project filter.
            limit: Maximum number of results (default 10).
            embedding: Optional pre-computed embedding vector.

        Returns:
            List of (ADR, similarity_score) tuples, or [] if no embedding_svc.
        """
        if embedding is None:
            if self._embedding_svc is None:
                logger.warning("adr_service.semantic_search.no_embedding_svc")
                return []
            embedding = await self._embedding_svc.embed_query(query)
        return await self._repo.vector_search(
            query_embedding=embedding,
            limit=limit,
            project_key=project_key,
            project_keys=project_keys,
        )

    async def list_all(
        self,
        project_key: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[ADR]:
        """List ADRs with optional filters and pagination.

        Args:
            project_key: Filter by project (optional).
            status: Filter by status (optional).
            limit: Maximum number of results (default 20).
            offset: Pagination offset (default 0).
            include_archived: When False (default), exclude merged/archived ADRs.

        Returns:
            List of ADR models ordered by number DESC.
        """
        return await self._repo.list_all(
            project_key=project_key,
            status=status,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    async def get_by_id(
        self,
        adr_id: UUID,
        *,
        project_key: str | None = None,
    ) -> ADR | None:
        """Fetch a single ADR by its UUID.

        Args:
            adr_id: UUID of the ADR.

        Returns:
            ADR if found, None otherwise.
        """
        if project_key is None:
            return await self._repo.get_by_id(adr_id)
        return await self._repo.get_by_id(adr_id, project_key=project_key)

    async def resolve_id_prefix(self, prefix_hex: str) -> list[UUID]:
        """Resolve a git-style short id prefix to matching ADR ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def get_by_number(self, number: int, project_key: str) -> ADR | None:
        """Fetch a single ADR by its project-scoped number.

        Args:
            number: ADR number within the project.
            project_key: Project key the ADR belongs to.

        Returns:
            ADR if found, None otherwise.
        """
        return await self._repo.get_by_number(number, project_key)

    async def deprecate(self, adr_id: UUID, reason: str | None = None) -> ADR | None:
        """Deprecate an ADR by setting status='deprecated'.

        Optionally appends a deprecation reason to the consequences field.

        Args:
            adr_id: UUID of the ADR to deprecate.
            reason: Optional reason for deprecation.

        Returns:
            Updated ADR if found, None otherwise.
        """
        existing = await self._repo.get_by_id(adr_id)
        if existing is None:
            return None

        consequences = existing.consequences
        if reason:
            consequences = f"{existing.consequences}\n\nDeprecated: {reason}".strip()

        data = ADRUpdate(title=None, status="deprecated", consequences=consequences)
        return await self._repo.update(adr_id, data)

    async def delete(
        self,
        adr_id: UUID,
        *,
        project_key: str | None = None,
    ) -> bool:
        """Delete an ADR by UUID.

        Args:
            adr_id: UUID of the ADR to delete.

        Returns:
            True if found and deleted, False otherwise.
        """
        if project_key is None:
            result = await self._repo.delete(adr_id)
            await graph_delete_entity(self._graph, "ADR", adr_id)
            return result
        result = await self._repo.delete(adr_id, project_key=project_key)
        if result:
            await graph_delete_entity(
                self._graph,
                "ADR",
                adr_id,
                project_key=project_key,
            )
        return result

    async def update(
        self,
        adr_id: UUID,
        data: ADRUpdate,
        *,
        project_key: str | None = None,
    ) -> ADR | None:
        """Update an ADR with partial data, optionally re-embedding.

        If embedding_svc is available and any semantic field (title, context,
        decision) changed, fetches the existing ADR, merges with updates,
        and generates a new embedding.

        Args:
            adr_id: UUID of the ADR to update.
            data: ADRUpdate payload with fields to change.

        Returns:
            Updated ADR or None if not found.
        """
        embedding: list[float] | None = None

        needs_re_embed = self._embedding_svc is not None and any(
            getattr(data, f) is not None for f in ("title", "context", "decision")
        )

        if needs_re_embed:
            if project_key is None:
                existing = await self._repo.get_by_id(adr_id)
            else:
                existing = await self._repo.get_by_id(adr_id, project_key=project_key)
            if existing is None:
                return None
            title = data.title if data.title is not None else existing.title
            context = data.context if data.context is not None else existing.context
            decision = data.decision if data.decision is not None else existing.decision
            embed_text = adr_embedding_text(title, context, decision)
            embedding = await self._embedding_svc.embed(embed_text)

        if project_key is None:
            return await self._repo.update(adr_id, data, embedding=embedding)
        return await self._repo.update(
            adr_id,
            data,
            embedding=embedding,
            project_key=project_key,
        )
