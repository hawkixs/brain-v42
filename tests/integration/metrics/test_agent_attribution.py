"""Integration tests for per-agent metric attribution via X-Brain-Agent header (Task 1.1, H6).

Starts the FastMCP HTTP server in-process on an ephemeral loopback port using
uvicorn, then fires concurrent tool calls from two fastmcp.Client sessions
with distinct X-Brain-Agent headers.

Tool choice: brain_list_projects — a cheap DB-only list query with no embedding
dependency, so the test does not require the GPU embedding service to be live.
What matters is that the call is *recorded* under the right agent bucket, not
the tool's result.

H6 risk: a naive middleware-contextvar relay would collapse to a single agent
label under concurrency. This test asserts RAW store isolation (_tool_stats[agent][tool])
which would catch any cross-contamination.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from contextlib import suppress
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
import uvicorn

from brain_v42.config import get_settings
from brain_v42.metrics.collector import MetricsCollector
from tests.integration.metrics.task_dump import collect_probes, dump_tasks_after

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_free_port() -> int:
    """Bind a TCP socket to get an OS-assigned ephemeral port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Server fixture — ephemeral HTTP server on 127.0.0.1:<random port>
# ---------------------------------------------------------------------------


# LE BUDGET DE L'ITEM, REFAIT. La première rédaction annonçait « 60 s de corps
# + 40 s de teardown = 100 s, sous le filet à 120 » et se trompait deux fois :
# le `faulthandler_timeout` est armé sur le PROTOCOLE de l'item — setup + call +
# teardown — et le corps porte DEUX attentes bornées CONSÉCUTIVES, pas une. Le
# vrai pire cas était 5 (démarrage) + 60 + 60 + 15 + 10 (arrêt) + 15 (dispose),
# soit 165 s ; même en s'arrêtant au premier dépassement il restait 5 + 60 + 40
# = 105 s. Et le runner est mesuré 22 % plus lent le 2026-08-25 (148 s contre
# 120,9 s sur la même suite) : 105 × 1,22 = 128 > 120. L'item franchissait donc
# le filet sur le profil de runner qui produit la panne, et sortait par
# `os._exit` — précisément le cas où l'on veut un verdict de test.
#
# Nouveau pire cas : 5 + 25 + 25 + 8 + 5 + 8 = 76 s, soit 93 s à +22 %, contre
# un filet à 120. La marge est de 27 s, elle est écrite ici, et elle se refait
# à la main dès qu'une de ces constantes bouge. Le normal mesuré de ces appels
# est sous la seconde : 25 s reste deux ordres de grandeur au-dessus.
_STARTUP_BUDGET_SECONDS = 5.0
_STARTUP_POLL_SECONDS = 0.1
_STOP_BUDGET_SECONDS = 8.0
_CANCEL_BUDGET_SECONDS = 5.0
_DISPOSE_BUDGET_SECONDS = 8.0
#: Large, mais BORNÉ : un appel qui ne revient pas doit se nommer bien avant
#: le `faulthandler_timeout` de 120 s, qui est le filet et non la borne.
_CALL_BUDGET_SECONDS = 25.0


def _dump_deadline(default: float) -> float:
    """Deadline d'un chien de garde de DIAGNOSTIC, armée SOUS sa borne applicative.

    Sous, parce qu'il doit tirer PENDANT que la tâche est encore suspendue :
    après le `wait_for`, `Task.get_stack()` d'une tâche déjà annulée rend une
    liste vide et le dump devient du code mort sur son propre chemin d'erreur.

    Surchargeable par l'environnement pour une seule raison : PROUVER que le dump
    tire sur le chemin réel. `BRAIN_TEST_TASK_DUMP_DEADLINE=0.001 pytest …` le
    déclenche sur un run sain, où l'attente observée est le vrai handshake
    `initialize`. Un diagnostic qu'on ne sait pas déclencher à la demande ne se
    vérifie jamais.
    """
    override = os.environ.get("BRAIN_TEST_TASK_DUMP_DEADLINE", "").strip()
    return float(override) if override else default


#: Trois zones, trois deadlines — parce que le premier lot n'armait le dump
#: qu'autour des deux attentes du CORPS et laissait nues les trois autres.
_CALL_DUMP_DEADLINE = _dump_deadline(10.0)
_STARTUP_DUMP_DEADLINE = _dump_deadline(3.0)
_TEARDOWN_DUMP_DEADLINE = _dump_deadline(4.0)


class Bench(NamedTuple):
    """Ce que le banc rend : l'URL, le collecteur, ET les objets à SONDER.

    Le serveur uvicorn et le FastMCP servant l'app remontent jusqu'au test parce
    que le dump de diagnostic les relève : `len(server.server_state.tasks)` est la
    mesure DIRECTE des « requêtes en vol » que le `ASGI callable returned without
    completing response` de l'arrêt ne fait qu'inférer.
    """

    base_url: str
    collector: MetricsCollector
    mcp: Any
    server: Any


async def _stop_bounded(
    task: asyncio.Task[None],
    *,
    what: str,
    stop_budget: float = _STOP_BUDGET_SECONDS,
    cancel_budget: float = _CANCEL_BUDGET_SECONDS,
) -> str | None:
    """Attendre l'arrêt d'une tâche AVEC une borne, et rendre ce qui n'est pas revenu.

    Rend ``None`` quand tout s'est arrêté, sinon le libellé à rapporter. Elle ne
    lève pas elle-même : l'appelant décide OÙ le `pytest.fail` est atteignable,
    ce qui est précisément ce que l'ancienne forme ratait.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=stop_budget)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=cancel_budget)
        return what
    return None


async def _fail_to_start(server: Any, server_task: asyncio.Task[None]) -> None:
    """Rendre la main sur un serveur qui n'a jamais démarré, puis ÉCHOUER.

    `should_exit = True` NE LIBÈRE RIEN ici, et c'est structurel : `Server._serve`
    fait `await self.startup()` PUIS `if not self.should_exit: await
    self.main_loop()`, et `main_loop` est le SEUL endroit qui relit le drapeau. Si
    `LifespanOn.startup()` bloque sur `startup_event.wait()` — sans timeout — on
    n'y arrive jamais. L'ancien `await server_task` nu rendait donc le
    `pytest.fail` INATTEIGNABLE sur exactement le chemin pour lequel il est écrit.
    On annule, borné, et on rapporte quoi qu'il arrive.
    """
    server.should_exit = True
    await _stop_bounded(server_task, what="un serveur metrics qui n'a jamais démarré")
    pytest.fail(f"uvicorn did not start within {_STARTUP_BUDGET_SECONDS}s")


@pytest_asyncio.fixture
async def http_server_and_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Start a real FastMCP HTTP server on an ephemeral loopback port.

    Yields (base_url: str, collector: MetricsCollector).

    Settings applied:
    - BRAIN_MCP_TRANSPORT=http   (so build_services sees HTTP mode)
    - METRICS_ENABLED=true       (so instrument_tool is active)
    - GRAPH_ENABLED=false        (no Neo4j required)
    - DECAY_ENABLED=false        (no decay flusher)
    - EMBEDDING_SERVICE_URL points to dev-pc but we don't call embedding tools
    """
    from tests.integration.conftest import INTEGRATION_DB_URL

    monkeypatch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
    monkeypatch.setenv("BRAIN_MCP_TRANSPORT", "http")
    monkeypatch.setenv("BRAIN_MCP_PROFILE", "compact")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    # Le couple, jamais la moitié — même raison que test_lifecycle.
    monkeypatch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "false")
    monkeypatch.setenv("DECAY_ENABLED", "false")
    get_settings.cache_clear()

    import brain_v42.db.engine as engine_module

    # Reset engine singleton so build_services picks up fresh settings
    original_engine = engine_module._engine
    original_factory = engine_module._session_factory
    engine_module._engine = None
    engine_module._session_factory = None

    from brain_v42.mcp.server import build_services, create_mcp_instance

    # Instance PROPRE au banc, jamais le singleton de module : le singleton
    # porte les tools que tests/integration/mcp/** y a enregistrés, fermés
    # sur un engine que leur teardown a déjà dispose() — 20 « Component
    # already exists » mesurés, et l'ordre de collecte devenait signifiant
    # (ticket 83d8785b). La factory vient de server.py : un seul câblage,
    # provenance comprise, pas un double monté à la main.
    mcp = create_mcp_instance()

    # TÉMOIN D'ENTRÉE — relevé SEUL, aucune assertion.
    #
    # Depuis l'isolement (ticket 83d8785b), `mcp` est une instance NEUVE : ce
    # relevé ne peut plus voir de tools hérités. Il garde sa valeur pour l'état
    # de PROCESSUS que l'isolement ne purge pas — le latch de classe
    # `sse_starlette.sse.AppStatus.should_exit`, ci-dessous.
    #
    # Ce qui se joue ici est UNE ligne du relevé :
    # `sse_starlette.sse.AppStatus.should_exit`. C'est un attribut de CLASSE, donc
    # global au processus et jamais remis à False ; `True` à l'entrée dit qu'un
    # module antérieur a armé le latch qui fait sortir
    # `_listen_for_exit_signal` immédiatement et sans un mot — la forme mesurée de
    # la panne, où le serveur envoie les en-têtes SSE puis revient sans corps. Ne
    # pas le confondre avec `uvicorn.Server.should_exit`, relevé lui aussi mais
    # propre à l'instance de ce banc.
    #
    # Une assertion ici convertirait un coin-flip en rouge franc avant qu'on sache
    # ce que cette valeur vaut en pratique : on MESURE d'abord. La contradiction
    # reste ouverte et n'est pas lissée — les six tests e2e passent, alors qu'un
    # latch armé tôt devrait les tuer.
    print(
        "-- témoin d'entrée du banc metrics (relevé, non assertif) --\n"
        + "\n".join(f"{name} = {value}" for name, value in collect_probes(mcp=mcp).items()),
        file=sys.stderr,
        flush=True,
    )

    services = build_services()
    collector: MetricsCollector = services["metrics_collector"]

    # Register tools with metrics instrumentation
    from brain_v42.mcp.tools.brain_tools import register_tools  # noqa: PLC0415

    register_tools(
        mcp=mcp,
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        snippet_svc=services["snippet_svc"],
        runbook_svc=services["runbook_svc"],
        adr_svc=services["adr_svc"],
        project_context_svc=services["project_context_svc"],
        brain_svc=services["brain_svc"],
        roadmap_svc=services.get("roadmap_svc"),
        metrics_collector=collector,
    )

    # Les métriques ne sont plus posées par register_tools (ticket c352eaaa) :
    # elles enveloppent les tools APRÈS enregistrement. Ce boot est monté à la
    # main plutôt que par _run_mcp, il doit donc reproduire ce point de câblage
    # — sans quoi il testerait un serveur que la production ne construit plus.
    from brain_v42.metrics.tool_instrumentation import (  # noqa: PLC0415
        instrument_registered_tools,
    )

    await instrument_registered_tools(mcp, collector)

    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile  # noqa: PLC0415

    profiled_mcp = apply_tool_catalog_profile(mcp, "compact")

    # Build ASGI app (stateless_http so each request gets a fresh transport).
    # json_response=True : le transport de PRODUCTION (plan_http_transport),
    # jamais le SSE-sur-POST que le défaut de http_app() choisirait — voir le
    # message du commit et le ticket 85559792 pour ce que ce banc CESSE de
    # prouver en quittant ce transport fantôme.
    app = profiled_mcp.http_app(stateless_http=True, json_response=True)

    port = _get_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
        # MESURÉ : uvicorn 0.41.0 livre `timeout_graceful_shutdown=None`, et
        # `Server.shutdown()` attend alors `_wait_tasks_to_complete()` SANS
        # borne, à l'intérieur de la tâche. Aucune borne extérieure ne libère
        # ça : FastMCP ferme son lifespan sous `anyio.CancelScope(shield=True)`,
        # donc l'annulation peut être absorbée.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    # Start uvicorn in a background task
    server_task = asyncio.create_task(server.serve())

    # LE DÉMARRAGE, SOUS CHIEN DE GARDE. Cette boucle attend `LifespanOn.startup()`,
    # qui attend `startup_event.wait()` SANS timeout : une des trois zones que le
    # premier lot laissait nues. Un démarrage qui ne vient jamais rendait
    # « uvicorn did not start » — vrai, et muet sur QUI attendait.
    async with dump_tasks_after(
        _STARTUP_DUMP_DEADLINE,
        label="metrics/uvicorn startup",
        mcp=profiled_mcp,
        server=server,
    ):
        for _ in range(int(_STARTUP_BUDGET_SECONDS / _STARTUP_POLL_SECONDS)):
            await asyncio.sleep(_STARTUP_POLL_SECONDS)
            if server.started:
                break
        else:
            await _fail_to_start(server, server_task)

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield Bench(base_url=base_url, collector=collector, mcp=profiled_mcp, server=server)
    finally:
        # Les globales sont rendues AVANT toute attente bornée : un
        # `pytest.fail` déclenché plus bas ne doit pas laisser le moteur du banc
        # installé pour les modules suivants.
        leftover_engine = engine_module._engine
        engine_module._engine = original_engine
        engine_module._session_factory = original_factory
        get_settings.cache_clear()

        # L'ARRÊT, SOUS CHIEN DE GARDE — et c'est la zone qui portait le seul
        # indice qu'on n'a jamais su lire : les deux `ASGI callable returned
        # without completing response` apparaissent À LA FERMETURE, pas pendant
        # les appels. `len(server.server_state.tasks)`, relevé par les sondes,
        # MESURE les requêtes encore en vol que ce message ne fait qu'inférer.
        # Le `pytest.fail` reste HORS du garde : le chien est déjà joint quand le
        # verdict tombe.
        async with dump_tasks_after(
            _TEARDOWN_DUMP_DEADLINE,
            label="metrics/teardown (arrêt uvicorn + dispose moteur)",
            mcp=profiled_mcp,
            server=server,
        ):
            server.should_exit = True
            stalled = await _stop_bounded(server_task, what="l'arrêt du serveur metrics")
            if leftover_engine is not None:
                try:
                    await asyncio.wait_for(
                        leftover_engine.dispose(), timeout=_DISPOSE_BUDGET_SECONDS
                    )
                except TimeoutError:
                    stalled = stalled or "la libération du moteur (engine.dispose)"
        if stalled is not None:
            pytest.fail(
                f"{stalled} n'est pas revenu dans son budget — attente bornée, panne NOMMÉE. "
                "Un serveur ou un moteur laissé vivant ici fait PENDRE le module suivant "
                "au lieu de le faire échouer."
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_attribution_concurrent(
    http_server_and_collector: Bench,
) -> None:
    """Two concurrent clients with distinct X-Brain-Agent headers record
    tool calls under isolated agent buckets in _tool_stats.

    Tool: brain_list_projects — no embedding, cheap DB-only list query.
    Concurrent: asyncio.gather asserts the contextvar relay is per-request
    (H6 — a middleware-based relay would collapse to a single agent).
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    bench = http_server_and_collector
    base_url, collector = bench.base_url, bench.collector
    # Le libellé porte l'identité : depuis l'isolement (83d8785b) il doit dire
    # False — s'il redisait True, le banc serait retombé sur le singleton et
    # les compteurs de lifespan relevés seraient ceux d'autres modules.
    from brain_v42.mcp.server import mcp as shared_mcp  # noqa: PLC0415

    label_suffix = f"profiled is singleton: {bench.mcp is shared_mcp}"

    async def visible_tools(profile: str | None = None) -> set[str]:
        headers = {"x-brain-tool-profile": profile} if profile else None
        transport = StreamableHttpTransport(url=f"{base_url}/mcp/", headers=headers)
        async with Client(transport) as client:
            listed = await asyncio.wait_for(client.list_tools(), timeout=_CALL_BUDGET_SECONDS)
            return {tool.name for tool in listed}

    # BORNÉ, et c'est ici que la CI a pendu : run 32779161805, `Timeout
    # (0:02:00)!` avec le nodeid de ce test imprimé juste après le dump. Le
    # corps du test n'avait AUCUNE borne — quatre attentes réseau nues — donc la
    # panne coûtait le timeout du job entier au lieu de se nommer.
    #
    # Le dump est armé AUTOUR de l'attente, jamais dans son `except TimeoutError` :
    # `wait_for` annule le gather, ATTEND l'annulation, PUIS lève — là-bas les deux
    # tâches sont déjà terminées et leurs frames ont disparu. Le blocage MESURÉ
    # n'est d'ailleurs pas dans `list_tools` mais dans `Client.__aenter__` ->
    # `client.py:571 ready_event.wait()`, donc le handshake `initialize` : le
    # message d'erreur ci-dessous a envoyé deux analyses sur une fausse piste.
    try:
        async with dump_tasks_after(
            _CALL_DUMP_DEADLINE,
            label=f"metrics/list_tools ({label_suffix})",
            mcp=bench.mcp,
            server=bench.server,
        ):
            compact_names, native_names = await asyncio.wait_for(
                asyncio.gather(visible_tools(), visible_tools("native")),
                timeout=_CALL_BUDGET_SECONDS,
            )
    except TimeoutError:
        pytest.fail(
            "lister le catalogue n'est jamais revenu — le serveur du banc metrics "
            "ne répond pas, attente bornée, panne NOMMÉE"
        )
    assert "brain_search" not in compact_names
    assert "brain_search" in native_names
    assert len(native_names) > len(compact_names)

    async def call_as_agent(agent_name: str) -> None:
        transport = StreamableHttpTransport(
            url=f"{base_url}/mcp/",
            headers={"x-brain-agent": agent_name},
        )
        async with Client(transport) as client:
            # brain_list_projects: cheap, no embedding
            await asyncio.wait_for(
                client.call_tool("brain_list_projects", {}), timeout=_CALL_BUDGET_SECONDS
            )

    # Fire both agents concurrently — this is the H6 stress point
    try:
        async with dump_tasks_after(
            _CALL_DUMP_DEADLINE,
            label=f"metrics/call_tool concurrent ({label_suffix})",
            mcp=bench.mcp,
            server=bench.server,
        ):
            await asyncio.wait_for(
                asyncio.gather(call_as_agent("red-shrik"), call_as_agent("red-codex")),
                timeout=_CALL_BUDGET_SECONDS,
            )
    except TimeoutError:
        pytest.fail("les appels concurrents ne sont jamais revenus — attente bornée, panne NOMMÉE")

    # Assert RAW store isolation (NOT get_flush_data — its shape is Task 2.1)
    assert "red-shrik" in collector._tool_stats, (
        "red-shrik not found in _tool_stats; agent attribution not wired"
    )
    assert "red-codex" in collector._tool_stats, (
        "red-codex not found in _tool_stats; agent attribution not wired"
    )

    assert "brain_list_projects" in collector._tool_stats["red-shrik"], (
        "brain_list_projects not recorded under red-shrik bucket"
    )
    assert "brain_list_projects" in collector._tool_stats["red-codex"], (
        "brain_list_projects not recorded under red-codex bucket"
    )

    # Isolation: each agent bucket holds exactly one tool entry (brain_list_projects only),
    # and the total call count across ALL agents == 2 (one per agent, no merging).
    # These assertions would fail if both calls collapsed into a single agent bucket.
    assert len(collector._tool_stats["red-shrik"]) == 1, (
        "red-shrik bucket should contain exactly 1 tool entry"
    )
    assert len(collector._tool_stats["red-codex"]) == 1, (
        "red-codex bucket should contain exactly 1 tool entry"
    )
    total_calls = sum(t["calls"] for ag in collector._tool_stats.values() for t in ag.values())
    assert total_calls == 2, (
        f"expected exactly 2 total calls across agents, got {total_calls}; "
        "cross-agent merging or duplicate recording detected"
    )


@pytest.mark.asyncio
async def test_agent_unknown_for_stdio_path() -> None:
    """Without an HTTP context (stdio / direct call), record_tool_call defaults
    to agent='unknown'. This is a unit-level assertion on the collector.
    """
    from unittest.mock import MagicMock

    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    collector.record_tool_call("brain_list_projects", latency_ms=5.0, agent="unknown")

    assert "unknown" in collector._tool_stats
    assert "brain_list_projects" in collector._tool_stats["unknown"]
    assert collector._tool_stats["unknown"]["brain_list_projects"]["calls"] == 1


@pytest.mark.asyncio
async def test_an_uncancellable_task_is_reported_instead_of_awaited_forever() -> None:
    """La preuve que le diagnostic est ATTEIGNABLE, et pas seulement écrit.

    L'ancienne forme faisait `task.cancel()` puis `await task` NU, avec le
    `pytest.fail` DERRIÈRE. Or FastMCP ferme son lifespan sous
    `anyio.CancelScope(shield=True)` : l'annulation peut être ABSORBÉE, l'attente
    ne revient jamais, et le message nommé devient du code mort sur exactement
    le chemin d'erreur pour lequel il a été écrit.

    Ici la tâche ignore délibérément son annulation. `_stop_bounded` doit RENDRE
    son libellé — pas pendre — pour que l'appelant puisse rapporter.
    """

    # La tâche absorbe l'annulation TANT QUE le test la retient, puis meurt sur
    # commande. La première rédaction bouclait pour toujours : elle rendait le
    # test vert et laissait derrière elle une tâche IMMORTELLE, que
    # `asyncio.Runner.close()` attend ensuite sans borne à la fermeture de la
    # boucle. C'est le filet `faulthandler` posé au même moment qui l'a montré —
    # dump en `runners.py:206 _cancel_all_tasks`. Borner sans garantir la MORT
    # de la tâche ne supprime pas le hang : il le déplace.
    release = asyncio.Event()

    async def ignores_cancellation() -> None:
        while not release.is_set():
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(release.wait()), timeout=0.05)

    task: asyncio.Task[None] = asyncio.create_task(ignores_cancellation())
    try:
        stalled = await _stop_bounded(
            task,
            what="une tâche qui refuse son annulation",
            stop_budget=0.2,
            cancel_budget=0.2,
        )
        assert stalled == "une tâche qui refuse son annulation"
    finally:
        release.set()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert task.done(), "le témoin laisse une tâche vivante — le hang serait déplacé, pas fermé"
