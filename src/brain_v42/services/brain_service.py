"""BrainService — global semantic search orchestrator.

Fans out pgvector semantic search across all domain services (DecisionService,
LearningService, SnippetService, RunbookService, ADRService), merges results
by cosine similarity score, and exposes two MCP-facing methods:
    - search(): global semantic search with optional type/project_key filter
    - what_do_i_know_about(): same but grouped by knowledge type

Design decisions:
- Embedding generated ONCE per search() / what_do_i_know_about() call.
  The pre-computed embedding is passed to each service's semantic_search()
  as an optional `embedding` kwarg. Services that don't accept this kwarg
  will fall back to generating their own (backward-compat).
- asyncio.gather(return_exceptions=True) for concurrent fan-out — one failing
  service does NOT abort the entire search.
- ProjectContextService is intentionally excluded (no embedding column per SCHEMA.md).
- min_score=0.2 by default; results below threshold are filtered out.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

import structlog

from brain_v42.dream_project_errors import DreamProjectAuthorizationError
from brain_v42.models.brain import (
    ALL_TYPES,
    KnowledgeByType,
    KnowledgeType,
    SearchResponse,
    SearchResult,
    WhatDoIKnowResponse,
)
from brain_v42.services.dream_project_scope import get_dream_project_scope
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

# Text extractors for reranker input — maps entity type to text extraction
_TEXT_EXTRACTORS: dict[str, Callable[..., str]] = {
    "decision": lambda e: f"{e.title} {e.description} {e.reasoning}",
    "learning": lambda e: f"{e.topic} {e.insight}",
    "snippet": lambda e: f"{e.title} {e.intention}",
    "runbook": lambda e: f"{e.title} {e.description}",
    "adr": lambda e: f"{e.title} {e.context} {e.decision}",
    "plan": lambda e: f"{e.section_title} {e.content}",
}

# Mapping from KnowledgeType to plural field name on KnowledgeByType
# "decision" → "decisions", "learning" → "learnings", etc.
_TYPE_TO_PLURAL: dict[str, str] = {t: t + "s" for t in ALL_TYPES}

_GRAPH_BACKED_TYPES: frozenset[KnowledgeType] = frozenset(
    ("decision", "learning", "snippet", "runbook", "adr")
)


class BrainService:
    """Global search orchestrator over all domain knowledge types.

    Constructor args (all injected):
        decision_svc: DecisionService instance
        learning_svc: LearningService instance
        snippet_svc:  SnippetService instance
        runbook_svc:  RunbookService instance
        adr_svc:      ADRService instance
        embedding_svc: GPUEmbeddingService instance (provides embed())
        min_score: Minimum cosine similarity threshold (default 0.2)
    """

    def __init__(
        self,
        decision_svc: Any,
        learning_svc: Any,
        snippet_svc: Any,
        runbook_svc: Any,
        adr_svc: Any,
        embedding_svc: Any,
        min_score: float = 0.2,
        metrics_collector: Any | None = None,
        hybrid_searcher: Any | None = None,
        decay_calculator: Any | None = None,
        access_logger: Any | None = None,
        decay_floor: float = 0.3,
        decay_human_signal_enabled: bool = False,
        graph: Any | None = None,
        project_context_svc: Any | None = None,
        plan_search_svc: IndexedPlanSearchService | None = None,
    ) -> None:
        self._services: dict[KnowledgeType, Any] = {
            "decision": decision_svc,
            "learning": learning_svc,
            "snippet": snippet_svc,
            "runbook": runbook_svc,
            "adr": adr_svc,
        }
        if plan_search_svc is not None:
            self._services["plan"] = plan_search_svc
        self._embedding_svc = embedding_svc
        self._min_score = min_score
        self._collector = metrics_collector
        self._hybrid_searcher = hybrid_searcher
        self._decay_calculator = decay_calculator
        self._access_logger = access_logger
        self._decay_floor = decay_floor
        # §5.5 — défaut FERMÉ dans la signature, pas seulement dans Settings :
        # un appelant qui oublie le réglage obtient le comportement d'aujourd'hui.
        self._decay_human_signal_enabled = decay_human_signal_enabled
        self._graph = graph
        self._project_context_svc = project_context_svc

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fan_out(
        self,
        types: list[KnowledgeType],
        query: str,
        project_key: str | None,
        limit: int,
        project_keys: list[str] | None = None,
        include_archived: bool = False,
    ) -> tuple[dict[KnowledgeType, list[tuple[Any, float]]], dict[str, Any] | None]:
        """Run search concurrently across all requested service types.

        Pre-computes the query embedding ONCE and passes it to each service,
        avoiding redundant GPU calls (5x → 1x).

        When hybrid_searcher is set, uses HybridSearcher (FTS + vector + RRF).
        Otherwise falls back to vector-only semantic_search.

        Uses asyncio.gather(return_exceptions=True) to isolate failures.

        Fix 2 — EmbeddingUnavailable:
            If the embedding service raises EmbeddingUnavailable (GPU down),
            we switch to FTS-only fan-out: each service's .search() method is
            called without embedding.  Scores are assigned by rank.
            The caller receives degraded={"search_mode": "fts_fallback"}.

        Fix 1 — Reranker blackout:
            HybridSearcher.search() now returns (results, rerank_mode). When
            rerank_mode == "rrf_fallback", the caller receives degraded info.

        Returns:
            2-tuple (results_by_type, degraded) where degraded is None (healthy)
            or a dict with mode keys ("rerank_mode", "search_mode").
        """
        scope = get_dream_project_scope()
        if scope is not None:
            project_key = scope.project_key
            project_keys = None

        # Fix 2: Catch EmbeddingUnavailable to trigger FTS-only fallback.
        shared_embedding: list[float] | None = None
        fts_only_fallback = False
        if self._embedding_svc is not None:
            try:
                shared_embedding = await self._embedding_svc.embed_query(query)
            except EmbeddingUnavailable:
                logger.warning(
                    "brain_service.fan_out.embedding_unavailable",
                    fallback="fts_only",
                )
                fts_only_fallback = True

        tasks: list[asyncio.Task[Any]] = []
        task_types: list[KnowledgeType] = []

        for t in types:
            svc = self._services.get(t)
            if svc is None:
                logger.debug("brain_service.fan_out.no_service", type=t)
                continue

            plan_archive_kwargs = {"include_archived": include_archived} if t == "plan" else {}
            if fts_only_fallback:
                # FTS-only: call svc.search (not semantic_search, which would re-embed)
                coro = svc.search(
                    query=query,
                    limit=limit,
                    **({"project_key": project_key} if project_key is not None else {}),
                    **({"project_keys": project_keys} if project_keys is not None else {}),
                    **plan_archive_kwargs,
                )
            elif self._hybrid_searcher:
                fts_search_fn = svc.search
                vector_search_fn = svc.semantic_search
                if t == "plan":
                    fts_search_fn = partial(
                        fts_search_fn,
                        include_archived=include_archived,
                    )
                    vector_search_fn = partial(
                        vector_search_fn,
                        include_archived=include_archived,
                    )
                coro = self._hybrid_searcher.search(
                    query=query,
                    fts_search_fn=fts_search_fn,
                    vector_search_fn=vector_search_fn,
                    text_extractor=_TEXT_EXTRACTORS[t],
                    limit=limit,
                    project_key=project_key,
                    project_keys=project_keys,
                    embedding=shared_embedding,
                )
            else:
                coro = svc.semantic_search(
                    query,
                    project_key=project_key,
                    project_keys=project_keys,
                    limit=limit,
                    embedding=shared_embedding,
                    **plan_archive_kwargs,
                )
            tasks.append(asyncio.create_task(coro))
            task_types.append(t)

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: dict[KnowledgeType, list[tuple[Any, float]]] = {}
        # Collect rerank modes from hybrid searcher results (Fix 1)
        observed_rerank_modes: list[str] = []

        for t, result in zip(task_types, raw_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "brain_service.fan_out.service_error",
                    type=t,
                    exception_type=type(result).__name__,
                )
                results[t] = []
            elif fts_only_fallback:
                # result is list[entity]; assign rank-based scores
                entities: list[Any] = result
                n = len(entities)
                results[t] = [
                    (entity, (n - rank) / n if n > 0 else 0.0)
                    for rank, entity in enumerate(entities)
                ]
            elif self._hybrid_searcher:
                # Fix 1: HybridSearcher now returns (list[tuple], rerank_mode)
                search_results, rerank_mode = result
                results[t] = search_results
                observed_rerank_modes.append(rerank_mode)
            else:
                results[t] = result

        # Build degraded marker
        degraded: dict[str, Any] | None = None
        if fts_only_fallback:
            degraded = {"search_mode": "fts_fallback"}
        elif observed_rerank_modes:
            from brain_v42.services.search.hybrid import RERANK_MODE_RRF_FALLBACK  # noqa: PLC0415

            # If ANY shard degraded to rrf_fallback, surface that
            if RERANK_MODE_RRF_FALLBACK in observed_rerank_modes:
                degraded = {"rerank_mode": RERANK_MODE_RRF_FALLBACK}
            else:
                # Surface the mode (reranked / rrf_only) — None means healthy
                mode = observed_rerank_modes[0] if observed_rerank_modes else None
                if mode not in (None, "reranked"):
                    degraded = {"rerank_mode": mode}

        return results, degraded

    def _build_search_results(
        self,
        results_by_type: dict[KnowledgeType, list[tuple[Any, float]]],
        limit: int,
        min_score: float | None = None,
        include_archived: bool = False,
        tags: list[str] | None = None,
    ) -> list[SearchResult]:
        """Flatten, filter by min_score, sort by score DESC, and slice to limit.

        When include_archived is False (default), entities with
        freshness_status='archived' or a non-null merged_into are excluded.
        When decay_calculator is set, results are re-ranked by effective_score.
        """
        threshold = min_score if min_score is not None else self._min_score
        flat: list[SearchResult] = []
        effective_scores: list[float] = []

        for t, items in results_by_type.items():
            for entity, score in items:
                if score < threshold:
                    continue

                # Filter archived/merged entities
                if not include_archived:
                    freshness_status = getattr(entity, "freshness_status", "fresh")
                    if t == "plan":
                        parent_freshness = getattr(entity, "parent_freshness_status", None)
                        if parent_freshness is not None:
                            freshness_status = parent_freshness
                    if freshness_status == "archived":
                        continue
                    if getattr(entity, "merged_into", None) is not None:
                        continue

                # Filter by tags (overlap — entity must have at least one matching tag)
                if tags:
                    entity_tags = getattr(entity, "tags", None) or []
                    if not set(tags) & set(entity_tags):
                        continue

                try:
                    item_dict = entity.model_dump(mode="json")
                except (ValueError, AttributeError):
                    logger.warning("brain_service.model_dump_failed", type=t, exc_info=True)
                    item_dict = vars(entity)

                # Compute decay if enabled
                if self._decay_calculator is not None:
                    is_validated = (
                        getattr(entity, "validated_at", None) is not None
                        or getattr(entity, "decided_at", None) is not None
                    )
                    created_at = entity.created_at
                    last_accessed_at = getattr(entity, "last_accessed_at", None)
                    access_count = getattr(entity, "access_count", 0)
                    if t == "plan":
                        parent_created_at = getattr(entity, "parent_created_at", None)
                        parent_last_accessed_at = getattr(entity, "parent_last_accessed_at", None)
                        parent_access_count = getattr(entity, "parent_access_count", None)
                        if parent_created_at is not None:
                            created_at = parent_created_at
                            last_accessed_at = parent_last_accessed_at
                        elif parent_last_accessed_at is not None:
                            last_accessed_at = parent_last_accessed_at
                        if parent_access_count is not None:
                            access_count = parent_access_count
                    # LE défaut que le focus appelle « decay inversé » : cette
                    # ligne passait le compteur TOTAL au multiplicateur, donc ce
                    # que le dream relit chaque nuit faisait vivre l'artefact. Le
                    # correctif est la SUBSTITUTION du signal, pas un recalibrage
                    # des baselines — celles-ci décrivent une distribution, et la
                    # distribution humaine n'est pas la distribution totale.
                    #
                    # Les DEUX signaux basculent ensemble : substituer le seul
                    # compteur ne répare que `freq_factor` (poids 0,2) et laisse
                    # `access_factor` (0,3, le plus lourd après l'âge) piloté par
                    # les lectures machine — 1 779 learnings mesurés dans ce cas.
                    if self._decay_human_signal_enabled:
                        signal_access_count = getattr(entity, "access_count_human", 0) or 0
                        signal_last_accessed = getattr(entity, "last_accessed_at_human", None)
                    else:
                        signal_access_count = access_count
                        signal_last_accessed = last_accessed_at

                    multiplier = self._decay_calculator.compute_multiplier(
                        entity_type=t,
                        created_at=created_at,
                        last_accessed_at=signal_last_accessed,
                        access_count=signal_access_count,
                        is_validated=is_validated,
                    )
                    effective_score = score * (
                        self._decay_floor + (1 - self._decay_floor) * multiplier
                    )
                    item_dict["_decay_multiplier"] = round(multiplier, 4)
                    item_dict["_freshness"] = self._decay_calculator.freshness_status(multiplier)
                else:
                    effective_score = score

                # Extract convenience fields from entity
                # Plan chunks use section_title; other entities use title
                result_title: str | None = (
                    getattr(entity, "section_title", None)
                    or getattr(entity, "title", None)
                    or getattr(entity, "topic", None)
                )
                result_project_key: str | None = getattr(entity, "project_key", None)
                result_tags: list[str] | None = getattr(entity, "tags", None) or None
                # parent_id populated for plan chunks (plan_id → parent indexed_plan row)
                result_parent_id = getattr(entity, "plan_id", None)

                flat.append(
                    SearchResult(
                        type=t,
                        score=score,
                        item=item_dict,
                        title=result_title,
                        project_key=result_project_key,
                        tags=result_tags,
                        parent_id=result_parent_id,
                    )
                )
                effective_scores.append(effective_score)

        # Sort by effective_score (falls back to raw score when no decay)
        paired = list(zip(flat, effective_scores, strict=True))
        paired.sort(key=lambda pair: pair[1], reverse=True)
        flat = [r for r, _ in paired]
        return flat[:limit]

    def _log_search_accesses(self, results: list[SearchResult]) -> int:
        """Emit one usage signal per canonical entity represented in a response."""
        if self._access_logger is None:
            return 0

        from uuid import UUID  # noqa: PLC0415

        logged: set[tuple[str, UUID]] = set()
        for result in results:
            raw_id = result.parent_id if result.type == "plan" else result.item.get("id")
            if raw_id is None:
                continue
            try:
                entity_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
            except (TypeError, ValueError, AttributeError):
                logger.warning("brain_service.access_id_invalid", type=result.type)
                continue
            key = (result.type, entity_id)
            if key in logged:
                continue
            self._access_logger.log_access(result.type, entity_id, "search_hit")
            logged.add(key)
        return len(logged)

    # ── Public API ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        types: list[KnowledgeType] | None = None,
        project_key: str | None = None,
        project_group: str | None = None,
        limit: int = 20,
        min_score: float | None = None,
        include_archived: bool = False,
        tags: list[str] | None = None,
    ) -> SearchResponse:
        """Global semantic search across all (or filtered) knowledge types.

        Args:
            query: Free-text query string — embedded ONCE via embedding_svc.
            types: Optional list of KnowledgeType to search. Defaults to ALL_TYPES.
            project_key: Optional project scope filter passed to each service.
            project_group: Optional project group — resolved to project_keys via
                ProjectContextService.get_keys_by_group(). When set, searches across
                all projects in the group simultaneously.
            limit: Maximum number of results to return (top scores).
            min_score: Override the instance-level min_score threshold for this call.
            include_archived: If True, include archived/merged entities in results.

        Returns:
            SearchResponse with sorted, deduplicated results and metadata.
            When a graph is configured, the response includes a ``related`` field
            with neighboring nodes for the top results (Task 8).

        Notes:
            Mixed degraded modes: if only one entity type falls back to
            rrf_fallback (e.g., reranker down mid-request), effective_min_score
            is set to 0.0 globally. This means sub-threshold results from healthy
            types are also admitted. The trade-off is accepted: the reranker is a
            shared singleton, so partial blackout is rare and transient. Full
            per-type score isolation would require a larger fan-out refactor.
        """
        types_to_search: list[KnowledgeType] = types if types is not None else list(ALL_TYPES)

        # Resolve project_group to a list of project_keys
        project_keys: list[str] | None = None
        if project_group and self._project_context_svc:
            project_keys = await self._project_context_svc.get_keys_by_group(project_group)
            if not project_keys:
                return SearchResponse(
                    results=[],
                    total=0,
                    query=query,
                    types_searched=types_to_search,
                )

        # Fix 3: Task 9 dead fetch removed — graph.get_project_tree() was called
        # but its result was never used (stored in _sub_project_keys, then discarded).
        # One wasted Neo4j roundtrip per scoped search. The full multi-project
        # fan-out is still deferred; the dead roundtrip is now gone.

        results_by_type, fan_out_degraded = await self._fan_out(
            types=types_to_search,
            query=query,
            project_key=project_key,
            limit=limit,
            project_keys=project_keys,
            include_archived=include_archived,
        )

        # In degraded mode (rrf_fallback or fts_fallback): skip absolute min_score
        # filtering — rank-based scores are not comparable to sigmoid-normalised scores,
        # and applying min_score=0.2 would silently discard all results (incident d3cf29e9).
        effective_min_score: float | None = min_score if fan_out_degraded is None else 0.0

        search_results = self._build_search_results(
            results_by_type,
            limit=limit,
            min_score=effective_min_score,
            include_archived=include_archived,
            tags=tags,
        )

        # Task 8: Graph neighbor enrichment — batch-fetch related nodes for result entities.
        related: list[dict] = []
        if self._graph and search_results:
            try:
                from uuid import UUID  # noqa: PLC0415

                scope = get_dream_project_scope()
                if scope is None:
                    result_ids = [
                        UUID(str(r.item["id"])) for r in search_results if r.item.get("id")
                    ]
                    if result_ids:
                        neighbors_by_id = await self._graph.get_related_ids(result_ids)
                        for neighbors in neighbors_by_id.values():
                            related.extend(neighbors)
                else:
                    raw_anchor_ids: list[UUID | str] = []
                    scoped_result_ids: list[UUID] = []
                    invalid_anchor = False
                    for result in search_results:
                        if result.type not in _GRAPH_BACKED_TYPES:
                            continue
                        raw_id = result.item.get("id")
                        normalized_id = raw_id if isinstance(raw_id, (UUID, str)) else ""
                        raw_anchor_ids.append(normalized_id)
                        if isinstance(normalized_id, UUID):
                            scoped_result_ids.append(normalized_id)
                            continue
                        try:
                            parsed_id = UUID(normalized_id)
                        except ValueError:
                            invalid_anchor = True
                            continue
                        if str(parsed_id) != normalized_id.lower():
                            invalid_anchor = True
                            continue
                        scoped_result_ids.append(parsed_id)

                    if invalid_anchor:
                        await scope.revalidate_ids(raw_anchor_ids)

                    if scoped_result_ids:
                        neighbors_by_id = await self._graph.get_related_ids(
                            scoped_result_ids,
                            project_key=scope.project_key,
                        )
                        returned_keys: list[UUID | str] = []
                        neighbor_ids: list[UUID | str] = []
                        neighbors_by_key: list[tuple[UUID | str, list[dict]]] = []
                        for raw_key, neighbors in neighbors_by_id.items():
                            normalized_key = raw_key if isinstance(raw_key, (UUID, str)) else ""
                            returned_keys.append(normalized_key)
                            normalized_neighbors: list[dict] = []
                            for neighbor in neighbors:
                                neighbor_id = neighbor.get("id")
                                neighbor_ids.append(
                                    neighbor_id if isinstance(neighbor_id, (UUID, str)) else ""
                                )
                                normalized_neighbors.append(neighbor)
                            neighbors_by_key.append((normalized_key, normalized_neighbors))

                        await scope.revalidate_ids([*raw_anchor_ids, *returned_keys, *neighbor_ids])
                        expected_anchors = set(scoped_result_ids)
                        scoped_related: list[dict] = []
                        for raw_key, neighbors in neighbors_by_key:
                            parsed_key = raw_key if isinstance(raw_key, UUID) else UUID(raw_key)
                            if parsed_key in expected_anchors:
                                scoped_related.extend(neighbors)
                        related.extend(scoped_related)
            except DreamProjectAuthorizationError:
                pass
            except Exception:
                logger.error("brain_service.graph_enrichment_failed", exc_info=True)

        # Wire in-memory search quality counters
        if self._collector is not None:
            top = search_results[0].score if search_results else 0.0
            self._collector.record_search(top, len(search_results))

        # Log access events for search results
        if self._access_logger is not None:
            logged = self._log_search_accesses(search_results)
            if logged:
                logger.debug(
                    "brain_service.access_logged",
                    count=logged,
                    queue_size=self._access_logger._queue.qsize(),
                )
        elif search_results:
            logger.warning("brain_service.access_logger_none", result_count=len(search_results))

        return SearchResponse(
            query=query,
            results=search_results,
            total=len(search_results),
            types_searched=types_to_search,
            related=related,
            degraded=fan_out_degraded,
        )

    async def what_do_i_know_about(
        self,
        topic: str,
        project_key: str | None = None,
        project_group: str | None = None,
        limit: int = 20,
        min_score: float | None = None,
        include_archived: bool = False,
    ) -> WhatDoIKnowResponse:
        """Retrieve knowledge about a topic, grouped by knowledge type.

        Args:
            topic: The topic to search for.
            project_key: Optional project scope filter.
            project_group: Optional project group — resolved to project_keys via
                ProjectContextService.get_keys_by_group().
            limit: Maximum results per type (applied per service call).
            min_score: Override the instance-level min_score threshold for this call.
            include_archived: If True, include archived/merged entities in results.

        Returns:
            WhatDoIKnowResponse with results grouped under by_type.
        """
        threshold = min_score if min_score is not None else self._min_score

        # Resolve project_group to a list of project_keys
        project_keys: list[str] | None = None
        if project_group and self._project_context_svc:
            project_keys = await self._project_context_svc.get_keys_by_group(project_group)
            if not project_keys:
                return WhatDoIKnowResponse(
                    topic=topic,
                    by_type=KnowledgeByType(),
                    total=0,
                    types_searched=list(ALL_TYPES),
                )

        results_by_type, _wdika_degraded = await self._fan_out(
            types=list(ALL_TYPES),
            query=topic,
            project_key=project_key,
            limit=limit,
            project_keys=project_keys,
            include_archived=include_archived,
        )
        # In degraded mode, use threshold=0 so rank-based scores are not filtered out.
        if _wdika_degraded is not None:
            threshold = 0.0

        by_type = KnowledgeByType()
        total = 0

        for t, items in results_by_type.items():
            type_results: list[SearchResult] = []
            for entity, score in items:
                if score < threshold:
                    continue

                # Filter archived/merged entities
                if not include_archived:
                    freshness_status = getattr(entity, "freshness_status", "fresh")
                    if t == "plan":
                        parent_freshness = getattr(entity, "parent_freshness_status", None)
                        if parent_freshness is not None:
                            freshness_status = parent_freshness
                    if freshness_status == "archived":
                        continue
                    if getattr(entity, "merged_into", None) is not None:
                        continue

                try:
                    item_dict = entity.model_dump(mode="json")
                except (ValueError, AttributeError):
                    logger.warning("brain_service.model_dump_failed", type=t, exc_info=True)
                    item_dict = vars(entity)
                type_results.append(
                    SearchResult(
                        type=t,
                        score=score,
                        item=item_dict,
                        parent_id=getattr(entity, "plan_id", None),
                    )
                )

            # Use plural mapping: "decision" → "decisions"
            plural = _TYPE_TO_PLURAL[t]
            setattr(by_type, plural, type_results)
            total += len(type_results)

        # Wire in-memory search quality counters
        if self._collector is not None:
            # Find top score across all types
            all_scores = [r.score for t in ALL_TYPES for r in getattr(by_type, _TYPE_TO_PLURAL[t])]
            top = max(all_scores) if all_scores else 0.0
            self._collector.record_search(top, total)

        # Log access events for search results
        if self._access_logger is not None:
            grouped_results = [
                result for t in ALL_TYPES for result in getattr(by_type, _TYPE_TO_PLURAL[t])
            ]
            logged = self._log_search_accesses(grouped_results)
            if logged:
                logger.debug(
                    "brain_service.access_logged",
                    count=logged,
                    queue_size=self._access_logger._queue.qsize(),
                )
        elif total > 0:
            logger.warning("brain_service.access_logger_none", result_count=total)

        return WhatDoIKnowResponse(
            topic=topic,
            by_type=by_type,
            total=total,
            types_searched=list(ALL_TYPES),
            degraded=_wdika_degraded,
        )
