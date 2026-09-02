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
import json
import os
import re
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, contextmanager, suppress
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import uvicorn
from _pytest.outcomes import Failed
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
_TRACER_ID = sa.text(
    "SELECT id::text FROM brain_sessions "
    "WHERE project_key = :project_key AND connection_id = :connection_id "
    "AND nature = 'agent' AND status = 'open'"
)
_ROW_IDENTITY = sa.text("SELECT connection_id, nature FROM brain_sessions WHERE id = :session_id")
_STARTED_FOCUS_REVISION = sa.text(
    "SELECT started_focus_revision FROM brain_sessions WHERE id = :session_id"
)
_ATTRIBUTION_MODE = sa.text(
    "SELECT attribution_mode FROM brain_session_artifacts WHERE knowledge_id = :knowledge_id"
)
_OPEN_TRACERS = sa.text(
    "SELECT count(*) FROM brain_sessions "
    "WHERE project_key = :project_key AND nature = 'agent' AND status = 'open'"
)
_OPEN_TRACER_IDS = sa.text(
    "SELECT id::text FROM brain_sessions "
    "WHERE project_key = :project_key AND nature = 'agent' AND status = 'open'"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_SHUTDOWN_BUDGET_SECONDS = 15.0
#: A tool call crosses HTTP, the database, and sometimes an unreachable embedding
#: service that retries. Generous, then — but BOUNDED: what matters is not the
#: value, it is that no wait is infinite.
_CALL_BUDGET_SECONDS = 90.0
#: What a cancellation is granted to complete. Short: past that delay the
#: diagnosis must come out, even if the task survives.
_CANCEL_BUDGET_SECONDS = 10.0
_LINK_BUDGET_SECONDS = 30.0

#: The keys this bench sets in the PROCESS environment. The exit witness reads
#: them back one by one: a single one leaking arms auto-open for every following
#: module of the same `pytest`, and adds a tracer and connections to each of their
#: tool calls.
_BENCH_ENV_KEYS = (
    "POSTGRES_URL",
    "BRAIN_MCP_TRANSPORT",
    "MCP_HTTP_STATELESS",
    "BRAIN_SESSION_AUTO_OPEN_ENABLED",
    "BRAIN_SESSION_DERIVED_CAPTURE_ENABLED",
    "BRAIN_MCP_PROFILE",
    "GRAPH_ENABLED",
    "GRAPH_LEDGER_WRITE_ENABLED",
    "DECAY_ENABLED",
    "METRICS_ENABLED",
    "CLIENT_ACTIVITY_REPORTING_ENABLED",
    "BRAIN_DREAM_CAPABILITY_ENFORCEMENT",
    "MCP_HTTP_TOKEN",
)


async def _bounded(awaitable: Any, *, budget: float, what: str) -> Any:
    """Wait WITH a bound, and NAME what did not come back.

    The bench no longer has any infinite wait, and that is a requirement in itself:
    an unbounded wait turns a failure into SILENCE. CI paid for it twice — thirty
    minutes of nothing, a killed job, and a log that does not say what was blocking.
    A named red after N seconds is worth more than a job timeout, even when the
    bound fires for a good reason.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=budget)
    except TimeoutError:
        pytest.fail(f"{what} n'est pas revenu en {budget}s — attente bornée, panne NOMMÉE")


async def _stop_or_fail(serving: asyncio.Task[None], port: int) -> None:
    """Stop uvicorn within a budget, and PROVE nothing survived the teardown.

    ``await serving`` was the FIRST unbounded wait to be closed here, in W17-e.
    It was not the only one, and saying so cost a second thirty-minute CI
    timeout: the bench also awaited ``engine.dispose()``, every tool call, every
    client open and close, and every read-back — all of them nue. They are all
    bounded now, through :func:`_bounded`.

    An unbounded wait is how a suite stops reporting anything at all: the job
    hit its 30-minute ceiling and was killed with ``Terminate orphan process:
    pytest`` — a hang, not a failure, and a hang says nothing about its cause.

    So the wait is bounded and the residue is asserted rather than assumed. Both
    checks belong here, after the real shutdown of the real server, because that
    is the only place the answer is observable.
    """
    try:
        await asyncio.wait_for(asyncio.shield(serving), timeout=_SHUTDOWN_BUDGET_SECONDS)
    except TimeoutError:
        serving.cancel()
        # BOUNDED too, and this is the defect W19-b had left behind: that `await`
        # was BARE, with the `pytest.fail` BEHIND it. A named diagnosis, dead on
        # exactly the error path it is written for. FastMCP closes its lifespan
        # under `anyio.CancelScope(shield=True)` — the cancellation can be absorbed,
        # and the wait never returns.
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(serving), timeout=_CANCEL_BUDGET_SECONDS)
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


_BACKENDS = sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")


async def _backend_count(engine: AsyncEngine) -> int:
    rows = await _read_rows(engine, _BACKENDS, {})
    return int(rows[0][0] or 0)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def module_exit_witness(engine: AsyncEngine) -> AsyncIterator[None]:
    """Prove that on the module's exit the PROCESS is returned as it was taken.

    This bench is the suite's only one to touch process state: environment
    variables, a substituted FastMCP class, a memoised opener, a global engine. Each
    of those states, left behind, arms auto-open or breaks the HTTP startup for ALL
    the following modules of the same `pytest` — and it does not show as a red on
    our side, it shows as a hang on the neighbour's. That is exactly what cost two
    lots of thirty CI minutes.

    It runs AFTER ``mcp_base_url`` because that one depends on it: pytest finalises
    in reverse order of setup.
    """
    import fastmcp.server.http as fastmcp_http

    import brain_v42.db.engine as engine_module

    before_env = {key: os.environ.get(key) for key in _BENCH_ENV_KEYS}
    from brain_v42.mcp.server import mcp as shared_mcp

    before_manager = fastmcp_http.StreamableHTTPSessionManager
    before_transforms = list(shared_mcp.transforms)
    before_engine = engine_module._engine
    before_factory = engine_module._session_factory
    before_backends = await _backend_count(engine)

    yield

    leaked = {
        key: (was, os.environ.get(key))
        for key, was in before_env.items()
        if os.environ.get(key) != was
    }
    assert not leaked, (
        "le banc laisse des variables d'environnement modifiées derrière lui ; "
        f"tout module suivant du même processus les verra : {leaked}"
    )
    assert fastmcp_http.StreamableHTTPSessionManager is before_manager, (
        "le gestionnaire de session de FastMCP reste substitué : le prochain "
        "module HTTP ne démarrera plus son serveur"
    )
    assert list(shared_mcp.transforms) == before_transforms, (
        f"le banc empile des transforms sur le singleton partagé : "
        f"{len(before_transforms)} à l'entrée, {len(shared_mcp.transforms)} à la sortie. "
        "Le module suivant servirait son catalogue à travers des passerelles chaînées"
    )
    assert engine_module._engine is before_engine, "le moteur global n'a pas été rendu"
    assert engine_module._session_factory is before_factory, (
        "la factory de session globale n'a pas été rendue"
    )

    # The backends do not disappear at the instant of `dispose()`: we WAIT for them
    # to come down, but with a bound, like everything else here.
    deadline = _SHUTDOWN_BUDGET_SECONDS
    while deadline > 0:
        after = await _backend_count(engine)
        if after <= before_backends:
            break
        await asyncio.sleep(0.25)
        deadline -= 0.25
    else:
        pytest.fail(
            f"connexions laissées ouvertes : {after} backends contre {before_backends} "
            "à l'entrée du module — un pool saturé n'échoue pas, il ATTEND"
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mcp_base_url(module_exit_witness: None) -> AsyncIterator[str]:
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

        # MEASURED: `apply_tool_catalog_profile` does `mcp.add_transform(...)` on
        # the MODULE singleton, and the transforms STACK — 0, then 1, then 2 at the
        # second application. `build_server()` adds one; the
        # `tests/integration/metrics/` bench adds another on the SAME object. The
        # next module therefore serves its catalogue through TWO chained compact
        # gateways. We return the stack as we took it.
        from brain_v42.mcp.server import mcp as shared_mcp

        original_transforms = list(shared_mcp.transforms)

        built = build_server()
        await _bounded(
            prepare_tools_for_transport(built.mcp, built.metrics_collector),
            budget=_LINK_BUDGET_SECONDS,
            what="la préparation des tools avant transport",
        )
        plan = plan_http_transport(built.mcp, built.settings)
        app = built.mcp.http_app(
            middleware=plan.middleware,
            json_response=plan.json_response,
            stateless_http=plan.stateless_http,
        )

        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                loop="asyncio",
                # MEASURED: uvicorn 0.41.0 ships `timeout_graceful_shutdown=None`,
                # and `Server.shutdown()` does
                # `wait_for(self._wait_tasks_to_complete(), timeout=None)` — an
                # INFINITE wait, inside the task. No bound set from the outside
                # releases it: FastMCP closes its lifespan under
                # `anyio.CancelScope(shield=True)`, so a `cancel()` can be ABSORBED.
                # The only bound that bites is this one, inside.
                timeout_graceful_shutdown=5,
            )
        )
        serving = asyncio.create_task(server.serve())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if server.started:
                break
        else:
            server.should_exit = True
            # Bounded too: this path is only taken when the server is ALREADY
            # unwell, and that is exactly where a bare `await` hangs forever instead
            # of reporting.
            await _bounded(
                serving,
                budget=_SHUTDOWN_BUDGET_SECONDS,
                what="l'arrêt d'un serveur qui n'a jamais démarré",
            )
            pytest.fail("uvicorn did not start within 5s")

        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            fastmcp_http.StreamableHTTPSessionManager = original_session_manager
            shared_mcp._transforms[:] = original_transforms
            reset_session_autoopener()
            await _stop_or_fail(serving, port)
            if engine_module._engine is not None:
                # `dispose()` WAITS for the connections to be genuinely returned.
                # It is the only unbounded wait left after W17-e's fix, and it falls
                # exactly where CI went silent: AFTER the module's last test.
                await _bounded(
                    engine_module._engine.dispose(),
                    budget=_SHUTDOWN_BUDGET_SECONDS,
                    what="la libération du moteur du banc (engine.dispose)",
                )
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
        self._client = await _bounded(
            self._stack.enter_async_context(Client(transport)),
            budget=_LINK_BUDGET_SECONDS,
            what="l'ouverture du lien client",
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await _bounded(
            self._stack.aclose(),
            budget=_LINK_BUDGET_SECONDS,
            what="la fermeture du lien client",
        )

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        result = await _bounded(
            self._client.call_tool(tool, arguments),
            budget=_CALL_BUDGET_SECONDS,
            what=f"l'appel de tool {tool!r}",
        )
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


_READ_BUDGET_SECONDS = 30.0


async def _read_rows(engine: AsyncEngine, statement: Any, params: dict[str, Any]) -> list[Any]:
    """Read the database back, BOUNDED — the connection acquisition included.

    This is the family of waits that stayed bare, and it is the one with the exact
    shape of a hang with no error: **a saturated pool does not fail, it WAITS.**
    `engine.connect()` can therefore hang indefinitely with no query at fault, and
    without writing anything to the log.
    """

    async def _run() -> list[Any]:
        async with engine.connect() as conn:
            return list((await conn.execute(statement, params)).all())

    return await _bounded(_run(), budget=_READ_BUDGET_SECONDS, what="une relecture de la base")


async def _ledger_sessions_for(engine: AsyncEngine, knowledge_id: UUID) -> list[str]:
    rows = await _read_rows(engine, _LEDGER_ROWS_FOR_ARTIFACT, {"knowledge_id": str(knowledge_id)})
    return sorted(str(row[0]) for row in rows)


async def _ledger_artifacts_for(engine: AsyncEngine, session_id: UUID) -> list[str]:
    rows = await _read_rows(engine, _LEDGER_ROWS_FOR_SESSION, {"session_id": str(session_id)})
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
        found = await _read_rows(engine, _LEARNING_EXISTS, {"knowledge_id": str(artifact)})
        assert found[0][0] == 1

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
    rows = await _read_rows(engine, _OPEN_TRACERS, {"project_key": project_key})
    return int(rows[0][0] or 0)


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


@contextmanager
def _derived_capture(enabled: bool) -> Iterator[None]:
    """Open or close the derivation FOR REAL, in the running server.

    The flag is read at call time — by ``derive_capture`` and by the service — and
    not captured at startup. Toggling it here therefore proves both regimes on ONE
    server; standing up a second would call ``plan_http_transport`` a second time,
    which ``_configure_http_security`` rightly refuses.
    """
    from brain_v42.config import get_settings

    key = "BRAIN_SESSION_DERIVED_CAPTURE_ENABLED"
    previous = os.environ.get(key)
    os.environ[key] = "true" if enabled else "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
        get_settings.cache_clear()


async def _tracer_of(engine: AsyncEngine, project_key: str, connection_id: str) -> str | None:
    rows = await _read_rows(
        engine, _TRACER_ID, {"project_key": project_key, "connection_id": connection_id}
    )
    return str(rows[0][0]) if rows else None


async def _sole_tracer(engine: AsyncEngine, project_key: str) -> str:
    """The project's single open tracer — the uniqueness is itself the assertion."""
    rows = await _read_rows(engine, _OPEN_TRACER_IDS, {"project_key": project_key})
    assert len(rows) == 1, f"attendu une seule traçante ouverte, vu {len(rows)}"
    return str(rows[0][0])


@pytest.mark.asyncio(loop_scope="module")
async def test_capture_is_derived_and_converges_without_a_single_explicit_capture(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """The user's request, proved from the database: zero `brain_session_capture`.

    Three moments. (a) Flag CLOSED: nothing is attributed — this is the negative
    witness, and it now holds for the closed regime, not for the absence of a
    mechanism. (b) Flag OPEN: the artifact lands in the TRACER, never directly in
    the user's session. (c) The convergence: at the heartbeat, the session absorbs
    and the artifact belongs to it.
    """
    project_key = f"integ-w18-{uuid4().hex[:10]}"
    async with _Conn(mcp_base_url, project_key) as link:
        await _bootstrap_project(link, project_key)
        session_id = await _session_id(link, project_key, "task-a")

        # (a) CLOSED-FLAG witness
        with _derived_capture(False):
            quiet = await _learning_id(link, project_key, f"w18 closed {uuid4().hex[:8]}")
        assert await _ledger_sessions_for(engine, quiet) == []

        with _derived_capture(True):
            # (b) the derivation deposits into the tracer, NOT into the session
            derived = await _learning_id(link, project_key, f"w18 derived {uuid4().hex[:8]}")
            tracer = await _sole_tracer(engine, project_key)
            assert await _ledger_sessions_for(engine, derived) == [tracer]
            assert tracer != str(session_id)

            # (c) the CONVERGENCE: an explicit command, but not a capture
            await link.call(
                "brain_session_heartbeat",
                {"session_id": str(session_id), "expected_client_key": "task-a"},
            )
            assert await _ledger_sessions_for(engine, derived) == [str(session_id)]


@pytest.mark.asyncio(loop_scope="module")
async def test_the_derivation_keeps_the_row_identity_the_bound_and_the_distinction(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """(d) row identity, (e) declared bound, (f) distinction between connections."""
    project_key = f"integ-w18-{uuid4().hex[:10]}"

    with _derived_capture(True):
        async with _Conn(mcp_base_url, project_key) as first:
            await _bootstrap_project(first, project_key)

            # (e) emitted BEFORE the `start`: outside the window an explicit
            # capture would have accepted, so it must STAY with the tracer.
            early = await _learning_id(first, project_key, f"w18 early {uuid4().hex[:8]}")
            session_a = await _session_id(first, project_key, "task-a")
            mine = await _learning_id(first, project_key, f"w18 mine {uuid4().hex[:8]}")

            async with _Conn(mcp_base_url, project_key) as second:
                session_b = await _session_id(second, project_key, "task-b")
                theirs = await _learning_id(second, project_key, f"w18 theirs {uuid4().hex[:8]}")

                await first.call(
                    "brain_session_heartbeat",
                    {"session_id": str(session_a), "expected_client_key": "task-a"},
                )
                await second.call(
                    "brain_session_heartbeat",
                    {"session_id": str(session_b), "expected_client_key": "task-b"},
                )

                # (f) two connections, zero crossing IN BOTH DIRECTIONS
                assert await _ledger_sessions_for(engine, mine) == [str(session_a)]
                assert await _ledger_sessions_for(engine, theirs) == [str(session_b)]

                # (e) the bound holds: the pre-`start` artifact has not moved
                landed = await _ledger_sessions_for(engine, early)
                assert landed and landed != [str(session_a)]
                assert landed != [str(session_b)]

                # (d) the user's session is NEVER promoted into a tracer
                for session in (session_a, session_b):
                    identity = (
                        await _read_rows(engine, _ROW_IDENTITY, {"session_id": str(session)})
                    )[0]
                    assert tuple(identity) == (None, None), (
                        "une session d'utilisateur qui gagne connection_id/nature "
                        "deviendrait re-datable par le serveur, donc un fantôme "
                        "que le balayage 7 j ne peut plus atteindre"
                    )


@pytest.mark.asyncio(loop_scope="module")
async def test_a_stalled_await_becomes_a_named_failure_not_a_hang() -> None:
    """The witness of the BOUND itself — and it is worth more than the hang's cause.

    An unbounded wait turns a failure into silence: CI returned two lots of thirty
    minutes of nothing, killed by the job timeout, with a log that does not say what
    was blocking. This test proves that `_bounded` returns a failure that NAMES
    itself, and it would redden if someone removed the bound — in which case it
    would hang, which is precisely the defect it guards.
    """

    async def never_returns() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(Failed, match="une attente de test volontairement bloquée"):
        await _bounded(
            never_returns(),
            budget=0.2,
            what="une attente de test volontairement bloquée",
        )


async def _started_focus_revision(engine: AsyncEngine, session_id: UUID) -> int:
    """The revision the session saw at its opening, read back from the database.

    `end` requires a compare-and-swap; reading it here avoids making the scene
    depend on `brain_session_start`'s textual rendering. A concurrent revision still
    closes the session (`focus_outcome='conflict'`), so this test cannot redden for
    a focus reason.
    """
    rows = await _read_rows(engine, _STARTED_FOCUS_REVISION, {"session_id": str(session_id)})
    return int(rows[0][0])


def _end_result(payload: str) -> dict[str, Any]:
    """`brain_session_end`'s result, decoded — and a decoding failure SAYS so.

    A test that silently fell back on a regex would redden later for a formatting
    reason, while presenting itself as a proof of attribution.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic
        raise AssertionError(
            f"réponse de brain_session_end non décodable ({exc}) : {payload[:400]!r}"
        ) from exc
    assert isinstance(decoded, dict), f"forme inattendue : {type(decoded).__name__}"
    return decoded


@pytest.mark.asyncio(loop_scope="module")
async def test_the_session_absorbs_across_a_transport_that_died(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """The production scene, with TWO connections. RED today.

    This is the test this file's "one connection, one tracer" invariant (``_Conn``)
    made unwritable until now: each scene held a single client from start to finish,
    so the per-connection pairing could not fail there. The suite was green while
    production was inert.

    No uvicorn restart is simulated, and that is deliberate: in production the
    `32662769` tracer was born AFTER the 23:18:22Z restart, then its transport was
    killed by the 900 s idle timeout (``mcp_http_session_idle_seconds``) 23 min
    before the `end`. The restart is INCIDENTAL; changing `Mcp-Session-Id` is the
    whole hole. A test that restarted the server would test the wrong thing.

    It cannot self-fulfil its hypothesis: the `Mcp-Session-Id` is minted by the SDK
    when the link opens, never set by the test. Two successive ``_Conn`` are two
    transports, measured as such.
    """
    project_key = f"integ-w20-{uuid4().hex[:10]}"

    with _derived_capture(True):
        # Connection 1: the session opens, the artifact derives into ITS tracer.
        async with _Conn(mcp_base_url, project_key) as first:
            await _bootstrap_project(first, project_key)
            session_id = await _session_id(first, project_key, "task-w20")
            derived = await _learning_id(first, project_key, f"w20 derived {uuid4().hex[:8]}")

            tracer = await _sole_tracer(engine, project_key)
            assert tracer != str(session_id)
            assert await _ledger_sessions_for(engine, derived) == [tracer], (
                "la dérivation elle-même est cassée — le rouge ci-dessous ne "
                "dirait rien de l'absorption"
            )

        # The link is closed: this `Mcp-Session-Id` will never come back. The
        # tracer's ROW, for its part, stays `open` — exactly the state measured in
        # production at the moment of the failed closure.
        revision = await _started_focus_revision(engine, session_id)

        # Connection 2: fresh transport, fresh and EMPTY tracer. The user closes
        # their session from there, as they did on 2026-08-24 at 23:58Z.
        async with _Conn(mcp_base_url, project_key) as second:
            payload = await second.call(
                "brain_session_end",
                {
                    "session_id": str(session_id),
                    "expected_client_key": "task-w20",
                    "summary": "Banc w20 : fermeture depuis un transport neuf.",
                    "next_focus": "Vérifier l'absorption à travers un transport mort.",
                    "expected_focus_revision": revision,
                },
            )

    # (1) THE PROOF, read back from the database and not from the tool's return.
    assert await _ledger_sessions_for(engine, derived) == [str(session_id)], (
        "l'artefact est resté dans la traçante d'un transport mort : la "
        "promesse faite à l'utilisateur n'est pas tenue"
    )

    # (2) And the receipt returned to the user must say the same as the database.
    result = _end_result(payload)
    assert result["session"]["attributed_knowledge_ids"] == [str(derived)]
    assert result["unattributed_in_window"] == 0


async def _attribution_mode(engine: AsyncEngine, knowledge_id: UUID) -> str | None:
    rows = await _read_rows(engine, _ATTRIBUTION_MODE, {"knowledge_id": str(knowledge_id)})
    assert rows, "aucune ligne de ledger pour cet artefact"
    return None if rows[0][0] is None else str(rows[0][0])


@pytest.mark.asyncio(loop_scope="module")
async def test_the_absorption_says_by_which_key_it_matched(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """BY WHICH KEY — without which a silent regression would stay green.

    The sibling test proves the artifact ARRIVES. This one proves by which path:
    `derived_window`, the DEDUCTION, and not `derived_connection`, the proof. If the
    exact layer started answering here again some day, that would be good news — but
    it must be VISIBLE, and a total would not show it.
    """
    project_key = f"integ-w20-{uuid4().hex[:10]}"

    with _derived_capture(True):
        async with _Conn(mcp_base_url, project_key) as first:
            await _bootstrap_project(first, project_key)
            session_id = await _session_id(first, project_key, "task-key")
            derived = await _learning_id(first, project_key, f"w20 key {uuid4().hex[:8]}")
            assert await _attribution_mode(engine, derived) == "derived_deposit"

        revision = await _started_focus_revision(engine, session_id)
        async with _Conn(mcp_base_url, project_key) as second:
            await second.call(
                "brain_session_end",
                {
                    "session_id": str(session_id),
                    "expected_client_key": "task-key",
                    "summary": "Banc w20 : quelle clé a apparié.",
                    "next_focus": "Lire attribution_mode en base.",
                    "expected_focus_revision": revision,
                },
            )

    assert await _ledger_sessions_for(engine, derived) == [str(session_id)]
    assert await _attribution_mode(engine, derived) == "derived_window"


@pytest.mark.asyncio(loop_scope="module")
async def test_two_concurrent_user_sessions_leave_the_artifact_where_it_is(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """The NEGATIVE witness, without which the test does not measure the rule.

    Two user sessions cover the creation instant. Neither has more claim than the
    other: nobody absorbs, and the artifact stays with the tracer — a VISIBLE
    orphan, which `unattributed_in_window` now counts as such and which a human can
    take back by naming its UUID.

    This is the accepted price of symmetric rivalry: the promise is not kept while
    two sessions overlap. A batch that attributed anyway would have chosen theft
    over abstention.
    """
    project_key = f"integ-w20-{uuid4().hex[:10]}"

    with _derived_capture(True):
        async with _Conn(mcp_base_url, project_key) as first:
            await _bootstrap_project(first, project_key)
            mine = await _session_id(first, project_key, "task-mine")
            async with _Conn(mcp_base_url, project_key) as rival_link:
                rival = await _session_id(rival_link, project_key, "task-rival")
                assert rival != mine

                derived = await _learning_id(first, project_key, f"w20 contested {uuid4().hex[:8]}")
                tracer = await _ledger_sessions_for(engine, derived)

        revision = await _started_focus_revision(engine, mine)
        async with _Conn(mcp_base_url, project_key) as third:
            payload = await third.call(
                "brain_session_end",
                {
                    "session_id": str(mine),
                    "expected_client_key": "task-mine",
                    "summary": "Banc w20 : deux prétendantes.",
                    "next_focus": "Vérifier que personne n'a absorbé.",
                    "expected_focus_revision": revision,
                },
            )

    # The artifact has moved nowhere — read back from the database.
    assert await _ledger_sessions_for(engine, derived) == tracer
    assert tracer != [str(mine)] and tracer != [str(rival)]

    # And the receipt no longer lies: an artifact parked with the server COUNTS as
    # unattributed. That is what makes the refusal visible instead of mute.
    result = _end_result(payload)
    assert result["session"]["attributed_knowledge_ids"] == []
    assert result["unattributed_in_window"] >= 1
