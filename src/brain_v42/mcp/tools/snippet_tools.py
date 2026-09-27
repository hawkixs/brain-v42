"""MCP tools for snippet management: brain_save_snippet, brain_use_snippet.

Note: brain_find_snippet has been removed. Use brain_search(types=["snippet"]) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.claim_writes import (
    claim_write_log_fields,
    claims_confirmation,
    gated_claim_session,
    persist_claims,
    resolve_claim_inputs,
)
from brain_v42.mcp.tools.formatters import (
    format_confirmation,
    format_error,
    format_id,
)
from brain_v42.mcp.tools.parsing import parse_uuid
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
)
from brain_v42.models.relation import RelationInput
from brain_v42.models.snippet import SnippetLanguage
from brain_v42.provenance import get_current_actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.facts.registry import FactRegistry
    from brain_v42.facts.verification import ClaimVerificationService
    from brain_v42.metrics.collector import MetricsCollector
    from brain_v42.services.access_logger import AccessLogger
    from brain_v42.services.graph_helpers import RelationAuthorization
    from brain_v42.services.snippet_service import SnippetService

logger = structlog.get_logger(__name__)


def register_snippet_tools(
    mcp: Any,
    snippet_svc: SnippetService,
    metrics_collector: MetricsCollector | None = None,
    access_logger: AccessLogger | None = None,
    fact_registry: FactRegistry | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    claim_verification_svc: ClaimVerificationService | None = None,
) -> None:
    """Register snippet MCP tools on the FastMCP instance via closures."""

    @mcp.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_save_snippet(
        title: str,
        intention: str,
        code: str,
        language: SnippetLanguage,
        dependencies: list[str] | None = None,
        usage_example: str | None = None,
        gotchas: str | None = None,
        project_key: str | None = None,
        tags: list[str] | None = None,
        related_to: list[dict] | None = None,
        claims: list[dict] | None = None,
    ) -> str:
        """Save a reusable code snippet keyed by its intention (semantic).

        Use for code patterns/queries/configs you'd want to retrieve later
        by purpose, not by name. ``intention`` is the embedded field — write
        it as a sentence describing WHEN to use the snippet.

        related_to items: {"id": UUID, "type": MOTIVATED_BY | IMPLEMENTS
        | DOCUMENTS | USES | RELATED_TO}.
        """
        from brain_v42.models.snippet import SnippetCreate

        data = SnippetCreate(
            title=title,
            intention=intention,
            code=code,
            language=language,
            dependencies=dependencies or [],
            usage_example=usage_example,
            gotchas=gotchas,
            project_key=project_key,
            tags=tags or [],
        )
        validated_relations = None
        if related_to:
            validated_relations = [RelationInput(**r).model_dump() for r in related_to]
        scope = get_dream_project_scope()
        if not claims:
            if scope is None:
                snippet = await snippet_svc.create(data, related_to=validated_relations)
            else:
                snippet = await snippet_svc.create(
                    data,
                    related_to=validated_relations,
                    authorization=cast("RelationAuthorization", scope),
                )
            logger.info(
                "mcp.brain_save_snippet",
                title_length=len(title),
                language_supplied=bool(snippet.language),
            )
            return format_confirmation(
                "Snippet saved", snippet.title, id=str(snippet.id), lang=snippet.language
            )

        if fact_registry is None or session_factory is None:
            raise RuntimeError("declared claims require the fact registry and a session factory")
        resolved = await resolve_claim_inputs(fact_registry, claims)
        if project_key is None:
            raise ValueError("declared claims require project_key")
        declared_at = datetime.now(UTC)
        async with gated_claim_session(
            session_factory, claim_verification_svc, resolved
        ) as session:
            snippet = await snippet_svc.create(data, session=session)
            outcomes = await persist_claims(
                session,
                entry_id=snippet.id,
                entity_type="snippet",
                project_key=project_key,
                resolved=resolved,
                declared_by=get_current_actor(),
                declared_at=declared_at,
                verification=claim_verification_svc,
            )
        snippet = await snippet_svc.enrich_created(
            snippet,
            data,
            related_to=validated_relations,
            authorization=cast("RelationAuthorization", scope) if scope is not None else None,
        )
        logger.info(
            "mcp.brain_save_snippet",
            title_length=len(title),
            language_supplied=bool(snippet.language),
            **claim_write_log_fields(outcomes),
        )
        return format_confirmation(
            "Snippet saved",
            snippet.title,
            id=str(snippet.id),
            lang=snippet.language,
            claims=claims_confirmation(outcomes),
        )

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_use_snippet(snippet_id: str) -> str:
        """Increment usage counter for a snippet (use_count + 1, last_used_at = now).

        Returns the updated snippet. Returns an error string if snippet_id is not found.
        """
        use_uid = parse_uuid(snippet_id)
        if use_uid is None:
            return format_error(f"Invalid UUID: {snippet_id}")
        snippet = await snippet_svc.increment_use(use_uid)
        if snippet is None:
            logger.warning("mcp.brain_use_snippet.not_found", snippet_id=snippet_id)
            return format_error(f"Snippet '{format_id(snippet_id)}' not found")
        logger.info(
            "mcp.brain_use_snippet",
            snippet_id=snippet_id,
            use_count=snippet.use_count,
        )
        result = format_confirmation("Snippet used", snippet.title, use_count=snippet.use_count)
        if access_logger is not None:
            access_logger.log_access("snippet", snippet.id, "use")
        return result
