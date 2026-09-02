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


# THE ITEM'S BUDGET, REDONE. The first draft announced "60 s of body + 40 s of
# teardown = 100 s, under the net at 120" and was wrong twice: the
# `faulthandler_timeout` is armed on the item's PROTOCOL — setup + call + teardown
# — and the body carries TWO CONSECUTIVE bounded waits, not one. The real worst
# case was 5 (startup) + 60 + 60 + 15 + 10 (shutdown) + 15 (dispose), i.e. 165 s;
# even stopping at the first overrun, 5 + 60 + 40 = 105 s remained. And the runner
# was measured 22 % slower on 2026-08-25 (148 s against 120.9 s on the same suite):
# 105 × 1.22 = 128 > 120. The item therefore crossed the net on the runner profile
# that produces the failure, and exited through `os._exit` — precisely the case
# where a test verdict is wanted.
#
# New worst case: 5 + 25 + 25 + 8 + 5 + 8 = 76 s, i.e. 93 s at +22 %, against a net
# at 120. The margin is 27 s, it is written here, and it is redone by hand as soon
# as one of these constants moves. The measured normal for these calls is under a
# second: 25 s stays two orders of magnitude above.
_STARTUP_BUDGET_SECONDS = 5.0
_STARTUP_POLL_SECONDS = 0.1
_STOP_BUDGET_SECONDS = 8.0
_CANCEL_BUDGET_SECONDS = 5.0
_DISPOSE_BUDGET_SECONDS = 8.0
#: Generous, but BOUNDED: a call that does not come back must name itself well
#: before the 120 s `faulthandler_timeout`, which is the net and not the bound.
_CALL_BUDGET_SECONDS = 25.0


def _dump_deadline(default: float) -> float:
    """Deadline of a DIAGNOSTIC watchdog, armed BELOW its application bound.

    Below, because it must fire WHILE the task is still suspended: after the
    `wait_for`, `Task.get_stack()` on an already-cancelled task returns an empty
    list and the dump becomes dead code on its own error path.

    Overridable through the environment for one reason only: to PROVE the dump fires
    on the real path. `BRAIN_TEST_TASK_DUMP_DEADLINE=0.001 pytest …` triggers it on
    a healthy run, where the observed wait is the real `initialize` handshake. A
    diagnosis one cannot trigger on demand is never verified.
    """
    override = os.environ.get("BRAIN_TEST_TASK_DUMP_DEADLINE", "").strip()
    return float(override) if override else default


#: Three zones, three deadlines — because the first batch only armed the dump
#: around the BODY's two waits and left the other three bare.
_CALL_DUMP_DEADLINE = _dump_deadline(10.0)
_STARTUP_DUMP_DEADLINE = _dump_deadline(3.0)
_TEARDOWN_DUMP_DEADLINE = _dump_deadline(4.0)


class Bench(NamedTuple):
    """What the bench returns: the URL, the collector, AND the objects to PROBE.

    The uvicorn server and the FastMCP serving the app come back up to the test
    because the diagnostic dump reads them: `len(server.server_state.tasks)` is the
    DIRECT measurement of the "in-flight requests" that shutdown's
    `ASGI callable returned without completing response` only infers.
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
    """Wait for a task to stop WITH a bound, and return what did not come back.

    Returns ``None`` when everything stopped, otherwise the label to report. It does
    not raise itself: the caller decides WHERE the `pytest.fail` is reachable, which
    is precisely what the old form got wrong.
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
    """Return control on a server that never started, then FAIL.

    `should_exit = True` RELEASES NOTHING here, and that is structural:
    `Server._serve` does `await self.startup()` THEN
    `if not self.should_exit: await self.main_loop()`, and `main_loop` is the ONLY
    place that re-reads the flag. If `LifespanOn.startup()` blocks on
    `startup_event.wait()` — with no timeout — we never get there. The old bare
    `await server_task` therefore made the `pytest.fail` UNREACHABLE on exactly the
    path it is written for. We cancel, bounded, and report whatever happens.
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
    # The pair, never half of it — same reason as test_lifecycle.
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

    # An instance OWN to the bench, never the module singleton: the singleton
    # carries the tools tests/integration/mcp/** registered on it, closed over an
    # engine their teardown has already dispose()d — 20 measured "Component already
    # exists", and the collection order became significant (ticket 83d8785b). The
    # factory comes from server.py: one single wiring, provenance included, not a
    # double stood up by hand.
    mcp = create_mcp_instance()

    # ENTRY WITNESS — recorded ALONE, no assertion.
    #
    # Since the isolation (ticket 83d8785b), `mcp` is a FRESH instance: this reading
    # can no longer see inherited tools. It keeps its value for the PROCESS state
    # the isolation does not purge — the class latch
    # `sse_starlette.sse.AppStatus.should_exit`, below.
    #
    # What is at stake here is ONE line of the reading:
    # `sse_starlette.sse.AppStatus.should_exit`. It is a CLASS attribute, hence
    # process-global and never reset to False; `True` on entry says an earlier
    # module armed the latch that makes `_listen_for_exit_signal` exit immediately
    # and without a word — the measured shape of the failure, where the server sends
    # the SSE headers then returns with no body. Do not confuse it with
    # `uvicorn.Server.should_exit`, also recorded but own to this bench's instance.
    #
    # An assertion here would convert a coin-flip into a plain red before we know
    # what this value is worth in practice: we MEASURE first. The contradiction stays
    # open and is not smoothed over — the six e2e tests pass, whereas a latch armed
    # early should kill them.
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

    # The metrics are no longer set by register_tools (ticket c352eaaa): they wrap
    # the tools AFTER registration. This boot is stood up by hand rather than by
    # _run_mcp, so it must reproduce that wiring point — without which it would test
    # a server production no longer builds.
    from brain_v42.metrics.tool_instrumentation import (  # noqa: PLC0415
        instrument_registered_tools,
    )

    await instrument_registered_tools(mcp, collector)

    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile  # noqa: PLC0415

    profiled_mcp = apply_tool_catalog_profile(mcp, "compact")

    # Build ASGI app (stateless_http so each request gets a fresh transport).
    # json_response=True: the PRODUCTION transport (plan_http_transport), never the
    # SSE-over-POST that http_app()'s default would choose — see the commit message
    # and ticket 85559792 for what this bench STOPS proving by leaving that phantom
    # transport.
    app = profiled_mcp.http_app(stateless_http=True, json_response=True)

    port = _get_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
        # MEASURED: uvicorn 0.41.0 ships `timeout_graceful_shutdown=None`, and
        # `Server.shutdown()` then waits on `_wait_tasks_to_complete()` WITHOUT a
        # bound, inside the task. No outside bound releases that: FastMCP closes its
        # lifespan under `anyio.CancelScope(shield=True)`, so the cancellation can
        # be absorbed.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    # Start uvicorn in a background task
    server_task = asyncio.create_task(server.serve())

    # THE STARTUP, UNDER WATCHDOG. This loop waits on `LifespanOn.startup()`, which
    # waits on `startup_event.wait()` WITHOUT a timeout: one of the three zones the
    # first batch left bare. A startup that never comes used to return "uvicorn did
    # not start" — true, and mute about WHO was waiting.
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
        # The globals are restored BEFORE any bounded wait: a `pytest.fail`
        # triggered further down must not leave the bench's engine installed for the
        # following modules.
        leftover_engine = engine_module._engine
        engine_module._engine = original_engine
        engine_module._session_factory = original_factory
        get_settings.cache_clear()

        # THE SHUTDOWN, UNDER WATCHDOG — and this is the zone that carried the one
        # clue we never managed to read: the two `ASGI callable returned without
        # completing response` appear AT CLOSING TIME, not during the calls.
        # `len(server.server_state.tasks)`, read by the probes, MEASURES the
        # still-in-flight requests that message only infers. The `pytest.fail` stays
        # OUTSIDE the guard: the watchdog is already joined when the verdict lands.
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
    # The label carries the identity: since the isolation (83d8785b) it must say
    # False — if it said True again, the bench would have fallen back on the
    # singleton and the lifespan counters read would be other modules'.
    from brain_v42.mcp.server import mcp as shared_mcp  # noqa: PLC0415

    label_suffix = f"profiled is singleton: {bench.mcp is shared_mcp}"

    async def visible_tools(profile: str | None = None) -> set[str]:
        headers = {"x-brain-tool-profile": profile} if profile else None
        transport = StreamableHttpTransport(url=f"{base_url}/mcp/", headers=headers)
        async with Client(transport) as client:
            listed = await asyncio.wait_for(client.list_tools(), timeout=_CALL_BUDGET_SECONDS)
            return {tool.name for tool in listed}

    # BOUNDED, and this is where CI hung: run 32779161805, `Timeout (0:02:00)!`
    # with this test's nodeid printed just after the dump. The test's body had NO
    # bound at all — four bare network waits — so the failure cost the whole job's
    # timeout instead of naming itself.
    #
    # The dump is armed AROUND the wait, never inside its `except TimeoutError`:
    # `wait_for` cancels the gather, WAITS for the cancellation, THEN raises — by
    # then both tasks are finished and their frames are gone. The MEASURED blockage
    # is not in `list_tools` anyway but in `Client.__aenter__` ->
    # `client.py:571 ready_event.wait()`, hence the `initialize` handshake: the error
    # message below sent two analyses down a false trail.
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
    """The proof that the diagnosis is REACHABLE, and not merely written.

    The old form did `task.cancel()` then a BARE `await task`, with the
    `pytest.fail` BEHIND it. Yet FastMCP closes its lifespan under
    `anyio.CancelScope(shield=True)`: the cancellation can be ABSORBED, the wait
    never returns, and the named message becomes dead code on exactly the error path
    it was written for.

    Here the task deliberately ignores its cancellation. `_stop_bounded` must RETURN
    its label — not hang — so that the caller can report.
    """

    # The task absorbs the cancellation AS LONG AS the test holds it, then dies on
    # command. The first draft looped forever: it made the test green and left an
    # IMMORTAL task behind, which `asyncio.Runner.close()` then waits for without a
    # bound at the loop's close. It was the `faulthandler` net laid at the same
    # moment that showed it — a dump at `runners.py:206 _cancel_all_tasks`. Bounding
    # without guaranteeing the task's DEATH does not remove the hang: it moves it.
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
