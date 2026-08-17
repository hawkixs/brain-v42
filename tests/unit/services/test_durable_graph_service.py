"""RED contract tests for the durable graph write facade.

The facade must persist intent in PostgreSQL before attempting the best-effort
Neo4j projection.  These tests deliberately use stateful fakes so ordering and
durability semantics are observable without coupling to implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from brain_v42.services.durable_graph_service import DurableGraphService


@dataclass(frozen=True)
class _StagedEvent:
    event_id: UUID


class _LedgerStageError(RuntimeError):
    pass


class _FakeLedger:
    def __init__(
        self,
        trace: list[tuple[Any, ...]],
        *,
        fail_stage: bool = False,
        can_fast_ack: bool = True,
    ) -> None:
        self.trace = trace
        self.fail_stage = fail_stage
        self.can_fast_ack = can_fast_ack
        self.event_id = uuid4()

    def _stage(self, entry: tuple[Any, ...]) -> _StagedEvent:
        self.trace.append(entry)
        if self.fail_stage:
            raise _LedgerStageError("postgres stage failed")
        return _StagedEvent(event_id=self.event_id)

    async def stage_uuid_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        relation_type: str,
        props: dict[str, Any] | None = None,
        origin: str = "explicit",
        confidence: float | None = None,
        project_key: str | None = None,
    ) -> _StagedEvent:
        return self._stage(
            (
                "ledger.stage_uuid_relation",
                source_id,
                target_id,
                relation_type,
                props,
                origin,
                confidence,
                project_key,
            )
        )

    async def stage_project_membership(self, entity_id: UUID, project_key: str) -> _StagedEvent:
        return self._stage(("ledger.stage_project_membership", entity_id, project_key))

    async def stage_domain_membership(
        self,
        entity_id: UUID,
        domain_name: str,
        *,
        project_key: str | None = None,
    ) -> _StagedEvent:
        return self._stage(("ledger.stage_domain_membership", entity_id, domain_name, project_key))

    async def stage_uuid_relation_delete(
        self, source_id: UUID, target_id: UUID, relation_type: str
    ) -> _StagedEvent:
        return self._stage(
            ("ledger.stage_uuid_relation_delete", source_id, target_id, relation_type)
        )

    async def mark_delivered(self, event_id: UUID) -> None:
        self.trace.append(("ledger.mark_delivered", event_id))

    async def mark_delivered_if_no_earlier_pending(self, event_id: UUID) -> bool:
        self.trace.append(("ledger.mark_delivered_if_no_earlier_pending", event_id))
        return self.can_fast_ack


class _FakeGraph:
    def __init__(
        self,
        trace: list[tuple[Any, ...]],
        *,
        relation_outcome: str = "created",
        node_outcome: str = "ok",
        domain_outcome: str = "created",
    ) -> None:
        self.trace = trace
        self.relation_outcome = relation_outcome
        self.node_outcome = node_outcome
        self.domain_outcome = domain_outcome

    async def create_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        rel_type: str,
        props: dict[str, Any] | None = None,
        *,
        project_key: str | None = None,
        secret_safe: bool = False,
    ) -> str:
        self.trace.append(
            (
                "graph.create_relation",
                source_id,
                target_id,
                rel_type,
                props,
                project_key,
                secret_safe,
            )
        )
        return self.relation_outcome

    async def link_to_project(self, entity_id: UUID, project_key: str) -> str:
        self.trace.append(("graph.link_to_project", entity_id, project_key))
        return self.node_outcome

    async def link_entity_to_domain(
        self,
        entity_id: UUID,
        domain_name: str,
        *,
        project_key: str | None = None,
    ) -> str:
        self.trace.append(("graph.link_entity_to_domain", entity_id, domain_name, project_key))
        return self.domain_outcome

    async def delete_relation(self, source_id: UUID, target_id: UUID, rel_type: str) -> str:
        self.trace.append(("graph.delete_relation", source_id, target_id, rel_type))
        return self.node_outcome

    async def upsert_node(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(("graph.upsert_node", args, kwargs))
        return self.node_outcome

    async def delete_node(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(("graph.delete_node", args, kwargs))
        return self.node_outcome

    async def upsert_project(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(("graph.upsert_project", args, kwargs))
        return self.node_outcome

    async def delete_project(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(("graph.delete_project", args, kwargs))
        return self.node_outcome

    async def upsert_domain(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(("graph.upsert_domain", args, kwargs))
        return self.node_outcome


@pytest.mark.asyncio
async def test_enabled_facade_uses_outbox_as_the_only_neo4j_writer() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    service = DurableGraphService(_FakeGraph(trace), ledger)
    source_id, target_id = uuid4(), uuid4()

    assert await service.create_relation(source_id, target_id, "RELATED_TO") == "created"
    assert await service.link_to_project(source_id, "brain-v42") == "ok"
    assert await service.link_entity_to_domain(source_id, "infra") == "created"
    assert await service.delete_relation(source_id, target_id, "RELATED_TO") == "ok"
    assert await service.upsert_node("Learning", source_id, {}) == "ok"
    assert await service.delete_node("Learning", source_id) == "ok"
    assert await service.upsert_project("brain-v42", source_id, "Brain") == "ok"
    assert await service.delete_project("brain-v42") == "ok"
    assert await service.upsert_domain("infra") == "ok"

    assert [entry[0] for entry in trace] == [
        "ledger.stage_uuid_relation",
        "ledger.stage_project_membership",
        "ledger.stage_domain_membership",
        "ledger.stage_uuid_relation_delete",
    ]


@pytest.mark.asyncio
async def test_create_relation_stages_without_direct_projection() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)
    source_id, target_id = uuid4(), uuid4()
    props = {"reason": "same invariant", "weight": 0.83}

    outcome = await service.create_relation(
        source_id,
        target_id,
        "RELATED_TO",
        props,
        project_key="brain-v42",
        secret_safe=True,
    )

    assert outcome == "created"
    assert trace == [
        (
            "ledger.stage_uuid_relation",
            source_id,
            target_id,
            "RELATED_TO",
            props,
            "explicit",
            None,
            "brain-v42",
        )
    ]


@pytest.mark.asyncio
async def test_link_to_project_stages_without_direct_projection() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)
    entity_id = uuid4()

    outcome = await service.link_to_project(entity_id, "brain-v42")

    assert outcome == "ok"
    assert trace == [("ledger.stage_project_membership", entity_id, "brain-v42")]


@pytest.mark.asyncio
async def test_link_to_project_does_not_depend_on_current_neo_anchor() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace, node_outcome="missing_node")
    service = DurableGraphService(graph, ledger)
    entity_id = uuid4()

    outcome = await service.link_to_project(entity_id, "new-project")

    assert outcome == "ok"
    assert trace == [("ledger.stage_project_membership", entity_id, "new-project")]


@pytest.mark.asyncio
async def test_link_entity_to_domain_stages_without_direct_projection() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)
    entity_id = uuid4()

    outcome = await service.link_entity_to_domain(
        entity_id, "architecture", project_key="brain-v42"
    )

    assert outcome == "created"
    assert trace == [("ledger.stage_domain_membership", entity_id, "architecture", "brain-v42")]


@pytest.mark.asyncio
async def test_delete_relation_stages_without_direct_projection() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)
    source_id, target_id = uuid4(), uuid4()

    outcome = await service.delete_relation(source_id, target_id, "SUPERSEDES")

    assert outcome == "ok"
    assert trace == [("ledger.stage_uuid_relation_delete", source_id, target_id, "SUPERSEDES")]


@pytest.mark.asyncio
async def test_stage_failure_prevents_neo4j_projection() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace, fail_stage=True)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)
    source_id, target_id = uuid4(), uuid4()

    with pytest.raises(_LedgerStageError, match="postgres stage failed"):
        await service.create_relation(source_id, target_id, "RELATED_TO")

    assert trace == [
        (
            "ledger.stage_uuid_relation",
            source_id,
            target_id,
            "RELATED_TO",
            None,
            "explicit",
            None,
            None,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "missing_node"])
async def test_graph_outcome_cannot_affect_staged_event(outcome: str) -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace, relation_outcome=outcome)
    service = DurableGraphService(graph, ledger)
    source_id, target_id = uuid4(), uuid4()

    actual = await service.create_relation(source_id, target_id, "RELATED_TO")

    assert actual == "created"
    assert trace == [
        (
            "ledger.stage_uuid_relation",
            source_id,
            target_id,
            "RELATED_TO",
            None,
            "explicit",
            None,
            None,
        )
    ]


@pytest.mark.asyncio
async def test_underlying_graph_match_is_not_used_for_delivery() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace)
    graph = _FakeGraph(trace, relation_outcome="matched")
    service = DurableGraphService(graph, ledger)

    outcome = await service.create_relation(uuid4(), uuid4(), "RELATED_TO")

    assert outcome == "created"
    assert [entry[0] for entry in trace] == ["ledger.stage_uuid_relation"]


@pytest.mark.asyncio
async def test_enabled_facade_never_fast_acks_outbox_revisions() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace, can_fast_ack=False)
    graph = _FakeGraph(trace)
    service = DurableGraphService(graph, ledger)

    outcome = await service.create_relation(uuid4(), uuid4(), "RELATED_TO")

    assert outcome == "created"
    assert [entry[0] for entry in trace] == ["ledger.stage_uuid_relation"]
    assert not any(item[0] == "ledger.mark_delivered" for item in trace)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("create_project_relation", ("brain-v42", "red-data", "DEPENDS_ON")),
        ("delete_project_relation", ("brain-v42", "red-data", "DEPENDS_ON")),
        ("unlink_from_project", (uuid4(), "brain-v42")),
        ("unlink_from_domain", (uuid4(), "infra")),
    ],
)
async def test_enabled_facade_fails_closed_for_unmapped_mutators(
    method_name: str,
    args: tuple[Any, ...],
) -> None:
    trace: list[tuple[Any, ...]] = []
    service = DurableGraphService(_FakeGraph(trace), _FakeLedger(trace))

    with pytest.raises(RuntimeError, match="canonical ledger mapping"):
        await getattr(service, method_name)(*args)

    assert trace == []


@pytest.mark.asyncio
async def test_disabled_facade_delegates_without_touching_ledger() -> None:
    trace: list[tuple[Any, ...]] = []
    ledger = _FakeLedger(trace, fail_stage=True)
    graph = _FakeGraph(trace, relation_outcome="matched")
    service = DurableGraphService(graph, ledger, enabled=False)
    source_id, target_id = uuid4(), uuid4()

    outcome = await service.create_relation(source_id, target_id, "RELATED_TO")

    assert outcome == "matched"
    assert trace == [
        (
            "graph.create_relation",
            source_id,
            target_id,
            "RELATED_TO",
            None,
            None,
            False,
        )
    ]


def test_build_graph_stack_keeps_cutover_dormant_or_builds_projector() -> None:
    from brain_v42.services.durable_graph_service import build_durable_graph_stack
    from brain_v42.services.neo4j_graph_projection_writer import Neo4jGraphProjectionWriter

    graph = object()
    neo4j_driver = object()
    session_factory = object()
    dormant = SimpleNamespace(
        graph_ledger_write_enabled=False,
        graph_outbox_interval_seconds=5.0,
        graph_outbox_batch_size=100,
        graph_outbox_max_attempts=10,
    )

    dormant_stack = build_durable_graph_stack(
        graph,
        session_factory,
        dormant,
        neo4j_driver=neo4j_driver,
    )

    assert dormant_stack.service is graph
    assert dormant_stack.ledger is None
    assert dormant_stack.projector is None

    enabled = SimpleNamespace(
        graph_ledger_write_enabled=True,
        graph_projector_enabled=True,
        graph_outbox_interval_seconds=2.5,
        graph_outbox_batch_size=40,
        graph_outbox_max_attempts=7,
    )
    enabled_stack = build_durable_graph_stack(
        graph,
        session_factory,
        enabled,
        neo4j_driver=neo4j_driver,
    )

    assert isinstance(enabled_stack.service, DurableGraphService)
    assert enabled_stack.ledger is not None
    assert enabled_stack.projector is not None
    assert isinstance(enabled_stack.projector._graph, Neo4jGraphProjectionWriter)
    assert enabled_stack.projector._graph._driver is neo4j_driver


def test_enabled_graph_stack_rejects_missing_raw_neo4j_driver() -> None:
    from brain_v42.services.durable_graph_service import build_durable_graph_stack

    enabled = SimpleNamespace(
        graph_ledger_write_enabled=True,
        graph_projector_enabled=True,
        graph_outbox_interval_seconds=2.5,
        graph_outbox_batch_size=40,
        graph_outbox_max_attempts=7,
        neo4j_timeout=3.0,
    )

    with pytest.raises(RuntimeError, match="raw Neo4j driver"):
        build_durable_graph_stack(object(), object(), enabled, neo4j_driver=None)


def test_enabled_graph_stack_supports_ledger_only_producer_role() -> None:
    from brain_v42.services.durable_graph_service import build_durable_graph_stack

    enabled = SimpleNamespace(
        graph_ledger_write_enabled=True,
        graph_projector_enabled=False,
        graph_outbox_interval_seconds=2.5,
        graph_outbox_batch_size=40,
        graph_outbox_max_attempts=7,
        neo4j_timeout=3.0,
    )

    graph = object()
    stack = build_durable_graph_stack(graph, object(), enabled, neo4j_driver=object())

    assert isinstance(stack.service, DurableGraphService)
    assert stack.service._graph is graph
    assert stack.ledger is not None
    assert stack.projector is None


@pytest.mark.asyncio
async def test_enabled_facade_reads_classification_orphans_from_ledger() -> None:
    trace: list[tuple[Any, ...]] = []
    first, second = uuid4(), uuid4()
    ledger = _FakeLedger(trace)
    ledger.list_active_classification_orphans = AsyncMock(
        return_value=[
            {"source_uuid": first, "entity_type": "learning"},
            {"source_uuid": second, "entity_type": "adr"},
        ]
    )
    graph = _FakeGraph(trace)
    graph.find_orphans_for_classification = AsyncMock()
    service = DurableGraphService(graph, ledger)

    rows = await service.find_orphans_for_classification(
        limit=9,
        project_key="brain-v42",
    )

    assert rows == [
        {"id": str(first), "labels": ["Learning"]},
        {"id": str(second), "labels": ["ADR"]},
    ]
    ledger.list_active_classification_orphans.assert_awaited_once_with(
        limit=9,
        project_key="brain-v42",
    )
    graph.find_orphans_for_classification.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_facade_keeps_legacy_orphan_reader() -> None:
    trace: list[tuple[Any, ...]] = []
    legacy = [{"id": str(uuid4()), "labels": ["Decision"]}]
    ledger = _FakeLedger(trace)
    ledger.list_active_classification_orphans = AsyncMock()
    graph = _FakeGraph(trace)
    graph.find_orphans_for_classification = AsyncMock(return_value=legacy)
    service = DurableGraphService(graph, ledger, enabled=False)

    rows = await service.find_orphans_for_classification(
        limit=4,
        project_key="brain-v42",
    )

    assert rows == legacy
    graph.find_orphans_for_classification.assert_awaited_once_with(
        limit=4,
        project_key="brain-v42",
    )
    ledger.list_active_classification_orphans.assert_not_awaited()
