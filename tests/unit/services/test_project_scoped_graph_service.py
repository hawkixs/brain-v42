"""Project-scoped GraphService contract tests (SEC1b Task 5)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.services.graph_service import (
    _CANONICAL_REL_TYPES,
    _DEFAULT_PATH_EXCLUDES,
    GraphService,
)

PROJECT_KEY = "brain_v42"
KNOWLEDGE_LABELS = {"Decision", "Learning", "Snippet", "Runbook", "ADR"}


@pytest.fixture
def graph() -> GraphService:
    service = GraphService(driver=AsyncMock())
    service._run_read = AsyncMock(return_value=[])
    service._run_counted = AsyncMock(return_value="matched")
    return service


def _assert_scoped_read(service: GraphService) -> tuple[str, dict]:
    query, params = service._run_read.await_args.args
    assert params["project_key"] == PROJECT_KEY
    assert set(params["knowledge_labels"]) == KNOWLEDGE_LABELS
    assert "$project_key" in query
    assert "$knowledge_labels" in query
    assert "BELONGS_TO" in query
    assert "size(" in query
    assert "all(label IN labels(" in query
    assert "any(label IN labels(" not in query
    return query, params


@pytest.mark.asyncio
async def test_create_relation_requires_two_uniquely_owned_knowledge_anchors(
    graph: GraphService,
) -> None:
    signature = inspect.signature(GraphService.create_relation)
    assert "project_key" in signature.parameters
    assert signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["project_key"].default is None
    source, target = uuid4(), uuid4()
    graph._run_counted.return_value = "missing_node"

    outcome = await graph.create_relation(
        source,
        target,
        "RELATED_TO",
        project_key=PROJECT_KEY,
    )

    query, params = graph._run_counted.await_args.args
    assert params == {
        "source_id": str(source),
        "target_id": str(target),
        "project_key": PROJECT_KEY,
        "knowledge_labels": params["knowledge_labels"],
    }
    assert set(params["knowledge_labels"]) == KNOWLEDGE_LABELS
    assert "all(label IN labels(a) WHERE label IN $knowledge_labels)" in query
    assert "all(label IN labels(b) WHERE label IN $knowledge_labels)" in query
    assert "size([(a)-[:BELONGS_TO]->(owner:Project) | owner]) = 1" in query
    assert "size([(b)-[:BELONGS_TO]->(owner:Project) | owner]) = 1" in query
    assert "[(a)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key]" in query
    assert "[(b)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key]" in query
    assert outcome == "missing_node"


@pytest.mark.asyncio
async def test_admin_create_relation_keeps_historical_query_and_params(
    graph: GraphService,
) -> None:
    source, target = uuid4(), uuid4()

    await graph.create_relation(source, target, "USES")

    graph._run_counted.assert_awaited_once_with(
        "MATCH (a {id: $source_id}) MATCH (b {id: $target_id}) "
        "MERGE (a)-[r:USES]->(b) RETURN count(a) AS anchors",
        {"source_id": str(source), "target_id": str(target)},
    )


@pytest.mark.asyncio
async def test_delete_node_requires_one_matching_project_owner(graph: GraphService) -> None:
    signature = inspect.signature(GraphService.delete_node)
    assert signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["project_key"].default is None
    entity_id = uuid4()
    graph._run = AsyncMock(return_value="ok")

    outcome = await graph.delete_node("Decision", entity_id, project_key=PROJECT_KEY)

    query, params = graph._run.await_args.args
    assert params == {
        "id": str(entity_id),
        "project_key": PROJECT_KEY,
        "knowledge_labels": params["knowledge_labels"],
    }
    assert set(params["knowledge_labels"]) == KNOWLEDGE_LABELS
    assert "MATCH (n:Decision {id: $id})" in query
    assert "size(labels(n)) > 0" in query
    assert "all(label IN labels(n) WHERE label IN $knowledge_labels)" in query
    assert "size([(n)-[:BELONGS_TO]->(owner:Project) | owner]) = 1" in query
    assert "[(n)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key]" in query
    assert "DETACH DELETE n" in query
    assert outcome == "ok"


@pytest.mark.asyncio
async def test_get_neighbors_scopes_every_path_node_and_bounds_depth(graph: GraphService) -> None:
    anchor = uuid4()

    await graph.get_neighbors(anchor, depth=99, project_key=PROJECT_KEY)

    query, params = _assert_scoped_read(graph)
    assert "*1..3" in query
    assert "all(node IN nodes(path)" in query
    assert params["id"] == str(anchor)


@pytest.mark.asyncio
async def test_get_related_ids_scopes_anchors_and_neighbors(graph: GraphService) -> None:
    anchors = [uuid4(), uuid4()]

    result = await graph.get_related_ids(anchors, project_key=PROJECT_KEY)

    query, params = _assert_scoped_read(graph)
    assert "UNWIND $ids AS eid" in query
    assert "all(node IN nodes(path)" in query
    assert params["ids"] == [str(value) for value in anchors]
    assert result == {}


@pytest.mark.asyncio
async def test_find_unlinked_nodes_scopes_candidates(graph: GraphService) -> None:
    await graph.find_unlinked_nodes(entity_type="Decision", limit=7, project_key=PROJECT_KEY)

    query, params = _assert_scoped_read(graph)
    assert "NOT (n)-[:RELATED_TO]-()" in query
    assert params["type"] == "Decision"
    assert params["limit"] == 7


@pytest.mark.asyncio
async def test_find_unlinked_nodes_excludes_projection_control_nodes(graph: GraphService) -> None:
    await graph.find_unlinked_nodes()

    query, params = graph._run_read.await_args.args
    assert params == {"type": None, "limit": 50}
    assert "BrainProjectionFence" in query
    assert "BrainProjectionCursor" in query


@pytest.mark.asyncio
async def test_get_all_related_edges_scopes_both_endpoints(graph: GraphService) -> None:
    await graph.get_all_related_edges(project_key=PROJECT_KEY)

    query, _params = _assert_scoped_read(graph)
    assert "a.id < b.id" in query
    assert "all(node IN [a, b]" in query


@pytest.mark.asyncio
async def test_get_path_searches_shortest_path_inside_authorized_subgraph(
    graph: GraphService,
) -> None:
    source, target = uuid4(), uuid4()

    await graph.get_path(source, target, max_depth=99, project_key=PROJECT_KEY)

    query, params = _assert_scoped_read(graph)
    assert "*1..6" in query
    assert "shortestPath" not in query
    assert "all(node IN nodes(p)" in query
    assert "ORDER BY length(p)" in query
    assert "LIMIT 1" in query
    assert params["source_id"] == str(source)
    assert params["target_id"] == str(target)


@pytest.mark.asyncio
async def test_link_entity_to_domain_scopes_anchor_and_returns_missing_node(
    graph: GraphService,
) -> None:
    anchor = uuid4()
    graph._run_counted.return_value = "missing_node"

    result = await graph.link_entity_to_domain(anchor, "infra", project_key=PROJECT_KEY)

    query, params = graph._run_counted.await_args.args
    assert params == {
        "entity_id": str(anchor),
        "domain_name": "infra",
        "project_key": PROJECT_KEY,
        "knowledge_labels": params["knowledge_labels"],
    }
    assert set(params["knowledge_labels"]) == KNOWLEDGE_LABELS
    assert "$project_key" in query
    assert "$knowledge_labels" in query
    assert "size(" in query
    assert "all(label IN labels(e)" in query
    assert result == "missing_node"


@pytest.mark.asyncio
async def test_find_orphans_for_classification_scopes_candidates(graph: GraphService) -> None:
    await graph.find_orphans_for_classification(limit=9, project_key=PROJECT_KEY)

    query, params = _assert_scoped_read(graph)
    assert "NOT (n)-[:RELATED_TO]-()" in query
    assert "NOT (n)-[:BELONGS_TO_DOMAIN]->()" in query
    assert params["limit"] == 9


@pytest.mark.asyncio
async def test_admin_calls_keep_historical_query_shapes(graph: GraphService) -> None:
    """None scope keeps its call shape while excluding projection controls."""
    anchor, target = uuid4(), uuid4()

    await graph.get_neighbors(anchor)
    await graph.get_related_ids([anchor])
    await graph.find_unlinked_nodes()
    await graph.get_all_related_edges()
    await graph.get_path(anchor, target)
    await graph.link_entity_to_domain(anchor, "infra")
    await graph.find_orphans_for_classification()

    read_calls = graph._run_read.await_args_list
    counted_calls = graph._run_counted.await_args_list
    assert len(read_calls) == 6
    assert len(counted_calls) == 1
    for call in [*read_calls, *counted_calls]:
        query, params = call.args
        assert "project_key" not in params
        assert "knowledge_labels" not in params
        assert "$knowledge_labels" not in query

    assert (
        read_calls[1].args[0]
        == """
            UNWIND $ids AS eid
            MATCH (e {id: eid})-[r]-(neighbor)
            WHERE neighbor.id <> eid
            WITH eid, neighbor, type(r) AS rel_type, labels(neighbor)[0] AS ntype
            RETURN eid,
                   collect({id: neighbor.id, type: ntype, rel: rel_type,
                            title: coalesce(neighbor.title, neighbor.topic)})[..5] AS neighbors
        """
    )
    assert (
        read_calls[2].args[0]
        == """
            MATCH (n)
            WHERE NOT (n)-[:RELATED_TO]-()
            AND ($type IS NULL OR $type IN labels(n))
            AND NOT n:BrainProjectionFence
            AND NOT n:BrainProjectionCursor
            RETURN n.id AS id
            LIMIT $limit
        """
    )
    assert (
        read_calls[3].args[0]
        == """
            MATCH (a)-[:RELATED_TO]-(b)
            WHERE a.id < b.id
            RETURN DISTINCT a.id AS src, b.id AS tgt
        """
    )
    allowed = _CANONICAL_REL_TYPES - set(_DEFAULT_PATH_EXCLUDES)
    rel_filter = ":" + "|".join(sorted(allowed))
    assert (
        read_calls[4].args[0]
        == f"""
            MATCH p = shortestPath(
                (a {{id: $source_id}})-[{rel_filter}*1..3]-(b {{id: $target_id}})
            )
            RETURN [n IN nodes(p) |
                    {{id: n.id, type: labels(n)[0],
                      label: coalesce(n.title, n.topic, n.name)}}] AS nodes,
                   [r IN relationships(p) | type(r)] AS rels
            LIMIT 1
        """
    )
    assert (
        read_calls[5].args[0]
        == """
        MATCH (n)
        WHERE NOT (n)-[:RELATED_TO]-()
          AND NOT (n)-[:BELONGS_TO_DOMAIN]->()
          AND ANY(l IN labels(n)
                  WHERE l IN ['Decision','Learning','Snippet','Runbook','ADR'])
        RETURN n.id AS id, labels(n) AS labels LIMIT $limit
    """
    )
