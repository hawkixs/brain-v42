"""Clamps apply before scoped/unscoped services and announce changed values."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools import crud_tools, decay_tools, dream_tools
from brain_v42.services.link_result import LinkJobResult


def _factory(rows: list[object] | None = None) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = rows or []
    result.mappings.return_value.all.return_value = []
    session.execute.return_value = result
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    return factory, session


def _scope(scoped: bool) -> SimpleNamespace | None:
    return SimpleNamespace(project_key="brain-v42", revalidate_ids=AsyncMock()) if scoped else None


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(("requested", "expected"), [(10**9, 100), (-5, 1)])
async def test_consolidation_candidates_clamps_limit(
    scoped: bool, requested: int, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decay_tools, "get_dream_project_scope", lambda: _scope(scoped))
    job = MagicMock(find_candidates=AsyncMock(return_value=[]))
    server = FastMCP("consolidation-clamp")
    decay_tools.register_decay_tools(server, MagicMock(), job)
    tool = await server.get_tool("brain_consolidation_candidates")
    assert tool is not None
    result = await tool.run({"limit": requested})
    kwargs = job.find_candidates.call_args.kwargs
    assert kwargs["limit"] == expected
    assert kwargs.get("project_key") == ("brain-v42" if scoped else None)
    assert f"limit {requested}" in str(result)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    ("limit", "max_links", "expected_limit", "expected_links"),
    [(10**9, 10**9, 100, 10), (-5, -5, 1, 1), (50, 3, 50, 3)],
)
async def test_backfill_links_batch_clamps_limit_and_max_links(
    scoped: bool,
    limit: int,
    max_links: int,
    expected_limit: int,
    expected_links: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dream_tools, "get_dream_project_scope", lambda: _scope(scoped))
    entity_id = uuid4()
    factory, _ = _factory([SimpleNamespace(id=entity_id, embedding=[0.1])])
    graph = MagicMock(find_unlinked_nodes=AsyncMock(return_value=[str(entity_id)]))
    linker = MagicMock(auto_link=AsyncMock(return_value=LinkJobResult()))
    server = FastMCP("backfill-clamp")
    dream_tools.register_dream_tools(
        server, session_factory=factory, auto_linker=linker, graph_service=graph
    )
    tool = await server.get_tool("brain_backfill_links_batch")
    assert tool is not None
    result = await tool.run({"entity_type": "Learning", "limit": limit, "max_links": max_links})
    assert graph.find_unlinked_nodes.call_args.kwargs["limit"] == expected_limit
    assert linker.auto_link.call_args.kwargs["max_links"] == expected_links
    assert linker.auto_link.call_args.kwargs["threshold"] == 0.6
    if limit != expected_limit:
        assert f"limit {limit}" in str(result)
    if max_links != expected_links:
        assert "max_links" in str(result)


async def test_backfill_empty_result_still_announces_clamps() -> None:
    server = FastMCP("empty-backfill")
    dream_tools.register_dream_tools(
        server,
        session_factory=MagicMock(),
        auto_linker=MagicMock(),
        graph_service=MagicMock(find_unlinked_nodes=AsyncMock(return_value=[])),
    )
    tool = await server.get_tool("brain_backfill_links_batch")
    assert tool is not None
    result = str(await tool.run({"limit": 10**9, "max_links": 10**9}))
    assert "limit 1000000000" in result and "max_links" in result


@pytest.mark.parametrize(("requested", "expected"), [(-5, 1), (10**9, 100)])
async def test_get_clusters_clamps_limit(requested: int, expected: int) -> None:
    edges = [(str(uuid4()), str(uuid4())) for _ in range(110)]
    server = FastMCP("cluster-limit")
    dream_tools.register_dream_tools(
        server,
        session_factory=MagicMock(),
        auto_linker=None,
        graph_service=MagicMock(get_all_related_edges=AsyncMock(return_value=edges)),
    )
    tool = await server.get_tool("brain_get_clusters")
    assert tool is not None
    result = str(await tool.run({"limit": requested, "summary_only": True}))
    assert f"{expected} cluster(s) found" in result
    assert f"limit {requested}" in result


@pytest.mark.parametrize(("requested", "expected"), [(10**9, 200), (-5, 1), (30, 30)])
async def test_get_clusters_caps_members_per_cluster(requested: int, expected: int) -> None:
    ids = [str(uuid4()) for _ in range(250)]
    factory, _ = _factory()
    graph = MagicMock(
        get_all_related_edges=AsyncMock(return_value=list(zip(ids, ids[1:], strict=False)))
    )
    server = FastMCP("cluster-members")
    dream_tools.register_dream_tools(
        server, session_factory=factory, auto_linker=None, graph_service=graph
    )
    tool = await server.get_tool("brain_get_clusters")
    assert tool is not None
    result = await tool.run({"max_members_per_cluster": requested})
    output = result.content[0].text
    assert isinstance(output, str)
    assert output.count("- [unknown]") == expected
    if requested != expected:
        assert "max_members_per_cluster" in output and "clamp" in output.lower()


async def test_curation_proposals_negative_offset_is_floored() -> None:
    factory, session = _factory()
    server = FastMCP("curation-offset")
    dream_tools.register_dream_tools(
        server, session_factory=factory, auto_linker=None, graph_service=None
    )
    tool = await server.get_tool("brain_list_curation_proposals")
    assert tool is not None
    await tool.run({"project_key": "brain-v42", "offset": -5})
    assert session.execute.call_args.args[1]["offset"] == 0


@pytest.mark.parametrize(
    ("entity_type", "method"),
    [
        ("learning", "list_all"),
        ("decision", "list_all"),
        ("snippet", "list_snippets"),
        ("runbook", "list_by_project"),
        ("adr", "list_all"),
    ],
)
async def test_brain_list_negative_offset_is_floored(entity_type: str, method: str) -> None:
    svc = MagicMock()
    setattr(svc, method, AsyncMock(return_value=[]))
    server = FastMCP("list-offset")
    crud_tools.register_crud_tools(
        server,
        decision_svc=svc,
        learning_svc=svc,
        snippet_svc=svc,
        runbook_svc=svc,
        adr_svc=svc,
        session_factory=MagicMock(),
    )
    tool = await server.get_tool("brain_list")
    assert tool is not None
    await tool.run({"entity_type": entity_type, "project_key": "brain-v42", "offset": -5})
    assert getattr(svc, method).call_args.kwargs["offset"] == 0
