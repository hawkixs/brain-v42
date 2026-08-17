#!/usr/bin/env python3
"""Inspect or resume one crash-safe PostgreSQL-to-Neo4j projection recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from uuid import UUID

from brain_v42.config import get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.db.neo4j import close_neo4j_driver, create_neo4j_driver
from brain_v42.maintenance.graph_projection_recovery import recover_projection_lineage
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo
from brain_v42.services.graph_projection_schema import ensure_graph_projection_schema
from brain_v42.services.neo4j_graph_projection_writer import Neo4jGraphProjectionWriter


def _recovery_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a complete UUID") from exc


def _lease_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 60 <= parsed <= 86_400:
        raise argparse.ArgumentTypeError("must be between 60 and 86400 seconds")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Resume the exact recovery ID until both stores finalize.",
    )
    parser.add_argument(
        "--recovery-id",
        type=_recovery_id,
        help="Operator-generated UUID; reuse this exact ID after every crash.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=_lease_seconds,
        default=3600,
        help="Recovery lease duration (60..86400, default: 3600).",
    )
    parser.add_argument("--writers-off-confirmed", action="store_true")
    parser.add_argument("--legacy-credential-revoked-confirmed", action="store_true")
    parser.add_argument("--neo4j-sessions-zero-confirmed", action="store_true")
    parser.add_argument("--neo4j-dedicated-confirmed", action="store_true")
    parser.add_argument("--postgres-restore-tested", action="store_true")
    args = parser.parse_args(argv)

    confirmations = {
        "--writers-off-confirmed": args.writers_off_confirmed,
        "--legacy-credential-revoked-confirmed": args.legacy_credential_revoked_confirmed,
        "--neo4j-sessions-zero-confirmed": args.neo4j_sessions_zero_confirmed,
        "--neo4j-dedicated-confirmed": args.neo4j_dedicated_confirmed,
        "--postgres-restore-tested": args.postgres_restore_tested,
    }
    if args.apply:
        missing = [name for name, confirmed in confirmations.items() if not confirmed]
        if args.recovery_id is None:
            missing.insert(0, "--recovery-id")
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
    elif args.recovery_id is not None or any(confirmations.values()):
        parser.error("recovery ID and offline attestations are only valid with --apply")
    return args


async def run_from_args(args: argparse.Namespace) -> int:
    repo = PgGraphLedgerRepo(get_session_factory())
    driver = None
    try:
        await repo.assert_schema_ready()
        if not args.apply:
            inventory = await repo.projection_inventory()
            print(json.dumps({"mode": "dry-run", "inventory": asdict(inventory)}, sort_keys=True))
            return 0

        settings = get_settings()
        if not settings.graph_projector_enabled:
            raise RuntimeError("recovery requires the private graph projector role")
        driver = create_neo4j_driver(
            settings.graph_projector_neo4j_url,
            user=settings.graph_projector_neo4j_user,
            password=settings.graph_projector_neo4j_password.get_secret_value(),
            enabled=True,
        )
        if driver is None:
            raise RuntimeError("private Neo4j projector connection is unavailable")
        await driver.verify_connectivity()
        await ensure_graph_projection_schema(driver)
        report = await recover_projection_lineage(
            repo,
            Neo4jGraphProjectionWriter(driver, timeout=settings.neo4j_timeout),
            recovery_id=args.recovery_id,
            worker_id=f"recovery:{args.recovery_id}",
            lease_seconds=args.lease_seconds,
        )
        print(
            json.dumps({"mode": "apply", "recovery": asdict(report)}, default=str, sort_keys=True)
        )
        return 0
    finally:
        await close_neo4j_driver(driver)
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_from_args(parse_args(argv)))
    except Exception as exc:  # noqa: BLE001 - secret-safe CLI boundary
        print(f"graph projection recovery failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
