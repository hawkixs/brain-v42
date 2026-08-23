"""MetricsServer — lightweight aiohttp HTTP sidecar for metrics."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import Callable
from typing import Any, cast

import structlog
from aiohttp import web
from aiohttp.helpers import parse_mimetype

from brain_v42.automation.webhook import (
    GitLabWebhookEndpoint,
    ProjectKeyResolver,
)
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import (
    MAX_OBSERVATION_BYTES,
    decode_observations,
)
from brain_v42.metrics.cockpit import CockpitCollector
from brain_v42.metrics.codex_telemetry import (
    MAX_IN_FLIGHT_REQUESTS,
    MAX_REQUEST_BYTES,
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)
from brain_v42.metrics.collector import MetricsCollector

logger = structlog.get_logger(__name__)

_OTLP_READ_CHUNK_BYTES = 64 * 1024
_OTLP_BODY_READ_TIMEOUT_SECONDS = 5.0
"""Budget TOTAL de lecture d'un corps, même valeur et même forme que le shim d'embedding.

Total et non par morceau : ``asyncio.timeout`` est posé à l'EXTÉRIEUR de la boucle, sinon
un émetteur envoyant un octet toutes les quatre secondes passerait indéfiniment.

Toujours lue dans le corps de la fonction, jamais en valeur par défaut d'argument — une
valeur par défaut est liée au moment du ``def``, et un test qui la substitue n'aurait alors
aucun effet tout en semblant mesurer la garde.
"""
_OTLP_ERROR_STATUSES = {
    400: (3, "invalid OTLP JSON payload"),
    403: (7, "OTLP receiver requires a loopback peer"),
    408: (4, "OTLP receiver timed out reading the request body"),
    413: (8, "OTLP JSON payload exceeds receiver limits"),
    415: (3, "unsupported OTLP request representation"),
    503: (14, "OTLP receiver is busy"),
}


class _BodyReadTimeout(Exception):
    """Le corps a cessé d'arriver avant la fin du budget de lecture.

    Type dédié plutôt qu'un ``except TimeoutError`` dans le handler : ``TimeoutError``
    dérive d'``OSError``, donc un tel except attraperait aussi un délai de socket surgi
    d'ailleurs et le maquillerait en 408.
    """


def _is_loopback_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return False


def _is_loopback_bind(value: str) -> bool:
    return value.casefold() == "localhost" or _is_loopback_ip(value)


def _has_loopback_tcp_peer(request: web.Request) -> bool:
    transport = request.transport
    if transport is None:
        return False
    peername = transport.get_extra_info("peername")
    if not isinstance(peername, tuple) or not peername:
        return False
    peer_host = peername[0]
    return isinstance(peer_host, str) and _is_loopback_ip(peer_host)


def _accepts_otlp_json(request: web.Request) -> bool:
    content_types = request.headers.getall("Content-Type", ())
    if len(content_types) != 1:
        return False
    media_type = parse_mimetype(content_types[0])
    if media_type.type != "application" or media_type.subtype != "json" or media_type.suffix:
        return False
    parameters = media_type.parameters
    if not parameters:
        return True
    return len(parameters) == 1 and "charset" in parameters and bool(parameters["charset"].strip())


def _accepts_identity_encoding(request: web.Request) -> bool:
    encodings = request.headers.getall("Content-Encoding", ())
    return not encodings or (len(encodings) == 1 and encodings[0].strip().casefold() == "identity")


ACCESS_LOG_EVENT = "metrics_server.receiver_rejected"

RECEIVER_CODEX_LOGS = "codex_logs"
RECEIVER_CLAUDE_LOGS = "claude_logs"
RECEIVER_CLIENT_ACTIVITY = "client_activity"


def _log_receiver_rejection(receiver: str, status: int, reason: str) -> None:
    """Journalise un rejet servi, sans jamais rien réintroduire ni rien casser.

    TROIS CONSTANTES ET RIEN D'AUTRE. Ce composant hache les identifiants bruts à la
    réception, avec un secret par processus : c'est sa raison d'être. Une ligne
    d'access log qui porterait l'adresse du pair, un en-tête, un identifiant de trace
    ou un fragment de corps reconstituerait précisément ce que le hachage retire —
    et elle le ferait en clair, dans le journal, hors de toute rétention. Les trois
    champs émis appartiennent donc à des ensembles clos et connus d'avance :
    ``receiver`` est choisi par le SITE D'APPEL (jamais lu dans la requête),
    ``status`` est une clé de ``_OTLP_ERROR_STATUSES``, et ``reason`` est le message
    statique que la réponse renvoie déjà au client. Rien ici n'est influençable par
    l'émetteur.

    ``suppress`` et non un ``try/except`` bavard : l'observation ne doit jamais
    devenir la panne de ce qu'elle observe — même règle que l'émetteur côté MCP
    (ticket ``1c40c36a``). Et surtout pas ici, où le pire moment est exactement celui
    que l'instrument sert à mesurer : la saturation. L'appel est synchrone et sans
    point d'attente, donc il n'ajoute ni verrou ni ordonnancement au chemin de rejet.
    ``Exception`` et non ``BaseException`` : un ``CancelledError`` doit continuer de
    remonter, sinon on avalerait l'annulation de la requête elle-même.
    """
    with contextlib.suppress(Exception):
        logger.warning(ACCESS_LOG_EVENT, receiver=receiver, status=status, reason=reason)


def _otlp_error(status: int, *, receiver: str) -> web.Response:
    """Seul constructeur des réponses de rejet — donc seul endroit où journaliser.

    ``receiver`` est un mot-clé OBLIGATOIRE : un septième code ajouté à la table ne
    peut pas arriver dans le journal par oubli, parce qu'il ne peut pas être construit
    sans nommer son récepteur. La couverture tient par construction, pas par vigilance.
    """
    rpc_code, message = _OTLP_ERROR_STATUSES[status]
    _log_receiver_rejection(receiver, status, message)
    headers = {"Retry-After": "1"} if status == 503 else None
    return web.json_response(
        {"code": rpc_code, "message": message, "details": []},
        status=status,
        headers=headers,
    )


async def _read_bounded_otlp_body(request: web.Request, max_bytes: int) -> bytes:
    content_length = request.content_length
    if content_length is not None and content_length > max_bytes:
        raise CodexTelemetryLimitError

    payload = bytearray()
    try:
        async with asyncio.timeout(_OTLP_BODY_READ_TIMEOUT_SECONDS):
            async for chunk in request.content.iter_chunked(_OTLP_READ_CHUNK_BYTES):
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise CodexTelemetryLimitError
    except TimeoutError as exc:
        raise _BodyReadTimeout from exc
    return bytes(payload)


class MetricsServer:
    """Aiohttp-based HTTP server exposing GET /metrics.

    Optionally exposes POST /gitlab/webhook when a ``gitlab_ingestor`` is provided.
    Started as an asyncio task alongside the MCP stdio server.
    """

    def __init__(
        self,
        collector: MetricsCollector,
        embedding_svc: Any,
        port: int = 9200,
        host: str = "127.0.0.1",
        gitlab_ingestor: Any | None = None,
        project_key_resolver: ProjectKeyResolver | None = None,
        webhook_secret: str = "",
        graph_svc: Any | None = None,
        codex_registry: ClientActivityRegistry | None = None,
    ) -> None:
        self._collector = collector
        self._embedding_svc = embedding_svc
        self._port = port
        self._host = host
        self._gitlab_ingestor = gitlab_ingestor
        self._project_key_resolver = project_key_resolver
        self._webhook_secret = webhook_secret
        self._graph_svc = graph_svc
        self._codex_registry = (
            ClientActivityRegistry() if codex_registry is None else codex_registry
        )
        self._codex_request_slots = asyncio.Semaphore(MAX_IN_FLIGHT_REQUESTS)
        self._runner: web.AppRunner | None = None
        self._cockpit: CockpitCollector | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/api/cockpit", self._handle_cockpit)
        if _is_loopback_bind(self._host):
            app.router.add_post("/v1/logs", self._handle_codex_logs)
            app.router.add_post("/v1/logs/claude", self._handle_claude_logs)
            app.router.add_post("/v1/client-activity", self._handle_client_activity)
        if self._gitlab_ingestor:
            if not self._webhook_secret:
                logger.warning(
                    "metrics_server.webhook_no_secret",
                    detail="GITLAB_WEBHOOK_SECRET unset — /gitlab/webhook fails closed (401)",
                )
            app.router.add_post("/gitlab/webhook", self._handle_webhook)
        return app

    async def _handle_bounded_receiver(
        self,
        request: web.Request,
        *,
        receiver: str,
        max_bytes: int,
        apply: Callable[[bytes], None],
    ) -> web.Response:
        """Run the loopback receiver hardening, then apply a validated body.

        Shared by every local push receiver so a new route cannot quietly ship
        with a weaker posture than ``/v1/logs``: loopback peer only, single
        identity representation, bounded body, capped in-flight requests, and a
        fail-closed ``apply`` that validates the whole batch before mutating
        anything. ``max_bytes`` is per route — the brain-side wire format is far
        smaller than an OTLP envelope and is bounded for itself.
        """
        if not _has_loopback_tcp_peer(request):
            return _otlp_error(403, receiver=receiver)

        if not _accepts_identity_encoding(request):
            return _otlp_error(415, receiver=receiver)
        if not _accepts_otlp_json(request):
            return _otlp_error(415, receiver=receiver)
        if request.content_length is not None and request.content_length > max_bytes:
            return _otlp_error(413, receiver=receiver)
        if self._codex_request_slots.locked():
            return _otlp_error(503, receiver=receiver)

        await self._codex_request_slots.acquire()
        try:
            try:
                payload = await _read_bounded_otlp_body(request, max_bytes)
                apply(payload)
            except _BodyReadTimeout:
                # Le créneau est rendu par le `finally` ci-dessous : c'est ce qui
                # empêche quatre corps figés de tuer les trois récepteurs à vie.
                return _otlp_error(408, receiver=receiver)
            except CodexTelemetryLimitError:
                return _otlp_error(413, receiver=receiver)
            except CodexTelemetryMalformedError:
                return _otlp_error(400, receiver=receiver)
        finally:
            self._codex_request_slots.release()
        return web.json_response({})

    async def _handle_codex_logs(self, request: web.Request) -> web.Response:
        """Accept a bounded OTLP/HTTP JSON batch from a local Codex client."""
        return await self._handle_bounded_receiver(
            request,
            max_bytes=MAX_REQUEST_BYTES,
            receiver=RECEIVER_CODEX_LOGS,
            apply=self._codex_registry.ingest_otlp_json,
        )

    async def _handle_claude_logs(self, request: web.Request) -> web.Response:
        """Accept a bounded OTLP/HTTP JSON batch from a local Claude Code client.

        A route of its own rather than one receiver guessing the schema:
        guessing would mean probing the attributes of a payload that has not
        been validated yet.
        """
        return await self._handle_bounded_receiver(
            request,
            max_bytes=MAX_REQUEST_BYTES,
            receiver=RECEIVER_CLAUDE_LOGS,
            apply=self._codex_registry.ingest_claude_otlp_json,
        )

    async def _handle_client_activity(self, request: web.Request) -> web.Response:
        """Accept a bounded batch of brain-side observations from the MCP process."""
        return await self._handle_bounded_receiver(
            request,
            max_bytes=MAX_OBSERVATION_BYTES,
            receiver=RECEIVER_CLIENT_ACTIVITY,
            apply=self._apply_observations,
        )

    def _apply_observations(self, payload: bytes) -> None:
        """Decode the whole batch before touching the registry."""
        self._codex_registry.record_observations(decode_observations(payload))

    async def _handle_cockpit(self, request: web.Request) -> web.Response:
        """Handle GET /api/cockpit — 2s-poll cockpit payload for red-monitor."""
        if self._cockpit is None:
            self._cockpit = CockpitCollector(
                collector=self._collector,
                session_factory=self._collector._session_factory,
                codex_registry=self._codex_registry,
            )
        payload = await self._cockpit.snapshot()
        return web.json_response(payload)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle GET /metrics — assemble full metrics JSON.

        Merges in-memory metrics with DB-aggregated cross-process data:
        - search_quality from search_log table (last 24h)
        - tool/embedding stats aggregated from all active process_metrics rows
        - DB row counts, coverage, pool stats from collect_db_stats()
        """
        metrics = self._collector.get_metrics()

        # DB stats (async)
        db_stats = await self._collector.collect_db_stats()
        metrics["database"] = db_stats

        # DB-aggregated search quality (replaces in-memory which was always zero)
        metrics["search_quality"] = await self._collector.collect_search_quality()

        # Cross-process aggregated tool/embedding stats
        process_agg = await self._collector.collect_process_metrics()

        # Override per-process tools/embedding/reranker with cross-process aggregation
        # when multiple processes are active
        if process_agg["active_processes"] > 0:
            agg_tools = process_agg["tools"]
            # Extract _reranker pseudo-tool into top-level reranker section
            reranker_agg = agg_tools.pop("_reranker", None)
            if reranker_agg:
                metrics["reranker"] = {
                    "total_calls": reranker_agg["calls"],
                    "total_errors": reranker_agg["errors"],
                    "recent_errors": reranker_agg["recent_errors"],
                    "total_candidates": reranker_agg.get("total_candidates", 0),
                    "avg_latency_ms": reranker_agg["avg_latency_ms"],
                }
            # Extract _graph pseudo-tool into top-level graph section
            graph_agg = agg_tools.pop("_graph", None)
            if graph_agg:
                metrics["graph"] = {
                    "total_queries": graph_agg["calls"],
                    "total_errors": graph_agg["errors"],
                    "recent_errors": graph_agg["recent_errors"],
                    "avg_latency_ms": graph_agg["avg_latency_ms"],
                }
            # Drop _cost/_buckets pseudo-tools: they duplicate top-level sections
            # assembled from in-memory state; merging them would double-count.
            agg_tools.pop("_cost", None)
            agg_tools.pop("_buckets", None)
            # Extract _decay into the top-level decay section: the sidecar's
            # in-memory collector never sees MCP-process decay stats, so the
            # cross-process values persisted by MetricsFlusher are authoritative.
            decay_agg = agg_tools.pop("_decay", None)
            if decay_agg is not None:
                metrics["decay"] = decay_agg
            metrics["tools"] = agg_tools
            emb_agg = process_agg["embedding"]
            metrics["embedding_service"]["total_requests"] = emb_agg["total_requests"]
            metrics["embedding_service"]["total_errors"] = emb_agg["total_errors"]
            metrics["embedding_service"]["recent_errors"] = emb_agg["recent_errors"]
            metrics["embedding_service"]["avg_latency_ms"] = emb_agg["avg_latency_ms"]

        metrics["cross_process"] = process_agg

        # Embedding service health
        try:
            healthy = await self._embedding_svc.healthcheck()
            metrics["embedding_service"]["status"] = "up" if healthy else "down"
        except Exception:
            metrics["embedding_service"]["status"] = "down"

        # Graph service health (optional — only if graph_svc provided)
        if self._graph_svc is not None:
            try:
                graph_healthy = await self._graph_svc.healthcheck()
            except Exception:
                graph_healthy = False
            graph_stats = metrics.get("graph", {})
            inventory = await self._collector.collect_graph_inventory(self._graph_svc)
            metrics["graph"] = {
                "status": "up" if graph_healthy else "down",
                "total_queries": graph_stats.get("total_queries", 0),
                "total_errors": graph_stats.get("total_errors", 0),
                "recent_errors": graph_stats.get("recent_errors", 0),
                "avg_latency_ms": graph_stats.get("avg_latency_ms", 0.0),
                "nodes_total": inventory.get("nodes_total", {}),
                "edges_total": inventory.get("edges_total", {}),
                "orphans_total": inventory.get("orphans_total", {}),
            }

        # Dream run metrics — merge run-level data + cumulative promotions counts
        # + per-target post-promotion health (ADR #4 v2 telemetry).
        dream_metrics = await self._collector.collect_dream_metrics()
        if dream_metrics:
            promo_counts = await self._collector.collect_dream_promotions()
            if promo_counts:
                dream_metrics["promotions"] = {
                    "total": sum(promo_counts.values()),
                    "by_type": promo_counts,
                }
            promoted_health = await self._collector.collect_dream_promoted_health()
            if promoted_health:
                dream_metrics["promoted_health"] = promoted_health
            metrics["dream"] = dream_metrics

        # Nightly-ops (killswitches, review roadmap/extract, dernier échec) —
        # consommé par le panel nightly-ops de red-monitor (ticket de1ad785).
        nightly = await self._collector.collect_nightly_ops()
        if nightly:
            metrics["nightly"] = nightly

        return web.json_response(metrics)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Delegate POST /gitlab/webhook to the shared automation endpoint."""
        endpoint = GitLabWebhookEndpoint(
            self._gitlab_ingestor,
            cast(ProjectKeyResolver, self._project_key_resolver),
            self._webhook_secret,
        )
        return await endpoint.handle(request)

    async def start(self) -> None:
        """Start the HTTP server.

        If the port is already in use (another MCP process owns it),
        logs a warning and continues without the sidecar.
        """
        app = self._build_app()
        self._runner = web.AppRunner(app, auto_decompress=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            logger.warning(
                "metrics_server.port_in_use",
                host=self._host,
                port=self._port,
                error=str(exc),
            )
            await self._runner.cleanup()
            self._runner = None
            return
        logger.info(
            "metrics_server.started",
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._runner is not None:
            await self._runner.cleanup()
            logger.info("metrics_server.stopped")
