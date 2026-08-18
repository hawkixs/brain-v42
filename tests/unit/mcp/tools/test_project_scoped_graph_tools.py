"""Point-of-use project authorization for graph-backed MCP tools."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.dream_project_authorization import DreamProjectAuthorizationError
from brain_v42.mcp.tools import brain_tools, dream_tools
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.dream_tools import register_dream_tools
from brain_v42.services.link_result import LinkJobResult

PROJECT_KEY = "brain_v42"


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.registered[function.__name__] = function
            return function

        return decorator


class RecordingScope:
    project_key = PROJECT_KEY

    def __init__(self) -> None:
        self.revalidate_id = AsyncMock()
        self.revalidate_ids = AsyncMock()


def _session_factory(session: AsyncMock) -> MagicMock:
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context)


async def _brain_graph_tool(graph: MagicMock, name: str) -> Any:
    mcp = FastMCP("project-scoped-graph-tools")
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        graph_svc=graph,
    )
    return (await mcp.get_tool(name)).fn


def _dream_graph_tools(
    graph: MagicMock,
    *,
    session: AsyncMock | None = None,
    auto_linker: MagicMock | None = None,
) -> tuple[dict[str, Any], AsyncMock, MagicMock]:
    actual_session = session or AsyncMock()
    linker = auto_linker or MagicMock()
    linker.auto_link = AsyncMock(return_value=LinkJobResult())
    mcp = MockMCP()
    register_dream_tools(
        cast(Any, mcp),
        session_factory=cast(Any, _session_factory(actual_session)),
        auto_linker=linker,
        graph_service=graph,
    )
    return mcp.registered, actual_session, linker


@pytest.mark.asyncio
async def test_scoped_neighbors_passes_claim_and_revalidates_one_complete_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor, neighbor = uuid4(), uuid4()
    graph = MagicMock()
    graph.get_neighbors = AsyncMock(
        return_value=[{"id": str(neighbor), "type": "Learning", "rel": "RELATED_TO"}]
    )
    scope = RecordingScope()
    monkeypatch.setattr(brain_tools, "get_dream_project_scope", lambda: scope)
    tool = await _brain_graph_tool(graph, "brain_get_neighbors")

    await tool(entity_id=str(anchor))

    assert graph.get_neighbors.await_args.kwargs["project_key"] == PROJECT_KEY
    scope.revalidate_ids.assert_awaited_once_with([anchor, str(neighbor)])


@pytest.mark.asyncio
async def test_scoped_neighbors_denies_malformed_result_without_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = MagicMock()
    graph.get_neighbors = AsyncMock(return_value=[{"id": "malformed", "type": "Decision"}])
    scope = RecordingScope()
    scope.revalidate_ids.side_effect = DreamProjectAuthorizationError("invalid_reference")
    monkeypatch.setattr(brain_tools, "get_dream_project_scope", lambda: scope)
    tool = await _brain_graph_tool(graph, "brain_get_neighbors")

    with pytest.raises(DreamProjectAuthorizationError):
        await tool(entity_id=str(uuid4()))

    scope.revalidate_ids.assert_awaited_once()


@pytest.mark.asyncio
async def test_scoped_graph_path_passes_claim_and_revalidates_endpoints_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, middle, target = uuid4(), uuid4(), uuid4()
    graph = MagicMock()
    graph.get_path = AsyncMock(
        return_value=[
            {"id": str(source), "type": "Decision", "label": "A", "rel_to_next": "USES"},
            {"id": str(middle), "type": "Learning", "label": "B", "rel_to_next": "USES"},
            {"id": str(target), "type": "ADR", "label": "C"},
        ]
    )
    scope = RecordingScope()
    monkeypatch.setattr(brain_tools, "get_dream_project_scope", lambda: scope)
    tool = await _brain_graph_tool(graph, "brain_graph_path")

    await tool(source_id=str(source), target_id=str(target))

    assert graph.get_path.await_args.kwargs["project_key"] == PROJECT_KEY
    scope.revalidate_ids.assert_awaited_once_with(
        [source, target, str(source), str(middle), str(target)]
    )


@pytest.mark.asyncio
async def test_admin_neighbor_and_path_calls_are_historical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = MagicMock()
    graph.get_neighbors = AsyncMock(return_value=[])
    graph.get_path = AsyncMock(return_value=[])
    monkeypatch.setattr(brain_tools, "get_dream_project_scope", lambda: None)
    neighbor_tool = await _brain_graph_tool(graph, "brain_get_neighbors")
    path_tool = await _brain_graph_tool(graph, "brain_graph_path")
    source, target = uuid4(), uuid4()

    await neighbor_tool(entity_id=str(source))
    await path_tool(source_id=str(source), target_id=str(target))

    assert graph.get_neighbors.await_args.kwargs == {"id": source, "rel_types": None, "depth": 1}
    assert graph.get_path.await_args.args == (source, target)
    assert graph.get_path.await_args.kwargs == {"max_depth": 3, "rel_types": None}


@pytest.mark.asyncio
async def test_scoped_backfill_revalidates_graph_ids_and_filters_embedding_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    graph = MagicMock()
    graph.find_unlinked_nodes = AsyncMock(return_value=[str(entity_id)])
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    session = AsyncMock()
    row = MagicMock(id=entity_id, embedding=[0.1])
    result = MagicMock()
    result.fetchall.return_value = [row]
    session.execute.return_value = result
    tools, _session, linker = _dream_graph_tools(graph, session=session)

    await tools["brain_backfill_links_batch"](entity_type="Decision")

    graph.find_unlinked_nodes.assert_awaited_once_with(
        entity_type="Decision", limit=50, project_key=PROJECT_KEY
    )
    scope.revalidate_ids.assert_awaited_once_with([str(entity_id)])
    statement = str(session.execute.await_args.args[0])
    assert "decisions.project_key" in statement
    linker.auto_link.assert_awaited_once()


@pytest.mark.asyncio
async def test_scoped_clusters_revalidate_endpoints_even_in_summary_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid4(), uuid4()
    graph = MagicMock()
    graph.get_all_related_edges = AsyncMock(return_value=[(str(first), str(second))])
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    tools, session, _linker = _dream_graph_tools(graph)

    output = await tools["brain_get_clusters"](summary_only=True)

    graph.get_all_related_edges.assert_awaited_once_with(project_key=PROJECT_KEY)
    scope.revalidate_ids.assert_awaited_once_with([str(first), str(second)])
    session.execute.assert_not_awaited()
    assert "1 cluster" in output


@pytest.mark.asyncio
async def test_scoped_clusters_filter_pg_enrichment_by_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid4(), uuid4()
    graph = MagicMock()
    graph.get_all_related_edges = AsyncMock(return_value=[(str(first), str(second))])
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute.return_value = result
    tools, _session, _linker = _dream_graph_tools(graph, session=session)

    await tools["brain_get_clusters"]()

    assert session.execute.await_count == 5
    for call in session.execute.await_args_list:
        assert ".project_key" in str(call.args[0])


@pytest.mark.asyncio
async def test_scoped_orphans_revalidate_all_ids_then_filter_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = uuid4()
    graph = MagicMock()
    graph.find_orphans_for_classification = AsyncMock(
        return_value=[{"id": str(orphan), "labels": ["Decision"]}]
    )
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute.return_value = result
    tools, _session, _linker = _dream_graph_tools(graph, session=session)

    await tools["brain_list_orphans_for_classification"]()

    graph.find_orphans_for_classification.assert_awaited_once_with(
        limit=20, project_key=PROJECT_KEY
    )
    scope.revalidate_ids.assert_awaited_once_with([str(orphan)])
    assert "decisions.project_key" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_scoped_assign_domain_revalidates_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    events: list[str] = []
    graph = MagicMock()
    graph.upsert_domain = AsyncMock(side_effect=lambda _name: events.append("upsert") or "ok")
    graph.link_entity_to_domain = AsyncMock(
        side_effect=lambda *_args, **_kwargs: events.append("link") or "created"
    )
    scope = RecordingScope()

    async def revalidate(_entity_id: UUID) -> None:
        events.append("revalidate")

    scope.revalidate_id.side_effect = revalidate
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    tools, _session, _linker = _dream_graph_tools(graph)

    output = await tools["brain_assign_domain"](str(entity_id), "infra")

    assert output == "created"
    assert events == ["revalidate", "upsert", "revalidate", "link"]
    graph.link_entity_to_domain.assert_awaited_once_with(
        entity_id, "infra", project_key=PROJECT_KEY
    )


@pytest.mark.asyncio
async def test_scoped_assign_domain_revalidates_again_immediately_before_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    events: list[str] = []
    denial = DreamProjectAuthorizationError("object_not_authorized")
    graph = MagicMock()
    graph.upsert_domain = AsyncMock(side_effect=lambda _name: events.append("upsert") or "ok")
    graph.link_entity_to_domain = AsyncMock(return_value="created")
    scope = RecordingScope()

    async def revalidate(_entity_id: UUID) -> None:
        events.append("revalidate")
        if events.count("revalidate") == 2:
            raise denial

    scope.revalidate_id.side_effect = revalidate
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    tools, _session, _linker = _dream_graph_tools(graph)

    with pytest.raises(DreamProjectAuthorizationError) as raised:
        await tools["brain_assign_domain"](str(entity_id), "infra")

    assert raised.value is denial
    assert events == ["revalidate", "upsert", "revalidate"]
    graph.link_entity_to_domain.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_graph_result_denial_is_total_and_skips_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = MagicMock()
    graph.get_all_related_edges = AsyncMock(return_value=cast(Any, [None]))
    scope = RecordingScope()
    scope.revalidate_ids.side_effect = DreamProjectAuthorizationError("invalid_reference")
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)
    tools, session, _linker = _dream_graph_tools(graph)

    with pytest.raises(DreamProjectAuthorizationError):
        await tools["brain_get_clusters"](summary_only=True)

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_dream_graph_calls_keep_historical_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = MagicMock()
    graph.find_unlinked_nodes = AsyncMock(return_value=[])
    graph.get_all_related_edges = AsyncMock(return_value=[])
    graph.find_orphans_for_classification = AsyncMock(return_value=[])
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: None, raising=False)
    tools, _session, _linker = _dream_graph_tools(graph)

    await tools["brain_backfill_links_batch"]()
    await tools["brain_get_clusters"]()
    await tools["brain_list_orphans_for_classification"]()

    graph.find_unlinked_nodes.assert_awaited_once_with(entity_type=None, limit=50)
    graph.get_all_related_edges.assert_awaited_once_with()
    graph.find_orphans_for_classification.assert_awaited_once_with(limit=20)


@pytest.mark.asyncio
async def test_scoped_backfill_survives_a_dirty_endpoint_and_still_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket 6d2cf2a9 (c) — la branche scopée n'avait aucun try/except.

    Une seule cible archived faisait donc remonter `UnknownGraphEndpoint` jusqu'au
    tool, d'où `STEP_A.errors=1` et le statut connect partial. Le lot doit survivre,
    MAIS l'échec doit rester compté : c'est l'honnêteté que codex a apportée et que
    gemini masquait en rapportant errors=0 sur les mêmes exceptions serveur.
    """
    from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint

    entity_id = uuid4()
    graph = MagicMock()
    graph.find_unlinked_nodes = AsyncMock(return_value=[str(entity_id)])
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)

    session = AsyncMock()
    result = MagicMock()
    result.fetchall = MagicMock(return_value=[MagicMock(id=entity_id, embedding=[0.1] * 4)])
    session.execute = AsyncMock(return_value=result)

    tools, _session, linker = _dream_graph_tools(graph, session=session)
    # `_dream_graph_tools` réassigne `auto_link` : le side_effect doit venir APRÈS.
    linker.auto_link = AsyncMock(
        side_effect=UnknownGraphEndpoint("one or more UUID endpoints are not registered")
    )

    output = await tools["brain_backfill_links_batch"]()

    assert "errors=1" in output
    assert "entities_processed=0" in output


@pytest.mark.asyncio
async def test_scoped_backfill_still_propagates_an_authorization_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-régression du fix (c) : le catch doit rester `UnknownGraphEndpoint` seul.

    Ce test vire au rouge si quelqu'un « symétrise » avec un `except Exception`,
    ce qui rendrait muet un refus d'autorisation scopé.
    """
    entity_id = uuid4()
    denial = DreamProjectAuthorizationError("object_not_authorized")
    graph = MagicMock()
    graph.find_unlinked_nodes = AsyncMock(return_value=[str(entity_id)])
    scope = RecordingScope()
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: scope, raising=False)

    session = AsyncMock()
    result = MagicMock()
    result.fetchall = MagicMock(return_value=[MagicMock(id=entity_id, embedding=[0.1] * 4)])
    session.execute = AsyncMock(return_value=result)

    tools, _session, linker = _dream_graph_tools(graph, session=session)
    # `_dream_graph_tools` réassigne `auto_link` : le side_effect doit venir APRÈS.
    linker.auto_link = AsyncMock(side_effect=denial)

    with pytest.raises(DreamProjectAuthorizationError) as raised:
        await tools["brain_backfill_links_batch"]()

    assert raised.value is denial
