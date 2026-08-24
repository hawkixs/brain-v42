"""End-to-end harness for the session capture ledger, over real HTTP.

The rule this harness exists to honour: **the trace is written AFTER the commit
and read back FROM THE DATABASE, never from the log of whoever wrote it.**  A
test that checks an attribution by reading the tool's own response reproduces
the very defect it believes it guards.

So every assertion below reads ``brain_session_artifacts`` through a connection
that shares nothing with the code under test — a different engine, raw SQL text,
no ORM table object.

It is exercised here on behaviour that already exists — ``brain_session_capture``
attributes an artifact to a session — because the harness, not the behaviour, is
the risky part.  No derivation is implemented or assumed.

Five properties, one per numbered comment in the tests:

1. negative witness BEFORE the gesture;
2. the real tool path (``brain_learn`` then ``brain_session_capture`` over HTTP),
   never ``PgBrainSessionRepo`` called by hand;
3. independent SQL read-back;
4. distinction witness — two concurrent sessions, and proof the artifact lands
   in the right one;
5. replayability — unique project keys per run, so two runs in a row pass.

Requires ``BRAIN_V42_TEST_DB_URL`` and nothing else; the conftest refuses the
prod ``brain`` database and skips loudly.  The embedding service is NOT a
dependency, and that was measured rather than assumed: pointed at a dead port,
``brain_learn`` still persists the learning (slower, without an embedding) and
both tests below still pass.  So a red here is never "the GPU box was down".
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, suppress
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Read-back statements. Deliberately raw text: binding these to the SQLAlchemy
# Table objects would let a mistake in tables.py hide itself from its own test.
_LEDGER_ROWS_FOR_ARTIFACT = sa.text(
    "SELECT session_id::text FROM brain_session_artifacts WHERE knowledge_id = :knowledge_id"
)
_LEDGER_ROWS_FOR_SESSION = sa.text(
    "SELECT knowledge_id::text FROM brain_session_artifacts WHERE session_id = :session_id"
)
_LEARNING_EXISTS = sa.text("SELECT count(*) FROM learnings WHERE id = :knowledge_id")
_OPEN_TRACERS = sa.text(
    "SELECT count(*) FROM brain_sessions "
    "WHERE project_key = :project_key AND nature = 'agent' AND status = 'open'"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_SHUTDOWN_BUDGET_SECONDS = 15.0


async def _stop_or_fail(serving: asyncio.Task[None], port: int) -> None:
    """Stop uvicorn within a budget, and PROVE nothing survived the teardown.

    ``await serving`` on its own is the only unbounded wait in this harness, and
    an unbounded wait is how a suite stops reporting anything at all: on CI the
    job hit its 30-minute ceiling and was killed with
    ``Terminate orphan process: pytest`` — a hang, not a failure, and a hang
    says nothing about what caused it.

    So the wait is bounded and the residue is asserted rather than assumed. Both
    checks belong here, after the real shutdown of the real server, because that
    is the only place the answer is observable.
    """
    try:
        await asyncio.wait_for(asyncio.shield(serving), timeout=_SHUTDOWN_BUDGET_SECONDS)
    except TimeoutError:
        serving.cancel()
        with suppress(asyncio.CancelledError):
            await serving
        pytest.fail(
            f"the harness server did not stop within {_SHUTDOWN_BUDGET_SECONDS}s; "
            "it was cancelled so the suite can keep reporting. A live server left "
            "behind is what makes the NEXT module hang instead of fail."
        )

    assert serving.done(), "uvicorn task outlived its own shutdown"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:  # pragma: no cover - only on a real leak
            pytest.fail(f"port {port} is still held after teardown: {exc}")

    from fastmcp.server.http import StreamableHTTPSessionManager

    assert StreamableHTTPSessionManager.__module__.startswith("mcp."), (
        "the harness left FastMCP's session manager substituted process-wide; "
        "that residue breaks every HTTP module that runs after this one"
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mcp_base_url() -> AsyncIterator[str]:
    """Boot the real FastMCP HTTP app on an ephemeral loopback port.

    Nothing here is reproduced.  ``build_server()`` is the wiring production
    runs; ``prepare_tools_for_transport()`` is the prelude every served tool
    carries; ``plan_http_transport()`` decides the shape of the app, and this
    harness applies that plan instead of inventing arguments.  What is left is
    the uvicorn call itself, because ``run_http_async`` neither takes an
    ephemeral port nor hands back a stop handle.

    Module-scoped on purpose: ``plan_http_transport`` is not idempotent —
    ``_configure_http_security`` refuses a second call on the same server, since
    configuring one authentication boundary twice is a production bug.  Booting
    once per module respects that instead of softening it.
    """
    from tests.integration.conftest import INTEGRATION_DB_URL

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
        patch.setenv("BRAIN_MCP_TRANSPORT", "http")
        # STATEFUL, and it is a hard precondition rather than a taste: auto-open
        # keys a tracer on (project, connection), and the connection id IS
        # `Mcp-Session-Id`, which only the stateful branch issues. Stateless
        # would leave this bench structurally unable to see a tracer at all.
        #
        # The price is paid, not avoided: the stateful branch installs
        # _IdleTimeoutSessionManager as a PROCESS-GLOBAL substitution that is
        # never undone, and left in place the next module's uvicorn never
        # reaches `started` — measured as `Failed: uvicorn did not start within
        # 5s` in tests/integration/metrics/test_agent_attribution.py. So the
        # class is saved below and restored in the finally, and _stop_or_fail
        # still asserts the restoration actually happened.
        patch.setenv("MCP_HTTP_STATELESS", "false")
        patch.setenv("BRAIN_SESSION_AUTO_OPEN_ENABLED", "true")
        patch.setenv("BRAIN_MCP_PROFILE", "compact")
        patch.setenv("GRAPH_ENABLED", "false")
        patch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "false")
        patch.setenv("DECAY_ENABLED", "false")
        patch.setenv("METRICS_ENABLED", "false")
        patch.setenv("CLIENT_ACTIVITY_REPORTING_ENABLED", "false")
        patch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "false")
        patch.delenv("MCP_HTTP_TOKEN", raising=False)

        from brain_v42.config import get_settings

        get_settings.cache_clear()

        import brain_v42.db.engine as engine_module

        original_engine = engine_module._engine
        original_factory = engine_module._session_factory
        engine_module._engine = None
        engine_module._session_factory = None

        from brain_v42.mcp.server import (
            build_server,
            plan_http_transport,
            prepare_tools_for_transport,
        )
        from brain_v42.mcp.session_autoopen import reset_session_autoopener

        # The auto-opener is a memoised GLOBAL. Built before this fixture swaps
        # the engine factory, it would capture the wrong one; left behind after,
        # it would leak that factory into the next module. Reset on both edges.
        reset_session_autoopener()

        import fastmcp.server.http as fastmcp_http

        original_session_manager = fastmcp_http.StreamableHTTPSessionManager

        built = build_server()
        await prepare_tools_for_transport(built.mcp, built.metrics_collector)
        plan = plan_http_transport(built.mcp, built.settings)
        app = built.mcp.http_app(
            middleware=plan.middleware,
            json_response=plan.json_response,
            stateless_http=plan.stateless_http,
        )

        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error", loop="asyncio")
        )
        serving = asyncio.create_task(server.serve())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if server.started:
                break
        else:
            server.should_exit = True
            await serving
            pytest.fail("uvicorn did not start within 5s")

        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            fastmcp_http.StreamableHTTPSessionManager = original_session_manager
            reset_session_autoopener()
            await _stop_or_fail(serving, port)
            if engine_module._engine is not None:
                await engine_module._engine.dispose()
            engine_module._engine = original_engine
            engine_module._session_factory = original_factory
            get_settings.cache_clear()


class _Conn:
    """One client held open for a whole scene: one connection, one tracer.

    A ``Client`` per call was a connection per call — therefore a tracer per
    call, therefore nothing that could ever be absorbed. Holding the link open
    is what makes the scene a scene.

    The agent header carries the disposable PROJECT KEY, not a fixed actor name.
    Auto-open derives a tracer's project from the ACTOR
    (``resolve_auto_open_identity``), never from the tool's arguments, so a
    hardcoded ``w17-e2e`` opened tracers on a project no assertion ever reads.
    """

    def __init__(self, base_url: str, project_key: str) -> None:
        self._base_url = base_url
        self._project_key = project_key
        self._stack = AsyncExitStack()
        self._client: Any = None

    async def __aenter__(self) -> _Conn:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(
            url=f"{self._base_url}/mcp/",
            headers={
                "x-brain-tool-profile": "native",
                "x-brain-agent": self._project_key,
            },
        )
        self._client = await self._stack.enter_async_context(Client(transport))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        result = await self._client.call_tool(tool, arguments)
        return str(result.content[0].text)  # type: ignore[union-attr]


def _only_uuid(payload: str, *, what: str) -> UUID:
    found = _UUID_RE.findall(payload)
    assert found, f"no UUID in the {what} response: {payload!r}"
    return UUID(found[0])


async def _bootstrap_project(conn: _Conn, project_key: str) -> None:
    """Create the project context by its own tool — a session needs one to exist."""
    await conn.call(
        "brain_set_project_context",
        {
            "project_key": project_key,
            "name": project_key,
            "description": "Disposable project for the W17 capture-ledger e2e harness.",
        },
    )


async def _session_id(conn: _Conn, project_key: str, client_key: str) -> UUID:
    payload = await conn.call(
        "brain_session_start", {"project_key": project_key, "client_key": client_key}
    )
    return _only_uuid(payload, what="brain_session_start")


async def _learning_id(conn: _Conn, project_key: str, topic: str) -> UUID:
    payload = await conn.call(
        "brain_learn",
        {
            "topic": topic,
            "insight": f"Harness artifact for {topic}; carries no operational meaning.",
            "project_key": project_key,
            "confidence": "low",
        },
    )
    return _only_uuid(payload, what="brain_learn")


async def _ledger_sessions_for(engine: AsyncEngine, knowledge_id: UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(_LEDGER_ROWS_FOR_ARTIFACT, {"knowledge_id": str(knowledge_id)})
        return sorted(str(row[0]) for row in rows)


async def _ledger_artifacts_for(engine: AsyncEngine, session_id: UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(_LEDGER_ROWS_FOR_SESSION, {"session_id": str(session_id)})
        return sorted(str(row[0]) for row in rows)


@pytest.mark.asyncio(loop_scope="module")
async def test_capture_lands_in_the_named_session_and_not_its_neighbour(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """The whole harness, exercised on behaviour that exists today."""
    project_key = f"integ-w17-{uuid4().hex[:10]}"
    async with _Conn(mcp_base_url, project_key) as link:
        await _bootstrap_project(link, project_key)

        # (2) real tool path, and (4) two concurrent sessions in one project.
        session_a, session_b = await asyncio.gather(
            _session_id(link, project_key, "task-a"),
            _session_id(link, project_key, "task-b"),
        )
        assert session_a != session_b

        artifact = await _learning_id(link, project_key, f"w17 harness {uuid4().hex[:8]}")
        async with engine.connect() as probe:
            assert await probe.scalar(_LEARNING_EXISTS, {"knowledge_id": str(artifact)}) == 1

        # (1) negative witness: nothing is attributed before the explicit gesture.
        assert await _ledger_sessions_for(engine, artifact) == []
        assert await _ledger_artifacts_for(engine, session_a) == []
        assert await _ledger_artifacts_for(engine, session_b) == []

        await link.call(
            "brain_session_capture",
            {
                "session_id": str(session_a),
                "expected_client_key": "task-a",
                "knowledge_ids": [str(artifact)],
            },
        )

        # (3) read back from the database, through an engine the server never saw.
        assert await _ledger_sessions_for(engine, artifact) == [str(session_a)]
        # (4) the distinction witness: a harness that attributed everything to the
        # first session would pass without this line.
        assert await _ledger_artifacts_for(engine, session_a) == [str(artifact)]
        assert await _ledger_artifacts_for(engine, session_b) == []


@pytest.mark.asyncio(loop_scope="module")
async def test_replaying_the_harness_keeps_the_ledger_exclusive(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """Second test in the module — and the asyncio-loop trap only shows up here.

    A single-test module boots the engine on the loop that happens to be first
    and never notices it cached one; this repository has already paid for that.
    """
    project_key = f"integ-w17-{uuid4().hex[:10]}"
    async with _Conn(mcp_base_url, project_key) as link:
        await _bootstrap_project(link, project_key)
        owner, rival = await asyncio.gather(
            _session_id(link, project_key, "owner"),
            _session_id(link, project_key, "rival"),
        )

        artifact = await _learning_id(link, project_key, f"w17 exclusive {uuid4().hex[:8]}")
        assert await _ledger_sessions_for(engine, artifact) == []

        captured = {
            "session_id": str(owner),
            "expected_client_key": "owner",
            "knowledge_ids": [str(artifact)],
        }
        await link.call("brain_session_capture", captured)
        assert await _ledger_sessions_for(engine, artifact) == [str(owner)]

        # Replaying the exact capture is idempotent — it must not duplicate or move.
        await link.call("brain_session_capture", captured)
        assert await _ledger_sessions_for(engine, artifact) == [str(owner)]

        # The rival cannot steal it, and the failure must not silently move the row.
        with pytest.raises(Exception):  # noqa: B017 - transport error type is not the point
            await link.call(
                "brain_session_capture",
                {
                    "session_id": str(rival),
                    "expected_client_key": "rival",
                    "knowledge_ids": [str(artifact)],
                },
            )
        assert await _ledger_sessions_for(engine, artifact) == [str(owner)]
        assert await _ledger_artifacts_for(engine, rival) == []


async def _open_tracer_count(engine: AsyncEngine, project_key: str) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(_OPEN_TRACERS, {"project_key": project_key}) or 0)


@pytest.mark.asyncio(loop_scope="module")
async def test_the_bench_can_see_a_tracer_at_all(mcp_base_url: str, engine: AsyncEngine) -> None:
    """Bench control, and it claims nothing about the product.

    Everything downstream assumes a tracer exists to derive INTO. If this bench
    cannot make one appear, every later assertion is theatre — it would pass by
    describing an empty world. So: one connection, one project, exactly one open
    ``agent`` session.
    """
    project_key = f"integ-w18-{uuid4().hex[:10]}"
    async with _Conn(mcp_base_url, project_key) as link:
        await _bootstrap_project(link, project_key)
        await _learning_id(link, project_key, f"w18 bench {uuid4().hex[:8]}")

        assert await _open_tracer_count(engine, project_key) == 1
