"""SEC1b point-of-use scoping for search fan-out and graph enrichment."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.mcp.dream_project_authorization import (
    DreamObjectReference,
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.services.brain_service import BrainService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

PROJECT_KEY = "sec1b-owned"
FAKE_EMBEDDING = [0.1] * 1536


class RecordingResolver:
    def __init__(
        self,
        *,
        allowed: bool = True,
        denied_ids: set[UUID] | None = None,
    ) -> None:
        self.allowed = allowed
        self.denied_ids = denied_ids or set()
        self.calls: list[tuple[str, tuple[DreamObjectReference, ...]]] = []

    async def references_belong_to_project(
        self,
        project_key: str,
        references: tuple[DreamObjectReference, ...],
    ) -> bool:
        self.calls.append((project_key, tuple(references)))
        return self.allowed and not any(
            reference.entity_id in self.denied_ids for reference in references
        )


def _scope(resolver: RecordingResolver | None = None) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=resolver or RecordingResolver(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name="brain_search",
    )


def _entity(*, entity_id: UUID | None = None) -> Decision:
    now = datetime.now(UTC)
    return Decision(
        id=entity_id or uuid4(),
        title="Scoped decision",
        description="description",
        reasoning="reasoning",
        project_key=PROJECT_KEY,
        created_at=now,
        updated_at=now,
    )


def _plan_entity(*, entity_id: UUID | None = None) -> IndexedPlanChunk:
    return IndexedPlanChunk(
        id=entity_id or uuid4(),
        plan_id=uuid4(),
        section_title="Scoped plan",
        section_path="Implementation",
        content="Plan content",
        section_order=1,
        word_count=2,
        project_key=PROJECT_KEY,
        plan_type="plan",
        status="active",
        created_at=datetime.now(UTC),
    )


def _raw_decision(item: dict[str, Any]) -> MagicMock:
    entity = MagicMock()
    entity.model_dump = MagicMock(return_value=item)
    entity.freshness_status = "fresh"
    entity.merged_into = None
    entity.section_title = None
    entity.title = "Raw scoped decision"
    entity.topic = None
    entity.project_key = PROJECT_KEY
    entity.tags = []
    entity.plan_id = None
    return entity


def _service(*, semantic_results: list[tuple[Any, float]] | None = None) -> MagicMock:
    service = MagicMock()
    service.semantic_search = AsyncMock(return_value=semantic_results or [])
    service.search = AsyncMock(return_value=[])
    return service


def _brain(
    *,
    decision_svc: MagicMock | None = None,
    embedding_svc: MagicMock | None = None,
    hybrid_searcher: MagicMock | None = None,
    graph: MagicMock | None = None,
    plan_search_svc: MagicMock | None = None,
) -> tuple[BrainService, MagicMock]:
    decision = decision_svc or _service()
    empty = [_service() for _ in range(4)]
    embedding = embedding_svc or MagicMock()
    if not hasattr(embedding, "embed") or not isinstance(embedding.embed, AsyncMock):
        embedding.embed = AsyncMock(return_value=FAKE_EMBEDDING)
        embedding.embed_query = AsyncMock(return_value=FAKE_EMBEDDING)
    else:
        embedding.embed.return_value = FAKE_EMBEDDING
        # A caller-supplied double stubs embed; the fan-out calls embed_query.
        # Without this the search path would hit a bare MagicMock and fail to
        # await, which is a test artefact rather than a real behaviour.
        if not isinstance(getattr(embedding, "embed_query", None), AsyncMock):
            embedding.embed_query = AsyncMock(return_value=FAKE_EMBEDDING)
        else:
            embedding.embed_query.return_value = FAKE_EMBEDDING
    return (
        BrainService(
            decision_svc=decision,
            learning_svc=empty[0],
            snippet_svc=empty[1],
            runbook_svc=empty[2],
            adr_svc=empty[3],
            embedding_svc=embedding,
            min_score=0.0,
            hybrid_searcher=hybrid_searcher,
            graph=graph,
            plan_search_svc=plan_search_svc,
        ),
        decision,
    )


@pytest.mark.asyncio
async def test_scoped_vector_fan_out_overrides_forged_project_arguments() -> None:
    brain, decision = _brain()

    with bind_dream_project_scope(_scope()):
        await brain._fan_out(
            ["decision"],
            "query",
            "forged-project",
            7,
            project_keys=["foreign-a", "foreign-b"],
        )

    decision.semantic_search.assert_awaited_once_with(
        "query",
        project_key=PROJECT_KEY,
        project_keys=None,
        limit=7,
        embedding=FAKE_EMBEDDING,
    )


@pytest.mark.asyncio
async def test_admin_vector_fan_out_preserves_historical_call_shape() -> None:
    brain, decision = _brain()

    await brain._fan_out(
        ["decision"],
        "query",
        "admin-project",
        7,
        project_keys=["admin-a", "admin-b"],
    )

    decision.semantic_search.assert_awaited_once_with(
        "query",
        project_key="admin-project",
        project_keys=["admin-a", "admin-b"],
        limit=7,
        embedding=FAKE_EMBEDDING,
    )


@pytest.mark.asyncio
async def test_scoped_hybrid_fan_out_overrides_forged_project_arguments() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=([], "reranked"))
    brain, decision = _brain(hybrid_searcher=hybrid)

    with bind_dream_project_scope(_scope()):
        await brain._fan_out(
            ["decision"],
            "query",
            "forged-project",
            7,
            project_keys=["foreign"],
        )

    call = hybrid.search.await_args
    assert call is not None
    assert call.kwargs["project_key"] == PROJECT_KEY
    assert call.kwargs["project_keys"] is None
    decision.semantic_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_fts_fallback_overrides_forged_project_arguments() -> None:
    embedding = MagicMock()
    embedding.embed = AsyncMock(side_effect=EmbeddingUnavailable("offline"))
    embedding.embed_query = AsyncMock(side_effect=EmbeddingUnavailable("offline"))
    brain, decision = _brain(embedding_svc=embedding)

    with bind_dream_project_scope(_scope()):
        await brain._fan_out(
            ["decision"],
            "query",
            "forged-project",
            7,
            project_keys=["foreign"],
        )

    decision.search.assert_awaited_once_with(
        query="query",
        limit=7,
        project_key=PROJECT_KEY,
    )


@pytest.mark.asyncio
async def test_scoped_what_do_i_know_inherits_authenticated_fan_out_project() -> None:
    brain, decision = _brain()

    with bind_dream_project_scope(_scope()):
        await brain.what_do_i_know_about("topic", project_key="forged-project")

    assert decision.semantic_search.await_args.kwargs["project_key"] == PROJECT_KEY
    assert decision.semantic_search.await_args.kwargs["project_keys"] is None


@pytest.mark.asyncio
async def test_scoped_plan_only_search_skips_graph_and_keeps_plan_result() -> None:
    plan = _plan_entity()
    resolver = RecordingResolver()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(return_value={})
    brain, _decision = _brain(
        graph=graph,
        plan_search_svc=_service(semantic_results=[(plan, 0.9)]),
    )

    with bind_dream_project_scope(_scope(resolver)):
        response = await brain.search("query", types=["plan"])

    assert response.total == 1
    assert response.results[0].type == "plan"
    assert response.related == []
    graph.get_related_ids.assert_not_awaited()
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_scoped_mixed_search_enriches_only_graph_backed_anchor() -> None:
    anchor, neighbor = uuid4(), uuid4()
    plan = _plan_entity()
    resolver = RecordingResolver()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={str(anchor): [{"id": str(neighbor), "type": "Learning"}]}
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
        plan_search_svc=_service(semantic_results=[(plan, 0.8)]),
    )

    with bind_dream_project_scope(_scope(resolver)):
        response = await brain.search("query", types=["plan", "decision"])

    assert {result.type for result in response.results} == {"plan", "decision"}
    graph.get_related_ids.assert_awaited_once_with([anchor], project_key=PROJECT_KEY)
    assert resolver.calls == [
        (
            PROJECT_KEY,
            (DreamObjectReference(anchor), DreamObjectReference(neighbor)),
        )
    ]
    assert response.related == [{"id": str(neighbor), "type": "Learning"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "malformed"])
async def test_scoped_invalid_graph_anchor_denies_safely_before_graph(failure: str) -> None:
    item = {"title": "Missing id", "project_key": PROJECT_KEY}
    if failure == "malformed":
        item["id"] = "not-a-full-uuid"
    resolver = RecordingResolver()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(return_value={})
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_raw_decision(item), 0.9)]),
        graph=graph,
    )

    with capture_logs() as logs:
        with bind_dream_project_scope(_scope(resolver)):
            response = await brain.search("query", types=["decision"])

    assert response.total == 1
    assert response.related == []
    graph.get_related_ids.assert_not_awaited()
    assert resolver.calls == []
    denials = [log for log in logs if log.get("event") == "dream_project.authorization_denied"]
    assert len(denials) == 1
    assert denials[0]["reason"] == "invalid_reference"
    assert "exc_info" not in denials[0]
    assert not [log for log in logs if log.get("event") == "brain_service.graph_enrichment_failed"]
    assert "not-a-full-uuid" not in repr(denials)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign", "malformed"])
async def test_scoped_invalid_graph_mapping_key_denies_without_related(failure: str) -> None:
    anchor, returned_anchor, neighbor = uuid4(), uuid4(), uuid4()
    raw_returned_anchor: str = "not-a-full-uuid" if failure == "malformed" else str(returned_anchor)
    resolver = RecordingResolver(
        denied_ids={returned_anchor} if failure == "foreign" else None,
    )
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={raw_returned_anchor: [{"id": str(neighbor), "type": "Learning"}]}
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    with capture_logs() as logs:
        with bind_dream_project_scope(_scope(resolver)):
            response = await brain.search("query", types=["decision"])

    assert response.related == []
    denials = [log for log in logs if log.get("event") == "dream_project.authorization_denied"]
    assert len(denials) == 1
    assert "exc_info" not in denials[0]
    assert not [log for log in logs if log.get("event") == "brain_service.graph_enrichment_failed"]
    assert raw_returned_anchor not in repr(denials)


@pytest.mark.asyncio
async def test_scoped_extra_owned_graph_key_is_validated_but_ignored() -> None:
    anchor, extra_anchor = uuid4(), uuid4()
    expected_neighbor, ignored_neighbor = uuid4(), uuid4()
    resolver = RecordingResolver()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={
            str(anchor): [{"id": str(expected_neighbor), "type": "Learning"}],
            str(extra_anchor): [{"id": str(ignored_neighbor), "type": "Snippet"}],
        }
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    with bind_dream_project_scope(_scope(resolver)):
        response = await brain.search("query", types=["decision"])

    assert resolver.calls == [
        (
            PROJECT_KEY,
            (
                DreamObjectReference(anchor),
                DreamObjectReference(extra_anchor),
                DreamObjectReference(expected_neighbor),
                DreamObjectReference(ignored_neighbor),
            ),
        )
    ]
    assert response.related == [{"id": str(expected_neighbor), "type": "Learning"}]


@pytest.mark.asyncio
async def test_scoped_related_revalidates_all_anchors_and_neighbors_in_one_batch() -> None:
    anchor, neighbor_a, neighbor_b = uuid4(), uuid4(), uuid4()
    resolver = RecordingResolver()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={
            str(anchor): [
                {"id": str(neighbor_a), "type": "Learning", "rel": "RELATED_TO"},
                {"id": str(neighbor_b), "type": "Snippet", "rel": "USES"},
            ]
        }
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    with bind_dream_project_scope(_scope(resolver)):
        response = await brain.search("query", types=["decision"])

    graph.get_related_ids.assert_awaited_once_with([anchor], project_key=PROJECT_KEY)
    assert resolver.calls == [
        (
            PROJECT_KEY,
            (
                DreamObjectReference(anchor),
                DreamObjectReference(neighbor_a),
                DreamObjectReference(neighbor_b),
            ),
        )
    ]
    assert {item["id"] for item in response.related} == {str(neighbor_a), str(neighbor_b)}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign", "malformed", "missing"])
async def test_scoped_related_fails_closed_without_partial_leak(failure: str) -> None:
    anchor, allowed_neighbor, rejected_neighbor = uuid4(), uuid4(), uuid4()
    resolver = RecordingResolver(allowed=failure != "foreign")
    if failure == "malformed":
        rejected: dict[str, Any] = {"id": "not-a-full-uuid", "type": "Learning"}
    elif failure == "missing":
        rejected = {"type": "Learning"}
    else:
        rejected = {"id": str(rejected_neighbor), "type": "Learning"}
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={
            str(anchor): [
                {"id": str(allowed_neighbor), "type": "Learning"},
                rejected,
            ]
        }
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    with capture_logs() as logs:
        with bind_dream_project_scope(_scope(resolver)):
            response = await brain.search("query", types=["decision"])

    assert response.related == []
    denials = [log for log in logs if log.get("event") == "dream_project.authorization_denied"]
    assert len(denials) == 1
    assert denials[0]["log_level"] == "warning"
    assert "exc_info" not in denials[0]
    assert not [log for log in logs if log.get("event") == "brain_service.graph_enrichment_failed"]
    rendered_denial = repr(denials[0])
    assert "traceback" not in rendered_denial.lower()
    assert "not-a-full-uuid" not in rendered_denial
    for entity_id in (anchor, allowed_neighbor, rejected_neighbor):
        assert str(entity_id) not in rendered_denial


@pytest.mark.asyncio
async def test_ordinary_graph_failure_keeps_historical_error_log() -> None:
    anchor = uuid4()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(side_effect=RuntimeError("neo4j unavailable"))
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    with capture_logs() as logs:
        response = await brain.search("query", types=["decision"])

    failures = [log for log in logs if log.get("event") == "brain_service.graph_enrichment_failed"]
    assert response.related == []
    assert len(failures) == 1
    assert failures[0]["log_level"] == "error"
    assert failures[0]["exc_info"] is True


@pytest.mark.asyncio
async def test_admin_related_preserves_global_call_without_resolver_or_kwarg() -> None:
    anchor, neighbor = uuid4(), uuid4()
    graph = MagicMock()
    graph.get_related_ids = AsyncMock(
        return_value={str(anchor): [{"id": str(neighbor), "type": "Learning"}]}
    )
    brain, _decision = _brain(
        decision_svc=_service(semantic_results=[(_entity(entity_id=anchor), 0.9)]),
        graph=graph,
    )

    response = await brain.search("query", types=["decision"])

    graph.get_related_ids.assert_awaited_once_with([anchor])
    assert response.related == [{"id": str(neighbor), "type": "Learning"}]


def test_public_search_signatures_are_unchanged() -> None:
    assert tuple(inspect.signature(BrainService.search).parameters) == (
        "self",
        "query",
        "types",
        "project_key",
        "project_group",
        "limit",
        "min_score",
        "include_archived",
        "tags",
    )
    assert tuple(inspect.signature(BrainService.what_do_i_know_about).parameters) == (
        "self",
        "topic",
        "project_key",
        "project_group",
        "limit",
        "min_score",
        "include_archived",
    )
