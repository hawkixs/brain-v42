"""RunbookService — service layer for runbook operations (PG only).

Wraps PgRunbookRepo and provides:
- create / get_by_id / update / delete (CRUD)
- get_by_title(title, project_key) — unique lookup
- list_by_project(project_key, limit, offset) — paginated list
- search(query, project_key, limit) — FTS via tsvector
- record_execution(id, status) — atomic execution tracking
- Optional: embedding generation via GPUEmbeddingService (passed at construction)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from brain_v42.models.runbook import (
    ExecutionStatus,
    Runbook,
    RunbookCreate,
    RunbookUpdate,
)
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.embedding_text import runbook_embedding_text
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
    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService

logger = structlog.get_logger(__name__)


class RunbookService:
    """Service layer for runbook operations.

    Wraps PgRunbookRepo and provides:
    - create / get_by_id / update / delete (CRUD)
    - get_by_title(title, project_key) — unique lookup
    - list_by_project(project_key, limit, offset) — paginated list
    - search(query, project_key, limit) — FTS via tsvector
    - record_execution(id, status) — atomic execution tracking
    - Optional: embedding generation via GPUEmbeddingService (passed at construction)

    Usage (no embeddings, PG only):
        svc = RunbookService(pg_repo=PgRunbookRepo())

    Usage (with embeddings — M4):
        svc = RunbookService(pg_repo=PgRunbookRepo(), embedding_svc=embedding_svc)
    """

    def __init__(
        self,
        pg_repo: PgRunbookRepo,
        embedding_svc: GPUEmbeddingService | None = None,
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

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Generate embedding if embedding_svc is available, else return None."""
        if self._embedding_svc is None:
            return None
        return await self._embedding_svc.embed(text)

    async def create(
        self,
        data: RunbookCreate,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> Runbook:
        """Create a durable runbook, then attempt bounded embedding enrichment.

        PostgreSQL stores a null vector first so embedding availability cannot
        decide whether the Runbook exists.
        """
        await require_known_project(self._project_context_repo, data.project_key)

        embedding_text = runbook_embedding_text(data.title, data.description, data.trigger)
        runbook = await self._repo.create(data, embedding=None)
        logger.info(
            "runbook_service.create",
            title_length=len(runbook.title),
            step_count=len(runbook.steps),
        )

        await graph_upsert_entity(
            self._graph,
            "Runbook",
            runbook.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            authorization=authorization,
        )
        return await self._enrich_created_runbook(
            runbook,
            data,
            embedding_text,
            authorization=authorization,
        )

    async def create_with_promotion(
        self,
        data: RunbookCreate,
        source_learning_id: uuid.UUID,
        dream_run_id: int | None = None,
        *,
        project_key: str | None = None,
        authorization: RelationAuthorization | None = None,
    ) -> Runbook:
        """Create a Runbook + atomically record promotion from a source learning.

        Repo owns the PG transaction (runbook + learnings.metadata +
        dream_promotions rows commit together). Graph upsert + feature-link +
        auto-link run post-commit via graph_helpers, which swallow their own
        exceptions — a Neo4j outage never rolls back the PG writes.
        """
        embedding_text = runbook_embedding_text(data.title, data.description, data.trigger)

        if project_key is None:
            runbook = await self._repo.create_with_promotion(
                data=data,
                embedding=None,
                source_learning_id=source_learning_id,
                dream_run_id=dream_run_id,
            )
        else:
            runbook = await self._repo.create_with_promotion(
                data=data,
                embedding=None,
                source_learning_id=source_learning_id,
                dream_run_id=dream_run_id,
                project_key=project_key,
            )
        logger.info(
            "runbook_service.create_with_promotion",
            id=str(runbook.id),
            source_learning_id=str(source_learning_id),
        )

        await graph_upsert_entity(
            self._graph,
            "Runbook",
            runbook.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            authorization=authorization,
        )
        return await self._enrich_created_runbook(
            runbook,
            data,
            embedding_text,
            authorization=authorization,
        )

    async def _enrich_created_runbook(
        self,
        runbook: Runbook,
        data: RunbookCreate,
        embedding_text: str,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> Runbook:
        """Enrich and link a Runbook whose authoritative transaction committed."""
        if self._embedding_enricher is None:
            return runbook

        enrichment = await self._embedding_enricher.enrich(
            repo=self._repo,
            entity_type="runbook",
            entity_id=runbook.id,
            text=embedding_text,
            expected_updated_at=runbook.updated_at,
        )
        if enrichment.status is not EnrichmentStatus.STORED or enrichment.embedding is None:
            return runbook

        await link_artifact_if_enabled(
            self._feature_linker,
            enrichment.embedding,
            "runbook",
            runbook.id,
            data.project_key,
            data.title,
        )
        await auto_link_if_enabled(
            self._auto_linker,
            "Runbook",
            runbook.id,
            enrichment.embedding,
            authorization=authorization,
        )
        if enrichment.row is not None:
            return Runbook.model_validate(enrichment.row)
        return runbook

    async def get_by_id(
        self,
        id: uuid.UUID,
        *,
        project_key: str | None = None,
    ) -> Runbook | None:
        """Fetch a runbook by UUID. Returns None if not found."""
        if project_key is None:
            return await self._repo.get_by_id(id)
        return await self._repo.get_by_id(id, project_key=project_key)

    async def resolve_id_prefix(self, prefix_hex: str) -> list[uuid.UUID]:
        """Resolve a git-style short id prefix to matching runbook ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def get_by_title(self, title: str, project_key: str) -> Runbook | None:
        """Fetch a runbook by (title, project_key) unique constraint. Returns None if not found."""
        return await self._repo.get_by_title(title, project_key)

    async def list_by_project(
        self,
        project_key: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Runbook]:
        """List runbooks for a project, ordered by created_at DESC."""
        return await self._repo.list_by_project(project_key, limit=limit, offset=offset)

    async def search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Runbook]:
        """Full-text search using PostgreSQL tsvector. Returns list[Runbook]."""
        return await self._repo.search_fts(
            query, project_key=project_key, project_keys=project_keys, limit=limit, offset=offset
        )

    async def record_execution(
        self,
        id: uuid.UUID,
        status: ExecutionStatus,
    ) -> Runbook | None:
        """Atomically increment execution_count, set last_executed_at and status.

        Returns the updated Runbook, or None if not found.
        """
        runbook = await self._repo.record_execution(id, status)
        if runbook is not None:
            logger.info(
                "runbook_service.record_execution",
                id=str(id),
                status=status,
                execution_count=runbook.execution_count,
            )
        return runbook

    async def update(
        self,
        id: uuid.UUID,
        data: RunbookUpdate,
        *,
        project_key: str | None = None,
    ) -> Runbook | None:
        """Update a runbook partially (PATCH semantics). Returns None if not found."""
        # Optionally refresh embedding if title/description changed
        embedding: list[float] | None = None
        if self._embedding_svc is not None and (
            data.title is not None or data.description is not None
        ):
            if project_key is None:
                current = await self._repo.get_by_id(id)
            else:
                current = await self._repo.get_by_id(id, project_key=project_key)
            if current is not None:
                new_title = data.title or current.title
                new_desc = data.description or current.description
                new_trigger = data.trigger or current.trigger
                embedding = await self._get_embedding(
                    runbook_embedding_text(new_title, new_desc, new_trigger)
                )
        if project_key is None:
            return await self._repo.update(id, data, embedding=embedding)
        return await self._repo.update(
            id,
            data,
            embedding=embedding,
            project_key=project_key,
        )

    async def semantic_search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 10,
        embedding: list[float] | None = None,
    ) -> list[tuple[Runbook, float]]:
        """Semantic search using pgvector cosine similarity.

        When a pre-computed ``embedding`` is provided, skips the embed() call.
        Returns list of (Runbook, score) tuples sorted by similarity DESC.
        Returns [] if no embedding_svc is configured and no embedding provided.
        """
        if embedding is None:
            if self._embedding_svc is None:
                return []
            embedding = await self._embedding_svc.embed_query(query)
        return await self._repo.vector_search(
            embedding, project_key=project_key, project_keys=project_keys, limit=limit
        )

    async def delete(
        self,
        id: uuid.UUID,
        *,
        project_key: str | None = None,
    ) -> bool:
        """Delete a runbook. Returns True if deleted, False if not found."""
        if project_key is None:
            result = await self._repo.delete(id)
            await graph_delete_entity(self._graph, "Runbook", id)
            return result
        result = await self._repo.delete(id, project_key=project_key)
        if result:
            await graph_delete_entity(
                self._graph,
                "Runbook",
                id,
                project_key=project_key,
            )
        return result
