"""FastMCP server for brain_v42 (stdio and http transports).

Entry point: python -m brain_v42.mcp.server
Transport: controlled by BRAIN_MCP_TRANSPORT env var (default: stdio)

Initialization sequence on startup:
1. get_session_factory() — shared SQLAlchemy async_sessionmaker singleton
2. GPUEmbeddingService — connects to GPU embedding service at localhost:8003
3. All domain repos (PgDecisionRepo, PgLearningRepo, etc.) — injected with session_factory
4. All domain services (DecisionService, LearningService, etc.) — injected with repo + embedding_svc
5. BrainService — fans out semantic search across all domain services
6. Registration roots expose 48 always-on + 2 graph-gated = 50 brain_* tools

Shutdown discipline (prevents zombie children when parent Claude Code exits abruptly):
- prctl(PR_SET_PDEATHSIG, SIGTERM) — kernel signals child on parent death (Linux only)
- asyncio signal handlers for SIGTERM/SIGINT — gracefully unblock the main loop (stdio only)
- app_lifecycle context manager owns flushers/neo4j close + dispose_engine() for BOTH transports
"""

from __future__ import annotations

import asyncio
import ctypes
import inspect
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, NamedTuple
from weakref import WeakSet

import structlog
from fastmcp import FastMCP
from sqlalchemy import text
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from brain_v42.config import Settings, get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.db.neo4j import close_neo4j_driver, create_neo4j_driver
from brain_v42.mcp.business_errors import surface_business_errors
from brain_v42.mcp.dream_capabilities import (
    DreamCapabilityConfigurationError,
    DreamCapabilityMiddleware,
    DreamCapabilityTokenVerifier,
    parse_dream_capability_registry,
)
from brain_v42.mcp.dream_project_authorization import (
    DreamProjectReferenceResolver,
    PostgresDreamProjectResolver,
)
from brain_v42.mcp.http_security import BearerTokenGuard, HostOriginGuard
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.metrics.tool_instrumentation import instrument_registered_tools
from brain_v42.release import package_version, shipped_alembic_head
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.adr_service import ADRService
from brain_v42.services.auto_linker import AutoLinker
from brain_v42.services.brain_service import BrainService
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.durable_graph_service import build_durable_graph_stack
from brain_v42.services.embedding_factory import (
    build_embedding_service,
    build_reranker_client,
)
from brain_v42.services.feature_creation_service import FeatureCreationService
from brain_v42.services.feature_linker import FeatureLinker
from brain_v42.services.graph_projection_schema import ensure_graph_projection_schema
from brain_v42.services.graph_service import GraphService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.project_context_service import ProjectContextService
from brain_v42.services.roadmap_service import RoadmapService
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService
from brain_v42.services.ticket_service import TicketService
from brain_v42.tracing import init_tracing, shutdown_tracing

logger = structlog.get_logger(__name__)

_http_security_configured_servers: WeakSet[FastMCP] = WeakSet()


def _select_usage_access_logger(settings: Settings, services: dict[str, Any]) -> Any | None:
    """Return the one access logger shared by decay-aware read tool paths."""
    if not settings.decay_enabled:
        return None
    return services["access_logger"]


def _configure_stdio_logging() -> None:
    """Route all logs to stderr. stdout is reserved for MCP JSON-RPC.

    Without this, structlog's default PrintLoggerFactory writes to stdout,
    corrupting the MCP protocol stream and causing the client to silently
    drop the connection (no tools registered). Must be called before any
    log is emitted, and only from the stdio entry point (not on import —
    pytest's caplog handlers must stay intact).
    """
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)


_PR_SET_PDEATHSIG = 1


def _apply_http_server_arg() -> None:
    """Force BRAIN_MCP_TRANSPORT=http if --http-server is in sys.argv.

    Must be called BEFORE get_settings() is first invoked (settings are cached via
    lru_cache). The literal --http-server token intentionally stays in sys.argv so
    the reaper sentinel (Task 4.1) can detect HTTP-mode processes by cmdline scan.
    """
    if "--http-server" in sys.argv:
        for key in tuple(os.environ):
            if key.casefold() == "brain_mcp_transport":
                os.environ.pop(key)
        os.environ["BRAIN_MCP_TRANSPORT"] = "http"


def _setup_parent_death_signal() -> None:
    """Ask the kernel to send SIGTERM to this process when its parent dies (Linux only).

    Root-cause defense for the zombie-leak: Claude Code does not reliably SIGTERM stdio
    MCP children on session end, and stdin EOF was insufficient to unblock all asyncio
    paths. PR_SET_PDEATHSIG guarantees delivery from the kernel itself.
    """
    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except OSError:
        logger.warning("brain_v42.server.pdeathsig_failed", exc_info=True)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event
) -> None:
    """Install SIGTERM/SIGINT handlers that set ``shutdown_event`` to unblock the main loop.

    Without these, SIGTERM (sent by kernel via PDEATHSIG or by the user) terminates
    the process abruptly, skipping the cleanup ``finally`` block — leaking asyncpg
    connections and Neo4j sessions.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass


@asynccontextmanager
async def app_lifecycle(
    settings: Settings,
    services: dict[str, Any],
    metrics_collector: Any,
) -> AsyncIterator[None]:
    """Start brain background tasks on enter; stop them + dispose engine/neo4j on exit.

    Sole owner of the flusher/engine/neo4j lifecycle for BOTH stdio and http
    transports. Using an explicit ``@asynccontextmanager`` (rather than
    ``FastMCP(lifespan=...)``) is intentional: ``mcp`` is constructed at module
    import long before ``services`` / ``metrics_collector`` exist, so we cannot
    pass them into a FastMCP lifespan callback.
    """
    graph_outbox_projector = services.get("graph_outbox_projector")
    graph_ledger_repo = services.get("graph_ledger_repo")
    access_logger = services["access_logger"]

    # Tracing OTel — armé ICI et non dans `_run_mcp`, qui est appelé par six
    # tests unitaires : ils poseraient un vrai provider et un vrai exporter.
    # `init_tracing` ne lève jamais et rend False quand l'extra est absent,
    # ce qui est l'état NORMAL d'une installation qui ne trace pas.
    tracing_armed = False
    if settings.otel_tracing_enabled:
        tracing_armed = init_tracing(settings.otel_endpoint)
        logger.info(
            "brain_v42.server.tracing", armed=tracing_armed, endpoint=settings.otel_endpoint
        )

    async def _background_plan_index() -> None:
        plan_indexer = services["plan_indexer"]
        try:
            results = await plan_indexer.index_all_projects()
            if results:
                logger.info(
                    "brain_v42.server.plan_index_done",
                    projects=list(results.keys()),
                )
        except Exception:
            logger.warning(
                "brain_v42.server.plan_index_failed",
                exc_info=True,
            )

    async def _cancel_task(task: asyncio.Task[None]) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Register cleanup before each potentially partial start. AsyncExitStack
    # then unwinds every earlier resource even when startup fails before yield.
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(dispose_engine)
        cleanup.push_async_callback(close_neo4j_driver, services["neo4j_driver"])

        if tracing_armed:
            # `shutdown_on_exit=False` a désarmé l'atexit du SDK pour qu'un
            # collecteur injoignable ne fasse pas traîner l'arrêt ; c'est donc
            # à nous de vider la file, dans un délai borné. Sans ce callback,
            # les spans en attente disparaissaient sans un mot — trou trouvé
            # par l'e2e du 2026-08-12, pas par relecture.
            cleanup.push_async_callback(asyncio.to_thread, shutdown_tracing, 3000)

        if graph_outbox_projector is not None:
            if graph_ledger_repo is None:
                raise RuntimeError("graph projector requires the PostgreSQL graph ledger")
            await graph_ledger_repo.assert_schema_ready()
            await ensure_graph_projection_schema(services["neo4j_driver"])
            cleanup.push_async_callback(graph_outbox_projector.stop)
            await graph_outbox_projector.start()

        if settings.metrics_enabled:
            from brain_v42.metrics.flusher import MetricsFlusher  # noqa: PLC0415
            from brain_v42.metrics.timeseries_flusher import (  # noqa: PLC0415
                TimeseriesFlusher,
            )

            session_factory = get_session_factory()
            metrics_flusher = MetricsFlusher(
                collector=metrics_collector,
                session_factory=session_factory,
            )
            cleanup.push_async_callback(metrics_flusher.stop)
            await metrics_flusher.start()
            timeseries_flusher = TimeseriesFlusher(
                collector=metrics_collector,
                session_factory=session_factory,
            )
            cleanup.push_async_callback(timeseries_flusher.stop)
            await timeseries_flusher.start()

        if settings.decay_enabled:
            from brain_v42.services.decay_flusher import DecayFlusher  # noqa: PLC0415

            cleanup.push_async_callback(access_logger.stop)
            await access_logger.start()
            decay_flusher = DecayFlusher(
                session_factory=get_session_factory(),
                access_log_repo=services["access_log_repo"],
                decay_calculator=services["decay_calculator"],
                interval_seconds=settings.decay_flush_interval_seconds,
                collector=metrics_collector if settings.metrics_enabled else None,
                human_signal_enabled=settings.decay_human_signal_enabled,
            )
            cleanup.push_async_callback(decay_flusher.stop)
            await decay_flusher.start()

        # Keep a strong reference so the GC cannot collect the task mid-flight.
        plan_index_task = asyncio.create_task(_background_plan_index())
        cleanup.push_async_callback(_cancel_task, plan_index_task)
        yield


# Module-level FastMCP instance
mcp = FastMCP("brain", mask_error_details=True)
# Provenance : posé ici et non dans register_tools, pour être indépendant de
# l'activation des métriques et de l'ordre d'enregistrement des tools.
# `apply_tool_catalog_profile` et `maybe_apply_code_mode` retournent le MÊME
# objet, donc ce middleware survit aux deux.
mcp.add_middleware(ProvenanceMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe for systemd watchdog and red-monitor.

    Executes a bounded SELECT 1 against the connection pool.  A wedged or
    saturated pool returns 503 fast (asyncio.timeout(2)) so the watchdog
    never blocks waiting on a stuck server.

    Also names the build that answers: `version` is the installed
    distribution, `alembic_head` the schema revision shipped WITH it.  Both
    are measured (see `brain_v42.release`) and memoised, never read from the
    database and never a literal -- a probe on a 10 s watchdog budget whose
    failure restarts this server pays no disk access per request.  They are
    reported on the degraded answer too: knowing which build is wedged is
    exactly what a degraded probe is for.

    Returns:
        200 {"status": "ok", "version": ..., "alembic_head": ...,
             "pool": {"size": ..., "checked_out": ...}}
        503 {"status": "degraded", "version": ..., "alembic_head": ...}
    """
    from brain_v42.db.engine import get_engine  # noqa: PLC0415

    identity = {"version": package_version(), "alembic_head": shipped_alembic_head()}
    engine = get_engine()
    try:
        async with asyncio.timeout(2):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "degraded", **identity}, status_code=503)
    pool = engine.pool
    return JSONResponse(
        {
            "status": "ok",
            **identity,
            "pool": {"size": pool.size(), "checked_out": pool.checkedout()},  # type: ignore[attr-defined]
        }
    )


def log_server_starting(settings: Settings) -> None:
    """Emit the one startup line, naming the build before anything runs.

    Extracted from the entrypoint so the payload is reachable by a test: the
    `__main__` block that used to hold it inline cannot be exercised.  Calling
    it also warms the memoised identity, so the first `/health` never pays the
    revision scan.
    """
    logger.info(
        "brain_v42.server.starting",
        version=package_version(),
        alembic_head=shipped_alembic_head(),
        transport=settings.brain_mcp_transport,
        tool_profile="code_mode" if settings.brain_code_mode else settings.brain_mcp_profile,
        metrics="flusher" if settings.metrics_enabled else "disabled",
        decay="enabled" if settings.decay_enabled else "disabled",
    )


def maybe_apply_code_mode(mcp: FastMCP, settings: Settings) -> FastMCP:
    """Wrap mcp with CodeMode if brain_code_mode is enabled."""
    if not settings.brain_code_mode:
        return mcp
    try:
        from fastmcp.experimental.transforms.code_mode import CodeMode  # noqa: PLC0415

        wrapped = CodeMode(mcp)  # type: ignore[arg-type,call-arg]
        logger.info("code_mode_enabled")
        return wrapped  # type: ignore[return-value]
    except ImportError:
        logger.warning(
            "code_mode_import_failed",
            msg="CodeMode not available in this FastMCP version",
        )
        return mcp


def build_brain_session_service(session_factory: Any) -> Any:
    """Wire the persistent lifecycle service without expanding core services."""
    from brain_v42.repositories.pg_brain_session import (  # noqa: PLC0415
        PgBrainSessionRepo,
    )
    from brain_v42.services.brain_session_service import (  # noqa: PLC0415
        BrainSessionService,
    )

    return BrainSessionService(PgBrainSessionRepo(session_factory))


def _neo4j_connection_settings(settings: Settings) -> tuple[str | None, str, str]:
    """Select the service-private projector credential at ledger cutover."""
    if settings.graph_projector_enabled:
        return (
            settings.graph_projector_neo4j_url,
            settings.graph_projector_neo4j_user,
            settings.graph_projector_neo4j_password.get_secret_value(),
        )
    return settings.neo4j_url, settings.neo4j_user, settings.neo4j_password


def build_services() -> dict[str, Any]:
    """Instantiate and wire all services. Called once at server startup.

    Returns:
        Dict with keys: decision_svc, learning_svc, snippet_svc,
        runbook_svc, adr_svc, project_context_svc, brain_svc,
        metrics_collector, embedding_svc.
    """
    session_factory = get_session_factory()
    settings = get_settings()
    ledger_enabled = getattr(settings, "graph_ledger_write_enabled", False) is True
    projector_enabled = getattr(settings, "graph_projector_enabled", False) is True
    if ledger_enabled and not projector_enabled:
        raise RuntimeError("MCP graph ledger requires the private projector role")
    embedding_svc = build_embedding_service(settings)

    # Neo4j graph (optional — disabled by default)
    neo4j_url, neo4j_user, neo4j_password = _neo4j_connection_settings(settings)
    neo4j_driver = create_neo4j_driver(
        url=neo4j_url,
        user=neo4j_user,
        password=neo4j_password,
        enabled=settings.graph_enabled,
    )
    graph_service: Any | None = (
        GraphService(neo4j_driver, timeout=settings.neo4j_timeout) if neo4j_driver else None
    )
    graph_ledger_repo = None
    graph_outbox_projector = None

    # Metrics collector (always created, but server only started if enabled)
    from brain_v42.db.engine import get_engine  # noqa: PLC0415
    from brain_v42.metrics.collector import MetricsCollector  # noqa: PLC0415
    from brain_v42.metrics.instrument import (  # noqa: PLC0415
        InstrumentedEmbeddingService,
        InstrumentedGraphService,
        InstrumentedReranker,
    )

    metrics_collector = MetricsCollector(
        engine=get_engine(),
        session_factory=session_factory,
    )

    # Wrap embedding service for metrics instrumentation
    if settings.metrics_enabled:
        embedding_svc = InstrumentedEmbeddingService(embedding_svc, metrics_collector)  # type: ignore[assignment]
        if graph_service is not None:
            graph_service = InstrumentedGraphService(graph_service, metrics_collector)

    if graph_service is not None:
        durable_stack = build_durable_graph_stack(
            graph_service,
            session_factory,
            settings,
            neo4j_driver=neo4j_driver,
        )
        graph_service = durable_stack.service
        graph_ledger_repo = durable_stack.ledger
        graph_outbox_projector = durable_stack.projector

    # AutoLinker — creates RELATED_TO graph edges on entity creation
    auto_linker: AutoLinker | None = None
    if graph_service is not None:
        auto_linker = AutoLinker(session_factory=session_factory, graph=graph_service)

    # Decay components
    from brain_v42.repositories.pg_access_log import PgAccessLogRepo  # noqa: PLC0415
    from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo  # noqa: PLC0415
    from brain_v42.services.access_logger import AccessLogger  # noqa: PLC0415
    from brain_v42.services.consolidation import ConsolidationJob  # noqa: PLC0415
    from brain_v42.services.decay import DecayCalculator  # noqa: PLC0415

    decay_calculator = DecayCalculator(
        stale_threshold=settings.stale_threshold,
        archive_threshold=settings.archive_threshold,
    )
    access_logger = AccessLogger(session_factory=session_factory)
    access_log_repo = PgAccessLogRepo(session_factory=session_factory)
    consolidation_log_repo = PgConsolidationLogRepo(session_factory=session_factory)
    consolidation_job = ConsolidationJob(
        session_factory=session_factory,
        consolidation_log_repo=consolidation_log_repo,
        threshold=settings.consolidation_similarity_threshold,
        graph=graph_service,
    )

    # Reranker client (HTTP, for ClusterGuard grey-zone scoring)

    reranker_client = build_reranker_client(settings)

    # StatusEngine (pure logic — monotonic feature status heuristic)
    from brain_v42.services.status_engine import StatusEngine  # noqa: PLC0415

    status_engine = StatusEngine()

    # ClusterGuard (anti-duplication resolver for feature signals)
    from brain_v42.services.cluster_guard import ClusterGuard  # noqa: PLC0415

    cluster_guard = ClusterGuard(
        session_factory=session_factory,
        embedding_svc=embedding_svc,
        reranker=reranker_client,
        status_engine=status_engine,
    )

    # Feature auto-linker (roadmap tracking, uses ClusterGuard)
    feature_linker = FeatureLinker(session_factory=session_factory, cluster_guard=cluster_guard)

    # PlanIndexer (scans plan/spec files, indexes + links to features)
    from brain_v42.services.plan_indexer import PlanIndexer  # noqa: PLC0415

    plan_indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_svc,
        cluster_guard=cluster_guard,
    )

    # Roadmap service (read-only)
    roadmap_svc = RoadmapService(session_factory=session_factory)
    feature_creation_svc = FeatureCreationService(
        session_factory=session_factory,
        embedding_svc=embedding_svc,
        embedding_dimension=settings.embedding_dimension,
    )

    # Repositories — all six share the same BasePgRepository constructor;
    # pass session_factory explicitly for consistent DI and testability.
    decision_repo = PgDecisionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)
    snippet_repo = PgSnippetRepo(session_factory)
    runbook_repo = PgRunbookRepo(session_factory)
    adr_repo = PgADRRepo(session_factory)
    project_context_repo = PgProjectContextRepo(session_factory)

    # Domain services — injected with repo + embedding_svc
    # project_context_repo wires the fail-closed project-existence guard
    # (LearningService/DecisionService/SnippetService/RunbookService/ADRService
    # .create() reject a missing or unknown project_key — see project_guard.py).
    decision_svc = DecisionService(
        repo=decision_repo,
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        graph=graph_service,
        auto_linker=auto_linker,
        project_context_repo=project_context_repo,
    )
    learning_svc = LearningService(
        pg_repo=learning_repo,
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        graph=graph_service,
        auto_linker=auto_linker,
        project_context_repo=project_context_repo,
    )
    snippet_svc = SnippetService(
        repo=snippet_repo,
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        graph=graph_service,
        auto_linker=auto_linker,
        project_context_repo=project_context_repo,
    )
    runbook_svc = RunbookService(
        pg_repo=runbook_repo,
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        graph=graph_service,
        auto_linker=auto_linker,
        project_context_repo=project_context_repo,
    )
    adr_svc = ADRService(
        pg_repo=adr_repo,
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        graph=graph_service,
        auto_linker=auto_linker,
        project_context_repo=project_context_repo,
    )
    project_context_svc = ProjectContextService(
        pg_repo=project_context_repo,
        graph=graph_service,
    )

    # Hybrid search — uses shared RerankerClient (same service as ClusterGuard)
    from brain_v42.services.search import HybridReranker, HybridSearcher  # noqa: PLC0415
    from brain_v42.services.search.batching_reranker import BatchingRerankerClient  # noqa: PLC0415

    # Wrap the reranker client for the hybrid search path ONLY.
    # ClusterGuard and FeatureDedupJob use solo calls (single query, no fan-out)
    # and would pay the coalescing window as pure overhead — keep them on the raw client.
    # BatchingRerankerClient is transparent: same duck-typed interface as RerankerClient.
    # 20 ms window: safe for local-network GPU; ~3–6x fan-out arrives within 1–5 ms.
    batching_reranker_client = BatchingRerankerClient(reranker_client, window_seconds=0.02)
    hybrid_reranker: Any = HybridReranker(client=batching_reranker_client)  # type: ignore[arg-type]
    if settings.metrics_enabled:
        hybrid_reranker = InstrumentedReranker(hybrid_reranker, metrics_collector)
    hybrid_searcher = HybridSearcher(reranker=hybrid_reranker)
    logger.info("brain_v42.server.hybrid_search_enabled")

    # Plan search service (over indexed_plan_chunks)
    from brain_v42.services.indexed_plan_search_service import (  # noqa: PLC0415
        IndexedPlanSearchService,
    )

    plan_search_svc = IndexedPlanSearchService(session_factory=session_factory)

    # Global search orchestrator
    brain_svc = BrainService(
        decision_svc=decision_svc,
        learning_svc=learning_svc,
        snippet_svc=snippet_svc,
        runbook_svc=runbook_svc,
        adr_svc=adr_svc,
        embedding_svc=embedding_svc,
        metrics_collector=metrics_collector,
        hybrid_searcher=hybrid_searcher,
        decay_calculator=decay_calculator if settings.decay_enabled else None,
        access_logger=access_logger if settings.decay_enabled else None,
        decay_floor=settings.decay_floor,
        decay_human_signal_enabled=settings.decay_human_signal_enabled,
        graph=graph_service,
        project_context_svc=project_context_svc,
        plan_search_svc=plan_search_svc,
    )

    # Tickets (coordination family — spec 2026-07-04)
    ticket_repo = PgTicketRepo(session_factory)
    ticket_svc = TicketService(
        repo=ticket_repo,
        project_context_repo=project_context_repo,
    )

    logger.info("brain_v42.server.services_initialized")

    return {
        "decision_svc": decision_svc,
        "learning_svc": learning_svc,
        "snippet_svc": snippet_svc,
        "runbook_svc": runbook_svc,
        "adr_svc": adr_svc,
        "project_context_svc": project_context_svc,
        "brain_svc": brain_svc,
        "metrics_collector": metrics_collector,
        "embedding_svc": embedding_svc,
        "feature_linker": feature_linker,
        "feature_creation_svc": feature_creation_svc,
        "roadmap_svc": roadmap_svc,
        "decay_calculator": decay_calculator,
        "access_logger": access_logger,
        "access_log_repo": access_log_repo,
        "consolidation_log_repo": consolidation_log_repo,
        "consolidation_job": consolidation_job,
        "reranker_client": reranker_client,
        "status_engine": status_engine,
        "cluster_guard": cluster_guard,
        "plan_indexer": plan_indexer,
        "graph_service": graph_service,
        "graph_ledger_repo": graph_ledger_repo,
        "graph_outbox_projector": graph_outbox_projector,
        "neo4j_driver": neo4j_driver,
        "auto_linker": auto_linker,
        "ticket_svc": ticket_svc,
    }


def _configure_http_security(
    mcp: FastMCP,
    settings: Settings,
    *,
    project_resolver: DreamProjectReferenceResolver | None = None,
) -> list[Middleware]:
    """Configure one HTTP server's authentication boundary exactly once.

    Disabled mode returns the historical ASGI bearer guard unchanged. Enabled
    mode parses the secret registry before Uvicorn starts, installs FastMCP's
    public token-verifier boundary, and adds one phase authorization middleware.
    """
    if mcp in _http_security_configured_servers:
        raise RuntimeError("HTTP security is already configured for this server")

    if not settings.brain_dream_capability_enforcement:
        middleware = [
            Middleware(HostOriginGuard),
            Middleware(BearerTokenGuard, token=settings.mcp_http_token),
        ]
        _http_security_configured_servers.add(mcp)
        return middleware

    if settings.brain_code_mode:
        raise DreamCapabilityConfigurationError(
            "Dream capability enforcement is incompatible with Code Mode"
        )
    if project_resolver is None:
        raise DreamCapabilityConfigurationError(
            "Dream project authorizer is required when capability enforcement is enabled"
        )

    registry = parse_dream_capability_registry(
        settings.mcp_http_dream_tokens,
        admin_token=settings.mcp_http_token,
    )
    mcp.auth = DreamCapabilityTokenVerifier(registry)
    mcp.add_middleware(DreamCapabilityMiddleware(project_resolver=project_resolver))
    _http_security_configured_servers.add(mcp)
    return [Middleware(HostOriginGuard)]


class SessionIdleTimeoutUnavailableError(RuntimeError):
    """La forme amont a changé et l'échéance de session ne serait plus posée."""


# Marqueur porté par la sous-classe injectée : il sert à la RECONNAÎTRE, donc à
# ne pas l'empiler sur elle-même à la deuxième installation.
_IDLE_TIMEOUT_MARKER = "_brain_v42_session_idle_timeout"


def _install_session_idle_timeout(seconds: float) -> None:
    """Poser l'échéance d'inactivité que FastMCP ne transmet pas.

    ``StreamableHTTPSessionManager`` accepte ``session_idle_timeout``, mais
    ``fastmcp.server.http`` le construit sans jamais le passer : le mode avec
    état garderait donc l'état de toute session dont le client meurt sans
    ``DELETE``, jusqu'au prochain redémarrage du processus.

    Substitution du symbole dans le module de FastMCP, faute de point
    d'extension public. Elle est ÉTROITE — une sous-classe qui ne fait que
    remplir un défaut — et surtout elle est GARDÉE : si le paramètre disparaît
    en amont, on lève au démarrage plutôt que de tourner sans échéance. Un
    monkeypatch silencieux qui cesse d'agir est pire que pas de monkeypatch,
    parce qu'il laisse croire que la borne existe.
    """
    from fastmcp.server import http as fastmcp_http

    base = fastmcp_http.StreamableHTTPSessionManager
    if "session_idle_timeout" not in inspect.signature(base.__init__).parameters:
        raise SessionIdleTimeoutUnavailableError(
            "StreamableHTTPSessionManager no longer accepts session_idle_timeout; "
            "stateful sessions would accumulate without expiry"
        )

    # IDEMPOTENCE, et ce n'est pas de la coquetterie : sans elle, deux appels
    # empilent deux sous-classes, et chaque appel suivant en rajoute une. Le
    # cas est réel — la production n'appelle qu'une fois, mais la suite de
    # tests traverse ``_run_mcp`` plusieurs fois dans un même processus.
    if getattr(base, _IDLE_TIMEOUT_MARKER, None) is not None:
        setattr(base, _IDLE_TIMEOUT_MARKER, seconds)
        return

    class _IdleTimeoutSessionManager(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            # ``setdefault`` : si FastMCP se met un jour à le transmettre, sa
            # valeur gagne et cette classe devient inerte d'elle-même.
            kwargs.setdefault(
                "session_idle_timeout",
                getattr(type(self), _IDLE_TIMEOUT_MARKER, seconds),
            )
            super().__init__(*args, **kwargs)

    setattr(_IdleTimeoutSessionManager, _IDLE_TIMEOUT_MARKER, seconds)
    # mypy refuse l'affectation à un nom de type ; c'est précisément ce qu'on
    # fait, faute de point d'extension public côté FastMCP. La garde de
    # signature ci-dessus est ce qui rend la substitution sûre.
    fastmcp_http.StreamableHTTPSessionManager = _IdleTimeoutSessionManager  # type: ignore[misc]
    logger.info("brain_v42.server.session_idle_timeout", seconds=seconds)


async def _run_mcp(
    mcp: FastMCP,
    settings: Settings,
    *,
    project_resolver: DreamProjectReferenceResolver | None = None,
    metrics_collector: Any | None = None,
) -> None:
    """Dispatch to the correct MCP transport (http or stdio).

    Extracted from the run_server closure so it is importable and independently
    testable. run_server() delegates here after wrapping with app_lifecycle.

    Business-error surfacing is applied here, once, rather than at each
    ``register_*`` site: this is the single async choke point both transports
    pass through, so a tool added tomorrow is covered without anyone having to
    remember a decorator (ticket 40ab2ced).
    """
    surfaced = await surface_business_errors(mcp)
    logger.info("brain_v42.server.business_errors_surfaced", tools=len(surfaced))

    if metrics_collector is not None:
        instrumented = await instrument_registered_tools(mcp, metrics_collector)
        logger.info("brain_v42.server.tools_instrumented", tools=len(instrumented))

    if settings.brain_mcp_transport == "http":
        resolved_project_resolver = project_resolver
        if settings.brain_dream_capability_enforcement and resolved_project_resolver is None:
            resolved_project_resolver = PostgresDreamProjectResolver(get_session_factory())
        middleware = _configure_http_security(
            mcp,
            settings,
            project_resolver=resolved_project_resolver,
        )
        auth_enabled = bool(settings.mcp_http_token) or settings.brain_dream_capability_enforcement
        logger.info(
            "brain_v42.server.http_auth",
            auth="enabled" if auth_enabled else "disabled",
        )
        if not settings.mcp_http_stateless:
            _install_session_idle_timeout(settings.mcp_http_session_idle_seconds)
        await mcp.run_http_async(
            transport="http",
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
            stateless_http=settings.mcp_http_stateless,
            json_response=True,
            uvicorn_config={"timeout_graceful_shutdown": 10},
            middleware=middleware,
        )
    else:
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        _install_signal_handlers(loop, shutdown_event)  # stdio only
        mcp_task = asyncio.create_task(mcp.run_async(transport="stdio"))
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            {mcp_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            if task is mcp_task and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]


class BuiltServer(NamedTuple):
    """What the entrypoint needs once every tool root has been registered."""

    mcp: FastMCP
    services: dict[str, Any]
    settings: Settings
    metrics_collector: Any


def build_server() -> BuiltServer:
    """Register every tool root on ``mcp`` and apply the catalog profile.

    Extracted from the entrypoint for the same reason as
    :func:`log_server_starting`, and with a sharper one: a ``__main__`` block
    cannot be imported, so the e2e harness had to REPRODUCE this wiring instead
    of calling it.  A double is worse than no test — a middleware or a tool root
    added on one side and not the other leaves the harness green about a server
    that exists nowhere.  There is now one wiring, and both callers use it.

    Behaviour is unchanged and deliberately so: same order, same profile branch,
    same services.  ``surface_business_errors`` and the metrics instrumentation
    still belong to :func:`_run_mcp`, which is the single async choke point both
    transports pass through.
    """
    # Import deferred to allow tools module to be populated by features #629-#635
    from brain_v42.mcp.tools.brain_tools import register_tools  # noqa: PLC0415

    services = build_services()
    settings = get_settings()
    metrics_collector = services["metrics_collector"]
    usage_access_logger = _select_usage_access_logger(settings, services)

    register_tools(
        mcp,
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        snippet_svc=services["snippet_svc"],
        runbook_svc=services["runbook_svc"],
        adr_svc=services["adr_svc"],
        project_context_svc=services["project_context_svc"],
        brain_svc=services["brain_svc"],
        metrics_collector=metrics_collector,
        roadmap_svc=services["roadmap_svc"],
        graph_svc=services["graph_service"],
        access_logger=usage_access_logger,
    )

    # Session tools
    from brain_v42.mcp.tools.session_tools import register_session_tools  # noqa: PLC0415
    from brain_v42.services.dream_run_service import DreamRunService  # noqa: PLC0415
    from brain_v42.services.feature_service import FeatureService  # noqa: PLC0415
    from brain_v42.services.schema_state_service import SchemaStateService  # noqa: PLC0415

    _session_factory = get_session_factory()
    _cross_project_svc = None
    if services["graph_service"] is not None:
        from brain_v42.services.cross_project_service import (  # noqa: PLC0415
            CrossProjectBriefingService,
        )

        _settings = get_settings()
        _cross_project_svc = CrossProjectBriefingService(
            _session_factory,
            services["graph_service"],
            top_n=_settings.brain_cross_project_briefing_domains_top_n,
            entries_max=_settings.brain_cross_project_briefing_entries_max,
        )
    feature_svc = FeatureService(_session_factory)
    brain_session_svc = build_brain_session_service(_session_factory)
    register_session_tools(
        mcp,
        project_context_svc=services["project_context_svc"],
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        dream_run_svc=DreamRunService(_session_factory),
        feature_svc=feature_svc,
        brain_session_svc=brain_session_svc,
        cross_project_svc=_cross_project_svc,
        ticket_svc=services["ticket_svc"],
        schema_state_svc=SchemaStateService(_session_factory),
    )

    # Roadmap tools
    from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools  # noqa: PLC0415

    register_roadmap_tools(
        mcp,
        roadmap_svc=services["roadmap_svc"],
        feature_svc=feature_svc,
        feature_creation_svc=services["feature_creation_svc"],
    )

    # Decay tools
    from brain_v42.mcp.tools.decay_tools import register_decay_tools  # noqa: PLC0415

    register_decay_tools(
        mcp,
        session_factory=get_session_factory(),
        consolidation_job=services["consolidation_job"],
    )

    # Plan indexing tools
    from brain_v42.mcp.tools.plan_tools import register_plan_tools  # noqa: PLC0415

    register_plan_tools(mcp, plan_indexer=services["plan_indexer"])

    # CRUD tools (brain_get, brain_delete, brain_update, brain_list)
    from brain_v42.mcp.tools.crud_tools import register_crud_tools  # noqa: PLC0415

    register_crud_tools(
        mcp,
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        snippet_svc=services["snippet_svc"],
        runbook_svc=services["runbook_svc"],
        adr_svc=services["adr_svc"],
        session_factory=get_session_factory(),
        access_logger=usage_access_logger,
    )

    # Dream tools (backfill links, clusters)
    from brain_v42.mcp.tools.dream_tools import register_dream_tools  # noqa: PLC0415

    register_dream_tools(
        mcp,
        session_factory=get_session_factory(),
        auto_linker=services.get("auto_linker"),
        graph_service=services.get("graph_service"),
    )

    # Ticket tools (coordination cross-projet)
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools  # noqa: PLC0415

    register_ticket_tools(mcp, ticket_svc=services["ticket_svc"])

    if settings.brain_code_mode:
        server = maybe_apply_code_mode(mcp, settings)
    else:
        from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile  # noqa: PLC0415

        server = apply_tool_catalog_profile(mcp, settings.brain_mcp_profile)

    return BuiltServer(
        mcp=server,
        services=services,
        settings=settings,
        metrics_collector=metrics_collector,
    )


if __name__ == "__main__":
    _configure_stdio_logging()
    _setup_parent_death_signal()
    _apply_http_server_arg()  # MUST be before get_settings() -- sets env for lru_cache

    built = build_server()

    async def run_server() -> None:
        async with app_lifecycle(built.settings, built.services, built.metrics_collector):
            await _run_mcp(built.mcp, built.settings, metrics_collector=built.metrics_collector)

    log_server_starting(built.settings)
    asyncio.run(run_server())
