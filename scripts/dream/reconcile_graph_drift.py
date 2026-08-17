#!/usr/bin/env python3
"""Reconcile PG ↔ Neo4j drift for lightweight entity nodes.

The PROMOTE / write-through pipeline is best-effort: Neo4j errors are
swallowed so PG stays authoritative. Over time that means the graph can
drift — either we have PG entities without a matching Neo4j node
(silent write-through failure) or orphan Neo4j nodes whose PG row got
hard-deleted. Neither breaks correctness, but both degrade brain_search
enrichment and graph inventory accuracy.

This script diffs the two sides and reports the drift per entity type.
In ``--fix`` mode it repairs both directions:
  * missing-in-neo4j → ``graph.upsert_node(label, id, title)``
  * missing-in-pg  → ``graph.delete_node(label, id)``

Usage:
    python -m scripts.dream.reconcile_graph_drift            # dry run
    python -m scripts.dream.reconcile_graph_drift --fix      # repair
    python -m scripts.dream.reconcile_graph_drift --limit 50 # cap repairs

Scope: all projects, all entity types with embeddings (decisions,
learnings, snippets, runbooks, adrs).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.config import Settings
from brain_v42.db.neo4j import create_neo4j_driver
from brain_v42.services.graph_service import GraphService

# PG table ↔ Neo4j label mapping (matches metrics/collector._PG_LABEL_MAP).
_PG_LABEL_MAP: dict[str, str] = {
    "decisions": "Decision",
    "learnings": "Learning",
    "snippets": "Snippet",
    "runbooks": "Runbook",
    "adrs": "ADR",
}

_TITLE_COLUMN: dict[str, str] = {
    # topic is the "title" of a learning — everything else uses `title`.
    "learnings": "topic",
}


def diff_sides(pg_ids: set[UUID], graph_ids: set[UUID]) -> tuple[set[UUID], set[UUID]]:
    """Return (missing_in_graph, missing_in_pg) sets.

    Pure helper so the reconciliation logic is testable without a DB.
    """
    return pg_ids - graph_ids, graph_ids - pg_ids


async def _pg_entities(
    session_factory: async_sessionmaker, table: str
) -> dict[UUID, tuple[str, str | None]]:
    """Fetch (id → (title, project_key)) for every PG entity with an embedding."""
    title_col = _TITLE_COLUMN.get(table, "title")
    query = sa.text(
        f"SELECT id, {title_col} AS title, project_key "  # noqa: S608
        f"FROM {table} WHERE embedding IS NOT NULL"
    )
    async with session_factory() as session:
        rows = (await session.execute(query)).all()
    return {UUID(str(r.id)): (r.title or "", r.project_key) for r in rows}


async def _graph_entities(graph: GraphService, label: str) -> set[UUID]:
    """Fetch all node UUIDs for a given Neo4j label."""
    query = f"MATCH (n:{label}) RETURN n.id AS id"
    rows = await graph._run_read(query, {})  # noqa: SLF001
    ids: set[UUID] = set()
    for row in rows:
        try:
            ids.add(UUID(row["id"]))
        except (ValueError, TypeError, KeyError):
            continue
    return ids


async def reconcile_type(
    table: str,
    label: str,
    session_factory: async_sessionmaker,
    graph: GraphService,
    fix: bool,
    limit: int,
) -> dict[str, Any]:
    """Reconcile one entity type. Returns a summary dict."""
    pg_map = await _pg_entities(session_factory, table)
    graph_ids = await _graph_entities(graph, label)
    pg_ids = set(pg_map.keys())
    missing_in_graph, missing_in_pg = diff_sides(pg_ids, graph_ids)

    repaired_add = 0
    repaired_del = 0
    if fix:
        for eid in list(missing_in_graph)[:limit]:
            title, project_key = pg_map[eid]
            props: dict[str, str] = {"title": title}
            if project_key:
                props["project_key"] = project_key
            await graph.upsert_node(label, eid, props)
            repaired_add += 1
        for eid in list(missing_in_pg)[:limit]:
            await graph.delete_node(label, eid)
            repaired_del += 1

    return {
        "table": table,
        "label": label,
        "pg_count": len(pg_ids),
        "graph_count": len(graph_ids),
        "missing_in_graph": len(missing_in_graph),
        "missing_in_pg": len(missing_in_pg),
        "repaired_add": repaired_add,
        "repaired_del": repaired_del,
    }


def _format_line(r: dict[str, Any]) -> str:
    marker = "✗" if r["missing_in_graph"] or r["missing_in_pg"] else "✓"
    repaired = ""
    if r["repaired_add"] or r["repaired_del"]:
        repaired = f"  fixed:+{r['repaired_add']}/-{r['repaired_del']}"
    return (
        f"{marker} {r['label']:<9} pg={r['pg_count']:>4}  graph={r['graph_count']:>4}  "
        f"pg→graph={r['missing_in_graph']:>3}  graph→pg={r['missing_in_pg']:>3}"
        f"{repaired}"
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile PG ↔ Neo4j drift for brain_v42 lightweight nodes."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Repair drift. Default is dry-run (report only).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max number of upserts/deletes per direction per type (default 100).",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.fix and settings.graph_ledger_write_enabled:
        print(
            "✗ --fix is disabled while the PostgreSQL graph ledger owns writes; "
            "use the outbox rebuild procedure.",
            file=sys.stderr,
        )
        return 2
    engine = create_async_engine(settings.postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    driver = create_neo4j_driver(
        url=settings.neo4j_url,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        enabled=True,
    )
    if driver is None:
        print("✗ Neo4j driver unavailable — check NEO4J_URL/USER/PASSWORD.", file=sys.stderr)
        await engine.dispose()
        return 2
    graph = GraphService(driver, timeout=settings.neo4j_timeout)

    mode = "FIX" if args.fix else "DRY-RUN"
    print(f"Graph drift reconciliation ({mode}, limit={args.limit})")
    print("─" * 78)

    totals = {"missing_in_graph": 0, "missing_in_pg": 0, "repaired": 0}
    try:
        for table, label in _PG_LABEL_MAP.items():
            summary = await reconcile_type(
                table, label, session_factory, graph, fix=args.fix, limit=args.limit
            )
            print(_format_line(summary))
            totals["missing_in_graph"] += summary["missing_in_graph"]
            totals["missing_in_pg"] += summary["missing_in_pg"]
            totals["repaired"] += summary["repaired_add"] + summary["repaired_del"]
    finally:
        await driver.close()
        await engine.dispose()

    print("─" * 78)
    print(
        f"TOTAL: pg→graph={totals['missing_in_graph']}  "
        f"graph→pg={totals['missing_in_pg']}  repaired={totals['repaired']}"
    )
    if totals["missing_in_graph"] + totals["missing_in_pg"] == 0:
        print("✓ No drift detected.")
        return 0
    if not args.fix:
        print("→ Re-run with --fix to repair.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
