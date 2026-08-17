#!/usr/bin/env python3
"""Populate Neo4j from existing PostgreSQL data.

Usage:
    python scripts/init_graph.py

Reads POSTGRES_URL, NEO4J_URL, NEO4J_USER, NEO4J_PASSWORD from env or .env file.
Idempotent — safe to run multiple times (uses MERGE).
"""

import asyncio
import os
import sys

import asyncpg
import yaml
from neo4j import AsyncDriver, AsyncGraphDatabase

from brain_v42.config import Settings, get_settings
from brain_v42.services.graph_projection_schema import GRAPH_PROJECTION_CONSTRAINTS


async def main() -> int:
    pg_url = os.getenv("POSTGRES_URL", "postgresql://brain:brain@localhost:5433/brain")
    settings = Settings(postgres_url=pg_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    if settings.graph_ledger_write_enabled:
        print(
            "init_graph is disabled while the PostgreSQL graph ledger owns writes; "
            "use the outbox rebuild procedure.",
            file=sys.stderr,
        )
        return 2

    # Convert SQLAlchemy URL to asyncpg format
    pg_url = pg_url.replace("postgresql+asyncpg://", "postgresql://")
    neo4j_url = os.getenv("NEO4J_URL", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "brain_v42_graph")

    pg = await asyncpg.connect(pg_url)
    driver = AsyncGraphDatabase.driver(neo4j_url, auth=(neo4j_user, neo4j_password))

    try:
        await create_constraints(driver)
        await create_project_nodes(pg, driver)
        await create_entity_nodes(pg, driver)
        await create_belongs_to(pg, driver)
        await create_supersedes(pg, driver)
        await create_merged_into(pg, driver)
        await create_project_hierarchy(driver)
    finally:
        await pg.close()
        await driver.close()

    print("Done!")
    return 0


async def create_constraints(driver: AsyncDriver) -> None:
    async with driver.session() as session:
        for statement in GRAPH_PROJECTION_CONSTRAINTS:
            await session.run(statement)
    print(f"Created {len(GRAPH_PROJECTION_CONSTRAINTS)} constraints")


async def create_project_nodes(pg: asyncpg.Connection, driver: AsyncDriver) -> None:
    rows = await pg.fetch("SELECT id, project_key, name FROM project_contexts")
    async with driver.session() as session:
        for row in rows:
            await session.run(
                "MERGE (p:Project {project_key: $pk}) SET p.name = $name, p.id = $id",
                {"pk": row["project_key"], "name": row["name"], "id": str(row["id"])},
            )
    print(f"Created {len(rows)} Project nodes")


async def create_entity_nodes(pg: asyncpg.Connection, driver: AsyncDriver) -> None:
    tables: dict[str, tuple[str, str]] = {
        "Decision": ("decisions", "title"),
        "Learning": ("learnings", "topic"),
        "Snippet": ("snippets", "title"),
        "Runbook": ("runbooks", "title"),
        "ADR": ("adrs", "title"),
    }
    total = 0
    for label, (table, title_col) in tables.items():
        rows = await pg.fetch(f"SELECT id, project_key, {title_col} FROM {table}")  # nosec B608 # {title_col}/{table} come from the `tables` dict literal above (line 78-84), no caller input (audited 2026-08-17)
        async with driver.session() as session:
            for row in rows:
                await session.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n.project_key = $pk, n.title = $title",
                    {"id": str(row["id"]), "pk": row["project_key"], "title": row[title_col]},
                )
        total += len(rows)
        print(f"  {label}: {len(rows)} nodes")
    print(f"Created {total} entity nodes total")


async def create_belongs_to(pg: asyncpg.Connection, driver: AsyncDriver) -> None:
    tables = ["decisions", "learnings", "snippets", "runbooks", "adrs"]
    total = 0
    for table in tables:
        rows = await pg.fetch(f"SELECT id, project_key FROM {table} WHERE project_key IS NOT NULL")  # nosec B608 # {table} iterates the `tables` list literal above (line 100), no caller input (audited 2026-08-17)
        async with driver.session() as session:
            for row in rows:
                await session.run(
                    "MATCH (e {id: $eid}) MATCH (p:Project {project_key: $pk}) "
                    "MERGE (e)-[:BELONGS_TO]->(p)",
                    {"eid": str(row["id"]), "pk": row["project_key"]},
                )
        total += len(rows)
    print(f"Created {total} BELONGS_TO relations")


async def create_supersedes(pg: asyncpg.Connection, driver: AsyncDriver) -> None:
    # Decision supersession: superseded_by on the OLD decision points to the NEW decision's ID.
    # In Neo4j: (new)-[:SUPERSEDES]->(old)
    rows = await pg.fetch("SELECT id, superseded_by FROM decisions WHERE superseded_by IS NOT NULL")
    async with driver.session() as session:
        for row in rows:
            old_id = str(row["id"])
            new_id = str(row["superseded_by"])
            await session.run(
                "MATCH (new:Decision {id: $new_id}) MATCH (old:Decision {id: $old_id}) "
                "MERGE (new)-[:SUPERSEDES]->(old)",
                {"new_id": new_id, "old_id": old_id},
            )
    print(f"Created {len(rows)} SUPERSEDES relations")


async def create_merged_into(pg: asyncpg.Connection, driver: AsyncDriver) -> None:
    tables = ["decisions", "learnings", "snippets", "runbooks", "adrs"]
    total = 0
    for table in tables:
        rows = await pg.fetch(f"SELECT id, merged_into FROM {table} WHERE merged_into IS NOT NULL")  # nosec B608 # {table} iterates the `tables` list literal above (line 132), no caller input (audited 2026-08-17)
        async with driver.session() as session:
            for row in rows:
                await session.run(
                    "MATCH (src {id: $src}) MATCH (tgt {id: $tgt}) "
                    "MERGE (src)-[:MERGED_INTO]->(tgt)",
                    {"src": str(row["id"]), "tgt": str(row["merged_into"])},
                )
        total += len(rows)
    print(f"Created {total} MERGED_INTO relations")


async def create_project_hierarchy(driver: AsyncDriver) -> None:
    # Configurable (BRAIN_PROJECT_HIERARCHY_PATH / PROJECT_HIERARCHY_PATH),
    # default config/project_hierarchy.yml resolved against the current
    # working directory -- this is an ops script meant to be run from a
    # repo checkout, not a portable installed entry point, so there is no
    # package-relative path that would survive a real wheel install anyway.
    # The tracked file is project_hierarchy.example.yml; the real,
    # operator-private topology is untracked.
    yaml_path = get_settings().brain_project_hierarchy_path
    if not yaml_path.exists():
        print(f"No project_hierarchy.yml found at {yaml_path}, skipping")
        return

    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    hierarchy = config.get("project_hierarchy", {})
    total = 0
    async with driver.session() as session:
        for project_key, rels in hierarchy.items():
            # Ensure the project node exists even if not in project_contexts
            await session.run(
                "MERGE (p:Project {project_key: $pk})",
                {"pk": project_key},
            )
            for sub_key in rels.get("contains", []):
                await session.run(
                    "MERGE (s:Project {project_key: $sk})",
                    {"sk": sub_key},
                )
                await session.run(
                    "MATCH (p:Project {project_key: $pk}) "
                    "MATCH (s:Project {project_key: $sk}) "
                    "MERGE (p)-[:CONTAINS]->(s)",
                    {"pk": project_key, "sk": sub_key},
                )
                total += 1
            for dep_key in rels.get("depends_on", []):
                await session.run(
                    "MERGE (d:Project {project_key: $dk})",
                    {"dk": dep_key},
                )
                await session.run(
                    "MATCH (p:Project {project_key: $pk}) "
                    "MATCH (d:Project {project_key: $dk}) "
                    "MERGE (p)-[:DEPENDS_ON]->(d)",
                    {"pk": project_key, "dk": dep_key},
                )
                total += 1
    print(f"Created {total} project hierarchy relations")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
