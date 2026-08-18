"""DecisionService — orchestration layer between embedding and PostgreSQL.

Responsibilities:
- Persist new decisions before bounded, best-effort embedding enrichment
- Generate embeddings via GPUEmbeddingService (duck-typed) on update/supersede
- Detect which fields trigger re-embedding on update (title, description, reasoning)
- Delegate all DB operations to PgDecisionRepo
- Provide search (FTS), semantic_search (pgvector), and supersession chain APIs

No session management here — sessions are owned by the repository layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.models.decision import Decision, DecisionCreate, DecisionUpdate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.embedding_text import decision_embedding_text
from brain_v42.services.graph_helpers import (
    auto_link_if_enabled,
    graph_create_relation_logged,
    graph_delete_entity,
    graph_upsert_entity,
    link_artifact_if_enabled,
)
from brain_v42.services.project_guard import require_known_project

if TYPE_CHECKING:
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo

logger = structlog.get_logger(__name__)

# Text fields that trigger re-embedding on update.
# Changes to alternatives, consequences, tags, status, metadata do NOT require re-embedding.
_TEXT_FIELDS = {"title", "description", "reasoning"}


class DecisionService:
    """Service layer for Decision entities.

    Constructor injection for both the repository and embedding service.
    The embedding_svc is duck-typed — only `async def embed(text: str) -> list[float]`
    is required. This avoids tight coupling to a specific embedding implementation.

    The graph parameter is optional. When provided, create/delete/supersede
    operations are mirrored to Neo4j via GraphService. Graph failures are
    caught and logged — they never break PG operations.
    """

    def __init__(
        self,
        repo: PgDecisionRepo,
        embedding_svc: Any,
        feature_linker: Any | None = None,
        graph: Any | None = None,
        auto_linker: Any | None = None,
        embedding_enricher: EmbeddingEnrichmentService | None = None,
        project_context_repo: PgProjectContextRepo | None = None,
    ) -> None:
        self._repo = repo
        self._embedding_svc = embedding_svc
        self._embedding_enricher = embedding_enricher or EmbeddingEnrichmentService(embedding_svc)
        self._feature_linker = feature_linker
        self._graph = graph
        self._auto_linker = auto_linker
        self._project_context_repo = project_context_repo

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_embed_text(self, title: str, description: str, reasoning: str) -> str:
        """Concatenate text fields into a single string for embedding."""
        return decision_embedding_text(title, description, reasoning)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    async def create(
        self,
        data: DecisionCreate,
        related_to: list[dict] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> Decision:
        """Create a durable decision, then attempt bounded embedding enrichment.

        PostgreSQL commits the authoritative row with a null embedding first.
        Embedding and similarity links are derived work and cannot undo that write.
        When ``session`` is provided, the caller owns the transaction; canonical
        graph projection and embedding backfill handle the derived work later.

        Args:
            data: DecisionCreate payload.
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
        return await self.enrich_created(result, data, related_to=related_to)

    async def enrich_created(
        self,
        result: Decision,
        data: DecisionCreate,
        related_to: list[dict] | None = None,
    ) -> Decision:
        """Run derived graph and embedding work after the PG transaction commits."""
        text = self._build_embed_text(data.title, data.description, data.reasoning)
        logger.info("decision.created", title=data.title, project_key=data.project_key)

        await graph_upsert_entity(
            self._graph,
            "Decision",
            result.id,
            {"project_key": data.project_key, "title": data.title},
            project_key=data.project_key,
            related_to=related_to,
        )

        enrichment = await self._embedding_enricher.enrich(
            repo=self._repo,
            entity_type="decision",
            entity_id=result.id,
            text=text,
            expected_updated_at=result.updated_at,
        )
        if enrichment.status is EnrichmentStatus.STORED and enrichment.embedding is not None:
            await link_artifact_if_enabled(
                self._feature_linker,
                enrichment.embedding,
                "decision",
                result.id,
                data.project_key,
                data.title,
            )
            _link_job = await auto_link_if_enabled(  # résultat ignoré à dessein (6d2cf2a9 d)
                self._auto_linker,
                "Decision",
                result.id,
                enrichment.embedding,
            )
            if enrichment.row is not None:
                return Decision.model_validate(enrichment.row)

        return result

    async def get_by_id(
        self,
        decision_id: UUID,
        *,
        project_key: str | None = None,
    ) -> Decision | None:
        """Fetch a decision by UUID. Returns None if not found."""
        if project_key is None:
            return await self._repo.get_by_id(decision_id)
        return await self._repo.get_by_id(decision_id, project_key=project_key)

    async def resolve_id_prefix(self, prefix_hex: str) -> list[UUID]:
        """Resolve a git-style short id prefix to matching decision ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def list_all(
        self,
        *,
        project_key: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Decision]:
        """List decisions with optional filters. Delegates to repo.list_all()."""
        return await self._repo.list_all(
            project_key=project_key,
            status=status,
            tags=tags,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    async def update(
        self,
        decision_id: UUID,
        data: DecisionUpdate,
        *,
        project_key: str | None = None,
    ) -> Decision | None:
        """Partial update for a decision.

        Re-generates the embedding ONLY when title, description, or reasoning changes.
        If decision_id doesn't exist (get_by_id returns None), skips embedding generation
        and passes embedding=None to repo.update() — repo returns None for missing IDs.
        """
        changed_fields = set(data.model_dump(exclude_none=True).keys())
        embedding: list[float] | None = None

        if changed_fields & _TEXT_FIELDS:
            if project_key is None:
                current = await self._repo.get_by_id(decision_id)
            else:
                current = await self._repo.get_by_id(decision_id, project_key=project_key)
            if current is not None:
                title = data.title if data.title is not None else current.title
                description = (
                    data.description if data.description is not None else current.description
                )
                reasoning = data.reasoning if data.reasoning is not None else current.reasoning
                text = self._build_embed_text(title, description, reasoning)
                embedding = await self._embedding_svc.embed(text)

        if project_key is None:
            return await self._repo.update(decision_id, data, embedding=embedding)
        return await self._repo.update(
            decision_id,
            data,
            embedding=embedding,
            project_key=project_key,
        )

    async def delete(
        self,
        decision_id: UUID,
        *,
        project_key: str | None = None,
    ) -> bool:
        """Delete a decision. Returns True if it existed, False otherwise.

        After PG delete, removes the corresponding node from Neo4j (if graph
        is configured). Graph failures are caught and logged.
        """
        if project_key is None:
            result = await self._repo.delete(decision_id)
            await graph_delete_entity(self._graph, "Decision", decision_id)
            return result
        result = await self._repo.delete(decision_id, project_key=project_key)
        if result:
            await graph_delete_entity(
                self._graph,
                "Decision",
                decision_id,
                project_key=project_key,
            )
        return result

    # ── Search ───────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Decision]:
        """Full-text search. Wraps repo.search_fts() and strips scores.

        The MCP tool `brain_search_decisions` calls this method directly.
        """
        results = await self._repo.search_fts(
            query,
            project_key=project_key,
            project_keys=project_keys,
            status=status,
            tags=tags,
            limit=limit,
            offset=offset,
        )
        return [decision for decision, _score in results]

    async def semantic_search(
        self,
        query: str,
        *,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 20,
        embedding: list[float] | None = None,
    ) -> list[tuple[Decision, float]]:
        """Semantic search via pgvector cosine similarity.

        Embeds the query string, then delegates to repo.search_vector().
        When a pre-computed ``embedding`` is provided, skips the embed() call.
        Returns list of (Decision, similarity_score) tuples.
        """
        if embedding is None:
            embedding = await self._embedding_svc.embed_query(query)
        return await self._repo.search_vector(
            embedding, project_key=project_key, project_keys=project_keys, limit=limit
        )

    # ── Supersession ─────────────────────────────────────────────────────────

    async def supersede(self, old_id: UUID, new_data: DecisionCreate) -> Decision:
        """Supersede an existing decision with a new one.

        Generates embedding for the new decision text, then delegates to
        repo.supersede() which performs the atomic INSERT + UPDATE.

        After PG write, upserts the new node in Neo4j and creates a SUPERSEDES
        relation from the new decision to the old one. Graph failures are caught
        and logged.
        """
        text = self._build_embed_text(new_data.title, new_data.description, new_data.reasoning)
        new_embedding = await self._embedding_svc.embed(text)
        result = await self._repo.supersede(old_id, new_data, new_embedding=new_embedding)
        logger.info("decision.superseded", old_id=str(old_id), new_id=str(result.id))

        if self._graph:
            try:
                await self._graph.upsert_node(
                    "Decision",
                    result.id,
                    {"project_key": new_data.project_key, "title": new_data.title},
                )
            except Exception:
                logger.error(
                    "graph_supersede_failed",
                    old_id=str(old_id),
                    new_id=str(result.id),
                    exc_info=True,
                )
            # Surfaces a WARN if Neo4j reports the SUPERSEDES write did not land.
            await graph_create_relation_logged(self._graph, result.id, old_id, "SUPERSEDES")

        return result

    async def get_supersession_chain(self, decision_id: UUID) -> list[Decision]:
        """Walk the supersession chain from decision_id.

        When a graph is configured, delegates to GraphService.get_supersession_chain()
        to traverse the Neo4j SUPERSEDES chain, then fetches full Decision objects
        from PG for each ID. Falls back to the PG recursive CTE when:
        - No graph is configured
        - Graph returns an empty chain
        - All fetched decisions resolve to None
        """
        if self._graph:
            try:
                chain_ids = await self._graph.get_supersession_chain(decision_id)
                if chain_ids:
                    decisions: list[Decision] = []
                    for cid in chain_ids:
                        uid = UUID(cid) if isinstance(cid, str) else cid
                        d = await self._repo.get_by_id(uid)
                        if d:
                            decisions.append(d)
                        else:
                            logger.warning("decision.chain_member_missing", decision_id=str(uid))
                    if decisions:
                        return decisions
            except Exception:
                logger.error(
                    "graph_supersession_chain_failed",
                    decision_id=str(decision_id),
                    exc_info=True,
                )
        return await self._repo.get_supersession_chain(decision_id)
