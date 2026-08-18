"""Service layer for Learning entities — PG-only implementation.

Sits between MCP tools and PgLearningRepo.
Handles optional post-commit embedding enrichment via an injected service.

Responsibilities:
- Persist new learnings before optionally enriching their embedding
- Optionally call embedding_svc.embed(topic + insight) on update
- Delegate all persistence operations to PgLearningRepo
- Provide FTS search, semantic search (pgvector), list_all with filters, and validate()

No session management here — sessions are owned by the repository layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.learning import Learning, LearningCreate, LearningUpdate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.embedding_text import learning_embedding_text
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


class LearningService:
    """Service layer for Learning entities.

    Constructor injection for both the repository and optional embedding service.
    The embedding_svc is duck-typed — only `embed(text: str) -> list[float]`
    is required (async). This avoids circular imports and also allows usage
    without embeddings when the embedding model is not available.
    """

    def __init__(
        self,
        pg_repo: PgLearningRepo,
        embedding_svc: Any | None = None,
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

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_embed_text(self, topic: str, insight: str) -> str:
        """Concatenate topic and insight for embedding."""
        return learning_embedding_text(topic, insight)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    async def create(
        self,
        data: LearningCreate,
        related_to: list[dict] | None = None,
        *,
        authorization: RelationAuthorization | None = None,
        session: AsyncSession | None = None,
    ) -> Learning:
        """Create a durable learning, then optionally enrich its embedding.

        PostgreSQL commits the authoritative row with a null embedding first.
        When embedding_svc is unavailable, the row remains in the durable backlog.
        When ``session`` is provided, the caller owns the transaction; canonical
        graph projection and embedding backfill handle the derived work later.

        Args:
            data: LearningCreate payload.
            related_to: Optional list of dicts with keys ``id`` (UUID string)
                and ``type`` (relation label, e.g. ``"MOTIVATED_BY"``).
                When provided and a graph is configured, relations are created
                in Neo4j after the PG write.
            session: Optional caller-owned transaction. This atomic path stores
                only the authoritative row and ignores ``related_to``.
        """
        await require_known_project(self._project_context_repo, data.project_key, session=session)

        if session is not None:
            return await self._repo.create(data, embedding=None, session=session)

        result = await self._repo.create(data, embedding=None)
        return await self.enrich_created(
            result,
            data,
            related_to=related_to,
            authorization=authorization,
        )

    async def enrich_created(
        self,
        result: Learning,
        data: LearningCreate,
        related_to: list[dict] | None = None,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> Learning:
        """Run derived graph and embedding work after the PG transaction commits."""
        text = self._build_embed_text(data.topic, data.insight)
        logger.info(
            "learning.created",
            topic_length=len(data.topic),
            project_key=data.project_key,
        )

        await graph_upsert_entity(
            self._graph,
            "Learning",
            result.id,
            {"project_key": data.project_key, "topic": data.topic},
            project_key=data.project_key,
            related_to=related_to,
            authorization=authorization,
        )

        if self._embedding_enricher is None:
            return result

        enrichment = await self._embedding_enricher.enrich(
            repo=self._repo,
            entity_type="learning",
            entity_id=result.id,
            text=text,
            expected_updated_at=result.updated_at,
        )
        if enrichment.status is EnrichmentStatus.STORED and enrichment.embedding is not None:
            await link_artifact_if_enabled(
                self._feature_linker,
                enrichment.embedding,
                "learning",
                result.id,
                data.project_key,
                data.topic,
            )
            _link_job = await auto_link_if_enabled(  # résultat ignoré à dessein (6d2cf2a9 d)
                self._auto_linker,
                "Learning",
                result.id,
                enrichment.embedding,
                authorization=authorization,
            )
            if enrichment.row is not None:
                return Learning.model_validate(enrichment.row)

        return result

    async def get_by_id(
        self,
        learning_id: UUID,
        *,
        project_key: str | None = None,
    ) -> Learning | None:
        """Fetch a learning by UUID. Returns None if not found."""
        if project_key is None:
            return await self._repo.get_by_id(learning_id)
        return await self._repo.get_by_id(learning_id, project_key=project_key)

    async def resolve_id_prefix(self, prefix_hex: str) -> list[UUID]:
        """Resolve a git-style short id prefix to matching learning ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def update(
        self,
        learning_id: UUID,
        data: LearningUpdate,
        *,
        project_key: str | None = None,
    ) -> Learning | None:
        """Partial update for a learning.

        Regenerates embedding ONLY when topic or insight changes AND embedding_svc is available.
        If embedding_svc is None, passes embedding=None (no embedding regeneration).
        If learning_id doesn't exist, repo.update() returns None.
        """
        embedding: list[float] | None = None

        if self._embedding_svc is not None:
            if project_key is None:
                current = await self._repo.get_by_id(learning_id)
            else:
                current = await self._repo.get_by_id(learning_id, project_key=project_key)
            if current is not None:
                topic = data.topic if data.topic is not None else current.topic
                insight = data.insight if data.insight is not None else current.insight
                text = self._build_embed_text(topic, insight)
                embedding = await self._embedding_svc.embed(text)

        if project_key is None:
            return await self._repo.update(learning_id, data, embedding=embedding)
        return await self._repo.update(
            learning_id,
            data,
            embedding=embedding,
            project_key=project_key,
        )

    async def delete(
        self,
        learning_id: UUID,
        *,
        project_key: str | None = None,
    ) -> bool:
        """Delete a learning by id. Returns True if deleted, False if not found."""
        if project_key is None:
            result = await self._repo.delete(learning_id)
            await graph_delete_entity(self._graph, "Learning", learning_id)
            return result
        result = await self._repo.delete(learning_id, project_key=project_key)
        if result:
            await graph_delete_entity(
                self._graph,
                "Learning",
                learning_id,
                project_key=project_key,
            )
        return result

    # ── List ─────────────────────────────────────────────────────────────────

    async def list_all(
        self,
        project_key: str | None = None,
        confidence: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Learning]:
        """List learnings with optional filters. Delegates to repo.list_all()."""
        return await self._repo.list_all(
            project_key=project_key,
            confidence=confidence,
            tags=tags,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    # ── Search ───────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        confidence: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Learning]:
        """Full-text search. Wraps repo.search_fts() and returns list[Learning].

        The MCP tool `brain_recall` calls this method for keyword searches.
        """
        return await self._repo.search_fts(
            query,
            project_key=project_key,
            project_keys=project_keys,
            confidence=confidence,
            limit=limit,
            offset=offset,
        )

    async def semantic_search(
        self,
        query: str,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        confidence: str | None = None,
        limit: int = 10,
        embedding: list[float] | None = None,
    ) -> list[tuple[Learning, float]]:
        """Semantic search via pgvector cosine similarity.

        When a pre-computed ``embedding`` is provided, skips the embed() call.
        When embedding_svc is None and no embedding provided, logs a warning
        and returns [] to avoid breaking MCP calls.

        Returns list of (Learning, similarity_score) tuples.
        """
        if embedding is None:
            if self._embedding_svc is None:
                logger.warning(
                    "learning.semantic_search.no_embedding_svc",
                    query_length=len(query),
                    note="Returning empty results — embedding_svc not configured",
                )
                return []
            embedding = await self._embedding_svc.embed(query)

        return await self._repo.search_vector(
            embedding,
            project_key=project_key,
            project_keys=project_keys,
            confidence=confidence,
            limit=limit,
        )

    # ── Validate ─────────────────────────────────────────────────────────────

    async def validate(
        self,
        learning_id: UUID,
        *,
        project_group: str | None = None,
    ) -> Learning | None:
        """Mark a learning as validated (sets validated_at to now).

        Returns the updated Learning, or None if not found.
        """
        if project_group is None:
            return await self._repo.validate(learning_id)
        return await self._repo.validate(learning_id, project_group=project_group)
