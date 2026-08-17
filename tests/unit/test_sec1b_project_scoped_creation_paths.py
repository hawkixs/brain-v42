"""Failure-first scope propagation for SEC1b creation and backfill entry paths."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectAuthorizationError,
    DreamProjectScope,
)
from brain_v42.mcp.tools import brain_tools, dream_tools, runbook_tools, snippet_tools
from brain_v42.models.adr import ADR, ADRCreate
from brain_v42.models.learning import Learning, LearningCreate
from brain_v42.models.runbook import Runbook, RunbookCreate, RunbookStep
from brain_v42.models.snippet import Snippet, SnippetCreate
from brain_v42.services import (
    adr_service as adr_service_module,
)
from brain_v42.services import (
    learning_service as learning_service_module,
)
from brain_v42.services import (
    runbook_service as runbook_service_module,
)
from brain_v42.services import (
    snippet_service as snippet_service_module,
)
from brain_v42.services.adr_service import ADRService
from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentResult,
    EnrichmentStatus,
)
from brain_v42.services.learning_service import LearningService
from brain_v42.services.link_result import LinkJobResult
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService

PROJECT_KEY = "sec1b-owned"
SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.registered[function.__name__] = function
            return function

        return decorator


class ResolverMustNotRun:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("point-of-use propagation must not invoke the resolver")


class ResolverAllows:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        return True


def scope(tool_name: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=ResolverMustNotRun(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name=tool_name,
    )


def allowing_scope(tool_name: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=ResolverAllows(),
        audit=DreamProjectAudit(principal="dream-codex-connect", phase="connect"),
        tool_name=tool_name,
    )


def learning() -> Learning:
    now = datetime.now(UTC)
    return Learning(
        id=uuid4(),
        topic="Scoped learning",
        insight="Propagate authorization",
        source_type="experience",
        confidence="medium",
        project_key=PROJECT_KEY,
        tags=[],
        metadata={},
        created_at=now,
        updated_at=now,
    )


def snippet() -> Snippet:
    return Snippet.model_validate(
        {
            "id": uuid4(),
            "title": "Scoped snippet",
            "intention": "Propagate authorization",
            "code": "pass",
            "language": "python",
            "project_key": PROJECT_KEY,
            "tags": [],
            "metadata": {},
        }
    )


def runbook() -> Runbook:
    return Runbook.model_validate(
        {
            "id": uuid4(),
            "title": "Scoped runbook",
            "description": "Propagate authorization",
            "project_key": PROJECT_KEY,
            "trigger": "On request",
            "steps": [RunbookStep(order=1, title="Run")],
            "tags": [],
            "metadata": {},
        }
    )


def adr() -> ADR:
    return ADR.model_validate(
        {
            "id": uuid4(),
            "number": 1,
            "title": "Scoped ADR",
            "context": "Context",
            "decision": "Decision",
            "consequences": "Consequences",
            "project_key": PROJECT_KEY,
            "tags": [],
            "metadata": {},
        }
    )


def creation_data(kind: str) -> LearningCreate | SnippetCreate | RunbookCreate | ADRCreate:
    if kind == "learning":
        return LearningCreate(
            topic="Scoped learning",
            insight="Propagate authorization",
            project_key=PROJECT_KEY,
        )
    if kind == "snippet":
        return SnippetCreate(
            title="Scoped snippet",
            intention="Propagate authorization",
            code="pass",
            language="python",
            project_key=PROJECT_KEY,
        )
    if kind == "runbook":
        return RunbookCreate(
            title="Scoped runbook",
            description="Propagate authorization",
            project_key=PROJECT_KEY,
            trigger="On request",
            steps=[RunbookStep(order=1, title="Run")],
        )
    return ADRCreate(
        title="Scoped ADR",
        context="Context",
        decision="Decision",
        consequences="Consequences",
        project_key=PROJECT_KEY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["learning", "snippet", "runbook", "adr"])
async def test_plain_service_forwards_same_scope_to_graph_and_auto_link(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = scope(f"brain_{kind}")
    entities = {
        "learning": learning(),
        "snippet": snippet(),
        "runbook": runbook(),
        "adr": adr(),
    }
    modules = {
        "learning": learning_service_module,
        "snippet": snippet_service_module,
        "runbook": runbook_service_module,
        "adr": adr_service_module,
    }
    graph_upsert = AsyncMock()
    auto_link = AsyncMock()
    monkeypatch.setattr(modules[kind], "graph_upsert_entity", graph_upsert)
    monkeypatch.setattr(modules[kind], "auto_link_if_enabled", auto_link)
    repo = MagicMock()
    repo.create = AsyncMock(return_value=entities[kind])
    enricher = MagicMock()
    enricher.enrich = AsyncMock(
        return_value=EmbeddingEnrichmentResult(
            status=EnrichmentStatus.STORED,
            embedding=[0.1],
        )
    )
    service_types = {
        "learning": lambda: LearningService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        ),
        "snippet": lambda: SnippetService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        ),
        "runbook": lambda: RunbookService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        ),
        "adr": lambda: ADRService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        ),
    }
    service = service_types[kind]()
    data = creation_data(kind)

    if kind in {"learning", "snippet"}:
        relations = [{"id": str(uuid4()), "type": "RELATED_TO"}]
        await service.create(data, related_to=relations, authorization=authorization)
        assert graph_upsert.await_args.kwargs["related_to"] is relations
    else:
        await service.create(data, authorization=authorization)

    assert graph_upsert.await_args.kwargs["authorization"] is authorization
    assert auto_link.await_args.kwargs["authorization"] is authorization


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runbook", "adr"])
async def test_promoted_service_keeps_sql_project_separate_and_forwards_same_scope(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = scope(f"brain_{kind}")
    module = runbook_service_module if kind == "runbook" else adr_service_module
    graph_upsert = AsyncMock()
    auto_link = AsyncMock()
    monkeypatch.setattr(module, "graph_upsert_entity", graph_upsert)
    monkeypatch.setattr(module, "auto_link_if_enabled", auto_link)
    repo = MagicMock()
    repo.create_with_promotion = AsyncMock(return_value=runbook() if kind == "runbook" else adr())
    enricher = MagicMock()
    enricher.enrich = AsyncMock(
        return_value=EmbeddingEnrichmentResult(EnrichmentStatus.STORED, [0.1])
    )

    if kind == "runbook":
        service = RunbookService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        )
        await service.create_with_promotion(
            creation_data(kind),
            SOURCE_ID,
            project_key=PROJECT_KEY,
            authorization=authorization,
        )
    else:
        service = ADRService(
            repo, graph=object(), auto_linker=object(), embedding_enricher=enricher
        )
        await service.create_with_promotion(
            creation_data(kind),
            SOURCE_ID,
            True,
            project_key=PROJECT_KEY,
            authorization=authorization,
        )

    assert repo.create_with_promotion.await_args.kwargs["project_key"] == PROJECT_KEY
    assert graph_upsert.await_args.kwargs["authorization"] is authorization
    assert auto_link.await_args.kwargs["authorization"] is authorization


def registered_creation_tools() -> tuple[dict[str, Any], dict[str, MagicMock]]:
    mcp = MockMCP()
    services = {name: MagicMock() for name in ("learning", "snippet", "runbook", "adr")}
    services["learning"].create = AsyncMock(return_value=learning())
    services["snippet"].create = AsyncMock(return_value=snippet())
    services["runbook"].create = AsyncMock(return_value=runbook())
    services["runbook"].create_with_promotion = AsyncMock(return_value=runbook())
    services["adr"].create = AsyncMock(return_value=adr())
    services["adr"].create_with_promotion = AsyncMock(return_value=adr())
    brain_tools.register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=services["learning"],
        snippet_svc=services["snippet"],
        runbook_svc=services["runbook"],
        adr_svc=services["adr"],
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return mcp.registered, services


PLAIN_TOOL_CASES = [
    (
        "brain_learn",
        brain_tools,
        "learning",
        {"topic": "Scoped learning", "insight": "Propagate authorization"},
    ),
    (
        "brain_save_snippet",
        snippet_tools,
        "snippet",
        {
            "title": "Scoped snippet",
            "intention": "Propagate authorization",
            "code": "pass",
            "language": "python",
        },
    ),
    (
        "brain_create_runbook",
        runbook_tools,
        "runbook",
        {
            "title": "Scoped runbook",
            "description": "Propagate authorization",
            "project_key": PROJECT_KEY,
            "trigger": "On request",
            "steps": [{"title": "Run"}],
        },
    ),
    (
        "brain_propose_adr",
        brain_tools,
        "adr",
        {
            "title": "Scoped ADR",
            "context": "Context",
            "decision": "Decision",
            "consequences": "Consequences",
            "project_key": PROJECT_KEY,
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "module", "service_name", "arguments"), PLAIN_TOOL_CASES)
async def test_plain_tool_reads_scope_once_and_forwards_identical_object(
    tool_name: str,
    module: Any,
    service_name: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = scope(tool_name)
    get_scope = MagicMock(return_value=authorization)
    monkeypatch.setattr(module, "get_dream_project_scope", get_scope, raising=False)
    tools, services = registered_creation_tools()

    await tools[tool_name](**arguments)

    get_scope.assert_called_once_with()
    assert services[service_name].create.await_args.kwargs["authorization"] is authorization


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "module", "service_name", "arguments"),
    [
        (
            "brain_create_runbook",
            runbook_tools,
            "runbook",
            {**PLAIN_TOOL_CASES[2][3], "source_learning_id": str(SOURCE_ID)},
        ),
        (
            "brain_propose_adr",
            brain_tools,
            "adr",
            {
                **PLAIN_TOOL_CASES[3][3],
                "source_learning_id": str(SOURCE_ID),
                "auto_accept": True,
            },
        ),
    ],
)
async def test_promotion_tool_reads_scope_once_and_forwards_identical_object(
    tool_name: str,
    module: Any,
    service_name: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = scope(tool_name)
    get_scope = MagicMock(return_value=authorization)
    monkeypatch.setattr(module, "get_dream_project_scope", get_scope)
    tools, services = registered_creation_tools()

    await tools[tool_name](**arguments)

    get_scope.assert_called_once_with()
    kwargs = services[service_name].create_with_promotion.await_args.kwargs
    assert kwargs["project_key"] == PROJECT_KEY
    assert kwargs["authorization"] is authorization


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "module", "service_name", "arguments"), PLAIN_TOOL_CASES)
async def test_admin_plain_tool_reads_scope_once_and_omits_authorization(
    tool_name: str,
    module: Any,
    service_name: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_scope = MagicMock(return_value=None)
    monkeypatch.setattr(module, "get_dream_project_scope", get_scope, raising=False)
    tools, services = registered_creation_tools()

    await tools[tool_name](**arguments)

    get_scope.assert_called_once_with()
    assert "authorization" not in services[service_name].create.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "module", "service_name", "arguments"),
    [
        (
            "brain_create_runbook",
            runbook_tools,
            "runbook",
            {**PLAIN_TOOL_CASES[2][3], "source_learning_id": str(SOURCE_ID)},
        ),
        (
            "brain_propose_adr",
            brain_tools,
            "adr",
            {
                **PLAIN_TOOL_CASES[3][3],
                "source_learning_id": str(SOURCE_ID),
                "auto_accept": True,
            },
        ),
    ],
)
async def test_admin_promotion_tool_omits_scope_kwargs(
    tool_name: str,
    module: Any,
    service_name: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_scope = MagicMock(return_value=None)
    monkeypatch.setattr(module, "get_dream_project_scope", get_scope)
    tools, services = registered_creation_tools()

    await tools[tool_name](**arguments)

    get_scope.assert_called_once_with()
    kwargs = services[service_name].create_with_promotion.await_args.kwargs
    assert "authorization" not in kwargs
    assert "project_key" not in kwargs


def backfill_dependencies(
    *,
    linker_side_effect: Exception | None = None,
) -> tuple[dict[str, Any], MagicMock, MagicMock, MagicMock]:
    entity_id = uuid4()
    graph = MagicMock()
    graph.find_unlinked_nodes = AsyncMock(return_value=[str(entity_id)])
    row = MagicMock(id=entity_id, embedding=[0.1])
    result = MagicMock()
    result.fetchall.return_value = [row]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    linker = MagicMock()
    linker.auto_link = AsyncMock(
        return_value=LinkJobResult(),
        side_effect=linker_side_effect,
    )
    mcp = MockMCP()
    dream_tools.register_dream_tools(
        mcp,  # type: ignore[arg-type]
        session_factory=session_factory,
        auto_linker=linker,
        graph_service=graph,
    )
    return mcp.registered, graph, linker, session


@pytest.mark.asyncio
async def test_scoped_backfill_forwards_identical_scope_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = allowing_scope("brain_backfill_links_batch")
    get_scope = MagicMock(return_value=authorization)
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", get_scope)
    tools, _graph, linker, _session = backfill_dependencies()

    await tools["brain_backfill_links_batch"](entity_type="Decision")

    get_scope.assert_called_once_with()
    assert linker.auto_link.await_args.kwargs["authorization"] is authorization


@pytest.mark.asyncio
async def test_scoped_backfill_authorization_denial_has_no_secondary_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = allowing_scope("brain_backfill_links_batch")
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: authorization)
    denial = DreamProjectAuthorizationError("resolver_failure")
    tools, _graph, _linker, _session = backfill_dependencies(linker_side_effect=denial)
    scoped_logger = MagicMock()
    monkeypatch.setattr(dream_tools, "logger", scoped_logger)

    with pytest.raises(DreamProjectAuthorizationError) as raised:
        await tools["brain_backfill_links_batch"](entity_type="Decision")

    assert raised.value is denial
    scoped_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_admin_backfill_omits_authorization_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_scope = MagicMock(return_value=None)
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", get_scope)
    tools, _graph, linker, _session = backfill_dependencies()

    await tools["brain_backfill_links_batch"](entity_type="Decision")

    get_scope.assert_called_once_with()
    assert set(linker.auto_link.await_args.kwargs) == {
        "entity_type",
        "entity_id",
        "embedding",
        "threshold",
        "max_links",
    }


def test_internal_authorization_parameters_are_keyword_only_with_admin_default() -> None:
    methods = (
        LearningService.create,
        SnippetService.create,
        RunbookService.create,
        RunbookService.create_with_promotion,
        RunbookService._enrich_created_runbook,
        ADRService.create,
        ADRService.create_with_promotion,
        ADRService._enrich_created_adr,
    )

    for method in methods:
        parameter = inspect.signature(method).parameters["authorization"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


def test_public_creation_tool_signatures_do_not_expose_authorization() -> None:
    tools, _services = registered_creation_tools()

    for tool_name in (
        "brain_learn",
        "brain_save_snippet",
        "brain_create_runbook",
        "brain_propose_adr",
    ):
        assert "authorization" not in inspect.signature(tools[tool_name]).parameters
