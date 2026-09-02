"""AutoLinker — automatic RELATED_TO graph links on entity creation.

After an entity is created with an embedding, AutoLinker finds the most
similar entities across all types via a single UNION vector search query,
then creates RELATED_TO edges in Neo4j.

Design:
- Uses the already-computed embedding (no extra GPU call)
- Single SQL UNION across decisions/learnings/snippets/runbooks/adrs
- Configurable threshold (default 0.6) and max_links (default 3); max_links
  caps SUCCESSFUL links only — see the auto_link docstring for the contract
- Admin graph failures degrade without breaking entity creation
- Scoped authorization failures propagate so a stale owner cannot create an edge
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint
from brain_v42.services.graph_helpers import (
    RelationAuthorization,
    graph_create_relation_logged,
)
from brain_v42.services.link_result import LinkJobResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = structlog.get_logger(__name__)

# Tables to search across, with their type label and text column.
# Tuple, not list: the declared literalness is already pinned by AST, but a
# list would stay mutable at runtime with nothing turning red (ticket c623fa75).
_ENTITY_TABLES = (
    ("decisions", "Decision", "title"),
    ("learnings", "Learning", "topic"),
    ("snippets", "Snippet", "title"),
    ("runbooks", "Runbook", "title"),
    ("adrs", "ADR", "title"),
)


class AutoLinker:
    """Creates RELATED_TO graph edges for newly created entities."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        graph: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._graph = graph

    async def auto_link(
        self,
        entity_type: str,
        entity_id: UUID,
        embedding: list[float] | None,
        threshold: float = 0.6,
        max_links: int = 3,
        *,
        authorization: RelationAuthorization | None = None,
    ) -> LinkJobResult:
        """Find similar entities and create RELATED_TO edges. 4-bucket result.

        When authorization is present, candidate selection is project-filtered
        and both anchors are revalidated immediately before every edge write.

        max_links contract (operator decision 2026-08-18, ticket fb62624f): the
        cap bounds SUCCESSFUL links (created + matched). Errors do not consume
        the cap — the historical behaviour of missing_node and write_failed,
        kept knowingly — and the number of ATTEMPTS stays bounded in practice by
        the ``limit = max_links * 2`` candidates selected. An entity whose every
        write fails can therefore report up to 2×max_links errors: to estimate a
        number of faulty entities from an error count, divide by 2×max_links,
        never by max_links.
        """
        result = LinkJobResult()
        if self._graph is None or embedding is None:
            return result

        try:
            if authorization is None:
                candidates = await self._find_similar(
                    entity_id=entity_id,
                    embedding=embedding,
                    limit=max_links * 2,
                )
            else:
                candidates = await self._find_similar(
                    entity_id=entity_id,
                    embedding=embedding,
                    limit=max_links * 2,
                    project_key=authorization.project_key,
                )
        except Exception:
            logger.error("auto_linker.find_similar_failed", exc_info=True)
            return result

        for row in candidates:
            entry_base = {
                "id": row["id"],
                "entity_type": row["entity_type"],
                "similarity": row["similarity"],
            }
            if row["similarity"] < threshold:
                result.skipped.append({**entry_base, "reason": "below_threshold"})
                continue
            if len(result.created) + len(result.matched) >= max_links:
                result.skipped.append({**entry_base, "reason": "max_links_cap"})
                continue
            try:
                outcome = await graph_create_relation_logged(
                    self._graph,
                    entity_id,
                    row["id"],
                    "RELATED_TO",
                    authorization=authorization,
                    properties={"similarity": row["similarity"]},
                    origin="auto_linker",
                    confidence=row["similarity"],
                )
            except UnknownGraphEndpoint:
                # Ticket 6d2cf2a9 — the target is no longer a valid graph
                # endpoint. This is a DATA pathology, not an authorization
                # refusal: it falls into `errors` so it stays counted as
                # errors=N, without cancelling the entity's remaining
                # candidates. The catch stays deliberately narrow —
                # UnknownGraphEndpoint derives from ValueError, but
                # DreamProjectAuthorizationError derives from AuthorizationError
                # and must keep propagating (see graph_helpers, scoped
                # contract).
                result.errors.append({**entry_base, "reason": "unknown_endpoint"})
                # Both anchors are named EVEN in scoped mode, unlike the
                # sibling branches. The pathology is chronic and repeats night
                # after night: a WARN without an identifier cannot be counted,
                # and forces the operator to replay SQL to find the faulty
                # entities. This is not a leak — a scoped id belongs to the
                # scope's project, unlike the context of an authorization
                # refusal.
                logger.warning(
                    "auto_linker.unknown_graph_endpoint",
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    target_id=str(row["id"]),
                )
                continue
            if outcome == "created":
                result.created.append(entry_base)
            elif outcome == "matched":
                result.matched.append(entry_base)
            elif outcome == "missing_node":
                # Fix 3: anchor node absent in Neo4j — MERGE was never reached.
                # Bucket as errors with a distinguishable reason so callers can
                # separate structural drift from transient write failures.
                result.errors.append({**entry_base, "reason": "missing_node"})
                if authorization is None:
                    logger.error(
                        "auto_linker.create_relation_missing_node",
                        entity_id=str(entity_id),
                        target_id=str(row["id"]),
                    )
            else:  # "error"
                result.errors.append({**entry_base, "reason": "write_failed"})
                if authorization is None:
                    logger.error(
                        "auto_linker.create_relation_failed",
                        entity_id=str(entity_id),
                        target_id=str(row["id"]),
                    )

        if result.created or result.matched:
            log_context = {
                "entity_type": entity_type,
                **{
                    f"count_{k}": len(getattr(result, k))
                    for k in ("created", "matched", "skipped", "errors")
                },
            }
            if authorization is None:
                log_context["entity_id"] = str(entity_id)
            logger.info("auto_linker.linked", **log_context)

        return result

    async def _find_similar(
        self,
        entity_id: UUID,
        embedding: list[float],
        limit: int = 6,
        *,
        project_key: str | None = None,
    ) -> list[dict]:
        """UNION vector search across all entity tables, excluding self.

        A provided project key is bound inside every UNION arm before the
        global ORDER BY/LIMIT. Returns rows sorted by cosine similarity DESC.
        """
        # Build UNION ALL query across all entity tables
        vec_literal = f"'[{','.join(str(v) for v in embedding)}]'::vector"

        unions = []
        project_filter = " AND project_key = :project_key" if project_key is not None else ""
        for table, type_label, _text_col in _ENTITY_TABLES:
            unions.append(
                f"SELECT id, '{type_label}' AS entity_type, "
                f"1.0 - (embedding <=> {vec_literal}) AS similarity "
                f"FROM {table} "
                f"WHERE embedding IS NOT NULL AND id != :entity_id{project_filter} "
                # Ticket 6d2cf2a9 — same predicate as
                # pg_graph_ledger._resolve_endpoints, which requires
                # lifecycle='active' on both anchors and otherwise raises
                # UnknownGraphEndpoint. Without this filter the selector offered
                # targets the resolver refuses (408 archived rows carrying an
                # embedding, 14 projects, measured 2026-08-18) → a daily partial
                # connect. `{table}.id` is qualified on purpose: brain_entities
                # carries ITS OWN `id` column, so a bare `id` would resolve to
                # the internal table and the EXISTS would be true for any row of
                # the ledger.
                f"AND EXISTS (SELECT 1 FROM brain_entities be "
                f"WHERE be.source_uuid = {table}.id AND be.lifecycle = 'active')"  # nosec B608 - table and type_label iterate _ENTITY_TABLES, a literal constant of this module (no caller feeds it); project_filter is one of the two literal strings built just above, the project_key value going out as the :project_key bind; brain_entities and be.lifecycle are frozen literals of this expression; vec_literal carries only the numbers of a vector already accepted by a pgvector Vector(1536) column — exception reviewed on 2026-08-18, to be re-examined before 2026-09-30
            )

        query = (
            "SELECT id, entity_type, similarity FROM (\n"
            + "\nUNION ALL\n".join(unions)  # nosec B608 - unions contains only the branches assembled above from _ENTITY_TABLES; entity_id and limit go out as the :entity_id and :limit binds — exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
            + "\n) sub ORDER BY similarity DESC LIMIT :limit"
        )

        async with self._session_factory() as session:
            params: dict[str, object] = {"entity_id": entity_id, "limit": limit}
            if project_key is not None:
                params["project_key"] = project_key
            result = await session.execute(
                sa.text(query),
                params,
            )
            rows = result.fetchall()

        return [
            {"id": row.id, "entity_type": row.entity_type, "similarity": float(row.similarity)}
            for row in rows
        ]
