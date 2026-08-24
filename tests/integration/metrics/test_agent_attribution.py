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
import socket
from contextlib import suppress
from typing import Any

import pytest
import pytest_asyncio
import uvicorn

from brain_v42.config import get_settings
from brain_v42.metrics.collector import MetricsCollector

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


_STOP_BUDGET_SECONDS = 15.0
#: Large, mais BORNÉ : un appel qui ne revient pas doit se nommer bien avant
#: le `faulthandler_timeout` de 120 s, qui est le filet et non la borne.
_CALL_BUDGET_SECONDS = 60.0
_CANCEL_BUDGET_SECONDS = 10.0


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
    monkeypatch.setenv("DECAY_ENABLED", "false")
    get_settings.cache_clear()

    import brain_v42.db.engine as engine_module

    # Reset engine singleton so build_services picks up fresh settings
    original_engine = engine_module._engine
    original_factory = engine_module._session_factory
    engine_module._engine = None
    engine_module._session_factory = None

    from brain_v42.mcp.server import build_services, mcp

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

    # Build ASGI app (stateless_http so each request gets a fresh transport)
    app = profiled_mcp.http_app(stateless_http=True)

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

    # Wait until server is ready
    for _ in range(50):
        await asyncio.sleep(0.1)
        if server.started:
            break
    else:
        # `should_exit = True` NE LIBÈRE RIEN ici, et c'est structurel :
        # `Server._serve` fait `await self.startup()` PUIS
        # `if not self.should_exit: await self.main_loop()`, et `main_loop` est
        # le SEUL endroit qui relit le drapeau. Si `LifespanOn.startup()` bloque
        # sur `startup_event.wait()` — sans timeout — on n'y arrive jamais.
        # L'ancien `await server_task` nu rendait donc le `pytest.fail`
        # ci-dessous INATTEIGNABLE sur exactement le chemin pour lequel il est
        # écrit. On annule, borné, et on rapporte quoi qu'il arrive.
        server.should_exit = True
        await _stop_bounded(server_task, what="un serveur metrics qui n'a jamais démarré")
        pytest.fail("uvicorn did not start within 5s")

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, collector
    finally:
        # Les globales sont rendues AVANT toute attente bornée : un
        # `pytest.fail` déclenché plus bas ne doit pas laisser le moteur du banc
        # installé pour les modules suivants.
        leftover_engine = engine_module._engine
        engine_module._engine = original_engine
        engine_module._session_factory = original_factory
        get_settings.cache_clear()

        server.should_exit = True
        stalled = await _stop_bounded(server_task, what="l'arrêt du serveur metrics")
        if leftover_engine is not None:
            try:
                await asyncio.wait_for(leftover_engine.dispose(), timeout=_STOP_BUDGET_SECONDS)
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
    http_server_and_collector: tuple[str, MetricsCollector],
) -> None:
    """Two concurrent clients with distinct X-Brain-Agent headers record
    tool calls under isolated agent buckets in _tool_stats.

    Tool: brain_list_projects — no embedding, cheap DB-only list query.
    Concurrent: asyncio.gather asserts the contextvar relay is per-request
    (H6 — a middleware-based relay would collapse to a single agent).
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    base_url, collector = http_server_and_collector

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
    try:
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
