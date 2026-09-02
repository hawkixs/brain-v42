"""OpenTelemetry spans for tool calls — on a PRIVATE provider.

WHY NEVER ``trace.set_tracer_provider()``. Verified in the venv on 2026-08-12:
FastMCP carries its own telemetry. ``fastmcp/server/telemetry.py``
(``server_span``, line 57) does, on failure ::

    span.record_exception(e)                           # args + stacktrace
    span.set_status(Status(StatusCode.ERROR, str(e)))  # message brut

and sets ``enduser.id`` — the principal of the Dream capability bearer —
through ``get_auth_span_attributes()`` (line 85). Its tracer comes from
``fastmcp/telemetry.py::get_tracer`` (line 38), which calls
``otel_get_tracer(INSTRUMENTATION_NAME)``: it reads the **GLOBAL** provider.

Installing a global provider would therefore arm that block. But
``business_errors.py:101`` does ``raise ToolError(str(exc)) from exc``:
``str(e)`` IS the raw business message, and ``record_exception`` serializes a
stacktrace that follows ``__cause__``. The secret that
``test_decorator_does_not_log_authorization_failure_context`` exists to hold back
would come out through that channel. Hence a private provider, pinned by a test
that reads this source.

WHY THE SDK IS IMPORTED ONLY INSIDE THE FUNCTIONS. It is an OPTIONAL dependency
(the ``tracing`` extra). A module-level import would make the server fail to
start everywhere the extra is not installed — which is to say, today, in CI and
in production.
"""

from __future__ import annotations

import logging
from typing import Any

from brain_v42.provenance import UNKNOWN_ACTOR

logger = logging.getLogger(__name__)

#: Cardinality cap on ``brain.actor``. Same value and same reason as
#: ``MetricsCollector._MAX_AGENTS`` (collector.py:130): ``X-Brain-Agent`` is
#: declared by the client hence falsifiable, and a span attribute has NO native
#: cap. A sampler does not replace this bound — it bounds VOLUME, not the number
#: of DISTINCT values. A cap deliberately independent of the collector's: reusing
#: its private state would cost a coupling worse than the divergence, since both
#: are bounded.
MAX_TRACED_ACTORS = 32
OVERFLOW_ACTOR = "_overflow"

_SPAN_OPERATION = "execute_tool"

_tracer: Any | None = None
#: The private provider, kept so it can be drained AT SHUTDOWN. Without this
#: reference, `shutdown_on_exit=False` would silently lose everything left in the
#: BatchSpanProcessor's queue — we would have traded a shutdown that drags for
#: spans that disappear.
_provider: Any | None = None
_known_actors: set[str] = set()


def set_tracer(tracer: Any | None) -> None:
    """Set the current tracer — the tests' injection point.

    Separate from ``reset_actor_cardinality`` on purpose: a helper doing both
    would create a hidden coupling between "I change tracer" and "I forget the
    actors seen", two unrelated gestures.
    """
    global _tracer
    _tracer = tracer


def get_tracer() -> Any | None:
    """The current tracer, or ``None`` when tracing is closed.

    Returning ``None`` rather than a no-op tracer is what makes the killswitch
    actually CUT: the OTel API happily returns a no-op, but we would still pay
    for building the attributes on a path that runs on every tool call.
    """
    return _tracer


def reset_actor_cardinality() -> None:
    """Clear the registry of actors already seen."""
    _known_actors.clear()


def bounded_actor(actor: str | None) -> str:
    """Fold an actor into a bounded number of buckets."""
    name = (actor or UNKNOWN_ACTOR).strip() or UNKNOWN_ACTOR
    if name in _known_actors:
        return name
    if len(_known_actors) >= MAX_TRACED_ACTORS:
        return OVERFLOW_ACTOR
    _known_actors.add(name)
    return name


def _error_type(exc: BaseException, unwrap: bool) -> str:
    """The class name to publish, unwrapping the business masking.

    ``business_errors._wrap`` relays every business error as a ``ToolError``.
    Publishing that name would degenerate ``error.type`` into "ToolError" for
    every failure — precisely the defect that ruled out a middleware as the
    measurement point.
    """
    if unwrap and exc.__cause__ is not None:
        return type(exc.__cause__).__name__
    return type(exc).__name__


def start_tool_span(tool_name: str) -> Any | None:
    """Open a ROOT span for a tool call, or ``None``.

    The empty context is passed EXPLICITLY. ``start_span(name)`` without a
    context resolves the parent from the current context: a client propagating a
    ``traceparent`` would adopt us as a child and decide our sampling. This
    server measures its own calls, it is not a link in a third party's trace.
    """
    tracer = _tracer
    if tracer is None:
        return None
    try:
        from opentelemetry.context import Context  # noqa: PLC0415

        span = tracer.start_span(f"{_SPAN_OPERATION} {tool_name}", context=Context())
        span.set_attribute("gen_ai.operation.name", _SPAN_OPERATION)
        span.set_attribute("gen_ai.tool.name", tool_name)
        return span
    except Exception:
        # A probe cannot bring down what it observes. This path runs on every
        # tool call in a shared process.
        logger.debug("tracing.start_span_failed", exc_info=True)
        return None


def finish_tool_span(
    span: Any | None,
    *,
    actor: str | None,
    error: bool,
    latency_ms: float,
    exception: BaseException | None = None,
    unwrap: bool = False,
) -> None:
    """Close the span with the SAME verdict as the counter.

    ``error`` and ``latency_ms`` are those passed to ``record_tool_call``: a span
    contradicting the counter would give two truths and make both unusable.

    What NEVER enters here: the tool's arguments and result, ``str(exc)``,
    ``exc.args``, a stacktrace, the project key, the session or transport
    identifier.
    """
    if span is None:
        return
    try:
        span.set_attribute("brain.actor", bounded_actor(actor))
        span.set_attribute("brain.tool.error", error)
        span.set_attribute("brain.tool.latency_ms", round(latency_ms, 1))
        if exception is not None:
            span.set_attribute("error.type", _error_type(exception, unwrap))
    except Exception:
        logger.debug("tracing.set_attribute_failed", exc_info=True)
    try:
        span.end()
    except Exception:
        logger.debug("tracing.end_span_failed", exc_info=True)


def init_tracing(endpoint: str, *, service_name: str = "brain-v42-mcp") -> bool:
    """Arm a PRIVATE provider exporting over OTLP. Returns False if impossible.

    ``shutdown_on_exit=False`` is explicit: otherwise
    ``TracerProvider.__init__`` registers ``atexit.register(self.shutdown)``
    (verified in SDK 1.44.0, lines 1316 and 1347), and an unreachable exporter
    would then drag the server's shutdown out to its own timeout. Shutdown is
    driven by the caller, bounded, not by ``atexit``.

    Every bound is passed, including ``export_timeout_millis`` — verified active
    in ``BatchSpanProcessor.__init__`` (line 169), against the widespread
    assumption that it is inert.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError:
        # The `tracing` extra is not installed: a NORMAL state, not a failure.
        logger.info("tracing.sdk_absent endpoint=%s", endpoint)
        return False
    try:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
            shutdown_on_exit=False,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=2),
                max_queue_size=2048,
                max_export_batch_size=256,
                schedule_delay_millis=5000,
                export_timeout_millis=2000,
            )
        )
        # `provider.get_tracer`, NEVER `trace.set_tracer_provider`: see the
        # module header.
        global _provider
        _provider = provider
        set_tracer(provider.get_tracer(__name__))
        return True
    except Exception:
        logger.warning("tracing.init_failed endpoint=%s", endpoint, exc_info=True)
        return False


def shutdown_tracing(timeout_ms: int = 3000) -> None:
    """Drain the queue then close the provider, within a BOUNDED delay.

    The mandatory counterpart of ``shutdown_on_exit=False``: the SDK's
    ``atexit`` having been disabled so an unreachable exporter does not drag out
    the shutdown, it falls to the caller to drain — otherwise the spans still
    queued disappear without a word. Found by the e2e, not by re-reading.

    Never raises: a shutdown must not fail because of its telemetry.
    """
    global _provider
    provider = _provider
    _provider = None
    set_tracer(None)
    if provider is None:
        return
    try:
        provider.force_flush(timeout_ms)
    except Exception:
        logger.debug("tracing.flush_failed", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logger.debug("tracing.shutdown_failed", exc_info=True)
