"""FastMCP server for brain_v42 (stdio and http transports).

Entry point: python -m brain_v42.mcp.server
Transport: controlled by BRAIN_MCP_TRANSPORT env var (default: stdio)

Initialization sequence on startup:
1. get_session_factory() — shared SQLAlchemy async_sessionmaker singleton
2. GPUEmbeddingService — connects to GPU embedding service at localhost:8003
3. All domain repos (PgDecisionRepo, PgLearningRepo, etc.) — injected with session_factory
4. All domain services (DecisionService, LearningService, etc.) — injected with repo + embedding_svc
5. BrainService — fans out semantic search across all domain services
6. Registration roots expose 76 always-on + 2 graph-gated = 78 brain_* tools

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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple
from weakref import WeakSet

import structlog
from fastmcp import FastMCP
from sqlalchemy import text
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from brain_v42.config import Settings, get_settings
from brain_v42.credentials.audit import AuditDrainer
from brain_v42.credentials.listener import CredentialListener
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.db.engine import dispose_engine, get_session_factory, use_engine_profile
from brain_v42.db.neo4j import close_neo4j_driver, create_neo4j_driver
from brain_v42.facts.definitions_startup import register_fact_definitions
from brain_v42.mcp.activity_reporter import close_activity_reporter
from brain_v42.mcp.business_errors import surface_business_errors
from brain_v42.mcp.credentials_http import CredentialGuard, CredentialTokenVerifier
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
from brain_v42.mcp.http_security import (
    BearerTokenGuard,
    HostOriginGuard,
    HttpAuthConfigurationError,
    RequestBodyLimitGuard,
)
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.mcp.session_autoopen import close_connection_traces
from brain_v42.metrics.tool_instrumentation import instrument_registered_tools
from brain_v42.release import package_version, shipped_alembic_head
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_client_credentials import PgClientCredentialRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.safe_logging import build_logging_processors
from brain_v42.services.adr_service import ADRService
from brain_v42.services.agent_trace_net import AgentTraceNet, agent_trace_net_is_armed
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
    log_format = get_settings().brain_log_format
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=build_logging_processors(log_format, colors=False),
    )
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    structlog.get_logger(__name__).info(
        "logging.configured", renderer=log_format, service="brain-v42-mcp", pid=os.getpid()
    )


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

    # OTel tracing — armed HERE and not in `_run_mcp`, which six unit tests
    # call: they would install a real provider and a real exporter.
    # `init_tracing` never raises and returns False when the extra is absent,
    # which is the NORMAL state of an installation that does not trace.
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
        # LIFO: the registry closes BEFORE the engine is disposed, so no probe
        # task outlives its connection factory (spec §5.4, shutdown).
        fact_registry = services.get("fact_registry")
        if fact_registry is not None:
            cleanup.push_async_callback(fact_registry.aclose)
        cleanup.push_async_callback(close_neo4j_driver, services["neo4j_driver"])
        # d5e4bd73, second hole: without this close, in-flight activity POSTs
        # died at shutdown without being counted. LIFO: it runs before
        # dispose_engine, while the loop is still serving.
        cleanup.push_async_callback(close_activity_reporter)

        credential_verifier = services.get("credential_verifier")
        if (
            credential_verifier is not None
            and settings.brain_mcp_transport == "http"
            and settings.brain_mcp_auth_mode == "credentials"
        ):
            # A failed refresh leaves HTTP available for health and explicit 503s.
            await credential_verifier.refresh()
            refresh_task = asyncio.create_task(credential_verifier.run_refresh_loop())
            cleanup.push_async_callback(_cancel_task, refresh_task)
            audit_drainer = AuditDrainer(
                PgClientCredentialRepo(get_session_factory()), clock=lambda: datetime.now(UTC)
            )
            audit_stop = asyncio.Event()
            audit_task = asyncio.create_task(audit_drainer.run(audit_stop))
            cleanup.push_async_callback(_cancel_task, audit_task)
            cleanup.callback(audit_stop.set)
            listeners = (
                CredentialListener(
                    settings.postgres_url,
                    "brain_client_credentials",
                    on_notification=credential_verifier.notify,
                    on_connect=credential_verifier.run_listener_reconnected,
                ),
                CredentialListener(
                    settings.postgres_url,
                    "brain_credential_audit",
                    on_notification=audit_drainer.wake,
                    on_connect=audit_drainer.wake,
                ),
            )
            for listener in listeners:
                listener_task = asyncio.create_task(listener.run())
                cleanup.push_async_callback(_cancel_task, listener_task)

        if tracing_armed:
            # `shutdown_on_exit=False` disarmed the SDK's atexit so an
            # unreachable collector would not drag out the shutdown; it is
            # therefore up to us to drain the queue, within a bounded delay.
            # Without this callback, pending spans disappeared without a word —
            # a hole found by the e2e of 2026-08-12, not by re-reading.
            cleanup.push_async_callback(asyncio.to_thread, shutdown_tracing, 3000)

        if graph_outbox_projector is not None:
            if graph_ledger_repo is None:
                raise RuntimeError("graph projector requires the PostgreSQL graph ledger")
            await graph_ledger_repo.assert_schema_ready()

        if fact_registry is not None:
            await register_fact_definitions(fact_registry, get_session_factory())

        if graph_outbox_projector is not None:
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

        if settings.plan_index_refresh_enabled:
            from brain_v42.services.plan_index_refresher import (  # noqa: PLC0415
                PlanIndexRefresher,
            )

            plan_index_refresher = PlanIndexRefresher(
                plan_indexer=services["plan_indexer"],
                interval_seconds=settings.plan_index_refresh_interval_seconds,
            )
            cleanup.push_async_callback(plan_index_refresher.stop)
            await plan_index_refresher.start()

        if agent_trace_net_is_armed(settings):
            from brain_v42.repositories.pg_brain_session import (  # noqa: PLC0415
                PgBrainSessionRepo,
            )

            agent_trace_net = AgentTraceNet(
                close_inactive=PgBrainSessionRepo(
                    get_session_factory()
                ).close_inactive_agent_traces,
            )
            cleanup.push_async_callback(agent_trace_net.stop)
            await agent_trace_net.start()

        # Keep a strong reference so the GC cannot collect the task mid-flight.
        # This one-shot covers t=0; the refresher above, when armed, sleeps its
        # interval before its first sweep so the two never walk the same files
        # at the same time.
        plan_index_task = asyncio.create_task(_background_plan_index())
        cleanup.push_async_callback(_cancel_task, plan_index_task)
        yield


def create_mcp_instance() -> FastMCP:
    """Build a FastMCP instance with its service-independent wiring.

    ONE definition for TWO consumers: the module singleton below (production —
    ``/health`` is added to it by decorator) and the integration benches that
    stand up their own server. Without it, a bench reusing the singleton
    inherited the tools registered by a test module collected before it — 20
    measured "Component already exists", closed on an already ``dispose()``d
    engine (ticket ``83d8785b``) — and pytest's collection order became
    meaningful. The remedy is NOT wiring reproduced by hand in the bench:
    ``build_server`` has already settled that a double is worse than no test.
    """
    instance = FastMCP("brain", mask_error_details=True)
    # Provenance: installed here and not in register_tools, so it is independent
    # of whether metrics are enabled and of the tool registration order.
    # `apply_tool_catalog_profile` and `maybe_apply_code_mode` return the SAME
    # object, so this middleware survives both.
    instance.add_middleware(ProvenanceMiddleware())
    return instance


# Module-level FastMCP instance
mcp = create_mcp_instance()


_health_probe: asyncio.Task[bool] | None = None


async def _probe_database(engine: Any) -> bool:
    """Bound the database round trip so a wedged pool reports degraded."""
    try:
        async with asyncio.timeout(2):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


async def _coalesced_database_probe(engine: Any) -> bool:
    """Share only in-flight work; cancellation of a caller leaves the probe alive."""
    global _health_probe
    if (
        _health_probe is None
        or _health_probe.done()
        or _health_probe.get_loop() is not asyncio.get_running_loop()
    ):
        _health_probe = asyncio.create_task(_probe_database(engine))
    return await asyncio.shield(_health_probe)


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
    if not await _coalesced_database_probe(engine):
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


def build_credential_verifier(settings: Settings) -> CredentialVerifier | None:
    """Build the registry verifier for credentials mode; shared-token stays independent of it.

    A stdio server has no transport boundary, so credentials mode there would run with no
    guard at all: it is refused here, at server start, rather than at settings load, which
    other entry points (the metrics sidecar, the CLIs) share without serving MCP.
    """
    if settings.brain_mcp_auth_mode != "credentials":
        return None
    if settings.brain_mcp_transport != "http":
        raise HttpAuthConfigurationError(
            "credentials mode requires BRAIN_MCP_TRANSPORT=http: a stdio server has no guard"
        )
    return CredentialVerifier(
        PgClientCredentialRepo(get_session_factory()),
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    )


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

    from brain_v42.delivery_config import DeliverySettings  # noqa: PLC0415
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo  # noqa: PLC0415
    from brain_v42.services.delivery_service import DeliveryService  # noqa: PLC0415

    delivery_svc = DeliveryService(PgDeliveryRepo(session_factory), settings=DeliverySettings())

    # Measured facts (spec 2026-09-19, lot A): the closed catalogue, frozen
    # here, verified against the identity the operator declared — never against
    # the DSN the probes connect with.
    from brain_v42.facts.composition import build_fact_registry_from_settings  # noqa: PLC0415

    fact_registry = build_fact_registry_from_settings(settings, session_factory)

    logger.info("brain_v42.server.services_initialized")

    services = {
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
        "delivery_svc": delivery_svc,
        "fact_registry": fact_registry,
    }
    credential_verifier = build_credential_verifier(settings)
    if credential_verifier is not None:
        services["credential_verifier"] = credential_verifier
    return services


def _configure_http_security(
    mcp: FastMCP,
    settings: Settings,
    *,
    project_resolver: DreamProjectReferenceResolver | None = None,
    credential_verifier: CredentialVerifier | None = None,
) -> list[Middleware]:
    """Configure one HTTP server's authentication boundary exactly once.

    Ordinary mode refuses absent authentication unless development opts out.
    Capability mode parses the secret registry before Uvicorn starts, installs FastMCP's
    public token-verifier boundary, and adds one phase authorization middleware.
    """
    if mcp in _http_security_configured_servers:
        raise RuntimeError("HTTP security is already configured for this server")

    if settings.brain_mcp_auth_mode == "credentials":
        if (
            settings.brain_dream_capability_enforcement
            or settings.mcp_http_allow_unauthenticated
            or settings.mcp_http_token
            or settings.mcp_http_stateless
        ):
            raise HttpAuthConfigurationError("credentials mode requires exclusive stateful auth")
        if credential_verifier is None:
            raise HttpAuthConfigurationError("credentials mode requires a registry verifier")
        mcp.auth = CredentialTokenVerifier(credential_verifier)
        _http_security_configured_servers.add(mcp)
        return [
            Middleware(HostOriginGuard),
            Middleware(CredentialGuard, verifier=credential_verifier),
            Middleware(RequestBodyLimitGuard, max_body_bytes=settings.mcp_http_max_body_bytes),
        ]

    if not settings.brain_dream_capability_enforcement:
        has_token = bool(settings.mcp_http_token.strip())
        opt_out = settings.mcp_http_allow_unauthenticated
        if not has_token and not opt_out:
            raise HttpAuthConfigurationError(
                "MCP_HTTP_TOKEN is required unless MCP_HTTP_ALLOW_UNAUTHENTICATED=true"
            )
        raw_token_present = any(
            os.environ.get(name) for name in ("MCP_HTTP_TOKEN", "BRAIN_MCP_HTTP_TOKEN")
        )
        if opt_out and (has_token or raw_token_present):
            raise HttpAuthConfigurationError(
                "MCP_HTTP_TOKEN and MCP_HTTP_ALLOW_UNAUTHENTICATED are contradictory"
            )
        bearer = Middleware(BearerTokenGuard, token=settings.mcp_http_token)
        if opt_out:
            bearer = Middleware(BearerTokenGuard, token="", allow_unauthenticated=True)
            logger.warning("brain_v42.server.http_auth", auth="disabled_by_opt_in")
        middleware = [
            Middleware(HostOriginGuard),
            bearer,
            Middleware(RequestBodyLimitGuard, max_body_bytes=settings.mcp_http_max_body_bytes),
        ]
        _http_security_configured_servers.add(mcp)
        return middleware

    if settings.mcp_http_allow_unauthenticated:
        raise DreamCapabilityConfigurationError(
            "MCP_HTTP_ALLOW_UNAUTHENTICATED is incompatible with Dream capability enforcement"
        )
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
    return [
        Middleware(HostOriginGuard),
        Middleware(RequestBodyLimitGuard, max_body_bytes=settings.mcp_http_max_body_bytes),
    ]


class SessionIdleTimeoutUnavailableError(RuntimeError):
    """The upstream shape changed and the session deadline would no longer be set."""


# Marker carried by the injected subclass: it serves to RECOGNIZE it, hence to
# avoid stacking it on itself at the second installation.
_IDLE_TIMEOUT_MARKER = "_brain_v42_session_idle_timeout"


def _install_session_idle_timeout(seconds: float) -> None:
    """Set the idle deadline FastMCP does not pass through.

    ``StreamableHTTPSessionManager`` accepts ``session_idle_timeout``, but
    ``fastmcp.server.http`` constructs it without ever passing it: the stateful
    mode would therefore keep the state of every session whose client dies
    without a ``DELETE``, until the next process restart.

    A symbol substitution inside FastMCP's module, for want of a public
    extension point. It is NARROW — a subclass that does nothing but fill in a
    default — and above all it is GUARDED: if the parameter disappears upstream,
    we raise at startup rather than run with no deadline. A silent monkeypatch
    that stops acting is worse than no monkeypatch, because it leaves people
    believing the bound exists.
    """
    from fastmcp.server import http as fastmcp_http

    base = fastmcp_http.StreamableHTTPSessionManager
    if "session_idle_timeout" not in inspect.signature(base.__init__).parameters:
        raise SessionIdleTimeoutUnavailableError(
            "StreamableHTTPSessionManager no longer accepts session_idle_timeout; "
            "stateful sessions would accumulate without expiry"
        )

    # IDEMPOTENCE, and it is not fussiness: without it, two calls stack two
    # subclasses, and every subsequent call adds another. The case is real —
    # production calls once, but the test suite goes through ``_run_mcp``
    # several times in a single process.
    if getattr(base, _IDLE_TIMEOUT_MARKER, None) is not None:
        setattr(base, _IDLE_TIMEOUT_MARKER, seconds)
        return

    class _IdleTimeoutSessionManager(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            # ``setdefault``: if FastMCP ever starts passing it through, its
            # value wins and this class becomes inert by itself.
            kwargs.setdefault(
                "session_idle_timeout",
                getattr(type(self), _IDLE_TIMEOUT_MARKER, seconds),
            )
            super().__init__(*args, **kwargs)

    setattr(_IdleTimeoutSessionManager, _IDLE_TIMEOUT_MARKER, seconds)
    # mypy refuses assignment to a type name; that is precisely what we are
    # doing, for want of a public extension point on the FastMCP side. The
    # signature guard above is what makes the substitution safe.
    fastmcp_http.StreamableHTTPSessionManager = _IdleTimeoutSessionManager  # type: ignore[misc]
    logger.info("brain_v42.server.session_idle_timeout", seconds=seconds)


class TransportTerminationHookUnavailableError(RuntimeError):
    """The SDK shape changed and terminated connections would no longer close their tracers."""


# Marker carried by the injected transport subclass, for the same reason as the
# idle-timeout one: recognise it, so a second installation does not stack.
_TERMINATION_HOOK_MARKER = "_brain_v42_on_terminated"


#: Window over which every termination report shares one time budget.
_TERMINATION_REPORT_WINDOW_SECONDS = 30.0

#: The value ``mcp_session_id`` is given on the probe instance the guard builds.
_SHAPE_PROBE_SESSION_ID = "brain-v42-transport-shape-probe"


def _assert_transport_shape(base: Any) -> None:
    """Refuse to start unless the transport still has the shape the hook relies on.

    The subclass calls ``terminate()`` with no argument and awaits it, reads
    ``is_terminated`` before and ``mcp_session_id`` after. Any of these changing
    upstream would make termination raise OUTSIDE the fail-open report, on the
    DELETE, eviction and shutdown paths: better no server than that.
    """
    terminate = getattr(base, "terminate", None)
    problems: list[str] = []
    if not inspect.iscoroutinefunction(terminate):
        problems.append("terminate is not a coroutine function")
    else:
        parameters = list(inspect.signature(terminate).parameters.values())
        if [p.name for p in parameters] != ["self"]:
            problems.append("terminate takes arguments")
    if not isinstance(inspect.getattr_static(base, "is_terminated", None), property):
        problems.append("is_terminated is not a property")
    if "mcp_session_id" not in inspect.signature(base.__init__).parameters:
        problems.append("__init__ takes no mcp_session_id")
    if not problems:
        # A signature says what the constructor accepts, not what the instance
        # carries: build one and read the two attributes the hook reads.
        try:
            probe = base(mcp_session_id=_SHAPE_PROBE_SESSION_ID)
            if getattr(probe, "mcp_session_id", None) != _SHAPE_PROBE_SESSION_ID:
                problems.append("the instance does not carry mcp_session_id")
            elif probe.is_terminated is not False:
                problems.append("a new instance is not reported live by is_terminated")
        except Exception as exc:  # noqa: BLE001 — any failure is a changed shape
            problems.append(f"a probe instance could not be built ({type(exc).__name__})")
    if problems:
        raise TransportTerminationHookUnavailableError(
            "StreamableHTTPServerTransport changed shape ("
            + "; ".join(problems)
            + "); ended connections would leave their agent tracers open"
        )


@dataclass
class _ReportLedger:
    """The time every termination report of one window draws on, and its turn.

    One per installation, so reinstalling (tests) starts from a fresh budget.
    """

    window_start: float = float("-inf")
    spent: float = 0.0
    turn: asyncio.Lock = field(default_factory=asyncio.Lock)

    def remaining(self, budget: float, now: float) -> float:
        if now - self.window_start >= _TERMINATION_REPORT_WINDOW_SECONDS:
            self.window_start = now
            self.spent = 0.0
        return budget - self.spent


async def _report_termination(
    report: Callable[[str], Awaitable[None]],
    session_id: str,
    budget: float,
    ledger: _ReportLedger,
) -> None:
    """Run one report within the time every report of the window shares.

    FastMCP's shutdown terminates the live transports one after the other, and
    the report swallows its own database errors, so neither a timeout nor an
    exception is a reliable signal: what is bounded is TIME. Every report of a
    window draws on one ``budget``; once it is spent, the next reports are
    skipped until the window ends, and their tracers are left to the inactivity
    net, which closes them on the same terms. A report that raises spends the
    rest of the budget. In normal operation a report takes milliseconds and the
    budget is never reached.

    Reports take turns (round 3 of #291): otherwise concurrent terminations, a
    burst of DELETEs or evictions, would each read the same remaining budget and
    each hold a stuck database for all of it. Waiting for the turn is itself
    bounded by the budget left when the termination arrived.
    """
    loop = asyncio.get_running_loop()
    remaining = ledger.remaining(budget, loop.time())
    if remaining <= 0:
        logger.debug("brain_v42.server.transport_termination_report_skipped")
        return
    try:
        await asyncio.wait_for(ledger.turn.acquire(), timeout=remaining)
    except TimeoutError:
        logger.debug("brain_v42.server.transport_termination_report_skipped")
        return
    try:
        now = loop.time()
        remaining = ledger.remaining(budget, now)
        if remaining <= 0:
            logger.debug("brain_v42.server.transport_termination_report_skipped")
            return
        try:
            await asyncio.wait_for(report(session_id), timeout=remaining)
        except Exception as exc:  # noqa: BLE001 — fail-open by contract
            ledger.spent = budget
            logger.warning(
                "brain_v42.server.transport_termination_report_failed",
                error=type(exc).__name__,
            )
            return
        ledger.spent += loop.time() - now
    finally:
        ledger.turn.release()


def _install_transport_termination_hook(
    on_terminated: Callable[[str], Awaitable[None]],
    *,
    budget_seconds: float = 5.0,
) -> None:
    """Report every terminated stateful transport, by its ``Mcp-Session-Id``.

    Ticket 09d2b56e. The SDK ends a stateful session through
    ``StreamableHTTPServerTransport.terminate`` on its three nominal paths — a
    client DELETE, the idle eviction, the server shutdown — and offers no
    callback. The session manager instantiates the class from ITS module, so a
    subclass substituted there sees every transport this server creates.

    GUARDED like the idle deadline: no ``terminate`` upstream means a refusal to
    start, never a server that believes it closes tracers. FAIL-OPEN and BOUNDED
    on the report itself: closing a database row must never keep a connection
    from terminating, nor hold a shutdown for longer than ``budget_seconds``. A
    crashed session never reaches ``terminate``; the inactivity net covers it.
    """
    from mcp.server import streamable_http_manager  # noqa: PLC0415

    base = streamable_http_manager.StreamableHTTPServerTransport
    _assert_transport_shape(base)
    if getattr(base, _TERMINATION_HOOK_MARKER, None) is not None:
        setattr(base, _TERMINATION_HOOK_MARKER, (on_terminated, budget_seconds, _ReportLedger()))
        return

    class _ReportingTransport(base):  # type: ignore[misc, valid-type]
        async def terminate(self) -> None:
            first = not self.is_terminated
            await super().terminate()
            session_id = getattr(self, "mcp_session_id", None)
            if not first or session_id is None:
                return
            report, budget, ledger = getattr(type(self), _TERMINATION_HOOK_MARKER)
            await _report_termination(report, session_id, budget, ledger)

    setattr(
        _ReportingTransport,
        _TERMINATION_HOOK_MARKER,
        (on_terminated, budget_seconds, _ReportLedger()),
    )
    # Same substitution, same justification as the idle deadline above.
    streamable_http_manager.StreamableHTTPServerTransport = _ReportingTransport  # type: ignore[misc]
    logger.info("brain_v42.server.transport_termination_hook", budget_seconds=budget_seconds)


async def prepare_tools_for_transport(mcp: FastMCP, metrics_collector: Any | None) -> None:
    """Apply the transport-agnostic prelude every served tool must carry.

    Business-error surfacing is applied here, once, rather than at each
    ``register_*`` site: a tool added tomorrow is covered without anyone having
    to remember a decorator (ticket 40ab2ced).  Instrumentation rides along for
    the same reason.

    Importable so a harness can go through it instead of guessing which half of
    it matters.  Guessing is how the e2e harness ended up serving uninstrumented
    tools while production served instrumented ones.
    """
    surfaced = await surface_business_errors(mcp)
    logger.info("brain_v42.server.business_errors_surfaced", tools=len(surfaced))

    if metrics_collector is not None:
        instrumented = await instrument_registered_tools(mcp, metrics_collector)
        logger.info("brain_v42.server.tools_instrumented", tools=len(instrumented))


class HttpTransportPlan(NamedTuple):
    """How the HTTP app must be shaped. Decided once, applied by every mount."""

    middleware: list[Middleware]
    stateless_http: bool
    json_response: bool


def plan_http_transport(
    mcp: FastMCP,
    settings: Settings,
    *,
    project_resolver: DreamProjectReferenceResolver | None = None,
    credential_verifier: CredentialVerifier | None = None,
) -> HttpTransportPlan:
    """Decide the HTTP boundary, and return it instead of serving it.

    Split from :func:`_run_mcp` so the shape of the served app has ONE source.
    ``_run_mcp`` hands the plan to ``run_http_async``; a test harness that needs
    an ephemeral port hands the same plan to ``http_app``.  What must not happen
    again is a harness inventing its own arguments — that is how a test ends up
    green about a server nobody runs.

    Not idempotent, deliberately: ``_configure_http_security`` refuses a second
    call on the same server, because configuring one authentication boundary
    twice is a production bug. A caller that mounts more than once must build a
    fresh server, not soften this.
    """
    resolved_project_resolver = project_resolver
    if settings.brain_dream_capability_enforcement and resolved_project_resolver is None:
        resolved_project_resolver = PostgresDreamProjectResolver(get_session_factory())
    middleware = _configure_http_security(
        mcp,
        settings,
        project_resolver=resolved_project_resolver,
        credential_verifier=credential_verifier,
    )
    auth_enabled = (
        bool(settings.mcp_http_token.strip())
        or settings.brain_dream_capability_enforcement
        or settings.brain_mcp_auth_mode == "credentials"
    )
    logger.info(
        "brain_v42.server.http_auth",
        auth="enabled" if auth_enabled else "disabled_by_opt_in",
    )
    if not settings.mcp_http_stateless:
        _install_session_idle_timeout(settings.mcp_http_session_idle_seconds)
        _install_transport_termination_hook(close_connection_traces)
    return HttpTransportPlan(
        middleware=middleware,
        stateless_http=settings.mcp_http_stateless,
        json_response=True,
    )


async def _run_mcp(
    mcp: FastMCP,
    settings: Settings,
    *,
    project_resolver: DreamProjectReferenceResolver | None = None,
    metrics_collector: Any | None = None,
    http_plan: HttpTransportPlan | None = None,
    credential_verifier: CredentialVerifier | None = None,
) -> None:
    """Dispatch to the correct MCP transport (http or stdio).

    Extracted from the run_server closure so it is importable and independently
    testable. run_server() checks HTTP security before entering app_lifecycle
    and passes its plan here so authentication is configured only once.

    Business-error surfacing is applied here, once, rather than at each
    ``register_*`` site: this is the single async choke point both transports
    pass through, so a tool added tomorrow is covered without anyone having to
    remember a decorator (ticket 40ab2ced).
    """
    await prepare_tools_for_transport(mcp, metrics_collector)

    if settings.brain_mcp_transport == "http":
        plan = (
            http_plan
            if http_plan is not None
            else plan_http_transport(
                mcp,
                settings,
                project_resolver=project_resolver,
                credential_verifier=credential_verifier,
            )
        )
        await mcp.run_http_async(
            transport="http",
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
            stateless_http=plan.stateless_http,
            json_response=plan.json_response,
            uvicorn_config={
                "timeout_graceful_shutdown": 10,
                "proxy_headers": False,
                "forwarded_allow_ips": "",
            },
            middleware=plan.middleware,
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
    from brain_v42.facts.verification import ClaimVerificationService  # noqa: PLC0415
    from brain_v42.mcp.tools.brain_tools import register_tools  # noqa: PLC0415

    services = build_services()
    settings = get_settings()
    metrics_collector = services["metrics_collector"]
    usage_access_logger = _select_usage_access_logger(settings, services)

    # Built once, ahead of every writer registration below, so `brain_learn` and
    # friends (measure=true, ADR 27 lot B remainder) and `brain_claim_verify`
    # (lot B3) share one service -- one semaphore bound, not two independent ones.
    claim_verification_svc = ClaimVerificationService(
        services["fact_registry"], get_session_factory()
    )
    # Shared claim reads (spec 2026-09-19, lot B4): one SELECT-only service,
    # injected into the claim tools and every knowledge reader that appends
    # the compact claim suffix (brain_get, brain_search, the session briefing).
    from brain_v42.services.claim_inventory_service import (  # noqa: PLC0415
        ClaimInventoryService,
    )
    from brain_v42.services.claim_read_service import ClaimReadService  # noqa: PLC0415

    claim_read_svc = ClaimReadService(get_session_factory())
    # One cached aggregate behind the briefing's CLAIMS line (spec 2026-09-30, section 8).
    claim_inventory_svc = ClaimInventoryService(get_session_factory())

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
        fact_registry=services["fact_registry"],
        session_factory=get_session_factory(),
        claim_verification_svc=claim_verification_svc,
        claim_read_svc=claim_read_svc,
        extraction_enabled=settings.brain_claim_extraction_enabled,
    )

    # Session tools
    from brain_v42.mcp.tools.session_tools import register_session_tools  # noqa: PLC0415
    from brain_v42.repositories.pg_focus_slot import PgFocusSlotRepo  # noqa: PLC0415
    from brain_v42.services.dream_run_service import DreamRunService  # noqa: PLC0415
    from brain_v42.services.feature_service import FeatureService  # noqa: PLC0415
    from brain_v42.services.focus_slot_service import FocusSlotService  # noqa: PLC0415
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
    focus_slot_svc = FocusSlotService(PgFocusSlotRepo(_session_factory))
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
        delivery_svc=services["delivery_svc"],
        fact_registry=services.get("fact_registry"),
        claim_read_svc=claim_read_svc,
        claim_inventory_svc=claim_inventory_svc,
        claim_extraction_enabled=settings.brain_claim_extraction_enabled,
        focus_slot_svc=focus_slot_svc,
    )

    from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools  # noqa: PLC0415

    register_focus_slot_tools(mcp, focus_slot_svc=focus_slot_svc)

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
        fact_registry=services.get("fact_registry"),
        claim_verification_svc=claim_verification_svc,
        claim_read_svc=claim_read_svc,
        extraction_enabled=settings.brain_claim_extraction_enabled,
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

    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools  # noqa: PLC0415

    register_delivery_tools(mcp, delivery_svc=services["delivery_svc"])

    # Measured facts (spec 2026-09-19, lot A): two read-only tools over the
    # frozen catalogue; a fact name resolves to a live value here.
    from brain_v42.mcp.tools.fact_tools import register_fact_tools  # noqa: PLC0415

    register_fact_tools(mcp, registry=services["fact_registry"])

    # Claims (spec 2026-09-19, lots B3/B4): a caller names a claim, the server
    # measures it through the same registry and appends the verdict (B3). No
    # Dream phase reaches brain_claim_verify before lot C binds a verified run
    # id. It reuses the SAME ClaimVerificationService instance built above for
    # the writers' measure=true path, so the concurrency semaphore is one shared
    # bound, not two. brain_claim_list/brain_claim_history (B4) share
    # claim_read_svc, built above, with the knowledge readers.
    from brain_v42.mcp.tools.claim_tools import register_claim_tools  # noqa: PLC0415

    register_claim_tools(mcp, claim_verification_svc, claim_read_svc)

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
    _apply_http_server_arg()  # MUST precede logging's first get_settings() call.
    _configure_stdio_logging()
    _setup_parent_death_signal()
    # The MCP server is the long-lived interactive process: bounded session budgets.
    # Here and not in build_server(): tests inject an engine and call build_server().
    use_engine_profile("interactive")

    built = build_server()

    async def run_server() -> None:
        plan = (
            plan_http_transport(
                built.mcp,
                built.settings,
                credential_verifier=built.services["credential_verifier"]
                if built.settings.brain_mcp_auth_mode == "credentials"
                else None,
            )
            if built.settings.brain_mcp_transport == "http"
            else None
        )
        async with app_lifecycle(built.settings, built.services, built.metrics_collector):
            await _run_mcp(
                built.mcp,
                built.settings,
                metrics_collector=built.metrics_collector,
                http_plan=plan,
            )

    log_server_starting(built.settings)
    asyncio.run(run_server())
