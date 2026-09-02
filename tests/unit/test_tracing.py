"""OTel tracing of tool calls — and the provider we NEVER install.

THE CENTRAL TRAP, verified in the venv on 2026-08-12. FastMCP carries its OWN
telemetry: ``fastmcp/server/telemetry.py::server_span`` (line 57) does, on
failure::

    span.record_exception(e)                          # args + stacktrace
    span.set_status(Status(StatusCode.ERROR, str(e)))  # raw message

and sets ``enduser.id`` through ``get_auth_span_attributes()`` (line 85). Its
tracer comes from ``fastmcp/telemetry.py::get_tracer`` (line 38), which calls
``otel_get_tracer(INSTRUMENTATION_NAME)`` — hence the **GLOBAL** provider.

Installing a global provider would arm that block. Yet ``business_errors.py:101``
does ``raise ToolError(str(exc)) from exc``: ``str(e)`` IS the raw business
message, and ``record_exception`` serialises a stacktrace that follows
``__cause__``. The secret ``test_decorator_does_not_log_authorization_failure_context``
exists to hold back would come out through that channel.

Hence a PRIVATE provider, and a test that reads the source to pin it — it must
stay green in CI, where the SDK is not installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from brain_v42 import tracing

_SOURCE = Path(tracing.__file__).read_text(encoding="utf-8")


class _FakeSpan:
    """A span double: records what is set on it, nothing more."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.status: object | None = None
        self.recorded_exceptions: list[BaseException] = []
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: object) -> None:
        self.status = status

    def record_exception(self, exc: BaseException) -> None:  # pragma: no cover
        self.recorded_exceptions.append(exc)

    def end(self) -> None:
        self.ended = True

    def __enter__(self) -> _FakeSpan:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.end()


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.calls: list[dict[str, object]] = []

    def start_span(self, name: str, **kwargs: object) -> _FakeSpan:
        span = _FakeSpan()
        span.attributes["__name__"] = name
        self.calls.append({"name": name, **kwargs})
        self.spans.append(span)
        return span


@pytest.fixture(autouse=True)
def _clean_tracer():
    tracing.set_tracer(None)
    tracing.reset_actor_cardinality()
    yield
    tracing.set_tracer(None)
    tracing.reset_actor_cardinality()


def _called_attribute_names(source: str) -> set[str]:
    """The attribute names REALLY called, docstrings excluded.

    An `assert "set_tracer_provider" not in source` looks simpler and is wrong in
    both directions: it reddens on the docstring that EXPLAINS why we do not call it
    (measured — it happened while writing this file), and it would stay green on a
    `getattr(trace, "set_tracer" + …)`. The AST answers the question asked: does
    this call take place?
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


class TestTheGlobalProviderIsNeverInstalled:
    def test_the_module_never_calls_set_tracer_provider(self) -> None:
        """THE test of this file. It reads the SOURCE, so it holds without the SDK.

        A `set_tracer_provider` here would arm FastMCP's `record_exception` and
        would let the raw business message plus the stacktrace out.
        """
        assert "set_tracer_provider" not in _called_attribute_names(_SOURCE), (
            "un provider GLOBAL arme la télémétrie interne de FastMCP, qui pose "
            "str(e) et une stacktrace dans le span — provider privé obligatoire"
        )

    def test_the_module_never_records_an_exception_object(self) -> None:
        """We set the class NAME, never the exception: its args carry the business
        message, and its stacktrace follows __cause__."""
        assert "record_exception" not in _called_attribute_names(_SOURCE)

    def test_the_guard_would_catch_a_real_call(self) -> None:
        """A guard test that has never reddened for the right reason proves
        nothing. Here we give it the code it must refuse."""
        coupable = "import x\ndef f(p):\n    trace.set_tracer_provider(p)\n"
        assert "set_tracer_provider" in _called_attribute_names(coupable)

    def test_the_module_imports_no_otel_symbol_at_top_level(self) -> None:
        """The SDK is an OPTIONAL dependency. A module-level import would fail the
        server's startup where it is absent — that is, in CI and in production as
        long as the extra is not installed."""
        tree = ast.parse(_SOURCE)
        top_level_imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.append(node.module)
        assert not [name for name in top_level_imports if name.startswith("opentelemetry")], (
            f"import OTel au niveau module : {top_level_imports}"
        )


class TestTheKillswitchActuallyCuts:
    def test_no_tracer_means_no_span(self) -> None:
        """`get_tracer()` must be able to return None, otherwise the killswitch cuts
        nothing: the OTel API returns a no-op tracer but we would still pay for
        building the attributes on the hot path."""
        assert tracing.get_tracer() is None
        assert tracing.start_tool_span("brain_search") is None

    def test_a_tracer_that_is_set_produces_a_span(self) -> None:
        tracer = _FakeTracer()
        tracing.set_tracer(tracer)

        span = tracing.start_tool_span("brain_search")

        assert span is not None
        assert tracer.calls[0]["name"] == "execute_tool brain_search"

    def test_the_span_is_forced_to_be_a_root(self) -> None:
        """`start_span(name)` without an explicit context resolves the parent from
        the CURRENT context. A client span propagated through `traceparent` would
        then adopt us as a child — and would decide our sampling. We therefore pass
        an EMPTY context, explicitly."""
        tracer = _FakeTracer()
        tracing.set_tracer(tracer)

        tracing.start_tool_span("brain_search")

        assert "context" in tracer.calls[0], "le contexte racine doit être EXPLICITE"
        assert tracer.calls[0]["context"] is not None


class TestTheActorCardinalityIsBounded:
    def test_a_known_actor_passes_through(self) -> None:
        assert tracing.bounded_actor("brain-v42") == "brain-v42"

    def test_beyond_the_cap_actors_collapse_into_one_bucket(self) -> None:
        """`X-Brain-Agent` is declared by the client, hence forgeable. A span
        attribute has NO native cap: without a bound, a caller varying its header
        blows up the backend's cardinality. Same cap as
        `MetricsCollector._MAX_AGENTS` (collector.py:130), for the same reason."""
        for index in range(tracing.MAX_TRACED_ACTORS):
            tracing.bounded_actor(f"agent-{index}")

        assert tracing.bounded_actor("agent-de-trop") == tracing.OVERFLOW_ACTOR
        assert tracing.bounded_actor("agent-0") == "agent-0", (
            "un acteur DÉJÀ connu doit continuer à passer une fois le plafond atteint"
        )


class TestTelemetryNeverBreaksTheObservedCall:
    def test_a_tracer_that_raises_yields_no_span_and_no_exception(self) -> None:
        """Same posture as `_report` and the provenance probe: an observation
        channel cannot bring down the operation it observes."""

        class _BoomTracer:
            def start_span(self, *_a: object, **_kw: object) -> None:
                raise RuntimeError("exporter cassé")

        tracing.set_tracer(_BoomTracer())

        assert tracing.start_tool_span("brain_search") is None

    def test_finishing_a_span_swallows_a_broken_span_object(self) -> None:
        class _BoomSpan:
            def set_attribute(self, *_a: object) -> None:
                raise RuntimeError("cassé")

            def end(self) -> None:
                raise RuntimeError("cassé aussi")

        # Ne doit rien lever.
        tracing.finish_tool_span(_BoomSpan(), actor="brain-v42", error=False, latency_ms=1.0)

    def test_finishing_a_none_span_is_a_no_op(self) -> None:
        tracing.finish_tool_span(None, actor="brain-v42", error=False, latency_ms=1.0)


class TestTheSpanCarriesTheRightAttributes:
    def test_a_successful_call_carries_actor_error_and_latency(self) -> None:
        tracer = _FakeTracer()
        tracing.set_tracer(tracer)

        span = tracing.start_tool_span("brain_search")
        tracing.finish_tool_span(span, actor="brain-v42", error=False, latency_ms=12.34)

        attributes = tracer.spans[0].attributes
        assert attributes["gen_ai.operation.name"] == "execute_tool"
        assert attributes["gen_ai.tool.name"] == "brain_search"
        assert attributes["brain.actor"] == "brain-v42"
        assert attributes["brain.tool.error"] is False
        assert attributes["brain.tool.latency_ms"] == pytest.approx(12.34, abs=0.05)
        assert tracer.spans[0].ended

    def test_a_failed_call_names_the_exception_class_and_never_its_message(self) -> None:
        """The class name diagnoses; the message, for its part, IS the business
        data (`business_errors.py:101` relays `str(exc)`)."""
        tracer = _FakeTracer()
        tracing.set_tracer(tracer)

        span = tracing.start_tool_span("brain_ticket_get")
        tracing.finish_tool_span(
            span,
            actor="brain-v42",
            error=True,
            latency_ms=3.0,
            exception=ValueError("ticket 42 du projet secret introuvable"),
        )

        attributes = tracer.spans[0].attributes
        assert attributes["error.type"] == "ValueError"
        assert attributes["brain.tool.error"] is True
        flat = " ".join(str(value) for value in attributes.values())
        assert "secret" not in flat, "le message d'exception ne doit JAMAIS entrer dans un span"
        assert tracer.spans[0].recorded_exceptions == []

    def test_a_masked_business_error_is_unwrapped_to_its_real_cause(self) -> None:
        """`business_errors._wrap` relays everything as `ToolError`. Without
        unwrapping, `error.type` would degenerate into "ToolError" for every failure
        — which is exactly the defect that got the middleware discarded."""
        tracer = _FakeTracer()
        tracing.set_tracer(tracer)

        cause = KeyError("clef")
        masked = RuntimeError("masqué")
        masked.__cause__ = cause

        span = tracing.start_tool_span("brain_get")
        tracing.finish_tool_span(
            span, actor="x", error=True, latency_ms=1.0, exception=masked, unwrap=True
        )

        assert tracer.spans[0].attributes["error.type"] == "KeyError"


class TestShutdownIsBoundedAndNeverRaises:
    """The counterpart of `shutdown_on_exit=False`.

    The SDK's `atexit` was disabled so that an unreachable exporter does not drag
    out the server's shutdown. Without an explicit flush, we would merely have
    traded that defect for another: spans disappearing in silence. A hole found by
    the e2e, not by review.
    """

    def test_shutdown_flushes_then_closes_the_provider(self) -> None:
        calls: list[str] = []

        class _Provider:
            def force_flush(self, timeout_ms: int) -> None:
                calls.append(f"flush:{timeout_ms}")

            def shutdown(self) -> None:
                calls.append("shutdown")

        tracing._provider = _Provider()
        tracing.set_tracer(object())

        tracing.shutdown_tracing(1234)

        assert calls == ["flush:1234", "shutdown"], "vider AVANT de fermer, sinon on jette la file"
        assert tracing.get_tracer() is None

    def test_a_provider_that_raises_never_breaks_the_shutdown(self) -> None:
        class _BoomProvider:
            def force_flush(self, timeout_ms: int) -> None:
                raise RuntimeError("collecteur injoignable")

            def shutdown(self) -> None:
                raise RuntimeError("cassé aussi")

        tracing._provider = _BoomProvider()
        tracing.shutdown_tracing()

        assert tracing.get_tracer() is None

    def test_shutdown_without_a_provider_is_a_no_op(self) -> None:
        tracing._provider = None
        tracing.shutdown_tracing()
