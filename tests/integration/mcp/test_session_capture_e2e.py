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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def mcp_base_url(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Boot the real FastMCP HTTP app on an ephemeral loopback port.

    The wiring is CALLED, not reproduced: ``build_server()`` is the same
    function ``server.py``'s ``__main__`` block runs, so there is no double that
    could drift.  ``test_server_wiring_has_one_source.py`` is what keeps it that
    way.  What is still reproduced here is ``_run_mcp``'s transport setup — this
    harness mounts the ASGI app itself instead of letting FastMCP serve it.
    """
    from tests.integration.conftest import INTEGRATION_DB_URL

    monkeypatch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
    monkeypatch.setenv("BRAIN_MCP_TRANSPORT", "http")
    monkeypatch.setenv("BRAIN_MCP_PROFILE", "compact")
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    monkeypatch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "false")
    monkeypatch.setenv("DECAY_ENABLED", "false")
    monkeypatch.setenv("METRICS_ENABLED", "false")
    monkeypatch.setenv("CLIENT_ACTIVITY_REPORTING_ENABLED", "false")
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "false")
    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)

    from brain_v42.config import get_settings

    get_settings.cache_clear()

    import brain_v42.db.engine as engine_module

    original_engine = engine_module._engine
    original_factory = engine_module._session_factory
    engine_module._engine = None
    engine_module._session_factory = None

    from brain_v42.mcp.business_errors import surface_business_errors
    from brain_v42.mcp.server import build_server

    built = build_server()
    # Same order as _run_mcp: business errors are surfaced before anything serves.
    await surface_business_errors(built.mcp)

    app = built.mcp.http_app(stateless_http=True)
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
        await serving
        if engine_module._engine is not None:
            await engine_module._engine.dispose()
        engine_module._engine = original_engine
        engine_module._session_factory = original_factory
        get_settings.cache_clear()


async def _call(base_url: str, tool: str, arguments: dict[str, Any]) -> str:
    """Call one tool the way a client does: HTTP, streamable transport, native catalog."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url=f"{base_url}/mcp/",
        headers={"x-brain-tool-profile": "native", "x-brain-agent": "w17-e2e"},
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments)
    return str(result.content[0].text)  # type: ignore[union-attr]


def _only_uuid(payload: str, *, what: str) -> UUID:
    found = _UUID_RE.findall(payload)
    assert found, f"no UUID in the {what} response: {payload!r}"
    return UUID(found[0])


async def _bootstrap_project(base_url: str, project_key: str) -> None:
    """Create the project context by its own tool — a session needs one to exist."""
    await _call(
        base_url,
        "brain_set_project_context",
        {
            "project_key": project_key,
            "name": project_key,
            "description": "Disposable project for the W17 capture-ledger e2e harness.",
        },
    )


async def _session_id(base_url: str, project_key: str, client_key: str) -> UUID:
    payload = await _call(
        base_url, "brain_session_start", {"project_key": project_key, "client_key": client_key}
    )
    return _only_uuid(payload, what="brain_session_start")


async def _learning_id(base_url: str, project_key: str, topic: str) -> UUID:
    payload = await _call(
        base_url,
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


async def test_capture_lands_in_the_named_session_and_not_its_neighbour(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """The whole harness, exercised on behaviour that exists today."""
    project_key = f"integ-w17-{uuid4().hex[:10]}"
    await _bootstrap_project(mcp_base_url, project_key)

    # (2) real tool path, and (4) two concurrent sessions in one project.
    session_a, session_b = await asyncio.gather(
        _session_id(mcp_base_url, project_key, "task-a"),
        _session_id(mcp_base_url, project_key, "task-b"),
    )
    assert session_a != session_b

    artifact = await _learning_id(mcp_base_url, project_key, f"w17 harness {uuid4().hex[:8]}")
    async with engine.connect() as conn:
        assert await conn.scalar(_LEARNING_EXISTS, {"knowledge_id": str(artifact)}) == 1

    # (1) negative witness: nothing is attributed before the explicit gesture.
    assert await _ledger_sessions_for(engine, artifact) == []
    assert await _ledger_artifacts_for(engine, session_a) == []
    assert await _ledger_artifacts_for(engine, session_b) == []

    await _call(
        mcp_base_url,
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


async def test_replaying_the_harness_keeps_the_ledger_exclusive(
    mcp_base_url: str, engine: AsyncEngine
) -> None:
    """Second test in the module — and the asyncio-loop trap only shows up here.

    A single-test module boots the engine on the loop that happens to be first
    and never notices it cached one; this repository has already paid for that.
    """
    project_key = f"integ-w17-{uuid4().hex[:10]}"
    await _bootstrap_project(mcp_base_url, project_key)
    owner, rival = await asyncio.gather(
        _session_id(mcp_base_url, project_key, "owner"),
        _session_id(mcp_base_url, project_key, "rival"),
    )

    artifact = await _learning_id(mcp_base_url, project_key, f"w17 exclusive {uuid4().hex[:8]}")
    assert await _ledger_sessions_for(engine, artifact) == []

    captured = {
        "session_id": str(owner),
        "expected_client_key": "owner",
        "knowledge_ids": [str(artifact)],
    }
    await _call(mcp_base_url, "brain_session_capture", captured)
    assert await _ledger_sessions_for(engine, artifact) == [str(owner)]

    # Replaying the exact capture is idempotent — it must not duplicate or move.
    await _call(mcp_base_url, "brain_session_capture", captured)
    assert await _ledger_sessions_for(engine, artifact) == [str(owner)]

    # The rival cannot steal it, and the failure must not silently move the row.
    with pytest.raises(Exception):  # noqa: B017 - transport error type is not the point
        await _call(
            mcp_base_url,
            "brain_session_capture",
            {
                "session_id": str(rival),
                "expected_client_key": "rival",
                "knowledge_ids": [str(artifact)],
            },
        )
    assert await _ledger_sessions_for(engine, artifact) == [str(owner)]
    assert await _ledger_artifacts_for(engine, rival) == []
