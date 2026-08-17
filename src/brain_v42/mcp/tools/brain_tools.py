"""brain_v42 MCP tools — brain_* tool registrations (consolidated).

This file is populated by features #629–#635. Each feature adds a group
of @mcp.tool(version="1.0") decorated functions via the register_tools() function.

The register_tools() function signature is stable — server.py depends on it.

Tools implemented here (by feature):
  #629: brain_log_decision, brain_supersede_decision
  #630: brain_learn, brain_validate_learning
  #631: brain_save_snippet, brain_use_snippet  [snippet_tools.py]
  #632: brain_create_runbook, brain_get_runbook, brain_execute_runbook  [runbook_tools.py]
  #633: brain_propose_adr, brain_accept_adr, brain_list_adrs
  #634: brain_set_project_context, brain_update_project_focus  [project_context_tools.py]
  #635: brain_search (group_by_type replaces brain_what_do_i_know_about)

Removed tools (replaced by brain_search params):
  brain_search_decisions -> brain_search(types=["decision"])
  brain_recall           -> brain_search(types=["learning"])
  brain_what_do_i_know_about -> brain_search(group_by_type=True)
  brain_find_snippet     -> brain_search(types=["snippet"])
  brain_search_runbooks  -> brain_search(types=["runbook"])
  brain_get_project_context -> brain_session_start already returns context
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError

from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.crud_tools import _build_adr_list_adapter
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.adr import ADRStatus, AlternativeConsidered
from brain_v42.models.brain import KnowledgeType
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.learning import Confidence, LearningCreate, SourceType
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.models.relation import RelationInput
from brain_v42.repositories.promotion import SourceLearningNotFound

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from brain_v42.services.access_logger import AccessLogger
    from brain_v42.services.adr_service import ADRService
    from brain_v42.services.brain_service import BrainService
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.graph_helpers import RelationAuthorization
    from brain_v42.services.learning_service import LearningService
    from brain_v42.services.project_context_service import ProjectContextService
    from brain_v42.services.runbook_service import RunbookService
    from brain_v42.services.snippet_service import SnippetService

from brain_v42.mcp.tools.formatters import (
    clamp_list_limit,
    format_confirmation,
    format_error,
    format_graph_path,
    format_id,
    format_knowledge_by_type,
    format_related_section,
    format_search_results,
    format_supersession_chain,
)
from brain_v42.mcp.tools.parsing import parse_uuid
from brain_v42.mcp.tools.project_context_tools import register_project_context_tools
from brain_v42.mcp.tools.runbook_tools import register_runbook_tools
from brain_v42.mcp.tools.snippet_tools import register_snippet_tools
from brain_v42.mcp.tools.workflow_guide_tools import register_workflow_guide_tools

logger = structlog.get_logger(__name__)


def register_tools(
    mcp: FastMCP,
    *,
    decision_svc: DecisionService,
    learning_svc: LearningService,
    snippet_svc: SnippetService,
    runbook_svc: RunbookService,
    adr_svc: ADRService,
    project_context_svc: ProjectContextService,
    brain_svc: BrainService,
    metrics_collector: Any | None = None,
    roadmap_svc: Any | None = None,
    graph_svc: Any | None = None,
    access_logger: AccessLogger | None = None,
) -> None:
    """Register all brain_* tools on the FastMCP instance.

    Called once at server startup from server.py __main__ block.
    Tools are defined as closures capturing the injected service instances.
    """
    logger.info("brain_v42.tools.register_tools.called")
    list_adrs = _build_adr_list_adapter(adr_svc)

    # Les métriques ne sont plus posées ici. Elles sont appliquées après
    # enregistrement par brain_v42.metrics.tool_instrumentation, depuis _run_mcp
    # (ticket c352eaaa) : plus de mutation de mcp.tool, plus de dépendance à
    # l'ordre de déclaration, et les passerelles du profil compact sont exclues
    # par construction.
    register_workflow_guide_tools(mcp)
    register_snippet_tools(
        mcp,
        snippet_svc,
        metrics_collector=metrics_collector,
        access_logger=access_logger,
    )
    register_runbook_tools(mcp, runbook_svc, access_logger=access_logger)
    register_project_context_tools(mcp, project_context_svc, roadmap_svc=roadmap_svc)

    # ── Feature #629: Decision tools ─────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_log_decision(
        title: str,
        context: str,
        decision_made: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        consequences: str | None = None,
        project_key: str | None = None,
        tags: list[str] | None = None,
        related_to: list[dict] | None = None,
    ) -> str:
        """Log a technical/architectural decision (the WHY, not just the WHAT).

        Use when picking between alternatives (lib X vs Y, refactor vs rewrite).
        For pure insights/gotchas use brain_learn; for code patterns use
        brain_save_snippet; for durable architecture use brain_propose_adr.

        related_to items: {"id": UUID, "type": one of MOTIVATED_BY,
        IMPLEMENTS, DOCUMENTS, USES, RELATED_TO}.
        """
        data = DecisionCreate(
            title=title,
            description=f"Context: {context}\n\nDecision: {decision_made}",
            reasoning=reasoning,
            alternatives=alternatives or [],
            consequences=consequences,
            project_key=project_key,
            tags=tags or [],
        )
        validated_relations = None
        if related_to:
            validated_relations = [RelationInput(**r).model_dump() for r in related_to]
        decision = await decision_svc.create(data, related_to=validated_relations)
        logger.info("mcp.brain_log_decision", title=title, project_key=project_key)
        return format_confirmation(
            "Decision logged",
            title,
            id=str(decision.id),
            project=project_key,
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_supersede_decision(
        old_decision_id: str,
        title: str,
        context: str,
        decision_made: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        consequences: str | None = None,
        project_key: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Supersede an existing decision with a new one."""
        data = DecisionCreate(
            title=title,
            description=f"Context: {context}\n\nDecision: {decision_made}",
            reasoning=reasoning,
            alternatives=alternatives or [],
            consequences=consequences,
            project_key=project_key,
            tags=tags or [],
        )
        old_uid = parse_uuid(old_decision_id)
        if old_uid is None:
            return format_error(f"Invalid UUID: {old_decision_id}")
        new_decision = await decision_svc.supersede(old_uid, data)
        logger.info(
            "mcp.brain_supersede_decision",
            old_id=old_decision_id,
            new_id=str(new_decision.id),
        )
        return format_confirmation(
            "Decision superseded",
            title,
            id=str(new_decision.id),
            replaces=format_id(old_decision_id),
        )

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_get_supersession_chain(decision_id: str) -> str:
        """Get the full supersession chain for a decision.

        Returns the chain from oldest to newest: old -> new -> newer.

        Args:
            decision_id: UUID of any decision in the chain.
        """
        chain_uid = parse_uuid(decision_id)
        if chain_uid is None:
            return format_error(f"Invalid UUID: {decision_id}")
        chain = await decision_svc.get_supersession_chain(chain_uid)
        logger.info("mcp.brain_get_supersession_chain", decision_id=decision_id, length=len(chain))
        return format_supersession_chain(chain)

    # ── Graph neighborhood traversal ─────────────────────────────────────────
    # Registered only when a graph_svc is wired (Neo4j enabled).

    if graph_svc is not None:

        @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
        async def brain_get_neighbors(
            entity_id: str,
            rel_types: list[str] | None = None,
            depth: int = 1,
        ) -> str:
            """Return the local neighborhood (1-3 hops) around an entity.

            Useful to discover what a Decision / Learning / ADR / Runbook is
            connected to via relations like SUPERSEDES, MOTIVATED_BY,
            IMPLEMENTS, DOCUMENTS, USES, RELATED_TO.

            Args:
                entity_id: UUID of any entity present in the knowledge graph.
                rel_types: Optional list of relation types to traverse. If
                    omitted, all relation types are followed.
                depth: Traversal depth, clamped to [1, 3]. Default 1.
            """
            try:
                eid = UUID(entity_id)
            except (ValueError, AttributeError):
                return format_error(f"Invalid entity_id UUID: '{entity_id}'")

            clamped_depth = max(1, min(3, depth))
            scope = get_dream_project_scope()
            if scope is None:
                neighbors = await graph_svc.get_neighbors(
                    id=eid, rel_types=rel_types, depth=clamped_depth
                )
            else:
                neighbors = await graph_svc.get_neighbors(
                    id=eid,
                    rel_types=rel_types,
                    depth=clamped_depth,
                    project_key=scope.project_key,
                )
                neighbor_ids = [
                    raw_id if isinstance(raw_id, (UUID, str)) else ""
                    for neighbor in neighbors
                    for raw_id in [neighbor.get("id") if isinstance(neighbor, dict) else ""]
                ]
                await scope.revalidate_ids([eid, *neighbor_ids])
            logger.info(
                "mcp.brain_get_neighbors",
                depth=clamped_depth,
                relation_filter_count=len(rel_types or []),
                count=len(neighbors),
            )
            if not neighbors:
                return f'No neighbors found for entity "{format_id(entity_id)}".'
            normalized = [
                {**n, "title": n.get("title") or n.get("label") or "untitled"} for n in neighbors
            ]
            return format_related_section(normalized).lstrip("\n")

        @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
        async def brain_graph_path(
            source_id: str,
            target_id: str,
            max_depth: int = 3,
            rel_types: list[str] | None = None,
        ) -> str:
            """Return the shortest graph path between two entities (1-6 hops).

            Discovers how two seemingly unrelated entities are connected —
            e.g., via a SUPERSEDES chain, through transitive MOTIVATED_BY/
            IMPLEMENTS links, or shared USES references. By default excludes
            BELONGS_TO_DOMAIN edges to avoid tautological paths via shared
            Domain nodes.

            Args:
                source_id: UUID of the start entity.
                target_id: UUID of the end entity.
                max_depth: Maximum hops, clamped to [1, 6]. Default 3.
                rel_types: Optional whitelist of relation types to follow.
                    If omitted, follows all canonical types except
                    BELONGS_TO_DOMAIN.

            Returns:
                Markdown-ish single-line path, or a "no path" message.
            """
            try:
                sid = UUID(source_id)
                tid = UUID(target_id)
            except (ValueError, AttributeError):
                return format_error("Invalid entity UUID")

            clamped_depth = max(1, min(6, max_depth))
            scope = get_dream_project_scope()
            if scope is None:
                path = await graph_svc.get_path(
                    sid, tid, max_depth=clamped_depth, rel_types=rel_types
                )
            else:
                path = await graph_svc.get_path(
                    sid,
                    tid,
                    max_depth=clamped_depth,
                    rel_types=rel_types,
                    project_key=scope.project_key,
                )
                path_ids = [
                    raw_id if isinstance(raw_id, (UUID, str)) else ""
                    for node in path
                    for raw_id in [node.get("id") if isinstance(node, dict) else ""]
                ]
                await scope.revalidate_ids([sid, tid, *path_ids])
            logger.info(
                "mcp.brain_graph_path",
                depth=clamped_depth,
                relation_filter_count=len(rel_types or []),
                hops=max(0, len(path) - 1) if path else 0,
            )
            if not path:
                return (
                    f'No path found between "{format_id(source_id)}" and '
                    f'"{format_id(target_id)}" within depth {clamped_depth}.'
                )
            return format_graph_path(path)

    # ── Feature #630: Learning tools ─────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_learn(
        topic: str,
        insight: str,
        source: str | None = None,
        source_type: SourceType = "experience",
        confidence: Confidence = "medium",
        project_key: str | None = None,
        tags: list[str] | None = None,
        related_to: list[dict] | None = None,
    ) -> str:
        """Record an insight, gotcha, or discovery (last-resort tool).

        Use ONLY for pure insights with no code and no choice between
        options. Otherwise prefer brain_log_decision (choices),
        brain_save_snippet (code), brain_create_runbook (procedures),
        brain_propose_adr (durable architecture).

        source_type: experience | documentation | code_review | bug |
        external | article | video | book | conversation | research.
        confidence: low | medium | high.
        related_to items: {"id": UUID, "type": MOTIVATED_BY | IMPLEMENTS
        | DOCUMENTS | USES | RELATED_TO}.
        """
        data = LearningCreate(
            topic=topic,
            insight=insight,
            source=source,
            source_type=source_type,
            confidence=confidence,
            project_key=project_key,
            tags=tags or [],
        )
        validated_relations = None
        if related_to:
            validated_relations = [RelationInput(**r).model_dump() for r in related_to]
        scope = get_dream_project_scope()
        if scope is None:
            learning = await learning_svc.create(data, related_to=validated_relations)
        else:
            learning = await learning_svc.create(
                data,
                related_to=validated_relations,
                authorization=cast("RelationAuthorization", scope),
            )
        return format_confirmation(
            "Learned",
            topic,
            id=str(learning.id),
            project=project_key,
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_validate_learning(learning_id: str) -> str:
        """Mark a learning as validated (sets validated_at to now)."""
        validate_uid = parse_uuid(learning_id)
        if validate_uid is None:
            return format_error(f"Invalid UUID: {learning_id}")
        learning = await learning_svc.validate(validate_uid)
        if learning is None:
            return format_error(f"Learning '{format_id(learning_id)}' not found")
        return format_confirmation(
            "Learning validated",
            learning.topic,
            id=str(learning.id),
        )

    # ── Feature #633: ADR tools ───────────────────────────────────────────────

    from brain_v42.models.adr import ADRCreate  # noqa: PLC0415

    @mcp.tool(version="1.1", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_propose_adr(
        title: str,
        context: str,
        decision: str,
        consequences: str,
        project_key: str,
        alternatives_considered: list[AlternativeConsidered] | None = None,
        tags: list[str] | None = None,
        source_learning_id: str | None = None,
        auto_accept: bool = False,
        dream_run_id: int | None = None,
    ) -> str:
        """Propose (or graduate) an Architecture Decision Record (ADR).

        Backwards-compatible: callers that pass only the original kwargs behave
        exactly as before — an ADR is created in status='proposed'.

        Dream-agent path: set source_learning_id + auto_accept=True together to
        graduate a mature insight directly into an accepted ADR via one atomic
        transaction that also updates the source learning's metadata and writes
        a dream_promotions audit row. Both kwargs must be set together.
        """
        if source_learning_id is not None and not auto_accept:
            return format_error("source_learning_id requires auto_accept=True (Dream-only path)")
        if auto_accept and source_learning_id is None:
            return format_error("auto_accept=True requires source_learning_id (Dream-only path)")

        data = ADRCreate(
            title=title,
            context=context,
            decision=decision,
            consequences=consequences,
            project_key=project_key,
            alternatives_considered=alternatives_considered or [],
            tags=tags or [],
        )
        scope = get_dream_project_scope()

        if source_learning_id is not None:
            src_uid = parse_uuid(source_learning_id)
            if src_uid is None:
                return format_error(f"Invalid UUID: {source_learning_id}")
            try:
                if scope is None:
                    adr = await adr_svc.create_with_promotion(
                        data=data,
                        source_learning_id=src_uid,
                        auto_accept=True,
                        dream_run_id=dream_run_id,
                    )
                else:
                    adr = await adr_svc.create_with_promotion(
                        data=data,
                        source_learning_id=src_uid,
                        auto_accept=True,
                        dream_run_id=dream_run_id,
                        project_key=scope.project_key,
                        authorization=cast("RelationAuthorization", scope),
                    )
            except SourceLearningNotFound:
                if scope is None:
                    raise
                return format_error("source learning not found")
            except IntegrityError:
                return format_error(
                    f"source_learning_id '{format_id(source_learning_id)}' already "
                    f"materialized (duplicate promotion blocked by unique index)"
                )
            logger.info(
                "mcp.brain_propose_adr.promoted",
                adr_id=str(adr.id),
                source_learning_id=source_learning_id,
            )
            return format_confirmation(
                f"ADR #{adr.number} accepted (auto-graduated from learning)",
                title,
                id=str(adr.id),
                project=project_key,
            )

        if scope is None:
            adr = await adr_svc.create(data)
        else:
            adr = await adr_svc.create(
                data,
                authorization=cast("RelationAuthorization", scope),
            )
        logger.info("mcp.brain_propose_adr", adr_id=str(adr.id), project_key=project_key)
        return format_confirmation(
            f"ADR #{adr.number} proposed",
            title,
            id=str(adr.id),
            project=project_key,
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_accept_adr(adr_id: str) -> str:
        """Accept a proposed ADR, setting its status to 'accepted' and recording decided_at.

        Args:
            adr_id: UUID string of the ADR to accept.

        Returns:
            Confirmation string or error if not found.
        """
        accept_uid = parse_uuid(adr_id)
        if accept_uid is None:
            return format_error(f"Invalid UUID: {adr_id}")
        adr = await adr_svc.accept(accept_uid)
        if adr is None:
            return format_error(f"ADR '{format_id(adr_id)}' not found")
        logger.info("mcp.brain_accept_adr", adr_id=adr_id)
        return format_confirmation(
            f"ADR #{adr.number} accepted",
            adr.title,
            id=str(adr.id),
        )

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list_adrs(
        project_key: str | None = None,
        status: ADRStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List ADRs with optional filters by project and/or status.

        Args:
            project_key: Optional project scope filter.
            status: Optional status filter — 'proposed', 'accepted', 'deprecated', 'superseded'.
            limit: Maximum number of results (default 20, clamped server-side to [1, 100]).
            offset: Number of results to skip (default 0).

        Returns:
            Formatted markdown list of ADRs.
        """
        return await list_adrs(
            project_key,
            status,
            limit,
            offset,
            False,
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_deprecate_adr(adr_id: str, reason: str | None = None) -> str:
        """Deprecate an ADR, setting status to 'deprecated'.

        Args:
            adr_id: UUID of the ADR to deprecate.
            reason: Optional reason for deprecation (appended to consequences).
        """
        deprecate_uid = parse_uuid(adr_id)
        if deprecate_uid is None:
            return format_error(f"Invalid UUID: {adr_id}")
        adr = await adr_svc.deprecate(deprecate_uid, reason=reason)
        if adr is None:
            return format_error(f"ADR '{format_id(adr_id)}' not found")
        logger.info("mcp.brain_deprecate_adr", adr_id=adr_id, reason=reason)
        return format_confirmation(
            f"ADR #{adr.number} deprecated",
            adr.title,
            id=str(adr.id),
        )

    # ── Feature #635: Global search tools ────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_search(
        query: str,
        types: list[KnowledgeType] | None = None,
        project_key: str | None = None,
        project_group: str | None = None,
        limit: int = 20,
        min_score: float = 0.2,
        include_archived: bool = False,
        group_by_type: bool = False,
        tags: list[str] | None = None,
        include_related: bool = False,
        full: bool = False,
    ) -> str:
        """Hybrid semantic search (pgvector + reranker) across knowledge.

        types: subset of {decision, learning, snippet, runbook, adr, plan}.
        project_key XOR project_group for scoping. tags filter by overlap.
        Results render with [s:score] prefix, sorted by score desc.
        group_by_type=True groups output sections (former what_do_i_know_about).
        include_related=True appends a "### Related" graph-neighbour block;
        default off — use brain_get_neighbors for targeted traversal instead.
        full=True restores complete decision/learning bodies; by default their
        compact summaries bound each search item. Use brain_get for full entity detail.
        limit: max results returned (default 20, clamped server-side to [1, 100]).
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        types = types or None
        limit, limit_notice = clamp_list_limit(limit)
        t0 = time.monotonic()

        if group_by_type:
            wdik_response = await brain_svc.what_do_i_know_about(
                topic=query,
                project_key=project_key,
                project_group=project_group,
                limit=limit,
                min_score=min_score,
                include_archived=include_archived,
            )
            latency_ms = (time.monotonic() - t0) * 1000

            if metrics_collector is not None:
                all_scores = [
                    r.score
                    for attr in ["decisions", "learnings", "snippets", "runbooks", "adrs"]
                    for r in getattr(wdik_response.by_type, attr)
                ]
                await metrics_collector.record_search_log(
                    tool_name="brain_search",
                    project_key=project_key,
                    result_count=wdik_response.total,
                    top_score=max(all_scores) if all_scores else None,
                    avg_score=sum(all_scores) / len(all_scores) if all_scores else None,
                    latency_ms=latency_ms,
                )

            logger.info(
                "mcp.brain_search.grouped",
                query_length=len(query),
                project_key=project_key,
                limit=limit,
            )
            return (
                format_knowledge_by_type(
                    wdik_response.by_type,
                    topic=query,
                    degraded=wdik_response.degraded,
                    full=full,
                )
                + limit_notice
            )

        search_response = await brain_svc.search(
            query=query,
            types=types,
            project_key=project_key,
            project_group=project_group,
            limit=limit,
            min_score=min_score,
            include_archived=include_archived,
            tags=tags,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if metrics_collector is not None:
            scores = [r.score for r in search_response.results]
            await metrics_collector.record_search_log(
                tool_name="brain_search",
                project_key=project_key,
                result_count=search_response.total,
                top_score=max(scores) if scores else None,
                avg_score=sum(scores) / len(scores) if scores else None,
                latency_ms=latency_ms,
            )

        logger.info(
            "mcp.brain_search",
            query_length=len(query),
            project_key=project_key,
            limit=limit,
        )
        output = format_search_results(
            search_response.results,
            query=query,
            degraded=search_response.degraded,
            full=full,
        )
        if include_related and search_response.related:
            output += "\n" + format_related_section(search_response.related)
        return output + limit_notice
