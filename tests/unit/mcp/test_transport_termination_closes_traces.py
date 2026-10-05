"""A terminated transport closes the tracers of its connection (ticket 09d2b56e).

The SDK ends a stateful session through ``StreamableHTTPServerTransport.terminate``
on all three nominal paths — a client DELETE, the idle eviction, the server
shutdown — and exposes no callback for it. The server substitutes the class the
session manager instantiates, as it already does for the idle deadline, and the
substitution is GUARDED: if ``terminate`` disappears upstream it refuses to start
rather than run believing tracers get closed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.mcp.session_autoopen import SessionAutoOpener

_FAKE_PG_URL = "postgresql+asyncpg://user:pass@127.0.0.1:1/fake"


def _transport_class() -> Any:
    from mcp.server import streamable_http_manager

    return streamable_http_manager.StreamableHTTPServerTransport


def test_terminating_a_transport_reports_its_session_id_once() -> None:
    from brain_v42.mcp.server import _install_transport_termination_hook

    reported: list[str] = []

    async def on_terminated(session_id: str) -> None:
        reported.append(session_id)

    _install_transport_termination_hook(on_terminated)
    transport = _transport_class()(mcp_session_id="conn-1", is_json_response_enabled=True)

    async def scenario() -> None:
        await transport.terminate()
        await transport.terminate()

    asyncio.run(scenario())

    assert reported == ["conn-1"]


def test_a_failing_report_never_breaks_the_termination() -> None:
    from brain_v42.mcp.server import _install_transport_termination_hook

    async def on_terminated(session_id: str) -> None:
        raise RuntimeError("database down")

    _install_transport_termination_hook(on_terminated)
    transport = _transport_class()(mcp_session_id="conn-2", is_json_response_enabled=True)

    asyncio.run(transport.terminate())

    assert transport.is_terminated


def test_a_slow_report_is_bounded() -> None:
    from brain_v42.mcp.server import _install_transport_termination_hook

    async def on_terminated(session_id: str) -> None:
        await asyncio.sleep(30)

    _install_transport_termination_hook(on_terminated, budget_seconds=0.05)
    transport = _transport_class()(mcp_session_id="conn-3", is_json_response_enabled=True)

    async def scenario() -> None:
        await asyncio.wait_for(transport.terminate(), timeout=2)

    asyncio.run(scenario())

    assert transport.is_terminated


def test_installing_twice_does_not_stack_and_the_last_report_wins() -> None:
    from brain_v42.mcp.server import _install_transport_termination_hook

    first: list[str] = []
    second: list[str] = []

    async def report_first(session_id: str) -> None:
        first.append(session_id)

    async def report_second(session_id: str) -> None:
        second.append(session_id)

    _install_transport_termination_hook(report_first)
    installed = _transport_class()
    _install_transport_termination_hook(report_second)

    assert _transport_class() is installed
    asyncio.run(_transport_class()(mcp_session_id="conn-4").terminate())
    assert (first, second) == ([], ["conn-4"])


def test_the_hook_refuses_to_install_when_terminate_is_gone(monkeypatch: Any) -> None:
    from mcp.server import streamable_http_manager

    from brain_v42.mcp.server import (
        TransportTerminationHookUnavailableError,
        _install_transport_termination_hook,
    )

    class _NoTerminate:
        pass

    monkeypatch.setattr(streamable_http_manager, "StreamableHTTPServerTransport", _NoTerminate)

    async def on_terminated(session_id: str) -> None:
        return None

    with pytest.raises(TransportTerminationHookUnavailableError):
        _install_transport_termination_hook(on_terminated)


@pytest.mark.parametrize("stateless", [False, True])
def test_the_stateful_http_plan_installs_the_hook_and_stateless_does_not(
    monkeypatch: Any, stateless: bool
) -> None:
    from brain_v42.config import Settings, get_settings
    from brain_v42.mcp import server

    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    get_settings.cache_clear()
    original = _transport_class()
    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
        mcp_http_stateless=stateless,
    )
    server._http_security_configured_servers.discard(server.mcp)
    server.plan_http_transport(server.mcp, settings)

    assert (_transport_class() is not original) is (not stateless)


def _opener(closed: list[str], *, fail: bool = False) -> SessionAutoOpener:
    async def opener(identity: Any) -> UUID | None:
        return uuid4()

    async def observer(session_id: UUID) -> bool:
        return True

    async def closer(connection_id: str) -> list[UUID]:
        if fail:
            raise RuntimeError("database down")
        closed.append(connection_id)
        return [uuid4()]

    return SessionAutoOpener(opener, observer, closer=closer)


def test_closing_a_connection_closes_its_tracers_and_forgets_its_memo() -> None:
    closed: list[str] = []
    autoopener = _opener(closed)
    autoopener._memo["conn-5"] = uuid4()

    asyncio.run(autoopener.close("conn-5"))

    assert closed == ["conn-5"]
    assert "conn-5" not in autoopener._memo
    assert autoopener.closed == 1


def test_closing_a_connection_never_raises() -> None:
    autoopener = _opener([], fail=True)

    asyncio.run(autoopener.close("conn-6"))

    assert autoopener.close_failed == 1


# --- Round 1 review of PR 291 (findings verified in source) -----------------


def test_a_connection_closed_while_its_observation_is_in_flight_never_raises() -> None:
    """`close` pops the memo while `ensure_open` awaits its observation: the
    tool-call path must stay fail-open (it used to raise KeyError)."""
    from brain_v42.mcp import session_autoopen
    from brain_v42.mcp.session_autoopen import AutoOpenIdentity

    identity = AutoOpenIdentity(
        project_key="brain-v42",
        connection_id="conn-r1",
        started_by_actor="actor",
        nature="agent",
        intent=None,
    )
    gate = asyncio.Event()
    closed: list[str] = []

    async def opener(identity: Any) -> UUID | None:
        return uuid4()

    async def observer(session_id: UUID) -> bool:
        await gate.wait()
        return True

    async def closer(connection_id: str) -> list[UUID]:
        closed.append(connection_id)
        return []

    autoopener = SessionAutoOpener(opener, observer, closer)
    autoopener._memo["conn-r1"] = uuid4()

    async def scenario() -> UUID | None:
        call = asyncio.create_task(autoopener.ensure_open())
        await asyncio.sleep(0)
        await autoopener.close("conn-r1")
        gate.set()
        return await call

    original = session_autoopen.resolve_auto_open_identity
    session_autoopen.resolve_auto_open_identity = lambda: (identity, "ok")  # type: ignore[assignment]
    try:
        asyncio.run(scenario())
    finally:
        session_autoopen.resolve_auto_open_identity = original  # type: ignore[assignment]

    assert "conn-r1" not in autoopener._memo


def test_a_tracer_opened_after_its_connection_terminated_is_closed_not_memoized() -> None:
    """An opening in flight when the connection terminates must not leave an
    open tracer behind, nor a memo entry for a dead connection."""
    from brain_v42.mcp import session_autoopen
    from brain_v42.mcp.session_autoopen import AutoOpenIdentity

    identity = AutoOpenIdentity(
        project_key="brain-v42",
        connection_id="conn-r2",
        started_by_actor="actor",
        nature="agent",
        intent=None,
    )
    gate = asyncio.Event()
    closed: list[str] = []

    async def opener(identity: Any) -> UUID | None:
        await gate.wait()
        return uuid4()

    async def observer(session_id: UUID) -> bool:
        return True

    async def closer(connection_id: str) -> list[UUID]:
        closed.append(connection_id)
        return []

    autoopener = SessionAutoOpener(opener, observer, closer)

    async def scenario() -> None:
        call = asyncio.create_task(autoopener.ensure_open())
        await asyncio.sleep(0)
        await autoopener.close("conn-r2")
        gate.set()
        await call

    original = session_autoopen.resolve_auto_open_identity
    session_autoopen.resolve_auto_open_identity = lambda: (identity, "ok")  # type: ignore[assignment]
    try:
        asyncio.run(scenario())
    finally:
        session_autoopen.resolve_auto_open_identity = original  # type: ignore[assignment]

    assert "conn-r2" not in autoopener._memo
    assert closed == ["conn-r2", "conn-r2"], "the late tracer was left open"


class _SyncTerminate:
    is_terminated = property(lambda self: False)

    def __init__(self, mcp_session_id: str | None) -> None:
        self.mcp_session_id = mcp_session_id

    def terminate(self) -> None:
        return None


class _TerminateWithReason:
    is_terminated = property(lambda self: False)

    def __init__(self, mcp_session_id: str | None) -> None:
        self.mcp_session_id = mcp_session_id

    async def terminate(self, reason: str) -> None:
        return None


class _NoIsTerminated:
    def __init__(self, mcp_session_id: str | None) -> None:
        self.mcp_session_id = mcp_session_id

    async def terminate(self) -> None:
        return None


class _NoSessionId:
    is_terminated = property(lambda self: False)

    def __init__(self) -> None:
        pass

    async def terminate(self) -> None:
        return None


@pytest.mark.parametrize(
    "shape", [_SyncTerminate, _TerminateWithReason, _NoIsTerminated, _NoSessionId]
)
def test_the_hook_refuses_every_incompatible_transport_shape(monkeypatch: Any, shape: type) -> None:
    from mcp.server import streamable_http_manager

    from brain_v42.mcp.server import (
        TransportTerminationHookUnavailableError,
        _install_transport_termination_hook,
    )

    monkeypatch.setattr(streamable_http_manager, "StreamableHTTPServerTransport", shape)

    async def on_terminated(session_id: str) -> None:
        return None

    with pytest.raises(TransportTerminationHookUnavailableError):
        _install_transport_termination_hook(on_terminated)


def test_a_dead_database_costs_one_budget_not_one_per_connection() -> None:
    """Shutdown terminates every transport in turn: after one failed report, the
    next ones are skipped for a cooldown, so N connections never cost N budgets."""
    from brain_v42.mcp.server import _install_transport_termination_hook

    calls: list[str] = []

    async def on_terminated(session_id: str) -> None:
        calls.append(session_id)
        await asyncio.sleep(30)

    _install_transport_termination_hook(on_terminated, budget_seconds=0.05)
    transports = [_transport_class()(mcp_session_id=f"conn-s{i}") for i in range(5)]

    async def scenario() -> None:
        for transport in transports:
            await transport.terminate()

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))

    assert calls == ["conn-s0"]
    assert all(transport.is_terminated for transport in transports)
