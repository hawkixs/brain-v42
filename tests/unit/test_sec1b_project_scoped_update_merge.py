"""SEC1b project-bounded graph relations for update and merge paths."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import structlog

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAuthorizationError,
    bind_dream_project_scope,
)
from brain_v42.services.consolidation import ConsolidationJob
from brain_v42.services.graph_helpers import graph_create_relation_logged

PROJECT_KEY = "sec1b-owned"


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _scope(
    tool_name: str,
    *,
    events: list[tuple[str, object]] | None = None,
    denial: DreamProjectAuthorizationError | None = None,
) -> MagicMock:
    scope = MagicMock()
    scope.project_key = PROJECT_KEY
    scope.tool_name = tool_name

    async def revalidate(entity_ids: list[UUID]) -> None:
        if events is not None:
            events.append(("validate", list(entity_ids)))
        if denial is not None:
            resolver_error = RuntimeError("SYNTHETIC_RESOLVER_SECRET")
            raise denial from resolver_error

    scope.revalidate_ids = AsyncMock(side_effect=revalidate)
    return scope


def _registered_update(
    *,
    events: list[tuple[str, object]] | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    graph = MagicMock()
    graph.create_relation = AsyncMock(return_value="created")
    services: dict[str, MagicMock] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        services[name] = MagicMock()
    decision = services["decision_svc"]
    decision._graph = graph

    async def update(*_args: Any, **_kwargs: Any) -> object:
        if events is not None:
            events.append(("pg_update", None))
        return object()

    decision.update = AsyncMock(side_effect=update)
    mcp = MockMCP()
    register_crud_tools(mcp, **services, session_factory=MagicMock())
    return mcp.registered["brain_update"], decision, graph


def _registered_merge(
    consolidation_job: MagicMock,
) -> tuple[Any, AsyncMock, MagicMock]:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    log_repo = MagicMock()
    log_repo.log_action = AsyncMock()
    mcp = MockMCP()
    register_decay_tools(
        mcp,
        session_factory,
        consolidation_job=consolidation_job,
    )
    return mcp.registered["brain_merge_entities"], session, log_repo


def _job_for_graph(graph: MagicMock) -> tuple[ConsolidationJob, MagicMock]:
    session = AsyncMock()
    transaction = AsyncMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=transaction)
    session_context = AsyncMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    log_repo = MagicMock()
    log_repo.log_action_in_session = AsyncMock(return_value=None)
    job = ConsolidationJob(MagicMock(return_value=session_context), log_repo, graph=graph)
    job._lock_merge_pair = AsyncMock(  # type: ignore[method-assign]
        return_value=({"tags": ["source"]}, {"tags": ["target"]})
    )
    job._update_merge_pair = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return job, log_repo


@pytest.mark.asyncio
async def test_scoped_update_validates_all_relations_before_any_write_and_uses_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp.tools import crud_tools

    entity_id, first, second = uuid4(), uuid4(), uuid4()
    events: list[tuple[str, object]] = []
    update, _decision, graph = _registered_update(events=events)
    scope = _scope("brain_update", events=events)

    async def create_relation(
        _graph: Any,
        source_id: UUID,
        target_id: UUID,
        relation_type: str,
        **kwargs: Any,
    ) -> str:
        events.append(("helper", target_id))
        assert source_id == entity_id
        assert relation_type == "RELATED_TO"
        assert kwargs["authorization"] is scope
        return "created"

    helper = AsyncMock(side_effect=create_relation)
    monkeypatch.setattr(crud_tools, "graph_create_relation_logged", helper, raising=False)

    with bind_dream_project_scope(scope):
        result = await update(
            "decision",
            str(entity_id),
            {"title": "Scoped"},
            [
                {"id": str(first), "type": "RELATED_TO"},
                {"id": str(second), "type": "RELATED_TO"},
            ],
        )

    assert result.startswith("ok Updated")
    assert events == [
        ("validate", [entity_id, first, second]),
        ("pg_update", None),
        ("helper", first),
        ("helper", second),
    ]
    assert helper.await_count == 2
    graph.create_relation.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_update_propagates_same_denial_without_secondary_log_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp.tools import crud_tools

    entity_id, target_id = uuid4(), uuid4()
    denial = DreamProjectAuthorizationError("resolver_failure")
    scope = _scope("brain_update", denial=denial)
    update, decision, graph = _registered_update()
    helper = AsyncMock()
    monkeypatch.setattr(crud_tools, "graph_create_relation_logged", helper, raising=False)

    with structlog.testing.capture_logs() as logs:
        with bind_dream_project_scope(scope):
            with pytest.raises(DreamProjectAuthorizationError) as raised:
                await update(
                    "decision",
                    str(entity_id),
                    {"title": "Scoped"},
                    [{"id": str(target_id), "type": "RELATED_TO"}],
                )

    assert raised.value is denial
    decision.update.assert_not_awaited()
    helper.assert_not_awaited()
    graph.create_relation.assert_not_awaited()
    assert logs == []


@pytest.mark.asyncio
async def test_admin_update_uses_helper_without_authorization_and_keeps_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp.tools import crud_tools

    entity_id, target_id = uuid4(), uuid4()
    update, _decision, graph = _registered_update()
    graph.create_relation.side_effect = RuntimeError("neo4j down")
    helper = AsyncMock(wraps=graph_create_relation_logged)
    monkeypatch.setattr(crud_tools, "graph_create_relation_logged", helper, raising=False)

    with structlog.testing.capture_logs() as logs:
        result = await update(
            "decision",
            str(entity_id),
            {"title": "Admin"},
            [{"id": str(target_id), "type": "RELATED_TO"}],
        )

    assert result.startswith("ok Updated")
    assert helper.await_count == 1
    assert "authorization" not in helper.await_args.kwargs
    assert any(entry["event"] == "graph_relation_write_failed" for entry in logs)


def test_consolidation_merge_authorization_is_keyword_only() -> None:
    signature = inspect.signature(ConsolidationJob.merge)
    authorization = signature.parameters["authorization"]

    assert authorization.kind is inspect.Parameter.KEYWORD_ONLY
    assert authorization.default is None


@pytest.mark.asyncio
async def test_consolidation_admin_ignores_bound_scope_and_preserves_helper_call() -> None:
    source_id, target_id = uuid4(), uuid4()
    graph = MagicMock()
    graph.create_relation = AsyncMock(return_value="created")
    job, _log_repo = _job_for_graph(graph)

    with bind_dream_project_scope(_scope("brain_merge_entities")):
        await job.merge("decision", source_id, target_id)

    graph.create_relation.assert_awaited_once_with(
        source_id,
        target_id,
        "MERGED_INTO",
        secret_safe=True,
    )


@pytest.mark.asyncio
async def test_consolidation_scoped_propagates_same_denial_without_secondary_log() -> None:
    source_id, target_id = uuid4(), uuid4()
    denial = DreamProjectAuthorizationError("object_not_authorized")
    authorization = _scope("brain_merge_entities", denial=denial)
    graph = MagicMock()
    graph.create_relation = AsyncMock(return_value="created")
    job, _log_repo = _job_for_graph(graph)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(DreamProjectAuthorizationError) as raised:
            await job.merge("decision", source_id, target_id, authorization=authorization)

    assert raised.value is denial
    authorization.revalidate_ids.assert_awaited_once_with([source_id, target_id])
    graph.create_relation.assert_not_awaited()
    assert logs == []


@pytest.mark.asyncio
async def test_scoped_merge_creates_relation_after_commit_with_identical_scope() -> None:
    source_id, target_id = uuid4(), uuid4()
    events: list[str] = []
    job = MagicMock()

    async def merge(*_args: Any, **_kwargs: Any) -> None:
        events.append("merge")

    job.merge = AsyncMock(side_effect=merge)
    tool, session, _log_repo = _registered_merge(job)
    scope = _scope("brain_merge_entities")

    with bind_dream_project_scope(scope):
        result = await tool("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    assert events == ["merge"]
    job.merge.assert_awaited_once_with("decision", source_id, target_id, authorization=scope)
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_merge_delegates_to_the_graph_owning_job() -> None:
    source_id, target_id = uuid4(), uuid4()
    events: list[str] = []
    job = MagicMock()
    job.merge = AsyncMock()
    tool, _session, _log_repo = _registered_merge(job)

    result = await tool("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    assert events == []
    job.merge.assert_awaited_once_with(
        "decision",
        source_id,
        target_id,
        authorization=None,
    )


@pytest.mark.asyncio
async def test_scoped_merge_denial_propagates_after_commit_without_rollback_or_log() -> None:
    source_id, target_id = uuid4(), uuid4()
    events: list[str] = []
    denial = DreamProjectAuthorizationError("object_not_authorized")
    job = MagicMock()

    async def deny(*_args: Any, **_kwargs: Any) -> None:
        events.append("merge")
        raise denial

    job.merge = AsyncMock(side_effect=deny)
    tool, session, log_repo = _registered_merge(job)

    with structlog.testing.capture_logs() as logs:
        with bind_dream_project_scope(_scope("brain_merge_entities")):
            with pytest.raises(DreamProjectAuthorizationError) as raised:
                await tool("decision", str(source_id), str(target_id))

    assert raised.value is denial
    assert events == ["merge"]
    session.rollback.assert_not_awaited()
    log_repo.log_action.assert_not_awaited()
    assert logs == []


def test_mcp_tool_signatures_remain_publicly_unchanged() -> None:
    update, _decision, _graph = _registered_update()
    merge, _session, _log_repo = _registered_merge(MagicMock())

    update_signature = inspect.signature(update)
    merge_signature = inspect.signature(merge)

    assert list(update_signature.parameters) == [
        "entity_type",
        "entity_id",
        "fields",
        "related_to",
    ]
    assert update_signature.parameters["related_to"].default is None
    assert list(merge_signature.parameters) == ["entity_type", "source_id", "target_id"]
