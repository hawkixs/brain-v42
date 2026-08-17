"""Neo4j identity constraints required by the durable graph projection."""

from __future__ import annotations

from typing import Any

GRAPH_PROJECTION_CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (n:Decision) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT learning_id IF NOT EXISTS FOR (n:Learning) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT snippet_id IF NOT EXISTS FOR (n:Snippet) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT runbook_id IF NOT EXISTS FOR (n:Runbook) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT adr_id IF NOT EXISTS FOR (n:ADR) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT feature_id IF NOT EXISTS FOR (n:Feature) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT plan_id IF NOT EXISTS FOR (n:Plan) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT project_pk IF NOT EXISTS FOR (n:Project) REQUIRE n.project_key IS UNIQUE",
    "CREATE CONSTRAINT domain_name IF NOT EXISTS FOR (n:Domain) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT brain_projection_fence_name IF NOT EXISTS "
    "FOR (n:BrainProjectionFence) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT brain_projection_cursor_key IF NOT EXISTS "
    "FOR (n:BrainProjectionCursor) REQUIRE n.aggregate_key IS UNIQUE",
)


async def ensure_graph_projection_schema(driver: Any) -> None:
    """Create every idempotent node identity constraint before projection."""
    async with driver.session() as session:
        for statement in GRAPH_PROJECTION_CONSTRAINTS:
            await session.run(statement)
