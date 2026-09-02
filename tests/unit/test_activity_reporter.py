"""The activity emitter must neither block nor break a tool call."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import socket
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from brain_v42.mcp.activity_reporter import _MAX_BUFFERED, ActivityReporter, _is_a_decade

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@contextlib.contextmanager
def _production_logging_into(buffer: io.StringIO) -> Iterator[None]:
    """Configure structlog like production, but towards a buffer.

    A copy of ``mcp/server.py::_configure_stdio_logging``: that is the chain that
    renders exceptions, and its default rendering (rich, when installed) displays
    each frame's local variables.

    Measured trap: ``PrintLoggerFactory(file=...)`` freezes the stream at the
    ``configure()`` call. Replacing ``sys.stderr`` afterwards captures nothing, and
    a test asserting "no identifier in the log" would go green on an empty log. The
    buffer is therefore passed to the factory itself.
    """
    saved = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    try:
        yield
    finally:
        structlog.reset_defaults()
        structlog.configure(**saved)


def _closed_loopback_port() -> int:
    """A loopback port nobody is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _longest_leaked_fragment(log: str, secret: str, minimum: int = 8) -> str:
    """The longest fragment of ``secret`` of at least ``minimum`` characters present.

    Looking for the *whole* secret would not be enough: the rich formatter
    truncates each local variable at 80 characters, so the observation body only
    leaks a prefix of the session UUID. An assertion "the whole UUID is absent"
    would therefore go green with fifteen characters of raw identifier in journald.
    """
    for size in range(len(secret), minimum - 1, -1):
        for start in range(len(secret) - size + 1):
            fragment = secret[start : start + size]
            if fragment in log:
                return fragment
    return ""


async def _log_of_one_failed_post(buffer: io.StringIO, session_id: str | None) -> str:
    """Provoke a real POST failure and return the log produced."""
    url = f"http://127.0.0.1:{_closed_loopback_port()}/v1/client-activity"
    reporter = ActivityReporter(url=url, timeout=1.0)
    with _production_logging_into(buffer):
        reporter.report("brain-v42", session_id)
        await reporter.drain()
    await reporter.close()
    return _ANSI.sub("", buffer.getvalue())


async def _one_answered_post(
    buffer: io.StringIO, status: int, body: str
) -> tuple[ActivityReporter, str]:
    """Emit an observation the receiver ANSWERS with ``status``.

    ``httpx`` does not raise on 4xx/5xx: the response comes back through the
    nominal path, not through the ``except``. The double therefore returns a real
    ``httpx.Response`` — a bare ``AsyncMock`` would return a ``MagicMock`` whose
    every attribute is truthy, and ``is_success`` would be true for a 404.
    """
    url = "http://127.0.0.1:9200/v1/client-activity"
    reporter = ActivityReporter(url=url)
    response = httpx.Response(
        status_code=status,
        text=body,
        request=httpx.Request("POST", url),
    )
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(return_value=response)
        with _production_logging_into(buffer):
            reporter.report("brain-v42", FAKE_UUID)
            await reporter.drain()
    await reporter.close()
    return reporter, _ANSI.sub("", buffer.getvalue())


# Resetting the ``_reporter`` global is a shared autouse fixture, in
# tests/unit/conftest.py: this module is no longer the only one injecting a
# double.


@pytest.mark.asyncio
async def test_report_posts_the_observation() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("brain-v42", FAKE_UUID)
        await reporter.drain()
        client.post.assert_awaited_once()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"observations": [{"actor": "brain-v42", "session": FAKE_UUID, "calls": 1}]}
    await reporter.close()


@pytest.mark.asyncio
async def test_absent_session_is_omitted_from_the_wire() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("codex", None)
        await reporter.drain()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"observations": [{"actor": "codex", "calls": 1}]}
    await reporter.close()


@pytest.mark.asyncio
async def test_transport_failure_is_swallowed() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=OSError("sidecar down"))
        reporter.report("brain-v42", None)
        await reporter.drain()  # must not raise
    await reporter.close()


@pytest.mark.asyncio
async def test_saturation_coalesces_instead_of_blocking_or_losing() -> None:
    """Under saturation, ``report()`` returns IMMEDIATELY — and no longer loses anything.

    This test used to be called ``test_saturation_drops_instead_of_blocking`` and
    asserted ``reporter.dropped == 10``. It therefore PINNED the loss ticket
    ``1c40c36a`` denounced: fixing it necessarily reddened the suite. The
    non-blocking assertion — its real reason to exist — is kept and STRENGTHENED:
    we additionally check that the ten calls reach the wire.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)
        for _ in range(10):
            reporter.report("brain-v42", None)  # must return immediately
        assert reporter.dropped == 0, "la contre-pression jette encore"
        assert reporter.coalesced == 10, "les dix appels n'ont pas été repliés"
        release.set()
        await reporter.drain()
        posted = sum(
            int(observation["calls"])
            for call in client.post.await_args_list
            for observation in json.loads(call.kwargs["content"])["observations"]
        )
    assert posted == 11, f"onze appels émis, {posted} arrivés sur le fil"
    await reporter.close()


@pytest.mark.asyncio
async def test_drain_returns_even_if_done_callback_has_not_run_yet() -> None:
    """Potential livelock: on Python 3.12+, ``asyncio.gather()`` handles futures
    that are already ``done()`` eagerly, and awaiting an already-finished future
    never yields to the event loop. If a ``_post()`` task finishes before its
    ``done_callback`` (``self._pending.discard``) has had its turn — which requires
    a second loop iteration — then ``while self._pending: await asyncio.gather(...)``
    loops forever without ever letting that callback run.

    ``asyncio.wait_for`` with a short delay: a ``drain()`` that livelocks must not
    block the whole suite, only fail this test.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()  # resolves instantly, with no real suspension
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)  # lets _post run, but not necessarily the callback
        await asyncio.wait_for(reporter.drain(), timeout=3)
    await reporter.close()


def test_construction_failure_never_breaks_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_activity_reporter() must never raise.

    In production ``get_settings()`` cannot fail (POSTGRES_URL is required at
    startup). But ``get_activity_reporter()``'s caller is the provenance
    middleware, on the path of EVERY tool call — if resolving the settings or
    building the client raises for another reason, that must never break the call
    in progress.
    """
    from brain_v42.mcp import activity_reporter

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(activity_reporter, "get_settings", _boom)

    assert activity_reporter.get_activity_reporter() is None


def test_closed_killswitch_silences_the_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate closed: no emitter, hence no emission.

    Pinning the default alone in ``test_config`` would prove nothing. A
    configuration value is a safety boundary only if the consumption point reads
    it — that is the false witness pattern (learning a6e1dd1f): a value captured
    that nobody reads back.
    """
    from brain_v42.mcp import activity_reporter

    class _Closed:
        client_activity_reporting_enabled = False
        client_activity_url = "http://127.0.0.1:9200/v1/client-activity"

    monkeypatch.setattr(activity_reporter, "get_settings", lambda: _Closed())

    assert activity_reporter.get_activity_reporter() is None


def test_open_killswitch_builds_the_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the previous test.

    Without it, ``get_activity_reporter() is None`` would go green for any reason
    at all — including an emitter broken for good.
    """
    from brain_v42.mcp import activity_reporter

    class _Open:
        client_activity_reporting_enabled = True
        client_activity_url = "http://127.0.0.1:9999/v1/probe"

    monkeypatch.setattr(activity_reporter, "get_settings", lambda: _Open())

    reporter = activity_reporter.get_activity_reporter()

    assert reporter is not None
    assert reporter._url == "http://127.0.0.1:9999/v1/probe"


# ──────────────────────────────────────────────────────────────────────────────
# The emitter's log is itself an output: what it writes goes into journald, on
# the path of EVERY tool call.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_failure_log_carries_no_raw_identifier() -> None:
    """An unreachable sidecar must not copy the session UUID into journald.

    The design asserts that no raw identifier leaves the registry; nobody had
    looked at the emitter's log. The trigger is the NOMINAL mode of a
    fire-and-forget: sidecar stopped, restart, timeout.

    The assertion on the event's presence is the positive control: without it, "no
    identifier in the log" would go green for a mute logger, a misconfigured chain
    or a POST that never failed.
    """
    buffer = io.StringIO()

    log = await _log_of_one_failed_post(buffer, FAKE_UUID)

    assert "activity_reporter.post_failed" in log, (
        "l'échec doit rester observable — sinon l'absence d'UUID ne prouve rien"
    )
    leaked = _longest_leaked_fragment(log, FAKE_UUID)
    assert leaked == "", f"fragment d'identifiant brut dans le journal : {leaked!r}"


@pytest.mark.asyncio
async def test_post_failure_log_stays_within_a_few_lines() -> None:
    """``_report`` is on the path of every tool call: the log is bounded.

    With the sidecar absent and the gate open at rollout, a traceback rendered by
    rich costs hundreds of lines per call — paid on the hot path, while the module
    promises that a stopped sidecar "must never slow down" the call.
    """
    buffer = io.StringIO()

    log = await _log_of_one_failed_post(buffer, FAKE_UUID)

    lines = log.count("\n")
    assert 0 < lines <= 3, f"journal de {lines} lignes pour un seul POST raté"


@pytest.mark.asyncio
async def test_post_failure_log_names_the_exception_type() -> None:
    """Diagnosing a dead sidecar needs the exception type, not the traceback."""
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    buffer = io.StringIO()

    class SidecarUnplugged(OSError):
        pass

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=SidecarUnplugged("connection refused"))
        with _production_logging_into(buffer):
            reporter.report("brain-v42", None)
            await reporter.drain()
    await reporter.close()

    log = _ANSI.sub("", buffer.getvalue())
    assert "SidecarUnplugged" in log


def test_unavailable_log_carries_no_local_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same defect on the module's second logging point.

    ``get_activity_reporter()`` is called by the provenance middleware at every
    tool call; as long as resolving the settings fails, it fails every time. The
    frames traversed are those of the settings construction — their local variables
    carry the configuration, DSN included.
    """
    from brain_v42.mcp import activity_reporter

    def _boom() -> Any:
        dsn = "postgresql+asyncpg://brain:s3cret-p4ssw0rd@127.0.0.1:5433/brain"  # noqa: F841
        raise RuntimeError("settings unavailable")

    # Explicit reset of the global: an emitter left behind by a previous test
    # would short-circuit the construction, ``get_settings`` would never be called,
    # and the assertion "no DSN in the log" would go green on an empty log.
    activity_reporter.set_activity_reporter(None)
    monkeypatch.setattr(activity_reporter, "get_settings", _boom)
    buffer = io.StringIO()

    with _production_logging_into(buffer):
        assert activity_reporter.get_activity_reporter() is None

    log = _ANSI.sub("", buffer.getvalue())
    assert "activity_reporter.unavailable" in log, (
        "contrôle positif : l'indisponibilité doit rester observable"
    )
    assert "s3cret-p4ssw0rd" not in log
    assert log.count("\n") <= 3


# ──────────────────────────────────────────────────────────────────────────────
# A receiver REFUSAL is not a transport failure. ``httpx`` does not raise on
# 4xx/5xx: without an explicit status read, 404, 403, 413, 415, 400 and 503 all
# come back through the nominal path, and the loss is neither counted nor logged.
# The measured case: a non-loopback ``METRICS_HOST`` — a value the configuration
# allows — does not register the ``/v1/client-activity`` route, so every POST
# receives a 404 and the dashboard's "brain" half stays empty forever while the
# whole MCP chain declares itself healthy.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_absent_404_is_counted_apart_from_local_backpressure() -> None:
    """The dominant loss mode at rollout must have its own counter.

    ``dropped`` only counts LOCAL back-pressure (slots taken). Conflating it with
    refusal would make "no call at all" and "every observation refused"
    indistinguishable — exactly the confusion the repository's doctrine forbids,
    moved into the emitter.
    """
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 404, "404: Not Found")

    assert reporter.refused == 1
    assert reporter.dropped == 0, "un refus n'est pas une contre-pression locale"
    assert "activity_reporter.refused" in log
    assert "status=404" in log


@pytest.mark.asyncio
async def test_receiver_saturation_503_is_counted_and_logged() -> None:
    """A saturated receiver also refuses through the nominal path, not the ``except``."""
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 503, "receiver saturated")

    assert reporter.refused == 1
    assert reporter.dropped == 0
    assert "activity_reporter.refused" in log
    assert "status=503" in log


@pytest.mark.asyncio
async def test_accepted_200_counts_nothing_and_stays_silent() -> None:
    """Positive control for the two previous tests.

    Without it, "the counter moved" would go green for a counter incrementing at
    every POST, and "the refusal is logged" for an emitter that logs all of its
    emissions — on the hot path of EVERY tool call.
    """
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 200, '{"accepted": 1}')

    assert reporter.refused == 0
    assert reporter.dropped == 0
    assert "activity_reporter.refused" not in log
    assert log == "", f"un POST accepté ne doit rien écrire, journal : {log!r}"


@pytest.mark.asyncio
async def test_refusal_log_carries_neither_body_nor_identifier() -> None:
    """The status alone. The refusal's body is an untrusted input.

    Same reason as in the fix that removed ``exc_info``: the log goes into journald
    at every tool call. A receiver may echo the request back — session UUID
    included — and many proxies do.
    """
    buffer = io.StringIO()
    echoed = f'{{"error": "no route", "echo": "SECRET-BODY-MARKER-{FAKE_UUID}"}}'

    reporter, log = await _one_answered_post(buffer, 404, echoed)

    assert reporter.refused == 1, "contrôle positif : le refus doit avoir été vu"
    assert "SECRET-BODY-MARKER" not in log
    leaked = _longest_leaked_fragment(log, FAKE_UUID)
    assert leaked == "", f"fragment d'identifiant brut dans le journal : {leaked!r}"
    lines = log.count("\n")
    assert lines <= 1, f"journal de {lines} lignes pour un seul refus"


FAKE_TRANSPORT = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class TestTransportOnTheWire:
    """The emitter carries the transport, and omits it when there is none."""

    @staticmethod
    async def _captured_body(**kwargs: Any) -> dict[str, Any]:
        reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
        with patch.object(reporter._client, "post", new=AsyncMock()) as post:
            post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://x"))
            reporter.report(**kwargs)
            await reporter.drain()
        (observation,) = json.loads(post.call_args.kwargs["content"])["observations"]
        return dict(observation)

    @pytest.mark.asyncio
    async def test_transport_is_emitted_when_present(self) -> None:
        body = await self._captured_body(actor="red-lab", session_id=None, transport=FAKE_TRANSPORT)
        assert body["transport"] == FAKE_TRANSPORT

    @pytest.mark.asyncio
    async def test_transport_key_absent_when_none(self) -> None:
        """Absent, never ``null``: the decoder distinguishes "not declared" from "empty"."""
        body = await self._captured_body(actor="red-lab", session_id=None, transport=None)
        assert "transport" not in body

    @pytest.mark.asyncio
    async def test_transport_defaults_to_absent(self) -> None:
        """The existing callers (2 arguments) stay valid and emit nothing."""
        body = await self._captured_body(actor="red-lab", session_id=None)
        assert "transport" not in body

    @pytest.mark.asyncio
    async def test_session_and_transport_coexist(self) -> None:
        body = await self._captured_body(
            actor="red-lab", session_id=FAKE_UUID, transport=FAKE_TRANSPORT
        )
        assert body["session"] == FAKE_UUID
        assert body["transport"] == FAKE_TRANSPORT


@pytest.mark.asyncio
async def test_local_backpressure_warns_once_then_stays_silent() -> None:
    """Local back-pressure was counted by NOBODY.

    `dropped` was incremented and `report()` returned, without a single line.
    Measured: beyond 8 concurrent calls, N-8 observations disappear — 12 calls → 4
    lost, 20 → 12. The dashboard therefore under-counts precisely on the peaks it
    exists to show, and nothing says so.

    ONE single line, at the FIRST loss. This is the hot path of EVERY tool call:
    one line per loss would turn a burst into a log storm, and would teach people
    to skip it.

    The trigger changed with ``1c40c36a``'s fix: repeating the SAME actor no longer
    loses anything, it is coalesced. The residual loss now lives beyond the
    buffer's bound, so we provoke it with actors that are ALL DISTINCT. The
    assertion itself is unchanged — it is the escalation that is protected here,
    not the way to trigger it.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        with capture_logs() as logs:
            reporter.report("filler-000", None)
            await asyncio.sleep(0)
            for i in range(_MAX_BUFFERED):  # saturates the buffer, with no loss
                reporter.report(f"filler-{i:03d}", None)
            for i in range(10):  # beyond: ten losses, all distinct actors
                reporter.report(f"overflow-{i:03d}", None)
        release.set()
        await reporter.drain()
    await reporter.close()

    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert [w["dropped"] for w in warnings] == [1, 10], (
        f"dix pertes = la 1re et la 10e, jamais les huit du milieu, vu : {warnings}"
    )
    assert warnings[0]["event"] == "activity_reporter.dropped"
    assert reporter.dropped == 10


@pytest.mark.asyncio
async def test_a_repeated_refusal_warns_once_per_distinct_status() -> None:
    """The measured case is PERMANENT: one 404 per tool call, forever.

    Logging every refusal at the same level would produce one line per tool call
    until the end of time. The signature — the status — speaks once.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")

    with patch.object(reporter, "_client") as client:
        with capture_logs() as logs:
            for status in (404, 404, 404, 503, 503):
                client.post = AsyncMock(return_value=httpx.Response(status_code=status))
                reporter.report("brain-v42", None)
                await reporter.drain()
    await reporter.close()

    warned = [e["status"] for e in logs if e["log_level"] == "warning"]
    assert warned == [404, 503], f"une ligne par signature, vu : {warned}"
    assert reporter.refused == 5


@pytest.mark.asyncio
async def test_a_run_that_lost_nothing_closes_silently() -> None:
    """THE NOMINAL CASE IS MUTE. Nothing lost, nothing said — not even a "0"."""
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(return_value=httpx.Response(status_code=204))
        with capture_logs() as logs:
            reporter.report("brain-v42", None)
            await reporter.drain()
    with capture_logs() as close_logs:
        await reporter.close()
    logs = logs + close_logs

    assert [e for e in logs if e["log_level"] == "warning"] == []


def test_a_decade_is_the_first_the_tenth_the_hundredth_and_nothing_between() -> None:
    """The escalation must be exact: `log10` would miss or double some bounds."""
    shouted = [n for n in range(1, 1001) if _is_a_decade(n)]
    assert shouted == [1, 10, 100, 1000]
    assert not _is_a_decade(0)


@pytest.mark.asyncio
async def test_the_magnitude_of_the_loss_stays_visible_without_a_line_per_loss() -> None:
    """Neither one line per loss, nor a single line forever.

    `close()` is wired NOWHERE in production — "the client dies with the process" —
    so a tally at close time would never be returned. The order of magnitude must
    travel in the lines themselves.

    The trigger changed with ``1c40c36a``'s fix: repeating the SAME actor no longer
    loses anything, it is coalesced. The residual loss now lives beyond the
    buffer's bound, so we provoke it with actors that are ALL DISTINCT. The
    assertion itself is unchanged — it is the escalation that is protected here,
    not the way to trigger it.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("filler-000", None)
        await asyncio.sleep(0)
        for i in range(_MAX_BUFFERED):  # saturates the buffer, with no loss
            reporter.report(f"filler-{i:03d}", None)
        with capture_logs() as logs:
            for i in range(100):  # beyond: a hundred losses, all distinct actors
                reporter.report(f"overflow-{i:03d}", None)
        release.set()
        await reporter.drain()
    await reporter.close()

    counts = [e["dropped"] for e in logs if e["log_level"] == "warning"]
    assert counts == [1, 10, 100], f"cent pertes, trois lignes attendues, vu : {counts}"
    assert reporter.dropped == 100
