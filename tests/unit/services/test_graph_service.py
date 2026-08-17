"""Unit tests for GraphService.

All tests use unittest.mock (AsyncMock/MagicMock) — no real Neo4j.

Test cases:
1.  test_upsert_node
2.  test_delete_node
3.  test_create_relation
4.  test_create_relation_with_props
5.  test_delete_relation
6.  test_link_to_project
7.  test_get_neighbors
8.  test_get_neighbors_with_rel_types
9.  test_get_supersession_chain
10. test_get_supersession_chain_empty
11. test_get_project_tree
12. test_get_project_tree_empty
13. test_get_related_ids
14. test_get_related_ids_empty
15. test_healthcheck_success
16. test_healthcheck_failure
17. test_run_logs_error_on_exception
18. test_run_read_returns_empty_on_exception
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.graph_service import (
    _CANONICAL_REL_TYPES,
    GraphService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_async_result(rows: list[dict]) -> AsyncMock:
    """Build an async iterable result that yields the given rows."""
    result = AsyncMock()

    async def _aiter():
        for row in rows:
            yield row

    result.__aiter__ = lambda self: _aiter()
    return result


@pytest.fixture
def mock_driver():
    """Mock Neo4j async driver with session context manager.

    The default result mock supports both read (async iteration) and write
    (consume() -> ResultSummary) paths.  counters.relationships_created = 0
    simulates a MERGE that matched an existing edge — the safe default for
    pre-existing tests that don't care about the outcome value.
    """
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    session = AsyncMock()
    result = _make_async_result([])

    # Wire up a summary so _run_counted can inspect counters without raising.
    summary = MagicMock()
    summary.counters.relationships_created = 0
    result.consume = AsyncMock(return_value=summary)

    session.run = AsyncMock(return_value=result)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    driver.session.return_value = ctx

    return driver, session, result


@pytest.fixture
def graph_service(mock_driver):
    driver, _, _ = mock_driver
    return GraphService(driver, timeout=5.0)


# ---------------------------------------------------------------------------
# Node tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_node(mock_driver, graph_service):
    """upsert_node fires a MERGE query with entity_type label and id."""
    _, session, _ = mock_driver
    entity_id = uuid4()
    props = {"title": "my-decision", "label": "Use PostgreSQL"}

    await graph_service.upsert_node("Decision", entity_id, props)

    session.run.assert_called_once()
    # session.run now receives a neo4j.Query object — use .text to inspect content.
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "MERGE" in query
    assert "Decision" in query
    assert params["id"] == str(entity_id)
    assert params["props"] == props


@pytest.mark.asyncio
async def test_delete_node(mock_driver, graph_service):
    """delete_node fires a DETACH DELETE query with entity_type label and id."""
    _, session, _ = mock_driver
    entity_id = uuid4()

    await graph_service.delete_node("Decision", entity_id)

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "DETACH DELETE" in query
    assert "Decision" in query
    assert params["id"] == str(entity_id)


# ---------------------------------------------------------------------------
# Relation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_relation(mock_driver, graph_service):
    """create_relation fires MATCH+MERGE query with rel_type."""
    _, session, _ = mock_driver
    src = uuid4()
    tgt = uuid4()

    await graph_service.create_relation(src, tgt, "SUPERSEDES")

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "MATCH" in query
    assert "MERGE" in query
    assert "SUPERSEDES" in query
    assert params["source_id"] == str(src)
    assert params["target_id"] == str(tgt)


@pytest.mark.asyncio
async def test_create_relation_with_props(mock_driver, graph_service):
    """create_relation with props adds SET r += $props to the query."""
    _, session, _ = mock_driver
    src = uuid4()
    tgt = uuid4()
    props = {"reason": "refactored", "since": "2026-01-01"}

    await graph_service.create_relation(src, tgt, "SUPERSEDES", props=props)

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "SET r += $props" in query
    assert params["props"] == props


@pytest.mark.asyncio
async def test_related_to_merge_matches_either_legacy_orientation(mock_driver, graph_service):
    """RELATED_TO is symmetric, including for edges imported in reverse order."""
    _, session, _ = mock_driver

    await graph_service.create_relation(uuid4(), uuid4(), "RELATED_TO")

    query = session.run.call_args[0][0].text
    assert "MERGE (a)-[r:RELATED_TO]-(b)" in query


@pytest.mark.asyncio
async def test_delete_relation(mock_driver, graph_service):
    """delete_relation fires a DELETE query for the specific rel_type."""
    _, session, _ = mock_driver
    src = uuid4()
    tgt = uuid4()

    await graph_service.delete_relation(src, tgt, "SUPERSEDES")

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "DELETE" in query
    assert "SUPERSEDES" in query
    assert params["source_id"] == str(src)
    assert params["target_id"] == str(tgt)


@pytest.mark.asyncio
async def test_related_to_delete_removes_either_legacy_orientation(mock_driver, graph_service):
    _, session, _ = mock_driver

    await graph_service.delete_relation(uuid4(), uuid4(), "RELATED_TO")

    query = session.run.call_args[0][0].text
    assert "-[r:RELATED_TO]-" in query
    assert "-[r:RELATED_TO]->" not in query


@pytest.mark.asyncio
async def test_link_to_project(mock_driver, graph_service):
    """link_to_project fires MATCH by project_key + MERGE BELONGS_TO."""
    _, session, _ = mock_driver
    entity_id = uuid4()

    await graph_service.link_to_project(entity_id, "brain_v42")

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "BELONGS_TO" in query
    assert "project_key" in query
    assert params["entity_id"] == str(entity_id)
    assert params["project_key"] == "brain_v42"


@pytest.mark.asyncio
async def test_unlink_from_project_matches_project_by_key(mock_driver, graph_service):
    """Project membership deletes must not assume Project nodes expose UUID ids."""
    _, session, _ = mock_driver
    entity_id = uuid4()

    outcome = await graph_service.unlink_from_project(entity_id, "brain_v42")

    assert outcome == "ok"
    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "[r:BELONGS_TO]" in query
    assert "Project {project_key: $project_key}" in query
    assert "DELETE r" in query
    assert params == {"entity_id": str(entity_id), "project_key": "brain_v42"}


@pytest.mark.asyncio
async def test_upsert_project_merges_by_stable_key(mock_driver, graph_service):
    """Project upserts must respect Neo4j's unique project_key constraint."""
    _, session, _ = mock_driver
    project_id = uuid4()

    outcome = await graph_service.upsert_project("brain-v42", project_id, "Brain v42")

    assert outcome == "ok"
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "MERGE (p:Project {project_key: $project_key})" in query
    assert "p.id = $id" in query
    assert params == {
        "project_key": "brain-v42",
        "id": str(project_id),
        "name": "Brain v42",
    }


@pytest.mark.asyncio
async def test_delete_project_matches_stable_key(mock_driver, graph_service):
    _, session, _ = mock_driver

    outcome = await graph_service.delete_project("brain-v42")

    assert outcome == "ok"
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    assert "MATCH (p:Project {project_key: $project_key})" in query_obj.text
    assert "DETACH DELETE p" in query_obj.text
    assert params == {"project_key": "brain-v42"}


@pytest.mark.asyncio
async def test_create_project_relation_uses_stable_keys(mock_driver, graph_service):
    _, session, _ = mock_driver

    outcome = await graph_service.create_project_relation("red-root", "brain-v42", "CONTAINS")

    assert outcome == "matched"
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    assert "Project {project_key: $source_key}" in query_obj.text
    assert "Project {project_key: $target_key}" in query_obj.text
    assert "[r:CONTAINS]" in query_obj.text
    assert params == {"source_key": "red-root", "target_key": "brain-v42"}


@pytest.mark.asyncio
async def test_delete_project_relation_uses_stable_keys(mock_driver, graph_service):
    _, session, _ = mock_driver

    outcome = await graph_service.delete_project_relation("brain-v42", "postgres", "DEPENDS_ON")

    assert outcome == "ok"
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    assert "Project {project_key: $source_key}" in query_obj.text
    assert "Project {project_key: $target_key}" in query_obj.text
    assert "[r:DEPENDS_ON]" in query_obj.text
    assert "DELETE r" in query_obj.text
    assert params == {"source_key": "brain-v42", "target_key": "postgres"}


@pytest.mark.asyncio
async def test_project_relation_rejects_non_hierarchy_type(mock_driver, graph_service):
    _, session, _ = mock_driver

    with pytest.raises(ValueError, match="unsupported project relation"):
        await graph_service.create_project_relation("a", "b", "RELATED_TO")

    session.run.assert_not_called()


@pytest.mark.asyncio
async def test_unlink_from_domain_matches_domain_by_name(mock_driver, graph_service):
    """Domain membership deletes must match the stable domain name."""
    _, session, _ = mock_driver
    entity_id = uuid4()

    outcome = await graph_service.unlink_from_domain(entity_id, "infra")

    assert outcome == "ok"
    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "[r:BELONGS_TO_DOMAIN]" in query
    assert "Domain {name: $domain_name}" in query
    assert "DELETE r" in query
    assert params == {"entity_id": str(entity_id), "domain_name": "infra"}


# ---------------------------------------------------------------------------
# Traversal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_neighbors(mock_driver):
    """get_neighbors returns list of dicts from the query result."""
    driver, session, _ = mock_driver
    rows = [
        {"id": "aaa", "type": "Learning", "rel": "RELATED_TO", "label": "some topic"},
    ]
    result = _make_async_result(rows)
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    entity_id = uuid4()
    neighbors = await svc.get_neighbors(entity_id)

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "MATCH" in query
    assert "neighbor" in query
    assert params["id"] == str(entity_id)
    assert neighbors == rows


@pytest.mark.asyncio
async def test_get_neighbors_with_rel_types(mock_driver):
    """get_neighbors with rel_types adds a rel filter to the query."""
    driver, session, _ = mock_driver
    result = _make_async_result([])
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    entity_id = uuid4()
    await svc.get_neighbors(entity_id, rel_types=["SUPERSEDES", "RELATED_TO"])

    query = session.run.call_args[0][0].text
    assert "SUPERSEDES" in query
    assert "RELATED_TO" in query


@pytest.mark.asyncio
async def test_get_neighbors_filters_non_canonical_rel_types(mock_driver):
    """get_neighbors drops rel_types not in _CANONICAL_REL_TYPES (Cypher injection guard).

    Mirrors the whitelist already applied by get_path. A malicious rel_type
    crafted to break out of the filter and inject Cypher must never reach the
    query string.
    """
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    injection = "X*0..99]-() DETACH DELETE start //"
    await svc.get_neighbors(uuid4(), rel_types=["SUPERSEDES", injection])

    query = session.run.call_args[0][0].text
    assert "SUPERSEDES" in query
    assert "DETACH DELETE start" not in query
    assert injection not in query


@pytest.mark.asyncio
async def test_get_neighbors_returns_empty_when_all_rel_types_invalid(mock_driver):
    """When every rel_type is non-canonical, get_neighbors short-circuits to []
    without ever issuing a query (matches get_path's empty-allowed behavior)."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    result = await svc.get_neighbors(uuid4(), rel_types=["NOPE", "DROP TABLE"])

    assert result == []
    session.run.assert_not_called()


@pytest.mark.asyncio
async def test_get_supersession_chain(mock_driver):
    """get_supersession_chain returns chain_ids from the query result."""
    driver, session, _ = mock_driver
    chain = ["uuid-3", "uuid-2", "uuid-1"]
    rows = [{"chain_ids": chain}]
    result = _make_async_result(rows)
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    decision_id = uuid4()
    result_chain = await svc.get_supersession_chain(decision_id)

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "SUPERSEDES" in query
    assert params["decision_id"] == str(decision_id)
    assert result_chain == chain


@pytest.mark.asyncio
async def test_get_supersession_chain_empty(mock_driver):
    """get_supersession_chain returns [decision_id] when no chain found."""
    driver, session, _ = mock_driver
    result = _make_async_result([])
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    decision_id = uuid4()
    result_chain = await svc.get_supersession_chain(decision_id)

    assert result_chain == [str(decision_id)]


@pytest.mark.asyncio
async def test_get_project_tree(mock_driver):
    """get_project_tree returns list of sub-project keys."""
    driver, session, _ = mock_driver
    sub_keys = ["brain_v42.ml", "brain_v42.infra"]
    rows = [{"sub_keys": sub_keys}]
    result = _make_async_result(rows)
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    result_keys = await svc.get_project_tree("brain_v42")

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "CONTAINS" in query
    assert params["project_key"] == "brain_v42"
    assert result_keys == sub_keys


@pytest.mark.asyncio
async def test_get_project_tree_empty(mock_driver):
    """get_project_tree returns [] when no sub-projects found."""
    driver, session, _ = mock_driver
    result = _make_async_result([])
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    result_keys = await svc.get_project_tree("brain_v42")

    assert result_keys == []


@pytest.mark.asyncio
async def test_get_related_ids(mock_driver):
    """get_related_ids returns dict mapping entity_id to list of neighbors."""
    driver, session, _ = mock_driver
    id1 = uuid4()
    id2 = uuid4()
    rows = [
        {
            "eid": str(id1),
            "neighbors": [{"id": "n1", "type": "Learning", "rel": "RELATED_TO", "title": "t"}],
        },
        {"eid": str(id2), "neighbors": []},
    ]
    result = _make_async_result(rows)
    session.run = AsyncMock(return_value=result)
    svc = GraphService(driver)

    output = await svc.get_related_ids([id1, id2])

    session.run.assert_called_once()
    query_obj, params = session.run.call_args[0][0], session.run.call_args[0][1]
    query = query_obj.text
    assert "UNWIND" in query
    assert params["ids"] == [str(id1), str(id2)]
    assert str(id1) in output
    assert str(id2) in output
    assert len(output[str(id1)]) == 1


@pytest.mark.asyncio
async def test_get_related_ids_empty(graph_service):
    """get_related_ids returns {} for empty input without calling Neo4j."""
    result = await graph_service.get_related_ids([])

    assert result == {}


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_success(mock_driver, graph_service):
    """healthcheck returns True when RETURN 1 query succeeds."""
    result = await graph_service.healthcheck()
    assert result is True


@pytest.mark.asyncio
async def test_healthcheck_failure(mock_driver):
    """healthcheck returns False when driver.session raises an exception."""
    driver = MagicMock()
    driver.session.side_effect = Exception("connection refused")
    svc = GraphService(driver)

    result = await svc.healthcheck()
    assert result is False


# ---------------------------------------------------------------------------
# Inventory tests (count_nodes_by_label / count_edges_by_type)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_nodes_by_label(mock_driver):
    """count_nodes_by_label returns {label: count} from a Cypher query."""
    driver, session, _ = mock_driver
    rows = [
        {"label": "Decision", "count": 12},
        {"label": "Learning", "count": 47},
        {"label": "ADR", "count": 5},
    ]
    session.run = AsyncMock(return_value=_make_async_result(rows))
    svc = GraphService(driver)

    result = await svc.count_nodes_by_label()

    assert result == {"Decision": 12, "Learning": 47, "ADR": 5}
    query = session.run.call_args[0][0].text
    assert "labels(" in query
    assert "count(" in query
    assert "BrainProjectionFence" in query
    assert "BrainProjectionCursor" in query


@pytest.mark.asyncio
async def test_count_nodes_by_label_empty(mock_driver):
    """count_nodes_by_label returns {} when graph has no nodes."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    assert await svc.count_nodes_by_label() == {}


@pytest.mark.asyncio
async def test_count_edges_by_type(mock_driver):
    """count_edges_by_type returns {rel_type: count} from a Cypher query."""
    driver, session, _ = mock_driver
    rows = [
        {"type": "RELATED_TO", "count": 89},
        {"type": "SUPERSEDES", "count": 3},
        {"type": "BELONGS_TO", "count": 64},
    ]
    session.run = AsyncMock(return_value=_make_async_result(rows))
    svc = GraphService(driver)

    result = await svc.count_edges_by_type()

    assert result == {"RELATED_TO": 89, "SUPERSEDES": 3, "BELONGS_TO": 64}
    query = session.run.call_args[0][0].text
    assert "type(" in query
    assert "count(" in query


@pytest.mark.asyncio
async def test_count_edges_by_type_empty(mock_driver):
    """count_edges_by_type returns {} when graph has no edges."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    assert await svc.count_edges_by_type() == {}


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_logs_error_on_exception(mock_driver):
    """_run catches exceptions and logs the error without re-raising."""
    driver, session, _ = mock_driver
    session.run.side_effect = Exception("Neo4j write failed")
    driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
    svc = GraphService(driver)

    # Must not raise
    await svc._run("MERGE (n:Test {id: $id})", {"id": "test"})


@pytest.mark.asyncio
async def test_run_read_returns_empty_on_exception(mock_driver):
    """_run_read catches exceptions and returns [] without re-raising."""
    driver, session, _ = mock_driver
    session.run.side_effect = Exception("Neo4j read failed")
    driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
    svc = GraphService(driver)

    result = await svc._run_read("MATCH (n) RETURN n", {})
    assert result == []


# ---------------------------------------------------------------------------
# Auto-reconnect tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_retries_after_reconnect(mock_driver):
    """_run retries once after successful reconnect."""
    driver, session, _ = mock_driver
    call_count = 0

    async def _fail_then_succeed(query, params, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("connection lost")

    session.run = AsyncMock(side_effect=_fail_then_succeed)
    svc = GraphService(driver)

    await svc._run("MERGE (n:Test {id: $id})", {"id": "test"})

    assert call_count == 2
    driver.verify_connectivity.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_read_retries_after_reconnect(mock_driver):
    """_run_read retries once after successful reconnect."""
    driver, session, _ = mock_driver
    call_count = 0
    expected_rows = [{"id": "test"}]

    async def _fail_then_succeed(query, params, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("connection lost")
        return _make_async_result(expected_rows)

    session.run = AsyncMock(side_effect=_fail_then_succeed)
    svc = GraphService(driver)

    result = await svc._run_read("MATCH (n) RETURN n", {})

    assert call_count == 2
    driver.verify_connectivity.assert_awaited_once()
    assert result == expected_rows


@pytest.mark.asyncio
async def test_run_no_retry_when_reconnect_fails(mock_driver):
    """_run does not retry when reconnect fails."""
    driver, session, _ = mock_driver
    session.run.side_effect = Exception("connection lost")
    driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))
    svc = GraphService(driver)

    await svc._run("MERGE (n:Test {id: $id})", {"id": "test"})

    # Only called once — no retry since reconnect failed
    assert session.run.await_count == 1
    driver.verify_connectivity.assert_awaited_once()


# ---------------------------------------------------------------------------
# _run_counted / RelationWriteOutcome tests (TDD — written before implementation)
# ---------------------------------------------------------------------------


def _make_session_cm_with_summary(relationships_created: int):
    """Build a full async driver mock returning a ResultSummary counter."""
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()

    summary = MagicMock()
    summary.counters.relationships_created = relationships_created

    result = AsyncMock()
    result.consume = AsyncMock(return_value=summary)

    session = AsyncMock()
    session.run = AsyncMock(return_value=result)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    driver.session = MagicMock(return_value=session_cm)

    return driver, session, result, summary


@pytest.mark.asyncio
async def test_create_relation_returns_created_on_new_edge():
    """create_relation returns 'created' when Neo4j counters.relationships_created == 1."""
    from uuid import UUID

    driver, session, result, summary = _make_session_cm_with_summary(relationships_created=1)
    svc = GraphService(driver)

    outcome = await svc.create_relation(
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        "RELATED_TO",
    )

    assert outcome == "created"
    session.run.assert_called_once()
    result.consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_relation_returns_matched_on_existing_edge():
    """create_relation returns 'matched' when Neo4j counters.relationships_created == 0."""
    from uuid import UUID

    driver, session, result, summary = _make_session_cm_with_summary(relationships_created=0)
    svc = GraphService(driver)

    outcome = await svc.create_relation(
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        "RELATED_TO",
    )

    assert outcome == "matched"
    session.run.assert_called_once()
    result.consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_relation_returns_error_on_exception():
    """create_relation returns 'error' when session.run raises (reconnect also fails)."""
    from uuid import UUID

    driver = MagicMock()
    driver.verify_connectivity = AsyncMock(side_effect=Exception("still down"))

    session = AsyncMock()
    session.run = AsyncMock(side_effect=Exception("Neo4j unavailable"))

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    driver.session = MagicMock(return_value=session_cm)

    svc = GraphService(driver)

    outcome = await svc.create_relation(
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        "RELATED_TO",
    )

    assert outcome == "error"
    # Reconnect must have been attempted
    driver.verify_connectivity.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_counted_retries_after_reconnect():
    """_run_counted retries once on transient failure when reconnect succeeds."""
    from uuid import UUID

    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()  # reconnect succeeds

    call_count = 0
    summary = MagicMock()
    summary.counters.relationships_created = 1
    good_result = AsyncMock()
    good_result.consume = AsyncMock(return_value=summary)

    session = AsyncMock()

    async def _fail_then_succeed(query, params, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("transient failure")
        return good_result

    session.run = AsyncMock(side_effect=_fail_then_succeed)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    driver.session = MagicMock(return_value=session_cm)

    svc = GraphService(driver)

    outcome = await svc.create_relation(
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
        "RELATED_TO",
    )

    assert outcome == "created"
    assert call_count == 2
    driver.verify_connectivity.assert_awaited_once()


# ---------------------------------------------------------------------------
# Domain node tests (B1 — TDD RED phase)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_domain_accepts_valid_name(mock_driver):
    """upsert_domain returns 'ok' and calls _run with MERGE query for valid domain.

    Updated from old bool contract (True/False) to tri-state Literal['ok','invalid_domain','error'].
    """
    _, session, _ = mock_driver
    driver, _, _ = mock_driver
    svc = GraphService(driver)

    result = await svc.upsert_domain("infra")

    assert result == "ok"
    session.run.assert_called_once()
    query = session.run.call_args[0][0].text
    assert "MERGE" in query
    assert "Domain" in query


@pytest.mark.asyncio
async def test_upsert_domain_rejects_invalid_name(mock_driver):
    """upsert_domain returns 'invalid_domain' and does NOT call _run for unknown domain names.

    Updated from old bool contract: 'invalid_domain' is reserved exclusively for
    ALLOWED_DOMAINS validation failures (distinct from 'error' for Neo4j failures).
    """
    _, session, _ = mock_driver
    driver, _, _ = mock_driver
    svc = GraphService(driver)

    result = await svc.upsert_domain("not_a_domain")

    assert result == "invalid_domain"
    session.run.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_domain_rejects_uppercase(mock_driver):
    """upsert_domain returns 'invalid_domain' for uppercase names — constraint is case-sensitive.

    Updated from old bool contract.
    """
    _, session, _ = mock_driver
    driver, _, _ = mock_driver
    svc = GraphService(driver)

    result = await svc.upsert_domain("INFRA")

    assert result == "invalid_domain"
    session.run.assert_not_called()


@pytest.mark.asyncio
async def test_link_entity_to_domain_returns_created_on_new_edge():
    """link_entity_to_domain returns 'created' when relationships_created == 1."""
    driver, session, result, _ = _make_session_cm_with_summary(relationships_created=1)
    svc = GraphService(driver)

    entity_id = uuid4()
    outcome = await svc.link_entity_to_domain(entity_id, "infra")

    assert outcome == "created"
    session.run.assert_called_once()
    query = session.run.call_args[0][0].text
    assert "BELONGS_TO_DOMAIN" in query


@pytest.mark.asyncio
async def test_link_entity_to_domain_returns_matched_on_existing_edge():
    """link_entity_to_domain returns 'matched' when relationships_created == 0."""
    driver, session, result, _ = _make_session_cm_with_summary(relationships_created=0)
    svc = GraphService(driver)

    entity_id = uuid4()
    outcome = await svc.link_entity_to_domain(entity_id, "ml")

    assert outcome == "matched"
    session.run.assert_called_once()


@pytest.mark.asyncio
async def test_find_orphans_for_classification_runs_cypher_query(mock_driver):
    """find_orphans_for_classification returns orphan dicts and uses expected query patterns."""
    driver, session, _ = mock_driver
    orphans = [
        {"id": "aaa-111", "labels": ["Decision"]},
        {"id": "bbb-222", "labels": ["Learning"]},
    ]
    session.run = AsyncMock(return_value=_make_async_result(orphans))
    svc = GraphService(driver)

    result = await svc.find_orphans_for_classification(limit=20)

    assert result == orphans
    session.run.assert_called_once()
    query = session.run.call_args[0][0].text
    assert "NOT (n)-[:RELATED_TO]" in query
    assert "NOT (n)-[:BELONGS_TO_DOMAIN]" in query
    assert "ANY(l IN labels(n)" in query


# ---------------------------------------------------------------------------
# get_path tests (C1 — TDD)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_path_returns_empty_when_no_rows(mock_driver):
    """get_path returns [] when _run_read finds no matching path."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    result = await svc.get_path(uuid4(), uuid4())

    assert result == []


@pytest.mark.asyncio
async def test_get_path_interleaves_rel_to_next(mock_driver):
    """get_path attaches rel_to_next to each node except the last."""
    driver, session, _ = mock_driver
    node_a = {"id": "aaa", "type": "Decision", "label": "A"}
    node_b = {"id": "bbb", "type": "Learning", "label": "B"}
    node_c = {"id": "ccc", "type": "Snippet", "label": "C"}
    rows = [{"nodes": [node_a, node_b, node_c], "rels": ["RELATED_TO", "USES"]}]
    session.run = AsyncMock(return_value=_make_async_result(rows))
    svc = GraphService(driver)

    result = await svc.get_path(uuid4(), uuid4())

    assert len(result) == 3
    assert result[0]["rel_to_next"] == "RELATED_TO"
    assert result[1]["rel_to_next"] == "USES"
    assert "rel_to_next" not in result[2]


@pytest.mark.asyncio
async def test_get_path_default_excludes_belongs_to_domain(mock_driver):
    """get_path does NOT include BELONGS_TO_DOMAIN in the rel filter by default."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    await svc.get_path(uuid4(), uuid4())

    query = session.run.call_args[0][0].text
    assert "BELONGS_TO_DOMAIN" not in query


@pytest.mark.asyncio
async def test_get_path_respects_explicit_rel_types_whitelist(mock_driver):
    """get_path uses exactly the provided rel_types when they are all canonical."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    await svc.get_path(uuid4(), uuid4(), rel_types=["RELATED_TO", "USES"])

    query = session.run.call_args[0][0].text
    assert "RELATED_TO" in query
    assert "USES" in query
    # No other canonical types should appear in the rel filter portion
    # We verify by checking the shortestPath pattern contains exactly those two
    for rel in _CANONICAL_REL_TYPES - {"RELATED_TO", "USES"}:
        assert rel not in query


@pytest.mark.asyncio
async def test_get_path_filters_unknown_rel_types(mock_driver):
    """get_path silently drops non-canonical rel types from whitelist."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    await svc.get_path(uuid4(), uuid4(), rel_types=["FAKE_REL", "RELATED_TO"])

    query = session.run.call_args[0][0].text
    assert "RELATED_TO" in query
    assert "FAKE_REL" not in query


@pytest.mark.asyncio
async def test_get_path_empty_whitelist_returns_empty(mock_driver):
    """get_path returns [] immediately when no canonical rel_types match, skipping Neo4j."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    result = await svc.get_path(uuid4(), uuid4(), rel_types=["UNKNOWN_ONLY"])

    assert result == []
    session.run.assert_not_called()


@pytest.mark.asyncio
async def test_get_path_clamps_depth_to_range(mock_driver):
    """get_path clamps max_depth to 6 even when 99 is passed."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    await svc.get_path(uuid4(), uuid4(), max_depth=99)

    query = session.run.call_args[0][0].text
    assert "*1..6" in query
    assert "*1..99" not in query


@pytest.mark.asyncio
async def test_get_path_exclude_rel_types_additive(mock_driver):
    """get_path excludes both default BELONGS_TO_DOMAIN and additional exclude_rel_types."""
    driver, session, _ = mock_driver
    session.run = AsyncMock(return_value=_make_async_result([]))
    svc = GraphService(driver)

    await svc.get_path(uuid4(), uuid4(), exclude_rel_types=["RELATED_TO"])

    query = session.run.call_args[0][0].text
    assert "BELONGS_TO_DOMAIN" not in query
    assert "RELATED_TO" not in query


# ---------------------------------------------------------------------------
# Cross-project read methods (Spec C MVP β)
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_service_with_read():
    svc = GraphService(driver=MagicMock())
    mock_read = AsyncMock()
    svc._run_read = mock_read
    return svc, mock_read


class TestFetchActiveDomains:
    @pytest.mark.asyncio
    async def test_returns_domain_names(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = [{"domain": "ml"}, {"domain": "memory"}]
        result = await svc.fetch_active_domains("brain-v42", top_n=2)
        assert result == ["ml", "memory"]
        query = mock_read.call_args[0][0]
        assert "BELONGS_TO_DOMAIN" in query
        assert "ORDER BY n DESC" in query

    @pytest.mark.asyncio
    async def test_empty_on_no_domains(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = []
        assert await svc.fetch_active_domains("brain-v42") == []


class TestFetchCrossProjectEntityIds:
    @pytest.mark.asyncio
    async def test_excludes_current_project_in_cypher(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = []
        await svc.fetch_cross_project_entity_ids(["ml"], exclude_project_key="brain-v42")
        query, params = mock_read.call_args[0]
        assert "p.project_key <> $exclude" in query
        assert params["exclude"] == "brain-v42"
        assert params["limit"] == 50  # candidate cap; recency sort happens in PG

    @pytest.mark.asyncio
    async def test_returns_rows_verbatim(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        rows = [{"id": "abc", "labels": ["Decision"], "project_key": "red-shrik"}]
        mock_read.return_value = rows
        assert await svc.fetch_cross_project_entity_ids(["ml"], exclude_project_key="x") == rows


class TestFetchDecisionIdsInDomain:
    @pytest.mark.asyncio
    async def test_returns_ids_for_domain(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = [{"id": "11111111-1111-1111-1111-111111111111"}]
        result = await svc.fetch_decision_ids_in_domain("ml")
        assert result == ["11111111-1111-1111-1111-111111111111"]
        query = mock_read.call_args[0][0]
        assert "(e:Decision)" in query
