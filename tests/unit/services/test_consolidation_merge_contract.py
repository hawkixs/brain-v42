"""Contract tests for the canonical PostgreSQL-first consolidation merge."""

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
from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
from brain_v42.services import consolidation
from brain_v42.services.consolidation import ConsolidationJob
from brain_v42.services.graph_service import GraphService
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class _MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class _BombSessionFactory:
    def __call__(self) -> Any:
        raise AssertionError("brain_merge_entities must not open a SQL session")


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("transaction_enter")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: Any,
    ) -> bool:
        self._events.append("transaction_commit" if exc_type is None else "transaction_rollback")
        return False


def _result(
    *,
    rows: list[dict[str, Any]] | None = None,
    scalar: UUID | None = None,
) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one_or_none.return_value = scalar
    return result


def _job_fixture(
    source_id: UUID,
    target_id: UUID,
    *,
    rows: list[dict[str, Any]] | None = None,
    graph_outcome: str = "created",
) -> tuple[ConsolidationJob, AsyncMock, AsyncMock, MagicMock, list[Any], list[str]]:
    statements: list[Any] = []
    events: list[str] = []
    source = {"id": source_id, "tags": ["source"], "project_key": "owned"}
    target = {"id": target_id, "tags": ["target"], "project_key": "owned"}

    async def execute(statement: Any) -> MagicMock:
        statements.append(statement)
        if str(statement).lstrip().upper().startswith("SELECT"):
            return _result(rows=[source, target] if rows is None else rows)
        params = statement.compile().params
        return _result(scalar=params.get("id_1"))

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.begin = MagicMock(return_value=_Transaction(events))
    session_context = AsyncMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)

    log_repo = AsyncMock()

    async def log_in_transaction(*_args: Any, **_kwargs: Any) -> None:
        events.append("audit")

    log_repo.log_action_in_session = AsyncMock(side_effect=log_in_transaction)
    graph = MagicMock()

    async def create_relation(*_args: Any, **_kwargs: Any) -> str:
        events.append("graph")
        return graph_outcome

    graph.create_relation = AsyncMock(side_effect=create_relation)
    job = ConsolidationJob(session_factory, log_repo, graph=graph)
    return job, session, log_repo, graph, statements, events


def test_merge_signature_includes_entity_type_and_keyword_only_authorization() -> None:
    signature = inspect.signature(ConsolidationJob.merge)

    assert list(signature.parameters) == [
        "self",
        "entity_type",
        "source_id",
        "target_id",
        "authorization",
    ]
    assert signature.parameters["authorization"].kind is inspect.Parameter.KEYWORD_ONLY


def test_repository_exposes_same_transaction_audit_seam() -> None:
    assert hasattr(PgConsolidationLogRepo, "log_action_in_session")


@pytest.mark.asyncio
async def test_repository_audit_seam_does_not_commit_caller_session() -> None:
    session = AsyncMock()
    repo = PgConsolidationLogRepo(MagicMock())

    await repo.log_action_in_session(
        session,
        source_id=uuid4(),
        target_id=uuid4(),
        entity_type="decision",
        similarity=1.0,
        action="merged",
    )

    statement = session.execute.await_args.args[0]
    assert "INSERT INTO consolidation_log" in str(statement)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_delegates_admin_merge_once_without_opening_sql() -> None:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(return_value=None)
    mcp = _MockMCP()
    register_decay_tools(mcp, _BombSessionFactory(), consolidation_job=job)

    result = await mcp.registered["brain_merge_entities"](
        "decision",
        str(source_id),
        str(target_id),
    )

    assert result.startswith("ok Merged")
    job.merge.assert_awaited_once_with(
        "decision",
        source_id,
        target_id,
        authorization=None,
    )


@pytest.mark.asyncio
async def test_job_locks_updates_and_audits_in_one_transaction_before_graph() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, session, log_repo, graph, statements, events = _job_fixture(source_id, target_id)

    await job.merge("decision", source_id, target_id)

    selects = [statement for statement in statements if str(statement).startswith("SELECT")]
    updates = [statement for statement in statements if str(statement).startswith("UPDATE")]
    assert len(selects) == 1
    assert "FOR UPDATE" in str(selects[0])
    assert "ORDER BY decisions.id" in str(selects[0])
    locked_ids = next(
        value for value in selects[0].compile().params.values() if isinstance(value, list)
    )
    assert locked_ids == sorted((source_id, target_id), key=lambda entity_id: entity_id.int)
    assert len(updates) == 2
    assert events == ["transaction_enter", "audit", "transaction_commit", "graph"]
    log_repo.log_action_in_session.assert_awaited_once_with(
        session,
        source_id=source_id,
        target_id=target_id,
        entity_type="decision",
        similarity=1.0,
        action="merged",
    )
    graph.create_relation.assert_awaited_once_with(
        source_id,
        target_id,
        "MERGED_INTO",
        secret_safe=True,
    )


@pytest.mark.asyncio
async def test_job_merges_target_tags_and_archives_source() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, _graph, statements, _events = _job_fixture(source_id, target_id)

    await job.merge("decision", source_id, target_id)

    updates = [statement for statement in statements if str(statement).startswith("UPDATE")]
    compiled = [statement.compile().params for statement in updates]
    assert any(params.get("tags") == ["target", "source"] for params in compiled)
    assert any(
        params.get("merged_into") == target_id and params.get("freshness_status") == "archived"
        for params in compiled
    )


@pytest.mark.asyncio
async def test_job_scopes_lock_updates_and_graph_to_explicit_authorization() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, statements, _events = _job_fixture(source_id, target_id)
    authorization = MagicMock()
    authorization.project_key = "owned"
    authorization.revalidate_ids = AsyncMock(return_value=None)

    await job.merge("decision", source_id, target_id, authorization=authorization)

    scoped_sql = [str(statement) for statement in statements]
    assert len(scoped_sql) == 3
    assert all("project_key" in sql.split("WHERE", maxsplit=1)[1] for sql in scoped_sql)
    authorization.revalidate_ids.assert_awaited_once_with([source_id, target_id])
    graph.create_relation.assert_awaited_once_with(
        source_id,
        target_id,
        "MERGED_INTO",
        project_key="owned",
        secret_safe=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ["plan", "unknown"])
async def test_job_rejects_non_mergeable_types_before_opening_session(entity_type: str) -> None:
    job = ConsolidationJob(_BombSessionFactory(), AsyncMock(), graph=MagicMock())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Unknown entity type"):
        await job.merge(entity_type, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_job_rejects_same_id_before_opening_session() -> None:
    job = ConsolidationJob(_BombSessionFactory(), AsyncMock(), graph=MagicMock())  # type: ignore[arg-type]
    entity_id = uuid4()

    with pytest.raises(ValueError, match="different"):
        await job.merge("decision", entity_id, entity_id)


@pytest.mark.asyncio
async def test_missing_scoped_target_rolls_back_without_updates_audit_or_graph() -> None:
    source_id = uuid4()
    target_id = uuid4()
    rows = [{"id": source_id, "tags": ["source"], "project_key": "owned"}]
    job, _session, log_repo, graph, statements, events = _job_fixture(
        source_id,
        target_id,
        rows=rows,
    )
    authorization = MagicMock(project_key="owned")
    authorization.revalidate_ids = AsyncMock(return_value=None)

    with pytest.raises(LookupError, match="Target decision .* not found"):
        await job.merge("decision", source_id, target_id, authorization=authorization)

    assert len(statements) == 1
    assert events == ["transaction_enter", "transaction_rollback"]
    log_repo.log_action_in_session.assert_not_awaited()
    graph.create_relation.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_updates_and_skips_graph() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, log_repo, graph, _statements, events = _job_fixture(source_id, target_id)
    log_repo.log_action_in_session.side_effect = RuntimeError("audit write failed")

    with pytest.raises(RuntimeError, match="audit write failed"):
        await job.merge("decision", source_id, target_id)

    assert events == ["transaction_enter", "transaction_rollback"]
    graph.create_relation.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_absence_is_bounded_observable_degradation_after_commit() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, _graph, _statements, events = _job_fixture(source_id, target_id)
    job._graph = None

    with structlog.testing.capture_logs() as logs:
        await job.merge("decision", source_id, target_id)

    assert events[-1] == "transaction_commit"
    assert logs == [
        {
            "entity_type": "decision",
            "reason": "graph_unavailable",
            "event": "consolidation_graph_write_degraded",
            "log_level": "warning",
        }
    ]
    assert str(source_id) not in str(logs)
    assert str(target_id) not in str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "missing_node"])
async def test_graph_non_success_outcome_is_bounded_observable_degradation(outcome: str) -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, _graph, _statements, events = _job_fixture(
        source_id,
        target_id,
        graph_outcome=outcome,
    )

    with structlog.testing.capture_logs() as logs:
        await job.merge("decision", source_id, target_id)

    assert events[-2:] == ["transaction_commit", "graph"]
    assert logs[0]["reason"] == outcome
    assert str(source_id) not in str(logs)
    assert str(target_id) not in str(logs)


@pytest.mark.asyncio
async def test_scoped_technical_graph_error_degrades_without_leaking_details() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, events = _job_fixture(source_id, target_id)
    graph.create_relation.side_effect = RuntimeError("NEO4J_PASSWORD=protected")
    authorization = MagicMock(project_key="owned")
    authorization.revalidate_ids = AsyncMock(return_value=None)

    with structlog.testing.capture_logs() as logs:
        await job.merge("decision", source_id, target_id, authorization=authorization)

    assert events[-1] == "transaction_commit"
    assert logs[0]["reason"] == "graph_error"
    assert "protected" not in str(logs)
    assert str(source_id) not in str(logs)
    assert str(target_id) not in str(logs)


@pytest.mark.asyncio
async def test_post_commit_scope_denial_propagates_same_error_without_graph_or_log() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, events = _job_fixture(source_id, target_id)
    denial = DreamProjectAuthorizationError("object_not_authorized")
    authorization = MagicMock(project_key="owned")
    authorization.revalidate_ids = AsyncMock(side_effect=denial)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(DreamProjectAuthorizationError) as raised:
            await job.merge("decision", source_id, target_id, authorization=authorization)

    assert raised.value is denial
    assert events[-1] == "transaction_commit"
    graph.create_relation.assert_not_awaited()
    assert logs == []


@pytest.mark.asyncio
async def test_graph_authorization_denial_is_never_degraded_or_logged() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, events = _job_fixture(source_id, target_id)
    denial = DreamProjectAuthorizationError("object_not_authorized")
    authorization = MagicMock(project_key="owned")
    authorization.revalidate_ids = AsyncMock(return_value=None)
    graph.create_relation.side_effect = denial

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(DreamProjectAuthorizationError) as raised:
            await job.merge("decision", source_id, target_id, authorization=authorization)

    assert raised.value is denial
    assert events[-1] == "transaction_commit"
    graph.create_relation.assert_awaited_once()
    assert logs == []


@pytest.mark.asyncio
async def test_direct_admin_job_ignores_accidentally_bound_scope() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, _events = _job_fixture(source_id, target_id)
    bound_scope = MagicMock(project_key="must-not-be-used")
    bound_scope.revalidate_ids = AsyncMock(side_effect=AssertionError("scope leaked"))

    with bind_dream_project_scope(bound_scope):
        await job.merge("decision", source_id, target_id)

    graph.create_relation.assert_awaited_once_with(
        source_id,
        target_id,
        "MERGED_INTO",
        secret_safe=True,
    )


def test_consolidation_exposes_specific_not_found_error() -> None:
    assert hasattr(consolidation, "ConsolidationEntityNotFoundError")


def _registered_handler(job: Any | None) -> tuple[Any, MagicMock]:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = _MockMCP()
    register_decay_tools(mcp, _BombSessionFactory(), consolidation_job=job)
    return mcp.registered["brain_merge_entities"], mcp


def test_decay_registration_has_no_audit_repository_dependency() -> None:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    assert list(inspect.signature(register_decay_tools).parameters) == [
        "mcp",
        "session_factory",
        "consolidation_job",
    ]


def test_merge_handler_contains_no_table_registry_or_sql_operation() -> None:
    job = MagicMock()
    job.merge = AsyncMock(return_value=None)
    merge, _mcp = _registered_handler(job)
    source = inspect.getsource(merge)

    assert "_CONSOLIDATION_ENTITY_TABLES" not in source
    assert "sa.select" not in source
    assert "sa.update" not in source
    assert "session_factory" not in source


@pytest.mark.asyncio
async def test_handler_forwards_identical_scope_explicitly() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(return_value=None)
    merge, _mcp = _registered_handler(job)
    scope = MagicMock(project_key="owned")

    with bind_dream_project_scope(scope):
        result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    job.merge.assert_awaited_once_with(
        "decision",
        source_id,
        target_id,
        authorization=scope,
    )


@pytest.mark.asyncio
async def test_handler_fails_closed_when_job_is_missing() -> None:
    merge, _mcp = _registered_handler(None)

    result = await merge("decision", str(uuid4()), str(uuid4()))

    assert result and result[0].isalnum()
    assert "not configured" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "source_id", "target_id", "expected"),
    [
        ("plan", lambda: str(uuid4()), lambda: str(uuid4()), "Unknown entity type"),
        ("unknown", lambda: str(uuid4()), lambda: str(uuid4()), "Unknown entity type"),
        ("decision", lambda: "not-a-uuid", lambda: str(uuid4()), "Invalid UUID"),
        ("decision", lambda: "same", lambda: "same", "Invalid UUID"),
    ],
)
async def test_handler_rejects_invalid_requests_without_calling_job(
    entity_type: str,
    source_id: Any,
    target_id: Any,
    expected: str,
) -> None:
    job = MagicMock()
    job.merge = AsyncMock()
    merge, _mcp = _registered_handler(job)

    result = await merge(entity_type, source_id(), target_id())

    assert result and result[0].isalnum()
    assert expected in result
    job.merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_rejects_same_uuid_without_calling_job() -> None:
    entity_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock()
    merge, _mcp = _registered_handler(job)

    result = await merge("decision", str(entity_id), str(entity_id))

    assert result and result[0].isalnum()
    assert "different" in result
    job.merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_formats_job_not_found_without_hiding_other_failures() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(
        side_effect=consolidation.ConsolidationEntityNotFoundError(
            f"Source decision {source_id} not found"
        )
    )
    merge, _mcp = _registered_handler(job)

    result = await merge("decision", str(source_id), str(target_id))

    assert result and result[0].isalnum()
    assert f"Source decision {source_id} not found" in result


@pytest.mark.asyncio
async def test_admin_mcp_real_job_stays_successful_on_technical_graph_failure() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, events = _job_fixture(source_id, target_id)
    graph.create_relation.side_effect = RuntimeError("protected graph detail")
    merge, _mcp = _registered_handler(job)

    with structlog.testing.capture_logs() as logs:
        result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    assert events[-1] == "transaction_commit"
    assert logs[0]["reason"] == "graph_error"
    serialized_logs = str(logs)
    assert "protected graph detail" not in serialized_logs
    assert str(source_id) not in serialized_logs
    assert str(target_id) not in serialized_logs


@pytest.mark.asyncio
async def test_scoped_mcp_real_job_stays_successful_on_missing_graph_node() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job, _session, _log_repo, graph, _statements, _events = _job_fixture(
        source_id,
        target_id,
        graph_outcome="missing_node",
    )
    merge, _mcp = _registered_handler(job)
    scope = MagicMock(project_key="owned")
    scope.revalidate_ids = AsyncMock(return_value=None)

    with structlog.testing.capture_logs() as logs:
        with bind_dream_project_scope(scope):
            result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    graph.create_relation.assert_awaited_once_with(
        source_id,
        target_id,
        "MERGED_INTO",
        project_key="owned",
        secret_safe=True,
    )
    assert logs[0]["reason"] == "missing_node"
    assert str(source_id) not in str(logs)
    assert str(target_id) not in str(logs)


def test_graph_relation_secret_safe_mode_is_keyword_only_and_opt_in() -> None:
    signature = inspect.signature(GraphService.create_relation)

    assert "secret_safe" in signature.parameters
    secret_safe = signature.parameters["secret_safe"]
    assert secret_safe.kind is inspect.Parameter.KEYWORD_ONLY
    assert secret_safe.default is False


def _failing_graph_service() -> tuple[GraphService, AsyncMock, AsyncMock]:
    session = AsyncMock()
    session.run = AsyncMock(side_effect=RuntimeError("NEO4J_DRIVER_SECRET"))
    session_context = AsyncMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session_context)
    driver.verify_connectivity = AsyncMock(side_effect=RuntimeError("NEO4J_RECONNECT_SECRET"))
    return GraphService(driver, timeout=0.1), session.run, driver.verify_connectivity


@pytest.mark.asyncio
async def test_real_graph_service_failure_has_one_bounded_cor3_warning_after_commit() -> None:
    source_id = uuid4()
    target_id = uuid4()
    graph, run, reconnect = _failing_graph_service()
    job, _session, _log_repo, _old_graph, _statements, events = _job_fixture(
        source_id,
        target_id,
    )
    job._graph = graph
    merge, _mcp = _registered_handler(job)

    with structlog.testing.capture_logs() as logs:
        result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    assert events[-1] == "transaction_commit"
    run.assert_awaited_once()
    reconnect.assert_awaited_once()
    assert [entry["event"] for entry in logs] == [
        "consolidation_graph_write_degraded",
        "brain_merge_entities",
    ]
    assert logs[0]["reason"] == "error"
    rendered = repr(logs)
    for protected in (
        str(source_id),
        str(target_id),
        "MATCH (a",
        "NEO4J_DRIVER_SECRET",
        "NEO4J_RECONNECT_SECRET",
        "RuntimeError",
        "Traceback",
        "exc_info",
    ):
        assert protected not in rendered


@pytest.mark.asyncio
async def test_instrumented_graph_proxies_secret_safe_outcome_and_counts_error() -> None:
    from brain_v42.metrics.instrument import InstrumentedGraphService

    source_id = uuid4()
    target_id = uuid4()
    graph, _run, _reconnect = _failing_graph_service()
    collector = MagicMock()
    collector.record_graph_query = MagicMock()
    instrumented_graph = InstrumentedGraphService(graph, collector)
    job, _session, _log_repo, _old_graph, _statements, _events = _job_fixture(
        source_id,
        target_id,
    )
    job._graph = instrumented_graph
    merge, _mcp = _registered_handler(job)

    with structlog.testing.capture_logs() as logs:
        result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    collector.record_graph_query.assert_called_once()
    assert collector.record_graph_query.call_args.kwargs["error"] is True
    assert [entry["event"] for entry in logs] == [
        "consolidation_graph_write_degraded",
        "brain_merge_entities",
    ]
    rendered = repr(logs)
    assert "NEO4J_DRIVER_SECRET" not in rendered
    assert "NEO4J_RECONNECT_SECRET" not in rendered
    assert "MATCH (a" not in rendered
