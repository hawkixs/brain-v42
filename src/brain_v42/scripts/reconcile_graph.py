#!/usr/bin/env python3
"""PG ↔ Neo4j consistency check.

Usage:
    python scripts/reconcile_graph.py          # report only
    python scripts/reconcile_graph.py --fix    # fix drift
"""

import asyncio
import os
import sys

import asyncpg
from neo4j import AsyncDriver, AsyncGraphDatabase

from brain_v42.config import Settings

# Maps Neo4j label -> (PG table, PG title column)
ENTITY_MAP: dict[str, tuple[str, str]] = {
    "Decision": ("decisions", "title"),
    "Learning": ("learnings", "topic"),
    "Snippet": ("snippets", "title"),
    "Runbook": ("runbooks", "title"),
    "ADR": ("adrs", "title"),
}


async def fetch_pg_ids(pg: asyncpg.Connection, table: str) -> dict[str, dict]:
    """Return {id_str: {id, project_key, title_col}} for all rows in a table."""
    # Determine title column
    title_col = next(
        (tc for lbl, (tbl, tc) in ENTITY_MAP.items() if tbl == table),
        "title",
    )
    rows = await pg.fetch(f"SELECT id, project_key, {title_col} AS title FROM {table}")  # nosec B608 # {title_col} resolves from the ENTITY_MAP literal above (line 18-24); {table} is this function's own caller-controlled `table` param, but every call site (line 278) passes a value drawn from that same literal map, never external input (audited 2026-08-17)
    return {str(r["id"]): dict(r) for r in rows}


async def fetch_neo4j_ids(driver: AsyncDriver, label: str) -> set[str]:
    """Return set of id strings for all nodes of a given label in Neo4j."""
    async with driver.session() as session:
        result = await session.run(f"MATCH (n:{label}) RETURN n.id AS id")
        records = await result.data()
    return {r["id"] for r in records if r["id"] is not None}


async def fetch_pg_project_keys(pg: asyncpg.Connection) -> set[str]:
    rows = await pg.fetch("SELECT project_key FROM project_contexts")
    return {r["project_key"] for r in rows}


async def fetch_neo4j_project_keys(driver: AsyncDriver) -> set[str]:
    async with driver.session() as session:
        result = await session.run("MATCH (p:Project) RETURN p.project_key AS pk")
        records = await result.data()
    return {r["pk"] for r in records if r["pk"] is not None}


async def create_missing_entity(
    driver: AsyncDriver,
    label: str,
    row: dict,
) -> None:
    async with driver.session() as session:
        await session.run(
            f"MERGE (n:{label} {{id: $id}}) SET n.project_key = $pk, n.title = $title",
            {"id": str(row["id"]), "pk": row["project_key"], "title": row["title"]},
        )
        if row.get("project_key"):
            await session.run(
                "MATCH (e {id: $eid}) MATCH (p:Project {project_key: $pk}) "
                "MERGE (e)-[:BELONGS_TO]->(p)",
                {"eid": str(row["id"]), "pk": row["project_key"]},
            )


async def delete_orphan_node(driver: AsyncDriver, label: str, node_id: str) -> None:
    async with driver.session() as session:
        await session.run(
            f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n",
            {"id": node_id},
        )


async def create_missing_project(
    driver: AsyncDriver,
    project_key: str,
    name: str | None,
    project_id: str | None,
) -> int:
    """Create a missing Project node and link its existing entity nodes.

    Mirrors init_graph.py's Project shape (project_key + name + id). Then ensures
    a BELONGS_TO edge from every already-synced entity carrying this project_key
    (entity nodes set n.project_key but were never linked because the Project
    node did not exist). Returns the number of BELONGS_TO edges ensured.
    """
    async with driver.session() as session:
        await session.run(
            "MERGE (p:Project {project_key: $pk}) "
            "SET p.name = coalesce($name, p.name, $pk), p.id = coalesce($id, p.id)",
            {"pk": project_key, "name": name, "id": project_id},
        )
        result = await session.run(
            "MATCH (p:Project {project_key: $pk}) "
            "MATCH (e {project_key: $pk}) WHERE NOT e:Project "
            "MERGE (e)-[:BELONGS_TO]->(p) "
            "RETURN count(*) AS linked",
            {"pk": project_key},
        )
        record = await result.single()
        return int(record["linked"]) if record else 0


async def check_supersedes_integrity(pg: asyncpg.Connection, driver: AsyncDriver) -> list[str]:
    """Return list of issues with SUPERSEDES relations."""
    issues: list[str] = []
    # Check PG superseded_by links have corresponding Neo4j SUPERSEDES edges
    rows = await pg.fetch("SELECT id, superseded_by FROM decisions WHERE superseded_by IS NOT NULL")
    for row in rows:
        old_id = str(row["id"])
        new_id = str(row["superseded_by"])
        async with driver.session() as session:
            result = await session.run(
                "MATCH (new:Decision {id: $new_id})-[:SUPERSEDES]->(old:Decision {id: $old_id}) "
                "RETURN count(*) AS cnt",
                {"new_id": new_id, "old_id": old_id},
            )
            record = await result.single()
            if not record or record["cnt"] == 0:
                issues.append(f"Missing SUPERSEDES: ({new_id})-[:SUPERSEDES]->({old_id})")
    return issues


async def check_merged_into_integrity(pg: asyncpg.Connection, driver: AsyncDriver) -> list[str]:
    """Return list of issues with MERGED_INTO relations."""
    issues: list[str] = []
    tables = ["decisions", "learnings", "snippets", "runbooks", "adrs"]
    for table in tables:
        # Skip dangling pointers (merged_into target row deleted from PG): they
        # cannot become a Neo4j edge and are a PG-integrity issue, not drift.
        rows = await pg.fetch(
            f"SELECT id, merged_into FROM {table} m WHERE m.merged_into IS NOT NULL "  # nosec B608 # {table} iterates the `tables` list literal above (line 138), no caller input (audited 2026-08-17)
            f"AND EXISTS (SELECT 1 FROM {table} t WHERE t.id = m.merged_into)"
        )
        for row in rows:
            src = str(row["id"])
            tgt = str(row["merged_into"])
            async with driver.session() as session:
                result = await session.run(
                    "MATCH (src {id: $src})-[:MERGED_INTO]->(tgt {id: $tgt}) "
                    "RETURN count(*) AS cnt",
                    {"src": src, "tgt": tgt},
                )
                record = await result.single()
                if not record or record["cnt"] == 0:
                    issues.append(f"Missing MERGED_INTO [{table}]: ({src})-[:MERGED_INTO]->({tgt})")
    return issues


async def count_dangling_merged_into(pg: asyncpg.Connection) -> dict[str, int]:
    """Return {table: count} of merged_into pointers whose target row no longer
    exists in PG. These cannot become Neo4j edges (target was hard-deleted) — a
    PG-integrity issue, NOT graph drift — so they are reported separately and do
    not count toward fixable drift.
    """
    tables = ["decisions", "learnings", "snippets", "runbooks", "adrs"]
    out: dict[str, int] = {}
    for table in tables:
        n = await pg.fetchval(
            f"SELECT count(*) FROM {table} m WHERE m.merged_into IS NOT NULL "  # nosec B608 # {table} iterates the `tables` list literal above (line 167), no caller input (audited 2026-08-17)
            f"AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.id = m.merged_into)"
        )
        if n:
            out[table] = int(n)
    return out


async def fix_supersedes(pg: asyncpg.Connection, driver: AsyncDriver) -> int:
    rows = await pg.fetch("SELECT id, superseded_by FROM decisions WHERE superseded_by IS NOT NULL")
    fixed = 0
    for row in rows:
        old_id = str(row["id"])
        new_id = str(row["superseded_by"])
        async with driver.session() as session:
            await session.run(
                "MATCH (new:Decision {id: $new_id}) MATCH (old:Decision {id: $old_id}) "
                "MERGE (new)-[:SUPERSEDES]->(old)",
                {"new_id": new_id, "old_id": old_id},
            )
        fixed += 1
    return fixed


async def fix_merged_into(pg: asyncpg.Connection, driver: AsyncDriver) -> int:
    tables = ["decisions", "learnings", "snippets", "runbooks", "adrs"]
    fixed = 0
    for table in tables:
        # Only attempt edges whose target row still exists (skip dangling refs);
        # this also keeps `fixed` an accurate count of edges actually created.
        rows = await pg.fetch(
            f"SELECT id, merged_into FROM {table} m WHERE m.merged_into IS NOT NULL "  # nosec B608 # {table} iterates the `tables` list literal above (line 196), no caller input (audited 2026-08-17)
            f"AND EXISTS (SELECT 1 FROM {table} t WHERE t.id = m.merged_into)"
        )
        for row in rows:
            src = str(row["id"])
            tgt = str(row["merged_into"])
            async with driver.session() as session:
                await session.run(
                    "MATCH (src {id: $src}) MATCH (tgt {id: $tgt}) "
                    "MERGE (src)-[:MERGED_INTO]->(tgt)",
                    {"src": src, "tgt": tgt},
                )
            fixed += 1
    return fixed


async def main() -> int:
    fix_mode = "--fix" in sys.argv
    pg_url = os.getenv("POSTGRES_URL", "postgresql://brain:brain@localhost:5433/brain")
    settings = Settings(postgres_url=pg_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    if fix_mode and settings.graph_ledger_write_enabled:
        print(
            "--fix is disabled while the PostgreSQL graph ledger owns writes; "
            "use the outbox rebuild procedure.",
            file=sys.stderr,
        )
        return 2

    pg_url = pg_url.replace("postgresql+asyncpg://", "postgresql://")
    neo4j_url = os.getenv("NEO4J_URL", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "brain_v42_graph")

    pg = await asyncpg.connect(pg_url)
    driver = AsyncGraphDatabase.driver(neo4j_url, auth=(neo4j_user, neo4j_password))

    total_missing = 0
    total_orphans = 0

    try:
        print("=== PG ↔ Neo4j Consistency Check ===\n")

        # --- Project nodes ---
        pg_keys = await fetch_pg_project_keys(pg)
        neo4j_keys = await fetch_neo4j_project_keys(driver)
        missing_projects = pg_keys - neo4j_keys
        orphan_projects = neo4j_keys - pg_keys

        print(f"Projects — PG: {len(pg_keys)}, Neo4j: {len(neo4j_keys)}")
        if missing_projects:
            print(f"  MISSING in Neo4j ({len(missing_projects)}): {sorted(missing_projects)}")
            total_missing += len(missing_projects)
            if fix_mode:
                linked = 0
                for pk in sorted(missing_projects):
                    meta = await pg.fetchrow(
                        "SELECT id, name FROM project_contexts WHERE project_key = $1", pk
                    )
                    linked += await create_missing_project(
                        driver,
                        pk,
                        meta["name"] if meta else None,
                        str(meta["id"]) if meta else None,
                    )
                print(
                    f"  FIXED: created {len(missing_projects)} Project nodes "
                    f"(+{linked} BELONGS_TO edges)"
                )
        if orphan_projects:
            print(f"  ORPHANS in Neo4j ({len(orphan_projects)}): {sorted(orphan_projects)}")
            total_orphans += len(orphan_projects)
        if not missing_projects and not orphan_projects:
            print("  OK")

        # --- Entity nodes ---
        for label, (table, _title_col) in ENTITY_MAP.items():
            pg_entities = await fetch_pg_ids(pg, table)
            neo4j_ids = await fetch_neo4j_ids(driver, label)

            missing = set(pg_entities.keys()) - neo4j_ids
            orphans = neo4j_ids - set(pg_entities.keys())

            print(f"\n{label} ({table}) — PG: {len(pg_entities)}, Neo4j: {len(neo4j_ids)}")

            if missing:
                print(f"  MISSING in Neo4j ({len(missing)}):")
                for mid in sorted(missing):
                    row = pg_entities[mid]
                    print(f"    {mid} — {row.get('title') or row.get('topic', '')[:60]}")
                total_missing += len(missing)

                if fix_mode:
                    for mid in missing:
                        row = pg_entities[mid]
                        # Normalise: ensure 'title' key exists
                        row = dict(row)
                        if "topic" in row and "title" not in row:
                            row["title"] = row["topic"]
                        await create_missing_entity(driver, label, row)
                    print(f"  FIXED: created {len(missing)} missing {label} nodes")

            if orphans:
                print(f"  ORPHANS in Neo4j ({len(orphans)}):")
                for oid in sorted(orphans):
                    print(f"    {oid}")
                total_orphans += len(orphans)

                if fix_mode:
                    for oid in orphans:
                        await delete_orphan_node(driver, label, oid)
                    print(f"  FIXED: deleted {len(orphans)} orphan {label} nodes")

            if not missing and not orphans:
                print("  OK")

        # --- Relation integrity ---
        print("\n--- Relation Integrity ---")

        supersedes_issues = await check_supersedes_integrity(pg, driver)
        if supersedes_issues:
            print(f"SUPERSEDES issues ({len(supersedes_issues)}):")
            for issue in supersedes_issues:
                print(f"  {issue}")
            if fix_mode:
                fixed = await fix_supersedes(pg, driver)
                print(f"  FIXED: re-created {fixed} SUPERSEDES relations")
        else:
            print("SUPERSEDES: OK")

        merged_issues = await check_merged_into_integrity(pg, driver)
        if merged_issues:
            print(f"MERGED_INTO issues ({len(merged_issues)}):")
            for issue in merged_issues:
                print(f"  {issue}")
            if fix_mode:
                fixed = await fix_merged_into(pg, driver)
                print(f"  FIXED: re-created {fixed} MERGED_INTO relations")
        else:
            print("MERGED_INTO: OK")

        dangling = await count_dangling_merged_into(pg)
        if dangling:
            print(
                f"MERGED_INTO: {sum(dangling.values())} dangling PG ref(s) skipped "
                f"(merged_into target row deleted — PG-integrity, not graph drift): {dangling}"
            )

        # --- Summary ---
        print("\n=== Summary ===")
        print(f"Missing nodes (in PG, not Neo4j): {total_missing}")
        print(f"Orphan nodes  (in Neo4j, not PG): {total_orphans}")
        total_rel_issues = len(supersedes_issues) + len(merged_issues)
        print(f"Relation issues: {total_rel_issues}")

        if total_missing + total_orphans + total_rel_issues == 0:
            print("\nAll checks PASSED — PG and Neo4j are in sync.")
        elif fix_mode:
            print("\nFix mode applied — re-run without --fix to verify.")
        else:
            print(
                f"\nDrift detected. Run with --fix to repair "
                f"({total_missing} missing, {total_orphans} orphans, "
                f"{total_rel_issues} relation issues)."
            )
            sys.exit(1)

    finally:
        await pg.close()
        await driver.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
