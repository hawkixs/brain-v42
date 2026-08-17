#!/usr/bin/env python3
"""Preview PostgreSQL-to-Neo4j graph inventory without mutating either store.

The legacy apply path is retired because it cannot atomically fence PostgreSQL
and Neo4j. Use ``recover_graph_projection.py`` for every projection recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Retired; use recover_graph_projection.py.",
    )
    parser.add_argument(
        "--neo4j-cleared-confirmed",
        action="store_true",
        help="Confirm that Neo4j business nodes/cursors were cleared but its fence was kept.",
    )
    parser.add_argument(
        "--writers-off-confirmed",
        action="store_true",
        help="Confirm that graph writers are stopped for the rebuild cutover.",
    )
    args = parser.parse_args(argv)
    if args.apply:
        parser.error("--apply is retired; use scripts/recover_graph_projection.py")
    if not args.apply and (args.neo4j_cleared_confirmed or args.writers_off_confirmed):
        parser.error("cutover confirmations are only valid with --apply")
    return args


async def run_from_args(args: argparse.Namespace) -> int:
    if args.apply:
        raise RuntimeError("mutating rebuild is retired; use scripts/recover_graph_projection.py")
    repo = PgGraphLedgerRepo(get_session_factory())
    try:
        await repo.assert_schema_ready()
        inventory = await repo.projection_inventory()
        output: dict[str, object] = {
            "mode": "dry-run",
            "inventory": asdict(inventory),
        }
        print(json.dumps(output, sort_keys=True))
        return 0
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_from_args(parse_args(argv)))
    except Exception as exc:  # noqa: BLE001 - bounded CLI boundary
        print(f"graph projection rebuild failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
