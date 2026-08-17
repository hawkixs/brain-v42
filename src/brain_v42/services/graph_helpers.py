"""Standalone helpers for safe graph writes + feature linking."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)


def _requires_durable_write_success(graph: Any) -> bool:
    """Return true only for an explicitly durable graph facade.

    ``MagicMock`` and legacy graph adapters expose arbitrary attributes, so an
    identity check is intentional here: only the canonical ledger facade may
    opt out of the historical best-effort Neo4j degradation contract.
    """
    return getattr(graph, "requires_durable_write_success", False) is True


class RelationAuthorization(Protocol):
    """Minimal capability required for a project-bounded graph relation."""

    project_key: str

    async def revalidate_ids(self, entity_ids: Sequence[UUID | str]) -> None:
        """Fail unless every relation anchor still belongs to the project."""
        ...


async def graph_create_relation_logged(
    graph: Any | None,
    src_id: UUID,
    tgt_id: UUID,
    rel_type: str,
    *,
    authorization: RelationAuthorization | None = None,
    properties: dict[str, Any] | None = None,
    origin: str = "explicit",
    confidence: float | None = None,
    **ctx: Any,
) -> str | None:
    """Create a graph relation and surface a degraded write-through.

    GraphService.create_relation returns ``"error"`` (without raising) when Neo4j
    reports the write did not land. That outcome was previously ignored at call
    sites, so PG->Neo4j write-through drift stayed invisible until reconciliation
    (the 2026-06-22 audit found 435 missing MERGED_INTO relations this way). Emit
    a structured WARN on the ``"error"`` outcome so drift is observable.

    Fix 3: also emits a WARN on ``"missing_node"`` — the outcome returned by
    ``_run_counted`` when the MATCH for the anchor nodes returns 0 rows (MERGE
    was never reached). This was previously indistinguishable from 'matched',
    masking write-through drift of a distinct class.

    Admin graph exceptions are logged at error and coerced to ``"error"`` — a
    degraded graph must never break the PG path. Scoped authorization and the
    scoped graph call deliberately remain outside that degradation wrapper so
    authorization failures propagate without a secondary identifier log.

    Returns the outcome (``"created" | "matched" | "missing_node" | "error"``),
    or ``None`` if ``graph`` is ``None``.
    """
    if graph is None:
        return None
    if authorization is not None:
        await authorization.revalidate_ids([src_id, tgt_id])
        if _requires_durable_write_success(graph):
            outcome: str = await graph.create_relation(
                src_id,
                tgt_id,
                rel_type,
                properties,
                project_key=authorization.project_key,
                origin=origin,
                confidence=confidence,
            )
        else:
            outcome = await graph.create_relation(
                src_id,
                tgt_id,
                rel_type,
                project_key=authorization.project_key,
            )
        return outcome
    try:
        if _requires_durable_write_success(graph):
            outcome = await graph.create_relation(
                src_id,
                tgt_id,
                rel_type,
                properties,
                origin=origin,
                confidence=confidence,
            )
        else:
            outcome = await graph.create_relation(src_id, tgt_id, rel_type)
    except Exception:
        logger.error(
            "graph_relation_write_failed",
            rel_type=rel_type,
            src_id=str(src_id),
            tgt_id=str(tgt_id),
            exc_info=True,
            **ctx,
        )
        if _requires_durable_write_success(graph):
            raise
        return "error"
    if outcome == "error":
        logger.warning(
            "graph_relation_write_degraded",
            rel_type=rel_type,
            src_id=str(src_id),
            tgt_id=str(tgt_id),
            **ctx,
        )
    elif outcome == "missing_node":
        logger.warning(
            "graph_relation_missing_node",
            rel_type=rel_type,
            src_id=str(src_id),
            tgt_id=str(tgt_id),
            **ctx,
        )
    return outcome


async def graph_upsert_entity(
    graph: Any | None,
    entity_type: str,
    entity_id: UUID,
    props: dict,
    project_key: str | None = None,
    related_to: list[dict] | None = None,
    *,
    authorization: RelationAuthorization | None = None,
) -> None:
    """Mirror an entity create to Neo4j without hiding scoped relation refusals.

    Fix 2: upsert_node now returns 'ok'|'error'. A returned 'error' outcome
    (swallowed Neo4j failure inside GraphService) is surfaced here as a
    structured WARN — mirroring graph_create_relation_logged — so that
    node-level write-through drift becomes observable without exceptions.
    """
    if graph is None:
        return
    if authorization is not None and related_to:
        await authorization.revalidate_ids(
            [entity_id, *(UUID(relation["id"]) for relation in related_to)]
        )

    async def create_related_relations() -> None:
        for rel in related_to or []:
            await graph_create_relation_logged(
                graph,
                entity_id,
                UUID(rel["id"]),
                rel["type"],
                authorization=authorization,
                entity_type=entity_type,
            )

    try:
        node_outcome = await graph.upsert_node(entity_type, entity_id, props)
        if node_outcome == "error":
            logger.warning(
                "graph_node_write_degraded",
                entity_type=entity_type,
                entity_id=str(entity_id),
            )
        if project_key:
            link_outcome = await graph.link_to_project(entity_id, project_key)
            if link_outcome == "error":
                logger.warning(
                    "graph_node_write_degraded",
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    project_key=project_key,
                )
    except Exception:
        logger.error(
            "graph_write_failed",
            entity_type=entity_type,
            entity_id=str(entity_id),
            exc_info=True,
        )
        if _requires_durable_write_success(graph):
            raise
        return
    if authorization is not None:
        await create_related_relations()
        return
    try:
        await create_related_relations()
    except Exception:
        logger.error(
            "graph_write_failed",
            entity_type=entity_type,
            entity_id=str(entity_id),
            exc_info=True,
        )
        if _requires_durable_write_success(graph):
            raise


async def graph_delete_entity(
    graph: Any | None,
    entity_type: str,
    entity_id: UUID,
    *,
    project_key: str | None = None,
) -> None:
    """Mirror an entity delete to Neo4j. Swallows exceptions to never break PG.

    MINOR 1 fix: delete_node now returns NodeWriteOutcome ('ok'|'error'). A returned
    'error' (swallowed Neo4j failure inside GraphService) is surfaced here as a
    structured WARN — mirroring graph_upsert_entity — so that node-level delete
    write-through drift becomes observable without exceptions.
    """
    if graph is None:
        return
    try:
        if project_key is None:
            delete_outcome = await graph.delete_node(entity_type, entity_id)
        else:
            delete_outcome = await graph.delete_node(
                entity_type,
                entity_id,
                project_key=project_key,
            )
        if delete_outcome == "error":
            logger.warning(
                "graph_node_write_degraded",
                entity_type=entity_type,
                entity_id=str(entity_id),
            )
    except Exception:
        logger.error(
            "graph_delete_failed",
            entity_type=entity_type,
            entity_id=str(entity_id),
            exc_info=True,
        )


async def auto_link_if_enabled(
    auto_linker: Any | None,
    entity_type: str,
    entity_id: UUID,
    embedding: list[float] | None,
    *,
    authorization: RelationAuthorization | None = None,
) -> None:
    """Create RELATED_TO edges, propagating scoped authorization refusals."""
    if auto_linker is None:
        return
    if authorization is not None:
        await auto_linker.auto_link(
            entity_type=entity_type,
            entity_id=entity_id,
            embedding=embedding,
            authorization=authorization,
        )
        return
    try:
        await auto_linker.auto_link(
            entity_type=entity_type,
            entity_id=entity_id,
            embedding=embedding,
        )
    except Exception:
        logger.error(
            "auto_link_failed",
            entity_type=entity_type,
            entity_id=str(entity_id),
            exc_info=True,
        )


async def link_artifact_if_enabled(
    feature_linker: Any | None,
    embedding: list[float] | None,
    artifact_type: str,
    artifact_id: UUID,
    project_key: str | None,
    title: str | None,
) -> None:
    """Link an artifact to a feature without breaking its authoritative write."""
    if not feature_linker or not embedding:
        return
    try:
        await feature_linker.link_artifact(
            embedding=embedding,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            project_key=project_key,
            title=title,
        )
    except Exception:
        logger.error(
            "feature_link_failed",
            artifact_type=artifact_type,
            artifact_id=str(artifact_id),
            project_key=project_key,
            exc_info=True,
        )
