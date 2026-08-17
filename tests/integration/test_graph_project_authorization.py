"""Real-Neo4j proof for conservative project-scoped graph traversal.

The shared integration fixtures require all three dedicated
``BRAIN_V42_TEST_NEO4J_*`` variables and skip before driver construction when
they are absent. There is deliberately no localhost fallback.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def run_migrations() -> None:
    """This Neo4j-only module must not require or migrate PostgreSQL."""


@pytest.fixture(scope="session", autouse=True)
def check_db_connection() -> None:
    """Shadow the package-level PostgreSQL connectivity fixture."""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data() -> None:
    """Cleanup is UUID-scoped in the test's ``finally`` block."""


@pytest.mark.asyncio
async def test_authorized_subgraph_path_and_ownership_fail_closed(
    graph_service: object, neo4j_driver: object
) -> None:
    project_key = f"integ-sec1b-{uuid4().hex}"
    foreign_key = f"integ-sec1b-foreign-{uuid4().hex}"
    stale_key = f"integ-sec1b-stale-{uuid4().hex}"
    cleanup_token = f"integ-sec1b-domain-{uuid4().hex}"
    ids = {name: uuid4() for name in ("source", "step1", "step2", "target", "foreign")}
    ids.update({name: uuid4() for name in ("missing", "stale", "multiple", "owned_peer")})
    raw_ids = {name: str(value) for name, value in ids.items()}

    driver = neo4j_driver
    try:
        async with driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                """
                CREATE (project:Project {project_key: $project_key})
                CREATE (foreign_project:Project {project_key: $foreign_key})
                CREATE (stale_project:Project {project_key: $stale_key})
                CREATE (domain:Domain {name: 'infra', cleanup_token: $cleanup_token})
                CREATE (source:Decision {id: $source, title: 'source'})
                CREATE (step1:Learning {id: $step1, topic: 'step1'})
                CREATE (step2:Snippet {id: $step2, title: 'step2'})
                CREATE (target:ADR {id: $target, title: 'target'})
                CREATE (foreign:Runbook {id: $foreign, title: 'foreign shortcut'})
                CREATE (missing:Decision {id: $missing})
                CREATE (stale:Decision {id: $stale})
                CREATE (multiple:Decision {id: $multiple})
                CREATE (owned_peer:Learning {id: $owned_peer})
                CREATE (source)-[:BELONGS_TO]->(project)
                CREATE (step1)-[:BELONGS_TO]->(project)
                CREATE (step2)-[:BELONGS_TO]->(project)
                CREATE (target)-[:BELONGS_TO]->(project)
                CREATE (owned_peer)-[:BELONGS_TO]->(project)
                CREATE (foreign)-[:BELONGS_TO]->(foreign_project)
                CREATE (stale)-[:BELONGS_TO]->(stale_project)
                CREATE (multiple)-[:BELONGS_TO]->(project)
                CREATE (multiple)-[:BELONGS_TO]->(foreign_project)
                CREATE (source)-[:RELATED_TO]->(step1)
                CREATE (step1)-[:RELATED_TO]->(step2)
                CREATE (step2)-[:RELATED_TO]->(target)
                CREATE (source)-[:RELATED_TO]->(foreign)
                CREATE (foreign)-[:RELATED_TO]->(target)
                CREATE (missing)-[:RELATED_TO]->(owned_peer)
                CREATE (stale)-[:RELATED_TO]->(owned_peer)
                CREATE (multiple)-[:RELATED_TO]->(owned_peer)
                """,
                {
                    "project_key": project_key,
                    "foreign_key": foreign_key,
                    "stale_key": stale_key,
                    "cleanup_token": cleanup_token,
                    **raw_ids,
                },
            )

        path = await graph_service.get_path(  # type: ignore[attr-defined]
            ids["source"], ids["target"], max_depth=4, project_key=project_key
        )
        assert [node["id"] for node in path] == [
            raw_ids["source"],
            raw_ids["step1"],
            raw_ids["step2"],
            raw_ids["target"],
        ]
        assert raw_ids["foreign"] not in {node["id"] for node in path}

        assert await graph_service.link_entity_to_domain(  # type: ignore[attr-defined]
            ids["source"], "infra", project_key=project_key
        ) in {"created", "matched"}

        for unsafe_anchor in ("missing", "stale", "multiple"):
            assert (
                await graph_service.get_neighbors(  # type: ignore[attr-defined]
                    ids[unsafe_anchor], project_key=project_key
                )
                == []
            )
            assert (
                await graph_service.link_entity_to_domain(  # type: ignore[attr-defined]
                    ids[unsafe_anchor], "infra", project_key=project_key
                )
                == "missing_node"
            )
    finally:
        async with driver.session() as session:  # type: ignore[attr-defined]
            await session.run(
                "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                {"ids": list(raw_ids.values())},
            )
            await session.run(
                "MATCH (p:Project) WHERE p.project_key IN $keys DETACH DELETE p",
                {"keys": [project_key, foreign_key, stale_key]},
            )
            await session.run(
                "MATCH (d:Domain {cleanup_token: $cleanup_token}) DETACH DELETE d",
                {"cleanup_token": cleanup_token},
            )
