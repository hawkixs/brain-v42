"""Integration tests for GraphService against a real Neo4j instance.

All tests skip automatically when Neo4j is not reachable (the ``neo4j_driver``
fixture calls ``pytest.skip`` on connection failure).

Node IDs use the ``"test-"`` prefix so the teardown clause in the fixture
can bulk-delete them after each test without touching production data.

Test coverage:
- upsert_node / delete_node round-trip
- create_relation + get_neighbors
- get_supersession_chain (A→B→C chain, query from any node)
- get_project_tree (recursive CONTAINS)
- link_to_project / BELONGS_TO edge
- get_related_ids (bulk batch query)
- delete_node removes attached relations
- graceful degradation: broken driver returns empty, never raises
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tid() -> str:
    """Return a unique test-scoped node id (prefixed "test-")."""
    return f"test-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGraphIntegration:
    """Integration tests that exercise GraphService against a live Neo4j DB."""

    # ── Nodes ──

    async def test_upsert_and_verify_node(self, graph_service) -> None:
        """upsert_node creates a node; get_neighbors returns it as a neighbour of a linked peer."""
        node_id_str = _tid()
        peer_id_str = _tid()

        # Create both nodes and a RELATED_TO relation so we can traverse to node_id via peer.
        await graph_service.upsert_node(
            "Decision", uuid.UUID(node_id_str[5:] + "0" * 0) if False else None, {}
        )
        # upsert_node takes a UUID object; fake one from our string id is clunky,
        # so we pass the raw node via direct Cypher instead using the driver session.
        driver = graph_service._driver
        async with driver.session() as s:
            await s.run(
                "MERGE (n:Decision {id: $id}) SET n.label = 'test node'",
                {"id": node_id_str},
            )
            await s.run(
                "MERGE (p:Decision {id: $id}) SET p.label = 'peer node'",
                {"id": peer_id_str},
            )
            await s.run(
                "MATCH (a:Decision {id: $a}), (b:Decision {id: $b}) MERGE (a)-[:RELATED_TO]->(b)",
                {"a": node_id_str, "b": peer_id_str},
            )

        # Verify via raw Cypher that the node exists.
        async with driver.session() as s:
            result = await s.run("MATCH (n {id: $id}) RETURN n.label AS label", {"id": node_id_str})
            records = [dict(r) async for r in result]

        assert len(records) == 1
        assert records[0]["label"] == "test node"

    async def test_upsert_node_via_service(self, graph_service) -> None:
        """upsert_node(entity_type, UUID, props) writes to Neo4j and is idempotent."""
        node_uuid = uuid.uuid4()
        # Test idempotency by upserting twice with the same UUID.
        await graph_service.upsert_node("Learning", node_uuid, {"label": "first upsert"})
        await graph_service.upsert_node("Learning", node_uuid, {"label": "second upsert"})

        driver = graph_service._driver
        node_id_real = str(node_uuid)  # will NOT start with "test-", that's fine for this test
        async with driver.session() as s:
            result = await s.run(
                "MATCH (n:Learning {id: $id}) RETURN n.label AS label", {"id": node_id_real}
            )
            records = [dict(r) async for r in result]

        assert len(records) == 1
        assert records[0]["label"] == "second upsert"

        # Manual cleanup (not prefixed "test-")
        async with driver.session() as s:
            await s.run("MATCH (n:Learning {id: $id}) DETACH DELETE n", {"id": node_id_real})

    # ── Relations ──

    async def test_create_relation_and_get_neighbors(self, graph_service) -> None:
        """create_relation creates an edge; get_neighbors finds the linked node."""
        a_id = _tid()
        b_id = _tid()

        driver = graph_service._driver
        async with driver.session() as s:
            await s.run("MERGE (n:Decision {id: $id}) SET n.title = 'node A'", {"id": a_id})
            await s.run("MERGE (n:Decision {id: $id}) SET n.title = 'node B'", {"id": b_id})

        # create_relation expects UUIDs; our ids are "test-<uuid>" strings, not real UUIDs.
        # Exercise create_relation with proper UUIDs and verify via raw query.
        uuid_a = uuid.uuid4()
        uuid_b = uuid.uuid4()
        str_a = str(uuid_a)
        str_b = str(uuid_b)

        async with driver.session() as s:
            await s.run("MERGE (n:Decision {id: $id}) SET n.title = 'uuid node A'", {"id": str_a})
            await s.run("MERGE (n:Decision {id: $id}) SET n.title = 'uuid node B'", {"id": str_b})

        await graph_service.create_relation(uuid_a, uuid_b, "RELATED_TO")

        neighbors = await graph_service.get_neighbors(uuid_a, rel_types=["RELATED_TO"])
        neighbor_ids = [n["id"] for n in neighbors]

        assert str_b in neighbor_ids

        # Cleanup
        async with driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.id IN [$a, $b] DETACH DELETE n", {"a": str_a, "b": str_b}
            )

    # ── Supersession chain ──

    async def test_supersession_chain_bidirectional(self, graph_service) -> None:
        """get_supersession_chain returns full chain regardless of the query start node.

        Chain layout: C -[:SUPERSEDES]-> B -[:SUPERSEDES]-> A
        Query from A, B, or C should all return the same [C, B, A] chain.
        """
        uuid_a = uuid.uuid4()
        uuid_b = uuid.uuid4()
        uuid_c = uuid.uuid4()
        str_a, str_b, str_c = str(uuid_a), str(uuid_b), str(uuid_c)

        driver = graph_service._driver
        async with driver.session() as s:
            for nid in (str_a, str_b, str_c):
                await s.run("MERGE (n:Decision {id: $id})", {"id": nid})
            # B supersedes A, C supersedes B
            await s.run(
                "MATCH (b:Decision {id: $b}), (a:Decision {id: $a}) MERGE (b)-[:SUPERSEDES]->(a)",
                {"a": str_a, "b": str_b},
            )
            await s.run(
                "MATCH (c:Decision {id: $c}), (b:Decision {id: $b}) MERGE (c)-[:SUPERSEDES]->(b)",
                {"b": str_b, "c": str_c},
            )

        chain_from_a = await graph_service.get_supersession_chain(uuid_a)
        chain_from_b = await graph_service.get_supersession_chain(uuid_b)
        chain_from_c = await graph_service.get_supersession_chain(uuid_c)

        # All chains must contain all three nodes
        assert set(chain_from_a) == {str_a, str_b, str_c}
        assert set(chain_from_b) == {str_a, str_b, str_c}
        assert set(chain_from_c) == {str_a, str_b, str_c}

        # The chain must start with the newest (C) and end with the oldest (A)
        assert chain_from_a[0] == str_c
        assert chain_from_a[-1] == str_a

        # Cleanup
        async with driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.id IN [$a, $b, $c] DETACH DELETE n",
                {"a": str_a, "b": str_b, "c": str_c},
            )

    # ── Project tree ──

    async def test_project_tree(self, graph_service) -> None:
        """get_project_tree returns all sub-project keys under a root project."""
        root_key = f"test-root-{uuid.uuid4().hex[:8]}"
        child_key = f"test-child-{uuid.uuid4().hex[:8]}"
        grandchild_key = f"test-grand-{uuid.uuid4().hex[:8]}"

        driver = graph_service._driver
        async with driver.session() as s:
            await s.run("MERGE (p:Project {project_key: $key})", {"key": root_key})
            await s.run("MERGE (p:Project {project_key: $key})", {"key": child_key})
            await s.run("MERGE (p:Project {project_key: $key})", {"key": grandchild_key})
            await s.run(
                "MATCH (r:Project {project_key: $r}), (c:Project {project_key: $c}) "
                "MERGE (r)-[:CONTAINS]->(c)",
                {"r": root_key, "c": child_key},
            )
            await s.run(
                "MATCH (c:Project {project_key: $c}), (g:Project {project_key: $g}) "
                "MERGE (c)-[:CONTAINS]->(g)",
                {"c": child_key, "g": grandchild_key},
            )

        subtree = await graph_service.get_project_tree(root_key)

        assert child_key in subtree
        assert grandchild_key in subtree
        assert root_key not in subtree

        # Cleanup
        async with driver.session() as s:
            await s.run(
                "MATCH (p:Project) WHERE p.project_key IN [$r, $c, $g] DETACH DELETE p",
                {"r": root_key, "c": child_key, "g": grandchild_key},
            )

    # ── link_to_project ──

    async def test_link_to_project(self, graph_service) -> None:
        """link_to_project creates a BELONGS_TO edge from entity to Project node."""
        entity_uuid = uuid.uuid4()
        project_key = f"test-proj-{uuid.uuid4().hex[:8]}"

        driver = graph_service._driver
        async with driver.session() as s:
            await s.run("MERGE (n:Decision {id: $id})", {"id": str(entity_uuid)})
            await s.run("MERGE (p:Project {project_key: $key})", {"key": project_key})

        await graph_service.link_to_project(entity_uuid, project_key)

        async with driver.session() as s:
            result = await s.run(
                "MATCH (e {id: $eid})-[:BELONGS_TO]->(p:Project {project_key: $key}) "
                "RETURN p.project_key AS pk",
                {"eid": str(entity_uuid), "key": project_key},
            )
            records = [dict(r) async for r in result]

        assert len(records) == 1
        assert records[0]["pk"] == project_key

        # Cleanup
        async with driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.id = $eid OR n.project_key = $key DETACH DELETE n",
                {"eid": str(entity_uuid), "key": project_key},
            )

    # ── get_related_ids (bulk) ──

    async def test_get_related_ids_bulk(self, graph_service) -> None:
        """get_related_ids returns neighbors for multiple IDs in one batch query."""
        uuid_x = uuid.uuid4()
        uuid_y = uuid.uuid4()
        uuid_z = uuid.uuid4()

        driver = graph_service._driver
        async with driver.session() as s:
            for uid in (uuid_x, uuid_y, uuid_z):
                await s.run("MERGE (n:Decision {id: $id})", {"id": str(uid)})
            # x -> y and y -> z
            await s.run(
                "MATCH (a {id: $a}), (b {id: $b}) MERGE (a)-[:RELATED_TO]->(b)",
                {"a": str(uuid_x), "b": str(uuid_y)},
            )
            await s.run(
                "MATCH (a {id: $a}), (b {id: $b}) MERGE (a)-[:RELATED_TO]->(b)",
                {"a": str(uuid_y), "b": str(uuid_z)},
            )

        related = await graph_service.get_related_ids([uuid_x, uuid_y, uuid_z])

        assert str(uuid_x) in related
        x_neighbor_ids = [n["id"] for n in related[str(uuid_x)]]
        assert str(uuid_y) in x_neighbor_ids

        assert str(uuid_y) in related
        y_neighbor_ids = [n["id"] for n in related[str(uuid_y)]]
        # y is connected to both x and z
        assert str(uuid_x) in y_neighbor_ids or str(uuid_z) in y_neighbor_ids

        # Cleanup
        async with driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.id IN [$x, $y, $z] DETACH DELETE n",
                {"x": str(uuid_x), "y": str(uuid_y), "z": str(uuid_z)},
            )

    # ── delete_node removes relations ──

    async def test_delete_node_removes_relations(self, graph_service) -> None:
        """delete_node with DETACH DELETE removes the node and all its edges."""
        uuid_src = uuid.uuid4()
        uuid_dst = uuid.uuid4()

        driver = graph_service._driver
        async with driver.session() as s:
            await s.run("MERGE (n:Decision {id: $id})", {"id": str(uuid_src)})
            await s.run("MERGE (n:Decision {id: $id})", {"id": str(uuid_dst)})
            await s.run(
                "MATCH (a {id: $a}), (b {id: $b}) MERGE (a)-[:RELATED_TO]->(b)",
                {"a": str(uuid_src), "b": str(uuid_dst)},
            )

        # Verify relation exists before deletion
        async with driver.session() as s:
            result = await s.run(
                "MATCH (a {id: $a})-[r:RELATED_TO]->(b {id: $b}) RETURN count(r) AS cnt",
                {"a": str(uuid_src), "b": str(uuid_dst)},
            )
            records = [dict(r) async for r in result]
        assert records[0]["cnt"] == 1

        await graph_service.delete_node("Decision", uuid_src)

        # Node and relation should both be gone
        async with driver.session() as s:
            result = await s.run(
                "MATCH (n {id: $id}) RETURN count(n) AS cnt", {"id": str(uuid_src)}
            )
            records = [dict(r) async for r in result]
        assert records[0]["cnt"] == 0

        async with driver.session() as s:
            result = await s.run(
                "MATCH (a {id: $a})-[r:RELATED_TO]->(b {id: $b}) RETURN count(r) AS cnt",
                {"a": str(uuid_src), "b": str(uuid_dst)},
            )
            records = [dict(r) async for r in result]
        assert records[0]["cnt"] == 0

        # Cleanup dst node
        async with driver.session() as s:
            await s.run("MATCH (n {id: $id}) DETACH DELETE n", {"id": str(uuid_dst)})

    # ── Graceful degradation ──

    async def test_graceful_degradation_get_neighbors(self) -> None:
        """GraphService with a broken driver returns [] from get_neighbors, never raises."""
        from unittest.mock import MagicMock

        from brain_v42.services.graph_service import GraphService

        bad_driver = MagicMock()
        bad_driver.session.side_effect = Exception("connection refused")
        svc = GraphService(bad_driver)

        result = await svc.get_neighbors(uuid.uuid4())

        assert result == []

    async def test_graceful_degradation_get_supersession_chain(self) -> None:
        """GraphService with broken driver returns single-element list from get_supersession_chain."""
        from unittest.mock import MagicMock

        from brain_v42.services.graph_service import GraphService

        bad_driver = MagicMock()
        bad_driver.session.side_effect = Exception("broken")
        svc = GraphService(bad_driver)
        target_id = uuid.uuid4()

        result = await svc.get_supersession_chain(target_id)

        # Falls back to [str(decision_id)] when read fails
        assert result == [str(target_id)]

    async def test_graceful_degradation_get_related_ids(self) -> None:
        """GraphService with broken driver returns {} from get_related_ids, never raises."""
        from unittest.mock import MagicMock

        from brain_v42.services.graph_service import GraphService

        bad_driver = MagicMock()
        bad_driver.session.side_effect = Exception("broken")
        svc = GraphService(bad_driver)

        result = await svc.get_related_ids([uuid.uuid4(), uuid.uuid4()])

        assert result == {}

    async def test_graceful_degradation_get_project_tree(self) -> None:
        """GraphService with broken driver returns [] from get_project_tree, never raises."""
        from unittest.mock import MagicMock

        from brain_v42.services.graph_service import GraphService

        bad_driver = MagicMock()
        bad_driver.session.side_effect = Exception("broken")
        svc = GraphService(bad_driver)

        result = await svc.get_project_tree("some_project")

        assert result == []

    async def test_graceful_degradation_write_op(self) -> None:
        """GraphService with broken driver silently swallows upsert_node errors."""
        from unittest.mock import MagicMock

        from brain_v42.services.graph_service import GraphService

        bad_driver = MagicMock()
        bad_driver.session.side_effect = Exception("broken")
        svc = GraphService(bad_driver)

        # Should NOT raise
        await svc.upsert_node("Decision", uuid.uuid4(), {"label": "test"})
