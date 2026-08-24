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
#: Un appel d'outil traverse HTTP, la base, et parfois un service d'embedding
#: injoignable qui réessaie. Large, donc — mais BORNÉ : ce qui compte n'est pas
#: la valeur, c'est qu'aucune attente ne soit infinie.
_CALL_BUDGET_SECONDS = 90.0
#: Ce qu'on accorde à une annulation pour aboutir. Courte : passé ce délai le
#: diagnostic doit sortir, même si la tâche survit.
_CANCEL_BUDGET_SECONDS = 10.0
_LINK_BUDGET_SECONDS = 30.0

#: Les clés que ce banc pose dans l'environnement du PROCESSUS. Le témoin de
#: sortie les relit une par une : une seule qui fuit arme l'auto-ouverture pour
#: tous les modules suivants du même `pytest`, et leur ajoute une traçante et
#: des connexions à chaque appel d'outil.
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
    """Attendre AVEC une borne, et NOMMER ce qui n'est pas revenu.

    Le banc n'a plus aucune attente infinie, et c'est une exigence en soi : une
    attente non bornée transforme une panne en SILENCE. La CI l'a payé deux
    fois — trente minutes de rien, un job tué, et un journal qui ne dit pas ce
    qui bloquait. Un rouge nommé au bout de N secondes vaut mieux qu'un timeout
    de job, même quand la borne se déclenche pour une bonne raison.
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
        # BORNÉE elle aussi, et c'est le défaut que W19-b avait laissé : ce
        # `await` était NU, avec le `pytest.fail` DERRIÈRE lui. Un diagnostic
        # nommé, mort sur exactement le chemin d'erreur pour lequel il est
        # écrit. FastMCP ferme son lifespan sous `anyio.CancelScope(shield=True)`
        # — l'annulation peut être absorbée, et l'attente ne revient jamais.
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
    """Prouver qu'à la sortie du module le PROCESSUS est rendu comme il a été pris.

    Ce banc est le seul de la suite à toucher des états de processus : des
    variables d'environnement, une classe de FastMCP substituée, un ouvreur
    mémoïsé, un moteur global. Chacun de ces états, laissé derrière, arme
    l'auto-ouverture ou casse le démarrage HTTP pour TOUS les modules suivants du
    même `pytest` — et ça ne se voit pas comme un rouge chez nous, ça se voit
    comme un hang chez le voisin. C'est exactement ce qui a coûté deux fois
    trente minutes de CI.

    Il tourne APRÈS ``mcp_base_url`` parce que celui-ci en dépend : pytest
    finalise à l'envers de la mise en place.
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

    # Les backends ne disparaissent pas à l'instant du `dispose()` : on ATTEND
    # qu'ils redescendent, mais avec une borne, comme tout le reste ici.
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

        # MESURÉ : `apply_tool_catalog_profile` fait `mcp.add_transform(...)` sur
        # le singleton de MODULE, et les transforms S'EMPILENT — 0, puis 1, puis
        # 2 à la deuxième application. `build_server()` en pose une ; le banc de
        # `tests/integration/metrics/` en pose une autre sur le MÊME objet. Le
        # module suivant sert donc son catalogue à travers DEUX passerelles
        # compactes chaînées. On rend la pile telle qu'on l'a prise.
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
                # MESURÉ : uvicorn 0.41.0 livre `timeout_graceful_shutdown=None`,
                # et `Server.shutdown()` fait
                # `wait_for(self._wait_tasks_to_complete(), timeout=None)` —
                # une attente INFINIE, à l'intérieur de la tâche. Aucune borne
                # posée à l'extérieur ne la libère : FastMCP ferme son lifespan
                # sous `anyio.CancelScope(shield=True)`, donc un `cancel()` peut
                # être ABSORBÉ. La seule borne qui mord est celle-ci, dedans.
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
            # Bornée elle aussi : ce chemin ne s'emprunte QUE lorsque le serveur
            # va déjà mal, et c'est exactement là qu'un `await` nu pend pour
            # toujours au lieu de rapporter.
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
                # `dispose()` ATTEND que les connexions soient réellement
                # rendues. C'est la seule attente non bornée qui restait après
                # le correctif de W17-e, et elle tombe exactement là où la CI
                # s'est tue : APRÈS le dernier test du module.
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
    """Relire la base, BORNÉ — y compris l'obtention de la connexion.

    C'est la famille d'attentes qui restait nue, et c'est celle qui a la forme
    exacte d'un hang sans erreur : **un pool saturé n'échoue pas, il ATTEND.**
    `engine.connect()` peut donc pendre indéfiniment sans qu'aucune requête ne
    soit en cause, et sans rien écrire dans le journal.
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
    """Ouvrir ou fermer la dérivation POUR DE VRAI, dans le serveur qui tourne.

    Le drapeau est lu à l'appel — par ``derive_capture`` et par le service — et
    non capturé au démarrage. Le basculer ici prouve donc les deux régimes sur
    UN seul serveur ; en monter un second appellerait ``plan_http_transport``
    une deuxième fois, ce que ``_configure_http_security`` refuse à raison.
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
    """L'unique traçante ouverte du projet — l'unicité est elle-même l'assertion."""
    rows = await _read_rows(engine, _OPEN_TRACER_IDS, {"project_key": project_key})
    assert len(rows) == 1, f"attendu une seule traçante ouverte, vu {len(rows)}"
    return str(rows[0][0])


@pytest.mark.asyncio(loop_scope="module")
async def test_capture_is_derived_and_converges_without_a_single_explicit_capture(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """La demande de l'utilisateur, prouvée depuis la base : zéro `brain_session_capture`.

    Trois temps. (a) Drapeau FERMÉ : rien n'est attribué — c'est le témoin
    négatif, et il vaut maintenant pour le régime fermé, pas pour l'absence de
    mécanisme. (b) Drapeau OUVERT : l'artefact atterrit dans la TRAÇANTE, jamais
    directement dans la session de l'utilisateur. (c) La convergence : au
    heartbeat, la session absorbe et l'artefact lui appartient.
    """
    project_key = f"integ-w18-{uuid4().hex[:10]}"
    async with _Conn(mcp_base_url, project_key) as link:
        await _bootstrap_project(link, project_key)
        session_id = await _session_id(link, project_key, "task-a")

        # (a) témoin de DRAPEAU FERMÉ
        with _derived_capture(False):
            quiet = await _learning_id(link, project_key, f"w18 closed {uuid4().hex[:8]}")
        assert await _ledger_sessions_for(engine, quiet) == []

        with _derived_capture(True):
            # (b) la dérivation dépose dans la traçante, PAS dans la session
            derived = await _learning_id(link, project_key, f"w18 derived {uuid4().hex[:8]}")
            tracer = await _sole_tracer(engine, project_key)
            assert await _ledger_sessions_for(engine, derived) == [tracer]
            assert tracer != str(session_id)

            # (c) la CONVERGENCE : une commande explicite, mais pas une capture
            await link.call(
                "brain_session_heartbeat",
                {"session_id": str(session_id), "expected_client_key": "task-a"},
            )
            assert await _ledger_sessions_for(engine, derived) == [str(session_id)]


@pytest.mark.asyncio(loop_scope="module")
async def test_the_derivation_keeps_the_row_identity_the_bound_and_the_distinction(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """(d) identité de ligne, (e) borne déclarée, (f) distinction entre connexions."""
    project_key = f"integ-w18-{uuid4().hex[:10]}"

    with _derived_capture(True):
        async with _Conn(mcp_base_url, project_key) as first:
            await _bootstrap_project(first, project_key)

            # (e) émis AVANT le `start` : hors de la fenêtre qu'une capture
            # explicite aurait acceptée, donc il doit RESTER chez la traçante.
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

                # (f) deux connexions, zéro croisement DANS LES DEUX SENS
                assert await _ledger_sessions_for(engine, mine) == [str(session_a)]
                assert await _ledger_sessions_for(engine, theirs) == [str(session_b)]

                # (e) la borne tient : l'artefact d'avant le `start` n'a pas bougé
                landed = await _ledger_sessions_for(engine, early)
                assert landed and landed != [str(session_a)]
                assert landed != [str(session_b)]

                # (d) la session de l'utilisateur n'est JAMAIS promue en traçante
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
    """Le témoin de la BORNE elle-même — et il vaut plus que la cause du hang.

    Une attente non bornée transforme une panne en silence : la CI a rendu deux
    fois trente minutes de rien, tuées par le timeout du job, avec un journal qui
    ne dit pas ce qui bloquait. Ce test prouve que `_bounded` rend un échec qui
    se NOMME, et il rougirait si quelqu'un retirait la borne — auquel cas il
    pendrait, ce qui est précisément le défaut qu'il garde.
    """

    async def never_returns() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(Failed, match="une attente de test volontairement bloquée"):
        await _bounded(
            never_returns(),
            budget=0.2,
            what="une attente de test volontairement bloquée",
        )
