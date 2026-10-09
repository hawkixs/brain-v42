"""Requeue a restored projection before the deployment chain starts MCP writers.

The deployment chain must keep writers stopped and use a dedicated Neo4j database.
Success records recovery preparation, not completion of the projector's replay.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import UTC, datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
import structlog

from brain_v42.config import get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.db.neo4j import close_neo4j_driver, create_neo4j_driver
from brain_v42.maintenance.graph_projection_recovery import recover_projection_lineage
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo
from brain_v42.services.graph_projection_schema import ensure_graph_projection_schema
from brain_v42.services.neo4j_graph_projection_writer import Neo4jGraphProjectionWriter

logger = structlog.get_logger(__name__)

# Changing this namespace would invalidate both completion and crash-resume identities.
BOOTSTRAP_NAMESPACE = UUID("863be3b7-a9df-5dd1-b033-d72cdb589dd3")
_RESTORE_MARKER = re.compile(r"brain-v42-restore sha256=([0-9a-f]{64}) at=(\S+)")


def _restore_sha256(marker: str | None) -> str | None:
    """Refuse unrelated or malformed database comments before touching the projection."""
    match = _RESTORE_MARKER.fullmatch(marker or "")
    if match is None:
        return None
    try:
        restored_at = datetime.fromisoformat(match[2])
    except ValueError:
        return None
    if restored_at.tzinfo is None or restored_at.utcoffset() != UTC.utcoffset(restored_at):
        return None
    return match[1]


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
        "--lease-seconds",
        type=_lease_seconds,
        default=600,
        help="Recovery lease duration (60..86400, default: 600).",
    )
    return parser.parse_args(argv)


async def run_from_args(args: argparse.Namespace) -> int:
    driver = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            marker = await session.scalar(
                sa.text(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = current_database()"
                )
            )
            dump_sha256 = _restore_sha256(marker)
            if dump_sha256 is None:
                logger.error("no_restore_marker")
                return 2
            recovery_id = uuid5(BOOTSTRAP_NAMESPACE, dump_sha256)
            # Completion must precede any lease attempt, including an old MCP's lease.
            completed_id = await session.scalar(
                sa.text(
                    "SELECT last_completed_recovery_id FROM graph_projection_leases "
                    "WHERE slot = 'neo4j' AND protocol_version = 2"
                )
            )
        if completed_id is not None and UUID(str(completed_id)) == recovery_id:
            logger.info("graph_bootstrap_already_completed")
            return 0

        settings = get_settings()
        if not settings.graph_projector_enabled:
            raise RuntimeError("bootstrap requires the private graph projector role")
        repo = PgGraphLedgerRepo(factory)
        await repo.assert_schema_ready()
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
        worker_id = "graph-bootstrap-" + str(uuid5(BOOTSTRAP_NAMESPACE, str(recovery_id)))
        await recover_projection_lineage(
            repo,
            Neo4jGraphProjectionWriter(driver, timeout=settings.neo4j_timeout),
            recovery_id=recovery_id,
            worker_id=worker_id,
            lease_seconds=args.lease_seconds,
        )
        logger.info("graph_bootstrap_recovered")
        return 0
    finally:
        try:
            await close_neo4j_driver(driver)
        finally:
            await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_from_args(parse_args(argv)))
    except Exception as exc:  # noqa: BLE001 - do not expose credential-bearing errors
        logger.error("graph_bootstrap_failed", error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
