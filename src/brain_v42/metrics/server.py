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
from brain_v42.metrics.slow_block_cache import SlowBlockCache

logger = structlog.get_logger(__name__)

_OTLP_READ_CHUNK_BYTES = 64 * 1024
_OTLP_BODY_READ_TIMEOUT_SECONDS = 5.0
"""TOTAL budget for reading a body, same value and same shape as the embedding shim.

Total and not per chunk: ``asyncio.timeout`` is placed OUTSIDE the loop, otherwise a
sender pushing one byte every four seconds would pass indefinitely.

Always read inside the function body, never as an argument default — a default is bound at
``def`` time, and a test substituting it would then have no effect while appearing to
measure the guard.
"""
_OTLP_ERROR_STATUSES = {
    400: (3, "invalid OTLP JSON payload"),
    403: (7, "OTLP receiver requires a loopback peer"),
    408: (4, "OTLP receiver timed out reading the request body"),
    413: (8, "OTLP JSON payload exceeds receiver limits"),
    415: (3, "unsupported OTLP request representation"),
    503: (14, "OTLP receiver is busy"),
}


class NonLoopbackReceiversError(RuntimeError):
    """``fail_closed`` posture: a non-loopback bind refuses to build the app.

    Raised at CONSTRUCTION and never later: an operator who chose this posture
    prefers a startup that fails while naming their setting to a sidecar running
    without its receivers.
    """


class _BodyReadTimeout(Exception):
    """The body stopped arriving before the read budget ran out.

    A dedicated type rather than an ``except TimeoutError`` in the handler:
    ``TimeoutError`` derives from ``OSError``, so such an except would also catch a socket
    timeout arising elsewhere and disguise it as a 408.
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


class ReceiverRejectionCounters:
    """Per-(receiver, code) counters of served rejections — track (b) of `d5e4bd73`.

    The ticket's lesson governs the shape: "a zero counter on a source that
    counts nothing is indistinguishable from a real zero". The three receivers
    are therefore present from construction, each empty: the PRESENCE of the
    structure in ``GET /metrics`` proves the instrument is armed, its contents
    say what it saw. No identifier, no address: the keys belong to the same
    closed sets as the access log.
    """

    def __init__(self) -> None:
        self._counts: dict[str, dict[int, int]] = {
            RECEIVER_CODEX_LOGS: {},
            RECEIVER_CLAUDE_LOGS: {},
            RECEIVER_CLIENT_ACTIVITY: {},
        }

    def increment(self, receiver: str, status: int) -> None:
        by_status = self._counts.setdefault(receiver, {})
        by_status[status] = by_status.get(status, 0) + 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Serializable copy, statuses as strings — the shape of the JSON served."""
        return {
            receiver: {str(status): count for status, count in sorted(by_status.items())}
            for receiver, by_status in self._counts.items()
        }


def _log_receiver_rejection(receiver: str, status: int, reason: str) -> None:
    """Log a served rejection, never reintroducing anything and never breaking anything.

    THREE CONSTANTS AND NOTHING ELSE. This component hashes raw identifiers on
    reception, with a per-process secret: that is its whole purpose. An access log line
    carrying the peer address, a header, a trace identifier or a body fragment would
    reconstitute precisely what the hashing removes — and it would do so in clear, in
    the log, outside any retention. The three emitted fields therefore belong to closed
    sets known in advance: ``receiver`` is chosen by the CALL SITE (never read from the
    request), ``status`` is a key of ``_OTLP_ERROR_STATUSES``, and ``reason`` is the
    static message the response already returns to the client. Nothing here can be
    influenced by the sender.

    ``suppress`` and not a chatty ``try/except``: observation must never become the
    failure of what it observes — same rule as the MCP-side emitter (ticket
    ``1c40c36a``). And least of all here, where the worst moment is exactly the one the
    instrument exists to measure: saturation. The call is synchronous and has no await
    point, so it adds neither a lock nor a scheduling step to the rejection path.
    ``Exception`` and not ``BaseException``: a ``CancelledError`` must keep propagating,
    otherwise we would swallow the cancellation of the request itself.
    """
    with contextlib.suppress(Exception):
        logger.warning(ACCESS_LOG_EVENT, receiver=receiver, status=status, reason=reason)


def _otlp_error(status: int, *, receiver: str, counters: ReceiverRejectionCounters) -> web.Response:
    """The only builder of rejection responses — hence the only place to observe.

    ``receiver`` and ``counters`` are MANDATORY keywords: a seventh code added to the
    table cannot reach the log or the counters by oversight, because it cannot be built
    without naming them. Coverage holds by construction, not by vigilance. Counting
    lives under ``suppress`` for the same reason as the log: the instrument never
    becomes the failure of what it observes — least of all under saturation, which it
    exists to measure.
    """
    rpc_code, message = _OTLP_ERROR_STATUSES[status]
    with contextlib.suppress(Exception):
        counters.increment(receiver, status)
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
        nonloopback_posture: str = "silent",
        allow_non_loopback: bool = False,
        slow_block_cache_ttl_seconds: float = 30.0,
        slow_block_cache_error_ttl_seconds: float = 5.0,
        slow_block_cache: SlowBlockCache | None = None,
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
        self._rejection_counters = ReceiverRejectionCounters()
        self._nonloopback_posture = nonloopback_posture
        self._allow_non_loopback = allow_non_loopback
        # Decision 1669d429 item 2: dream/nightly/graph-inventory are memoized behind
        # a TTL + single-flight cache (`database` and the embedding healthcheck stay
        # live, uncached, below). `slow_block_cache` is an injection point for tests
        # that need a controllable clock; production wiring passes the TTLs instead
        # (`runtime.py`), which come from `Settings`.
        self._slow_block_cache = slow_block_cache or SlowBlockCache(
            ttl_seconds=slow_block_cache_ttl_seconds,
            error_ttl_seconds=slow_block_cache_error_ttl_seconds,
        )

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/api/cockpit", self._handle_cockpit)
        # Always registered, including on a bind that has no receivers: it is the
        # ONLY place a monitor can read that they are missing. The startup line
        # below says it once, to whoever reads a boot; nobody greps a log to build
        # an alert.
        app.router.add_get("/healthz", self._handle_healthz)
        if _is_loopback_bind(self._host):
            app.router.add_post("/v1/logs", self._handle_codex_logs)
            app.router.add_post("/v1/logs/claude", self._handle_claude_logs)
            app.router.add_post("/v1/client-activity", self._handle_client_activity)
        elif self._nonloopback_posture == "fail_closed":
            # Posture (1) of eac03668: the operator preferred a startup that
            # fails while naming their setting to a crippled sidecar that stays
            # silent.
            raise NonLoopbackReceiversError(
                f"metrics bind {self._host!r} is not loopback: the three POST "
                "receivers cannot be served safely there and "
                "METRICS_NONLOOPBACK_POSTURE=fail_closed refuses to run without "
                "them. Bind loopback, or choose the posture that says so "
                "(warn) or the historical silence (silent)."
            )
        elif self._allow_non_loopback or self._nonloopback_posture == "warn":
            # Said once, at startup, never per request — the per-request refusal
            # comes from the router and is invisible from here.
            #
            # Two ways in, and the first is the one that matters now. The OPT-IN
            # (eac03668, arbitrated 2026-09-03): a non-loopback bind is refused by
            # `Settings` unless someone named `METRICS_ALLOW_NON_LOOPBACK`, so
            # reaching here means they did — and what they bought has to be said.
            # The `warn` POSTURE remains for the deployment that pinned it before
            # the arbitration; production is one of them.
            logger.warning(
                "metrics_server.receivers_disabled_non_loopback",
                host=self._host,
                absent_routes=["/v1/logs", "/v1/logs/claude", "/v1/client-activity"],
                detail=(
                    "non-loopback bind: OTLP/activity receivers are NOT registered; "
                    "their clients get router 404s that no access log or counter sees"
                ),
            )
        # `silent`: the historical behaviour, to the byte — the DEFAULT posture
        # until the operator decision (eac03668) is made.
        if self._gitlab_ingestor:
            if not self._webhook_secret:
                logger.warning(
                    "metrics_server.webhook_no_secret",
                    detail="GITLAB_WEBHOOK_SECRET unset — /gitlab/webhook fails closed (401)",
                )
            app.router.add_post("/gitlab/webhook", self._handle_webhook)
        return app

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        """Whether the three POST receivers are being served, in one field.

        `ingest_receivers` is the thing an operator cannot otherwise observe: on a
        non-loopback bind the routes are absent, so their clients get router 404s
        that no access log and no rejection counter of this process ever sees. A
        log line at startup is not readable by a monitor; this is.

        The bind address is deliberately NOT echoed: this endpoint is served on
        the very bind it describes, including a LAN one.
        """
        return web.json_response(
            {
                "status": "ok",
                "ingest_receivers": ("enabled" if _is_loopback_bind(self._host) else "disabled"),
            }
        )

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
            return _otlp_error(403, receiver=receiver, counters=self._rejection_counters)

        if not _accepts_identity_encoding(request):
            return _otlp_error(415, receiver=receiver, counters=self._rejection_counters)
        if not _accepts_otlp_json(request):
            return _otlp_error(415, receiver=receiver, counters=self._rejection_counters)
        if request.content_length is not None and request.content_length > max_bytes:
            return _otlp_error(413, receiver=receiver, counters=self._rejection_counters)
        if self._codex_request_slots.locked():
            return _otlp_error(503, receiver=receiver, counters=self._rejection_counters)

        await self._codex_request_slots.acquire()
        try:
            try:
                payload = await _read_bounded_otlp_body(request, max_bytes)
                apply(payload)
            except _BodyReadTimeout:
                # The slot is returned by the `finally` below: that is what
                # stops four frozen bodies from killing the three receivers for
                # good.
                return _otlp_error(408, receiver=receiver, counters=self._rejection_counters)
            except CodexTelemetryLimitError:
                return _otlp_error(413, receiver=receiver, counters=self._rejection_counters)
            except CodexTelemetryMalformedError:
                return _otlp_error(400, receiver=receiver, counters=self._rejection_counters)
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

    async def _compute_dream_block(self) -> dict[str, Any]:
        """Assemble the `dream` block from its three collector calls.

        Passed whole to `SlowBlockCache.get`: the cache memoizes the COMBINED
        result (and its single `generated_at`), not each call separately —
        otherwise a partial refresh could mix a fresh `last_run` with a stale
        `promotions` sub-block.
        """
        dream_metrics = await self._collector.collect_dream_metrics()
        if not dream_metrics:
            return {}
        promo_counts = await self._collector.collect_dream_promotions()
        if promo_counts:
            dream_metrics["promotions"] = {
                "total": sum(promo_counts.values()),
                "by_type": promo_counts,
            }
        promoted_health = await self._collector.collect_dream_promoted_health()
        if promoted_health:
            dream_metrics["promoted_health"] = promoted_health
        return dream_metrics

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle GET /metrics — assemble full metrics JSON.

        Merges in-memory metrics with DB-aggregated cross-process data:
        - search_quality from search_log table (last 24h)
        - tool/embedding stats aggregated from all active process_metrics rows
        - DB row counts, coverage, pool stats from collect_db_stats()
        """
        metrics = self._collector.get_metrics()

        # Rejections served by the POST receivers — the structure is always
        # present, three receivers, so that its zero means something (d5e4bd73:
        # a zero on a source that counts nothing says nothing).
        metrics["receiver_rejections"] = self._rejection_counters.snapshot()

        # What the activity registry threw away, by cause, plus its occupancy —
        # the reopening condition of 863ff2ca reads BOTH: occupancy above 48 of
        # 64 sustained over 24 h, or any bearing eviction at all. Same path and
        # same always-present shape as the rejections above, for the same reason.
        metrics["client_activity_registry"] = self._codex_registry.eviction_counters()

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
            # decay: the sidecar's in-memory collector never sees MCP-process decay
            # stats, so the cross-process value is authoritative. It is a DB-wide
            # GAUGE (latest-row-wins, never summed — 04c09575) and collect_process_metrics
            # already reduces it that way and returns it split out of "tools", so there
            # is nothing left to pop here. (Removed from process_agg entirely, below,
            # right before cross_process is published — decay stays top-level only.)
            metrics["decay"] = process_agg["decay"]
            metrics["tools"] = agg_tools
            emb_agg = process_agg["embedding"]
            metrics["embedding_service"]["total_requests"] = emb_agg["total_requests"]
            metrics["embedding_service"]["total_errors"] = emb_agg["total_errors"]
            metrics["embedding_service"]["recent_errors"] = emb_agg["recent_errors"]
            metrics["embedding_service"]["avg_latency_ms"] = emb_agg["avg_latency_ms"]
            metrics["embedding_service"]["usage"] = emb_agg.get(
                "usage",
                {
                    "read": {"total_tokens": 0, "reported_requests": 0},
                    "write": {"total_tokens": 0, "reported_requests": 0},
                },
            )
            # embedding identity (ticket 3a4ed612): a LIVE MCP process's own
            # {model, backend, endpoint_host, models_seen} outranks the sidecar's
            # own settings that collector.get_metrics() already put here as the
            # labelled fallback. No live identity (fresh deploy, or every live
            # row still pre-dates this feature) -> leave those defaults alone,
            # model_source stays "sidecar_settings".
            identity = process_agg.get("embedding_identity")
            if identity is not None:
                metrics["embedding_service"]["model"] = identity["model"]
                metrics["embedding_service"]["backend"] = identity["backend"]
                metrics["embedding_service"]["endpoint_host"] = identity["endpoint_host"]
                metrics["embedding_service"]["models_seen"] = identity["models_seen"]
                metrics["embedding_service"]["model_source"] = "live_processes"

        # decay/embedding_identity are published only via the top-level
        # embedding_service (set above when active): strip them from the raw
        # cross_process block unconditionally, so an inactive process_agg
        # (structural zeros, active_processes == 0) doesn't leak a duplicate
        # copy either, and cross_process never grows a payload key the operator
        # decision for this lot didn't agree to.
        process_agg.pop("decay", None)
        process_agg.pop("embedding_identity", None)
        metrics["cross_process"] = process_agg

        # Embedding service health
        try:
            healthy = await self._embedding_svc.healthcheck()
            metrics["embedding_service"]["status"] = "up" if healthy else "down"
        except Exception:
            metrics["embedding_service"]["status"] = "down"

        # Graph service health (optional — only if graph_svc provided). The
        # healthcheck stays LIVE on every poll; only the inventory (Neo4j counts +
        # a PG orphan scan) goes through the slow-block cache (1669d429 item 2).
        if self._graph_svc is not None:
            try:
                graph_healthy = await self._graph_svc.healthcheck()
            except Exception:
                graph_healthy = False
            graph_stats = metrics.get("graph", {})
            inventory = await self._slow_block_cache.get(
                "graph_inventory", lambda: self._collector.collect_graph_inventory(self._graph_svc)
            )
            inventory = inventory or {}
            metrics["graph"] = {
                "status": "up" if graph_healthy else "down",
                "total_queries": graph_stats.get("total_queries", 0),
                "total_errors": graph_stats.get("total_errors", 0),
                "recent_errors": graph_stats.get("recent_errors", 0),
                "avg_latency_ms": graph_stats.get("avg_latency_ms", 0.0),
                "nodes_total": inventory.get("nodes_total", {}),
                "edges_total": inventory.get("edges_total", {}),
                "orphans_total": inventory.get("orphans_total", {}),
                "generated_at": inventory.get("generated_at"),
            }

        # Dream run metrics — merge run-level data + cumulative promotions counts
        # + per-target post-promotion health (ADR #4 v2 telemetry). Assembled
        # inside one `compute` so the cache memoizes the three collector calls
        # (and their generated_at) as a single block (1669d429 item 2).
        dream_metrics = await self._slow_block_cache.get("dream", self._compute_dream_block)
        if dream_metrics:
            metrics["dream"] = dream_metrics

        # Nightly-ops (killswitches, roadmap/extract review, last failure) —
        # consumed by red-monitor's nightly-ops panel (ticket de1ad785).
        # Resolved through a lambda, not a bare `self._collector.collect_nightly_ops`
        # reference: the ATTRIBUTE LOOKUP itself must happen inside the cache's
        # protected `compute()` call, not while building this call's arguments --
        # a collector double that lacks the method must degrade this block to
        # absent, never 500 the whole endpoint (a real incident: a `MinimalCollector`
        # test double raised AttributeError here, outside any try/except).
        nightly = await self._slow_block_cache.get(
            "nightly", lambda: self._collector.collect_nightly_ops()
        )
        if nightly:
            metrics["nightly"] = nightly

        # Per-project ticket counters (ticket 0fb857ef) — replaces red-monitor's
        # abandoned roadmap tab. Categorisation is brain's alone: collect_ticket_counts
        # (collector_tickets.py) reuses list_grouped's own _ACTIONABLE/_CONFIRMABLE
        # predicates, red-monitor renders whatever it receives with no status logic
        # of its own. `projects: []` (truthy dict) still publishes the key — only a
        # raised exception (caught by the cache, see slow_block_cache.py) leaves
        # "tickets" absent from the payload. Same lambda-deferral reasoning as
        # "nightly" just above: a collector lacking the method must degrade this
        # block to absent, not 500 the whole endpoint.
        tickets_block = await self._slow_block_cache.get(
            "tickets", lambda: self._collector.collect_ticket_counts()
        )
        if tickets_block:
            metrics["tickets"] = tickets_block

        # Deprecated top-level alias (ticket 3a4ed612): re-synced here, after any
        # cross-process override above, so it never drifts from the field it
        # mirrors -- get_metrics() only had the sidecar's own pre-override value.
        # Guarded: a test double stubbing get_metrics() with a bare
        # {"embedding_service": {}} must not crash a handler it isn't exercising.
        embedding_service_block = metrics.get("embedding_service", {})
        if "model" in embedding_service_block:
            metrics["model"] = embedding_service_block["model"]

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
