#!/usr/bin/env python3
"""Backfill PostgreSQL's graph ledger from a bounded Neo4j snapshot.

Dry-run is the default. ``--apply --writers-off-confirmed`` is required before
PostgreSQL is opened; all graph writers and the outbox projector must stay off
for the complete snapshot/import cutover window.
Only facts newly inserted from Neo4j receive already-delivered initial outbox
events. Existing PostgreSQL facts keep their pending events so projection can
converge potentially stale properties safely.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_v42.config import Settings
from brain_v42.db.neo4j import create_neo4j_driver
from brain_v42.services.legacy_graph_import import (
    LegacyGraphImportBlocked,
    LegacyGraphImporter,
    LegacyGraphImportReport,
)
from brain_v42.services.legacy_graph_snapshot import LegacyGraphSnapshotReader


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _batch_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 1_000:
        raise argparse.ArgumentTypeError("must be at most 1000")
    return parsed


def _timeout(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or parsed > 300:
        raise argparse.ArgumentTypeError("must be between 0 and 300 seconds")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_graph_ledger",
        description="Import a bounded legacy Neo4j snapshot into PostgreSQL's graph ledger.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to PostgreSQL (default: inspect only).",
    )
    parser.add_argument(
        "--writers-off-confirmed",
        action="store_true",
        help="Confirm graph writers/projector are stopped for the full cutover.",
    )
    parser.add_argument(
        "--allow-skips",
        action="store_true",
        help="Commit supported facts even when invalid/unresolved facts are reported.",
    )
    parser.add_argument("--max-nodes", type=_positive_int, default=20_000)
    parser.add_argument("--max-relations", type=_positive_int, default=100_000)
    parser.add_argument("--batch-size", type=_batch_size, default=500)
    parser.add_argument("--timeout", type=_timeout, default=30.0)
    return parser.parse_args(argv)


def _print_report(report: LegacyGraphImportReport) -> None:
    mode = "APPLY" if report.applied else "DRY-RUN"
    projects = ",".join(report.canonical_project_keys) or "-"
    print(
        f"[{mode}] entities={report.candidate_entities} "
        f"relations={report.candidate_relations} "
        f"projects={projects} "
        f"inserted_entities={report.imported_entities} "
        f"inserted_relations={report.imported_relations} "
        f"ack_entities={report.acknowledged_entities} "
        f"ack_relations={report.acknowledged_relations} "
        f"skipped_nodes={report.skipped_nodes} "
        f"skipped_relations={report.skipped_relations} "
        f"truncated_nodes={report.truncated_nodes} "
        f"truncated_relations={report.truncated_relations}"
    )


async def run(args: argparse.Namespace) -> int:
    if args.apply and not args.writers_off_confirmed:
        print(
            "Apply refusé: confirmation writers-off requise pour tout le cutover.",
            file=sys.stderr,
        )
        return 2
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        print("Configuration invalide; vérifiez les variables Brain.", file=sys.stderr)
        return 2

    try:
        driver = create_neo4j_driver(
            settings.neo4j_url,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            enabled=True,
        )
    except Exception as exc:
        print(f"Échec connexion Neo4j: {type(exc).__name__}", file=sys.stderr)
        return 2
    if driver is None:
        print("Neo4j indisponible; vérifiez sa configuration.", file=sys.stderr)
        return 2

    engine = None
    try:
        snapshot = await LegacyGraphSnapshotReader(
            driver,
            timeout=args.timeout,
        ).read(args.max_nodes, args.max_relations)
        if args.apply:
            engine = create_async_engine(settings.postgres_url, pool_pre_ping=True)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
        else:
            session_factory = async_sessionmaker(expire_on_commit=False)
        importer = LegacyGraphImporter(session_factory, batch_size=args.batch_size)
        report = await importer.import_snapshot(
            snapshot,
            apply=args.apply,
            allow_skips=args.allow_skips,
        )
        _print_report(report)
        incomplete = (
            report.skipped_nodes
            or report.skipped_relations
            or report.truncated_nodes
            or report.truncated_relations
        )
        return 1 if incomplete else 0
    except LegacyGraphImportBlocked as exc:
        print(f"Import refusé: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Échec import: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        await driver.close()
        if engine is not None:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
