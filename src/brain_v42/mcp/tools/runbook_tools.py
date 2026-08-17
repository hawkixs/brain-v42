"""MCP tools for runbook operations: brain_create_runbook, brain_get_runbook, brain_execute_runbook.

Note: brain_search_runbooks has been removed. Use brain_search(types=["runbook"]) instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy.exc import IntegrityError

from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.formatters import (
    format_confirmation,
    format_error,
    format_id,
    format_runbook,
    format_runbooks,
)
from brain_v42.mcp.tools.parsing import parse_uuid, resolve_entity_id
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.models.runbook import ExecutionStatus, RunbookCreate
from brain_v42.repositories.promotion import SourceLearningNotFound
from brain_v42.services.runbook_service import RunbookService

if TYPE_CHECKING:
    from brain_v42.services.access_logger import AccessLogger
    from brain_v42.services.graph_helpers import RelationAuthorization

_RUNBOOK_LIST_LIMIT_MAX = 50

logger = structlog.get_logger(__name__)


def register_runbook_tools(
    mcp: Any,
    runbook_svc: RunbookService,
    access_logger: AccessLogger | None = None,
) -> None:
    """Register the runbook MCP tools on the FastMCP server."""

    @mcp.tool(version="1.1", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_create_runbook(
        title: str,
        description: str,
        project_key: str,
        trigger: str,
        steps: list[dict],
        prerequisites: list[str] | None = None,
        rollback_steps: list[dict] | None = None,
        estimated_duration: str | None = None,
        tags: list[str] | None = None,
        source_learning_id: str | None = None,
        dream_run_id: int | None = None,
    ) -> str:
        """Create (or graduate) an operational runbook.

        Backwards-compatible: callers that pass only the original kwargs behave
        exactly as before.

        Dream-agent path: set source_learning_id to graduate a mature insight
        directly into a runbook via one atomic transaction that also updates
        the source learning's metadata and writes a dream_promotions audit row.
        No auto_accept — runbooks have no proposed/accepted state machine.
        """
        # Steps that omit ``order`` are numbered by RunbookBase's validator, so
        # create and update agree on what a valid step is (ticket 2af71e69).
        data = RunbookCreate(
            title=title,
            description=description,
            project_key=project_key,
            trigger=trigger,
            steps=steps,  # type: ignore[arg-type]
            prerequisites=prerequisites or [],
            rollback_steps=rollback_steps or [],  # type: ignore[arg-type]
            estimated_duration=estimated_duration,
            tags=tags or [],
        )
        scope = get_dream_project_scope()

        if source_learning_id is not None:
            src_uid = parse_uuid(source_learning_id)
            if src_uid is None:
                return format_error(f"Invalid UUID: {source_learning_id}")
            try:
                if scope is None:
                    runbook = await runbook_svc.create_with_promotion(
                        data=data,
                        source_learning_id=src_uid,
                        dream_run_id=dream_run_id,
                    )
                else:
                    runbook = await runbook_svc.create_with_promotion(
                        data=data,
                        source_learning_id=src_uid,
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
                "mcp.brain_create_runbook.promoted",
                runbook_id=str(runbook.id),
                source_learning_id=source_learning_id,
            )
            return format_confirmation(
                "Runbook created (auto-graduated from learning)",
                runbook.title,
                id=str(runbook.id),
                steps=len(runbook.steps),
            )

        if scope is None:
            runbook = await runbook_svc.create(data)
        else:
            runbook = await runbook_svc.create(
                data,
                authorization=cast("RelationAuthorization", scope),
            )
        logger.info(
            "mcp.brain_create_runbook",
            title_length=len(title),
            step_count=len(runbook.steps),
        )
        return format_confirmation(
            "Runbook created", runbook.title, id=str(runbook.id), steps=len(runbook.steps)
        )

    @mcp.tool(version="1.2", annotations=_READ_ANNOTATIONS)
    async def brain_get_runbook(
        runbook_id: str | None = None,
        title: str | None = None,
        project_key: str | None = None,
        limit: int = 10,
    ) -> str:
        """Get a runbook by ID, by title+project_key, or list runbooks for a project.

        runbook_id accepts a full UUID or a git-style short id (unique
        ≥8-hex-char prefix, hyphens optional); an ambiguous prefix returns
        the matching UUIDs.

        When project_key is the only argument, lists up to *limit* runbooks
        (default 10, clamped to [1, 50]) as compact summary rows rather than
        full detail — avoids token-bombing the caller with dozens of full
        runbooks. Use brain_get(entity_type='runbook', entity_id=…) for full
        detail on a specific runbook.

        Args:
            runbook_id: UUID or unique id prefix of a specific runbook
                (highest priority).
            title: Runbook title — combined with project_key for lookup.
            project_key: Project scope for list or title lookup.
            limit: Max runbooks returned in list mode (default 10, max 50).
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        if runbook_id:
            rb_uid = await resolve_entity_id(
                runbook_id, runbook_svc.resolve_id_prefix, label="runbook"
            )
            if isinstance(rb_uid, str):
                return format_error(rb_uid)
            rb = await runbook_svc.get_by_id(rb_uid)
            if rb is None:
                return format_error(f"Runbook '{format_id(runbook_id)}' not found")
            result = format_runbook(rb)
            if access_logger is not None:
                access_logger.log_access("runbook", rb.id, "get_by_id")
            return result
        elif title and project_key:
            rb = await runbook_svc.get_by_title(title, project_key)
            if rb is None:
                return format_error(f'Runbook "{title}" not found in project "{project_key}"')
            result = format_runbook(rb)
            if access_logger is not None:
                access_logger.log_access("runbook", rb.id, "get_by_id")
            return result
        elif project_key:
            clamped = max(1, min(limit, _RUNBOOK_LIST_LIMIT_MAX))
            # Fetch one extra to detect pagination overflow without a separate COUNT query.
            fetched = await runbook_svc.list_by_project(project_key, limit=clamped + 1)
            has_more = len(fetched) > clamped
            runbooks = fetched[:clamped]
            result = format_runbooks(runbooks, project_key=project_key)
            if has_more:
                result += (
                    f"\n\n… (au moins 1 runbook omis — augmentez limit (max {_RUNBOOK_LIST_LIMIT_MAX})"
                    f" ou utilisez brain_search(types=['runbook'], project_key='{project_key}'))"
                )
            return result
        else:
            return format_error("Provide runbook_id, or title+project_key, or project_key")

    @mcp.tool(version="1.1", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_execute_runbook(
        runbook_id: str,
        status: ExecutionStatus = "success",
    ) -> str:
        """Record a runbook execution (increments execution_count, sets last_executed_at and status).

        runbook_id accepts a full UUID or a unique ≥8-hex-char id prefix.
        """
        exec_uid = await resolve_entity_id(
            runbook_id, runbook_svc.resolve_id_prefix, label="runbook"
        )
        if isinstance(exec_uid, str):
            return format_error(exec_uid)
        runbook = await runbook_svc.record_execution(exec_uid, status)
        if runbook is None:
            return format_error(f"Runbook '{format_id(runbook_id)}' not found")
        logger.info("mcp.brain_execute_runbook", runbook_id=runbook_id, status=status)
        result = format_confirmation(
            "Runbook executed", runbook.title, status=status, count=runbook.execution_count
        )
        if access_logger is not None:
            access_logger.log_access("runbook", runbook.id, "execute")
        return result
