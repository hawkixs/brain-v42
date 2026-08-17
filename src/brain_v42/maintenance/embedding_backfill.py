"""CLI for bounded recovery of rows with missing semantic embeddings.

Usage:
    python -m brain_v42.maintenance.embedding_backfill
    python -m brain_v42.maintenance.embedding_backfill --execute --entity-type decision
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

from brain_v42.config import Settings, get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.db.neo4j import create_neo4j_driver
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.services.auto_linker import AutoLinker
from brain_v42.services.cluster_guard import ClusterGuard
from brain_v42.services.durable_graph_service import build_durable_graph_stack
from brain_v42.services.embedding_backfill import (
    ALL_EMBEDDING_ENTITY_TYPES,
    EmbeddingBackfillJob,
    EmbeddingBacklogRepository,
    persist_backfill_metrics,
)
from brain_v42.services.embedding_text import EmbeddingEntityType
from brain_v42.services.feature_linker import FeatureLinker
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.graph_service import GraphService
from brain_v42.services.reranker_client import RerankerClient
from brain_v42.services.status_engine import StatusEngine


@dataclass(slots=True)
class LinkerDependencies:
    feature_linker: FeatureLinker
    auto_linker: AutoLinker | None
    reranker: RerankerClient
    neo4j_driver: Any | None
    graph_ledger: Any | None

    async def close(self) -> None:
        await self.reranker.close()
        if self.neo4j_driver is not None:
            await self.neo4j_driver.close()


def build_linkers(
    settings: Settings,
    session_factory: Any,
    embedding_svc: GPUEmbeddingService,
) -> LinkerDependencies:
    """Build the same semantic linkers used by the MCP creation services."""
    reranker = RerankerClient(
        base_url=settings.reranker_url,
        timeout=settings.reranker_timeout,
    )
    cluster_guard = ClusterGuard(
        session_factory=session_factory,
        embedding_svc=embedding_svc,
        reranker=reranker,
        status_engine=StatusEngine(),
    )
    feature_linker = FeatureLinker(
        session_factory=session_factory,
        cluster_guard=cluster_guard,
    )
    neo4j_driver = create_neo4j_driver(
        url=settings.neo4j_url,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        enabled=settings.graph_enabled,
    )
    auto_linker = None
    graph_ledger = None
    if neo4j_driver is not None:
        graph = GraphService(neo4j_driver, timeout=settings.neo4j_timeout)
        durable_stack = build_durable_graph_stack(
            graph,
            session_factory,
            settings,
            neo4j_driver=neo4j_driver,
        )
        auto_linker = AutoLinker(session_factory=session_factory, graph=durable_stack.service)
        graph_ledger = durable_stack.ledger
    return LinkerDependencies(feature_linker, auto_linker, reranker, neo4j_driver, graph_ledger)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _batch_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must be at most 100")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write embeddings. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--entity-type",
        dest="entity_types",
        action="append",
        choices=ALL_EMBEDDING_ENTITY_TYPES,
        help="Entity type to process; repeat to select several. Defaults to all.",
    )
    parser.add_argument("--batch-size", type=_batch_size, default=20)
    parser.add_argument("--limit", type=_positive_int, default=200)
    parser.add_argument("--project-key", type=canonicalize_project_key, default=None)
    return parser.parse_args(argv)


async def run_from_args(args: argparse.Namespace) -> int:
    settings = get_settings()
    session_factory = get_session_factory()
    embedding_svc = GPUEmbeddingService(base_url=settings.embedding_service_url)
    repos: dict[EmbeddingEntityType, EmbeddingBacklogRepository] = {
        "decision": PgDecisionRepo(session_factory),
        "learning": PgLearningRepo(session_factory),
        "snippet": PgSnippetRepo(session_factory),
        "runbook": PgRunbookRepo(session_factory),
        "adr": PgADRRepo(session_factory),
    }
    linkers: LinkerDependencies | None = None
    try:
        if args.execute:
            linkers = build_linkers(settings, session_factory, embedding_svc)
            if linkers.graph_ledger is not None:
                await linkers.graph_ledger.assert_schema_ready()
        job = EmbeddingBackfillJob(
            session_factory=session_factory,
            repos=repos,
            embedding_svc=embedding_svc,
            feature_linker=linkers.feature_linker if linkers else None,
            auto_linker=linkers.auto_linker if linkers else None,
        )
        report = await job.run(
            entity_types=args.entity_types,
            batch_size=args.batch_size,
            limit=args.limit,
            project_key=args.project_key,
            dry_run=not args.execute,
        )
        if args.execute:
            report.metrics_persisted = await persist_backfill_metrics(session_factory, report)
        print(json.dumps(asdict(report), default=str, sort_keys=True))
        return 1 if report.has_failures else 0
    finally:
        if linkers is not None:
            await linkers.close()
        await embedding_svc.close()
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_from_args(parse_args(argv)))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"embedding backfill failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
