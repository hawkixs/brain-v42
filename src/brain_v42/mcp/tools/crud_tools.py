"""Generic CRUD MCP tools: brain_get, brain_delete, brain_update, brain_list.

Routes by entity_type to the appropriate domain service, using the same
dispatch pattern as decay_tools.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.formatters import (
    clamp_list_limit,
    format_adr_detail,
    format_adrs,
    format_confirmation,
    format_decision_detail,
    format_decisions,
    format_error,
    format_learning_detail,
    format_learnings,
    format_runbook,
    format_runbooks,
    format_snippet_detail,
    format_snippets,
)
from brain_v42.mcp.tools.parsing import resolve_entity_id
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _READ_ANNOTATIONS,
    _TERMINAL_ANNOTATIONS,
)
from brain_v42.models.adr import ADRUpdate
from brain_v42.models.brain import ALL_TYPES, KnowledgeType, MutableKnowledgeType
from brain_v42.models.decision import DecisionUpdate
from brain_v42.models.indexed_plan import IndexedPlan
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Confidence, LearningUpdate
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.models.relation import RelationInput
from brain_v42.models.runbook import RunbookUpdate
from brain_v42.models.snippet import SnippetLanguage, SnippetUpdate
from brain_v42.services.graph_helpers import graph_create_relation_logged

if TYPE_CHECKING:
    from brain_v42.services.access_logger import AccessLogger
    from brain_v42.services.adr_service import ADRService
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.graph_helpers import RelationAuthorization
    from brain_v42.services.learning_service import LearningService
    from brain_v42.services.runbook_service import RunbookService
    from brain_v42.services.snippet_service import SnippetService

logger = structlog.get_logger(__name__)

_PLAN_DETAIL_MAX_CHARS = 8_000


def _build_adr_list_adapter(
    adr_svc: ADRService,
) -> Callable[[str | None, str | None, int, int, bool], Awaitable[str]]:
    """Build the shared ADR-list adapter used by both public MCP surfaces."""

    async def list_adrs(
        project_key: str | None,
        status: str | None,
        limit: int,
        offset: int,
        include_archived: bool,
    ) -> str:
        project_key = canonicalize_project_key(project_key, strict=False)
        clamped_limit, limit_notice = clamp_list_limit(limit)
        adrs = await adr_svc.list_all(
            project_key=project_key,
            status=status,
            limit=clamped_limit,
            offset=offset,
            include_archived=include_archived,
        )
        return format_adrs(adrs, project_key=project_key) + limit_notice

    return list_adrs


def _format_plan_detail(
    plan: IndexedPlan,
    chunks: list[IndexedPlanChunk],
    max_chars: int = _PLAN_DETAIL_MAX_CHARS,
) -> str:
    """Format a plan with its chunks, bounded by *max_chars*.

    When the accumulated output would exceed *max_chars*, chunk rendering stops
    and a trailing notice is appended — no content is silently dropped.  The
    notice names the number of omitted chunks and suggests using
    ``section_path`` filtering (once available) to paginate.
    """
    header_lines = [
        f"# {plan.title}",
        f"**Type:** {plan.plan_type}  |  **Status:** {plan.status}  |  **Project:** {plan.project_key}",
        f"**File:** `{plan.file_path}`",
        f"**Chunks:** {plan.chunk_count}  |  **Words:** {plan.word_count}",
    ]
    if plan.summary:
        header_lines += ["", f"**Summary:** {plan.summary}"]
    if plan.tags:
        header_lines += [f"**Tags:** {', '.join(plan.tags)}"]
    header_lines += ["", "---", ""]

    header = "\n".join(header_lines)
    budget = max_chars - len(header)

    rendered_chunks: list[str] = []
    chars_used = 0
    for chunk in chunks:
        chunk_text = "\n".join(
            [
                f"## §{chunk.section_order + 1}. {chunk.section_path}",
                "",
                chunk.content,
                "",
            ]
        )
        if chars_used + len(chunk_text) > budget:
            # Break at first overflow — contiguous prefix guarantees no §-numbering gaps.
            break
        rendered_chunks.append(chunk_text)
        chars_used += len(chunk_text)

    omitted = len(chunks) - len(rendered_chunks)
    body = "\n".join(rendered_chunks)
    result = header + body

    if omitted:
        result += (
            f"\n\n… ({omitted} chunk(s) omis — affinez avec"
            f" brain_search(query=…, types=['plan'])"
            f" ou passez max_chars=… à brain_get pour élargir le budget)"
        )

    return result


def _format_plan_list(plans: list[IndexedPlan]) -> str:
    """Format a list of plans into a brief one-line-per-plan listing."""
    if not plans:
        return "0 plans found."
    header = f"{len(plans)} plan(s) found.\n"
    lines = [header]
    for plan in plans:
        tags_str = f" [{', '.join(plan.tags)}]" if plan.tags else ""
        lines.append(
            f"- **{plan.title}** ({plan.plan_type}/{plan.status})"
            f"  `{plan.project_key}`{tags_str}  [{plan.id}]"
        )
    return "\n".join(lines)


#: The provenance a DREAM writer stamps on a freshness transition. `judgment`
#: belongs to the closed vocabulary of 043's `CHECK` — four values — and had NO
#: writer: reserved, unused, and it describes exactly what REORG does, the only
#: phase the server grants a write to. No migration is therefore needed.
#: Exercised for the first time on 2026-08-22 against `brain_test`: accepted,
#: date stamped, transaction rolled back; a value outside the vocabulary refused
#: by the same constraint.
DREAM_FRESHNESS_SOURCE = "judgment"

#: The field the SERVER sets and the caller cannot forge.
_SERVER_ONLY_UPDATE_FIELDS = frozenset({"freshness_source"})


def register_crud_tools(
    mcp: Any,
    *,
    decision_svc: DecisionService,
    learning_svc: LearningService,
    snippet_svc: SnippetService,
    runbook_svc: RunbookService,
    adr_svc: ADRService,
    session_factory: async_sessionmaker[AsyncSession],
    access_logger: AccessLogger | None = None,
) -> None:
    """Register generic CRUD MCP tools (brain_get, brain_delete, brain_update, brain_list)."""

    list_adrs = _build_adr_list_adapter(adr_svc)

    _services: dict[str, Any] = {
        "decision": decision_svc,
        "learning": learning_svc,
        "snippet": snippet_svc,
        "runbook": runbook_svc,
        "adr": adr_svc,
    }

    _detail_formatters = {
        "decision": format_decision_detail,
        "learning": format_learning_detail,
        "snippet": format_snippet_detail,
        "runbook": format_runbook,
        "adr": format_adr_detail,
    }

    _update_models: dict[str, type[BaseModel]] = {
        "decision": DecisionUpdate,
        "learning": LearningUpdate,
        "snippet": SnippetUpdate,
        "runbook": RunbookUpdate,
        "adr": ADRUpdate,
    }

    # ── brain_get ─────────────────────────────────────────────────────────

    @mcp.tool(version="1.2", annotations=_READ_ANNOTATIONS)
    async def brain_get(
        entity_type: KnowledgeType,
        entity_id: str,
        max_chars: int = _PLAN_DETAIL_MAX_CHARS,
    ) -> str:
        """Fetch a single entity by type and UUID (or unique id prefix).

        entity_id accepts a full UUID or a git-style short id: at least the
        first 8 hex chars, hyphens optional. An ambiguous prefix returns the
        matching UUIDs. plan is the exception — full UUID only.

        For entity_type='plan', content is bounded by max_chars (default 8000).
        When the plan exceeds the budget a trailing notice lists the number of
        omitted chunks — increase max_chars to retrieve more content.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr, plan
            entity_id: UUID of the entity, or a unique ≥8-hex-char prefix
                (prefix not supported for plan)
            max_chars: Max character budget for plan content (default 8000).
                Only applies when entity_type='plan'. Increase to retrieve
                more chunks when a truncation notice appears.
        """
        if entity_type not in ALL_TYPES:
            return format_error(f"Unknown entity type: {entity_type}. Use: {', '.join(ALL_TYPES)}")

        scope = get_dream_project_scope()

        if entity_type == "plan":
            # PgIndexedPlanRepo is not a BasePgRepository — no prefix
            # resolution for plans (v1), exact UUID only.
            try:
                uid = UUID(entity_id)
            except ValueError:
                return format_error(f"Invalid UUID: {entity_id}")

            from brain_v42.repositories.pg_indexed_plan_repo import (
                PgIndexedPlanRepo,  # noqa: PLC0415
            )

            async with session_factory() as session:
                repo = PgIndexedPlanRepo(session)
                if scope is None:
                    result = await repo.get_with_chunks(uid)
                else:
                    result = await repo.get_with_chunks(uid, project_key=scope.project_key)
            if result is None:
                return format_error(f"plan {entity_id} not found")
            plan, chunks = result
            formatted = _format_plan_detail(plan, chunks, max_chars=max_chars)
            if access_logger is not None:
                access_logger.log_access("plan", plan.id, "get_by_id")
            return formatted

        svc = _services[entity_type]
        resolved = await resolve_entity_id(entity_id, svc.resolve_id_prefix, label=entity_type)
        if isinstance(resolved, str):
            return format_error(resolved)
        if scope is None:
            entity = await svc.get_by_id(resolved)
        else:
            entity = await svc.get_by_id(resolved, project_key=scope.project_key)

        if entity is None:
            return format_error(f"{entity_type} {resolved} not found")

        formatter = _detail_formatters[entity_type]
        formatted = formatter(entity)  # type: ignore[operator]
        if access_logger is not None:
            access_logger.log_access(entity_type, entity.id, "get_by_id")
        return formatted  # type: ignore[no-any-return]

    # ── brain_delete ──────────────────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_TERMINAL_ANNOTATIONS)
    async def brain_delete(entity_type: KnowledgeType, entity_id: str) -> str:
        """Delete an entity by type and UUID.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr, plan
            entity_id: UUID of the entity to delete
        """
        if entity_type not in ALL_TYPES:
            return format_error(f"Unknown entity type: {entity_type}. Use: {', '.join(ALL_TYPES)}")

        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        scope = get_dream_project_scope()

        if entity_type == "plan":
            from brain_v42.repositories.pg_indexed_plan_repo import (
                PgIndexedPlanRepo,  # noqa: PLC0415
            )

            async with session_factory() as session:
                repo = PgIndexedPlanRepo(session)
                if scope is None:
                    deleted = await repo.delete(uid)
                else:
                    deleted = await repo.delete(uid, project_key=scope.project_key)
            if not deleted:
                return format_error(f"plan {entity_id} not found")
            logger.info("brain_delete", entity_type="plan", entity_id=entity_id)
            return format_confirmation("Deleted", "", id=str(entity_id), type="plan")

        svc = _services[entity_type]
        if scope is None:
            deleted = await svc.delete(uid)
        else:
            deleted = await svc.delete(uid, project_key=scope.project_key)

        if not deleted:
            return format_error(f"{entity_type} {entity_id} not found")

        logger.info("brain_delete", entity_type=entity_type, entity_id=entity_id)
        return format_confirmation("Deleted", "", id=str(entity_id), type=entity_type)

    # ── brain_update ──────────────────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_update(
        entity_type: MutableKnowledgeType,
        entity_id: str,
        fields: dict,
        related_to: list[dict] | None = None,
    ) -> str:
        """Update an entity by type and UUID with partial fields.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr
            entity_id: UUID of the entity to update
            fields: Dict of fields to update (validated via Pydantic model)
            related_to: Optional list of graph relations to add/update.
                Each item must have keys ``id`` (UUID string) and ``type``
                (one of: MOTIVATED_BY, IMPLEMENTS, DOCUMENTS, USES, RELATED_TO).
        """
        if entity_type not in ALL_TYPES:
            return format_error(f"Unknown entity type: {entity_type}. Use: {', '.join(ALL_TYPES)}")

        if entity_type == "plan":
            return format_error(
                "plans are immutable; re-run brain_reindex_plans to refresh from source files"
            )

        try:
            uid = UUID(entity_id)
        except ValueError:
            return format_error(f"Invalid UUID: {entity_id}")

        update_cls = _update_models[entity_type]
        forged = _SERVER_ONLY_UPDATE_FIELDS.intersection(fields)
        if forged:
            # Fail-closed BEFORE any write: a forged provenance would sign a
            # transition in someone else's name. 043 says it in its own body —
            # "a false provenance, which is believed, instead of a missing
            # provenance, which is seen".
            return format_error(f"server-controlled field(s) refused: {', '.join(sorted(forged))}")
        try:
            update_data = update_cls(**fields)
        except ValidationError as e:
            # List the valid fields so the caller can self-correct — the
            # body of a decision lives in `description` (Context + Decision
            # merged), not `decision_made` (learning 0cd1109a footgun).
            valid = ", ".join(
                name for name in update_cls.model_fields if name not in _SERVER_ONLY_UPDATE_FIELDS
            )
            return format_error(f"Invalid fields: {e}. Valid fields: {valid}")

        svc = _services[entity_type]
        scope = get_dream_project_scope()
        # The SAME predicate as the repository: `model_dump(exclude_none=True)`
        # is exactly what `repo.update` turns into columns. Testing the typed
        # `BaseModel` would require an unchecked `getattr`; testing the raw
        # `fields` would also stamp a `freshness_status: None`, which writes
        # nothing. Here, stamping and writing are true together.
        if scope is not None and "freshness_status" in update_data.model_dump(exclude_none=True):
            # A DREAM write of `freshness_status` declares its provenance.
            # Bounded to THIS field: stamping a write that does not touch the
            # status would describe a transition that did not happen. And
            # bounded to the scope: outside the dream the transition stays
            # silent, HENCE counted by `post_run_alert.fetch_mute_transitions` —
            # which is what makes the next unrecorded source visible instead of
            # conflating it with REORG.
            update_data = update_data.model_copy(
                update={"freshness_source": DREAM_FRESHNESS_SOURCE}
            )
        graph = getattr(svc, "_graph", None)
        validated_relations: list[dict[str, Any]] = []
        if scope is not None and related_to and graph is not None:
            validated_relations = [
                RelationInput(**relation).model_dump() for relation in related_to
            ]
            await scope.revalidate_ids(
                [uid, *(UUID(relation["id"]) for relation in validated_relations)]
            )

        if scope is None:
            updated = await svc.update(uid, update_data)
        else:
            updated = await svc.update(uid, update_data, project_key=scope.project_key)

        if updated is None:
            return format_error(f"{entity_type} {entity_id} not found")

        # Add graph relations if provided and service has a graph.
        if related_to and graph is not None:
            if scope is not None:
                for rel in validated_relations:
                    await graph_create_relation_logged(
                        graph,
                        uid,
                        UUID(rel["id"]),
                        rel["type"],
                        authorization=cast("RelationAuthorization", scope),
                    )
            else:
                try:
                    validated_relations = [
                        RelationInput(**relation).model_dump() for relation in related_to
                    ]
                    for rel in validated_relations:
                        await graph_create_relation_logged(
                            graph,
                            uid,
                            UUID(rel["id"]),
                            rel["type"],
                        )
                except Exception:
                    logger.warning(
                        "brain_update.graph_relations_failed",
                        entity_type=entity_type,
                        entity_id=entity_id,
                    )

        logger.info("brain_update", entity_type=entity_type, entity_id=entity_id)
        return format_confirmation("Updated", "", id=str(entity_id), type=entity_type)

    # ── brain_list ────────────────────────────────────────────────────────

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list(
        entity_type: KnowledgeType,
        project_key: str | None = None,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        confidence: Confidence | None = None,
        language: SnippetLanguage | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
        summary_only: bool = False,
    ) -> str:
        """List entities of a given type with optional filters.

        Args:
            entity_type: One of: decision, learning, snippet, runbook, adr, plan
            project_key: Filter by project (required for runbook)
            limit: Maximum results (default 20, clamped server-side to [1, 100])
            offset: Pagination offset (default 0)
            status: Filter by status (decision, adr, plan)
            confidence: Filter by confidence (learning)
            language: Filter by language (snippet)
            tags: Filter by tags (decision, learning)
            include_archived: When False (default), exclude merged/archived entities
            summary_only: When True (decision/learning only), drop body content
                and surface metadata (project_key, tags, access_count,
                freshness_status). For full-corpus pagination such as Dream
                REORG Part 1 where the body is unused.
        """
        if entity_type not in ALL_TYPES:
            return format_error(f"Unknown entity type: {entity_type}. Use: {', '.join(ALL_TYPES)}")

        if entity_type == "adr":
            return await list_adrs(
                project_key,
                status,
                limit,
                offset,
                include_archived,
            )

        project_key = canonicalize_project_key(project_key, strict=False)

        # Server cap to bound the token budget — ANNOUNCED, never silent: a page
        # truncated silently is indistinguishable from a corpus that ends there.
        limit, limit_notice = clamp_list_limit(limit)

        if entity_type == "decision":
            decisions = await decision_svc.list_all(
                project_key=project_key,
                status=status,
                tags=tags,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
            return format_decisions(decisions, summary_only=summary_only) + limit_notice

        if entity_type == "learning":
            learnings = await learning_svc.list_all(
                project_key=project_key,
                confidence=confidence,
                tags=tags,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
            return format_learnings(learnings, summary_only=summary_only) + limit_notice

        if entity_type == "snippet":
            snippets = await snippet_svc.list_snippets(
                project_key=project_key,
                language=language,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
            return format_snippets(snippets) + limit_notice

        if entity_type == "runbook":
            if project_key is None:
                return format_error("project_key is required for listing runbooks")
            runbooks = await runbook_svc.list_by_project(
                project_key=project_key,
                limit=limit,
                offset=offset,
            )
            return format_runbooks(runbooks, project_key=project_key) + limit_notice

        if entity_type == "plan":
            from brain_v42.repositories.pg_indexed_plan_repo import (
                PgIndexedPlanRepo,  # noqa: PLC0415
            )

            async with session_factory() as session:
                repo = PgIndexedPlanRepo(session)
                plans = await repo.list_plans(
                    project_key=project_key,
                    plan_type=None,
                    status=status,
                    limit=limit,
                    offset=offset,
                )
            return _format_plan_list(plans) + limit_notice

        raise RuntimeError(f"Unhandled knowledge type: {entity_type}")
