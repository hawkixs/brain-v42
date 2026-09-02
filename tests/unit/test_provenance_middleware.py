"""The provenance middleware facing the compact profile's re-entrancy."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from brain_v42.mcp.activity_reporter import set_activity_reporter
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import get_current_actor, get_current_session

SESSION_UUID = "3d7a88d7-791b-45da-b8b9-75727e3c9eec"


TRANSPORT_ID = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class _SpyReporter:
    """An emitter double that records what it is given, in the order received.

    ``calls`` stays a pair ``(actor, session)``: the assertions reading it pin the
    order of the two historical positions, and grafting the transport onto it would
    make them all true for a bad reason. The third argument is therefore recorded
    separately.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.transports: list[str | None] = []

    def report(
        self,
        actor: str,
        session_id: str | None,
        transport: str | None = None,
    ) -> None:
        self.calls.append((actor, session_id))
        self.transports.append(transport)


@pytest.mark.asyncio
async def test_compact_gateway_reports_one_outermost_call() -> None:
    """A compact call triggers on_call_tool twice: the gateway then the inner
    tool. The guard must keep only one.

    This test MUST simulate the real nesting. A test calling the middleware twice
    flat would go green without proving anything.
    """
    middleware = ProvenanceMiddleware()

    async def inner_call_next(_context: Any) -> str:
        return "inner"

    async def outer_call_next(_context: Any) -> str:
        # The gateway tool re-enters the middleware chain.
        return await middleware.on_call_tool(object(), inner_call_next)

    with patch.object(ProvenanceMiddleware, "_report", autospec=True) as report:
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "brain-v42"},
        ):
            result = await middleware.on_call_tool(object(), outer_call_next)

    assert result == "inner"
    assert report.call_count == 1


@pytest.mark.asyncio
async def test_actor_and_session_are_posted_from_headers() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["actor"] = get_current_actor()
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={
                "x-brain-agent": "/home/hawixs/git/red-lab",
                "x-brain-session": "3d7a88d7-791b-45da-b8b9-75727e3c9eec",
            },
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["actor"] == "red-lab"
    assert captured["session"] == "3d7a88d7-791b-45da-b8b9-75727e3c9eec"


@pytest.mark.asyncio
async def test_absent_session_header_leaves_session_none() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "codex"},
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["session"] is None


@pytest.mark.asyncio
async def test_reporter_receives_actor_then_session_in_that_order() -> None:
    """The REAL ``_report`` must reach the emitter, each value in its place.

    The three tests above replace ``_report`` with a mock, and the tests in
    tests/unit/mcp/ traverse it with the killswitch closed, so
    ``get_activity_reporter()`` returns ``None`` and the body does nothing
    observable. Measured result: neutralising ``_report``'s body entirely, or
    swapping its two arguments, left the whole suite green. This is the false
    witness pattern (learning a6e1dd1f) in its hard version — the value is not
    merely captured without being read back, it is replaced by a mock in 100% of
    the tests that touch the point of application.

    Swapping the arguments is not cosmetic: it would put the raw session UUID in
    the actor position on the wire. ``normalize_agent`` on the sidecar side would
    leave it as is (no ``${``, no ``/``, 36 <= 64 characters) and the cockpit would
    publish ``"id": "unattributed:<uuid>"`` — the design's central property, "no
    raw identifier leaves the registry", bypassed through the one path never
    executed.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={
            "x-brain-agent": "/home/hawixs/git/red-lab",
            "x-brain-session": SESSION_UUID,
        },
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.calls == [("red-lab", SESSION_UUID)]


@pytest.mark.asyncio
async def test_reporter_receives_a_null_session_when_none_is_declared() -> None:
    """With no declared session, the actor stays in the actor position.

    Negative control for the previous test: it forbids filling the session position
    with the actor for want of anything better. ``None`` means "no declared
    session" and must stay distinguishable.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex"},
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.calls == [("codex", None)]


@pytest.mark.asyncio
async def test_transport_reaches_the_reporter_from_the_mcp_header() -> None:
    """``Mcp-Session-Id`` must travel through to the emitter.

    It does not get there on its own: ``get_http_headers()`` EXCLUDES
    ``mcp-session-id`` by default (verified in fastmcp 3.x, it is hard-coded in
    ``exclude_headers``). Without ``include=``, this test would stay green on a
    ``None`` — the false witness pattern this file already documents.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex", "mcp-session-id": TRANSPORT_ID},
    ) as headers:
        await middleware.on_call_tool(object(), call_next)

    assert spy.transports == [TRANSPORT_ID]
    assert spy.calls == [("codex", None)], "le transport ne doit PAS occuper la place session"
    # The control that forbids believing the header accessible by default.
    assert headers.call_args.kwargs.get("include") == {"mcp-session-id"}


@pytest.mark.asyncio
async def test_stateless_mode_leaves_transport_none() -> None:
    """With no server-minted header, the absence must stay speakable."""
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex"},
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.transports == [None]


@pytest.mark.asyncio
async def test_client_forged_transport_is_refused_by_shape() -> None:
    """A value that lacks the server-minted shape is ``None``.

    In stateless mode the server serves an ``mcp-session-id`` invented by the
    client without validating it: the only thing that holds at this level is the
    SHAPE. A dashboard driven by the caller would be worse than an empty one.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    for forged in ("../../etc/passwd", SESSION_UUID, TRANSPORT_ID.upper(), "x" * 4096):
        spy.transports.clear()
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "codex", "mcp-session-id": forged},
        ):
            await middleware.on_call_tool(object(), call_next)
        assert spy.transports == [None], f"forme acceptée à tort : {forged!r}"
