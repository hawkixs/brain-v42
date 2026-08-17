"""PostgreSQL-first facade for Neo4j graph mutations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from brain_v42.services.graph_service import ALLOWED_DOMAINS

_CLASSIFICATION_LABELS = {
    "decision": "Decision",
    "learning": "Learning",
    "snippet": "Snippet",
    "runbook": "Runbook",
    "adr": "ADR",
}


@dataclass(frozen=True, slots=True)
class DurableGraphStack:
    """Wiring result kept explicit for lifecycle ownership."""

    service: Any
    ledger: Any | None
    projector: Any | None


def build_durable_graph_stack(
    graph: Any,
    session_factory: Any,
    settings: Any,
    *,
    neo4j_driver: Any | None = None,
) -> DurableGraphStack:
    """Build the ledger facade only after the additive cutover is enabled."""
    if not settings.graph_ledger_write_enabled:
        return DurableGraphStack(service=graph, ledger=None, projector=None)

    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo  # noqa: PLC0415

    ledger = PgGraphLedgerRepo(session_factory)
    service = DurableGraphService(graph, ledger)
    if not getattr(settings, "graph_projector_enabled", False):
        return DurableGraphStack(service=service, ledger=ledger, projector=None)
    if neo4j_driver is None:
        raise RuntimeError("durable graph projection requires the raw Neo4j driver")

    from brain_v42.services.graph_outbox_projector import (  # noqa: PLC0415
        GraphOutboxProjector,
    )
    from brain_v42.services.neo4j_graph_projection_writer import (  # noqa: PLC0415
        Neo4jGraphProjectionWriter,
    )

    writer = Neo4jGraphProjectionWriter(
        neo4j_driver,
        timeout=getattr(settings, "neo4j_timeout", 5.0),
    )
    projector = GraphOutboxProjector(
        ledger,
        writer,
        interval_seconds=settings.graph_outbox_interval_seconds,
        batch_size=settings.graph_outbox_batch_size,
        max_attempts=settings.graph_outbox_max_attempts,
    )
    return DurableGraphStack(service=service, ledger=ledger, projector=projector)


class DurableGraphService:
    """Expose graph mutation contracts while PostgreSQL owns every write.

    With the cutover enabled, relation calls stage outbox work and node calls
    rely on the source-table registry triggers. Only the projector may mutate
    Neo4j, which prevents delayed write-through calls from overwriting a newer
    canonical revision. Reads remain transparently delegated.
    """

    requires_durable_write_success = True

    def __init__(self, graph: Any, ledger: Any, *, enabled: bool = True) -> None:
        self._graph = graph
        self._ledger = ledger
        self._enabled = enabled

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    async def find_orphans_for_classification(
        self,
        limit: int = 20,
        *,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._enabled:
            return cast(
                list[dict[str, Any]],
                await self._graph.find_orphans_for_classification(
                    limit=limit,
                    project_key=project_key,
                ),
            )

        rows = await self._ledger.list_active_classification_orphans(
            limit=limit,
            project_key=project_key,
        )
        return [
            {
                "id": str(row["source_uuid"]),
                "labels": [_CLASSIFICATION_LABELS[str(row["entity_type"])]],
            }
            for row in rows
        ]

    async def create_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        rel_type: str,
        props: dict[str, Any] | None = None,
        *,
        project_key: str | None = None,
        secret_safe: bool = False,
        origin: str = "explicit",
        confidence: float | None = None,
    ) -> str:
        if not self._enabled:
            return cast(
                str,
                await self._graph.create_relation(
                    source_id,
                    target_id,
                    rel_type,
                    props,
                    project_key=project_key,
                    secret_safe=secret_safe,
                ),
            )

        await self._ledger.stage_uuid_relation(
            source_id,
            target_id,
            rel_type,
            props=props,
            origin=origin,
            confidence=confidence,
            project_key=project_key,
        )
        return "created"

    async def link_to_project(self, entity_id: UUID, project_key: str) -> str:
        if not self._enabled:
            return cast(str, await self._graph.link_to_project(entity_id, project_key))

        await self._ledger.stage_project_membership(entity_id, project_key)
        return "ok"

    async def link_entity_to_domain(
        self,
        entity_id: UUID,
        domain_name: str,
        *,
        project_key: str | None = None,
    ) -> str:
        if not self._enabled:
            return cast(
                str,
                await self._graph.link_entity_to_domain(
                    entity_id,
                    domain_name,
                    project_key=project_key,
                ),
            )

        await self._ledger.stage_domain_membership(
            entity_id,
            domain_name,
            project_key=project_key,
        )
        return "created"

    async def delete_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        rel_type: str,
    ) -> str:
        if not self._enabled:
            return cast(
                str,
                await self._graph.delete_relation(source_id, target_id, rel_type),
            )

        await self._ledger.stage_uuid_relation_delete(
            source_id,
            target_id,
            rel_type,
        )
        return "ok"

    async def _reject_unmapped_mutation(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        if not self._enabled:
            method = getattr(self._graph, method_name)
            return cast(str, await method(*args, **kwargs))
        raise RuntimeError(
            f"{method_name} has no canonical ledger mapping while the durable graph is active"
        )

    async def create_project_relation(self, *args: Any, **kwargs: Any) -> str:
        return await self._reject_unmapped_mutation("create_project_relation", *args, **kwargs)

    async def delete_project_relation(self, *args: Any, **kwargs: Any) -> str:
        return await self._reject_unmapped_mutation("delete_project_relation", *args, **kwargs)

    async def unlink_from_project(self, *args: Any, **kwargs: Any) -> str:
        return await self._reject_unmapped_mutation("unlink_from_project", *args, **kwargs)

    async def unlink_from_domain(self, *args: Any, **kwargs: Any) -> str:
        return await self._reject_unmapped_mutation("unlink_from_domain", *args, **kwargs)

    async def upsert_node(self, *args: Any, **kwargs: Any) -> str:
        if not self._enabled:
            return cast(str, await self._graph.upsert_node(*args, **kwargs))
        return "ok"

    async def delete_node(self, *args: Any, **kwargs: Any) -> str:
        if not self._enabled:
            return cast(str, await self._graph.delete_node(*args, **kwargs))
        return "ok"

    async def upsert_project(self, *args: Any, **kwargs: Any) -> str:
        if not self._enabled:
            return cast(str, await self._graph.upsert_project(*args, **kwargs))
        return "ok"

    async def delete_project(self, *args: Any, **kwargs: Any) -> str:
        if not self._enabled:
            return cast(str, await self._graph.delete_project(*args, **kwargs))
        return "ok"

    async def upsert_domain(self, domain_name: str) -> str:
        if not self._enabled:
            return cast(str, await self._graph.upsert_domain(domain_name))
        if domain_name not in ALLOWED_DOMAINS:
            return "invalid_domain"
        return "ok"
