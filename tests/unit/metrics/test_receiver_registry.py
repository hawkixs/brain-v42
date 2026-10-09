"""The sidecar owns a read-only registry and cancellable LISTEN refresh."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.credentials.listener import CredentialListener
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.metrics.runtime import build_metrics_runtime
from tests.unit.metrics.test_receiver_credentials import settings


def test_runtime_wires_the_core_and_listener_only_in_credentials_mode() -> None:
    runtime = build_metrics_runtime(
        settings=settings(metrics_receiver_auth="credentials"), engine=MagicMock()
    )
    server = runtime._resources.server_factory(None, None)
    assert server._receiver_auth == "credentials"
    assert isinstance(server._credential_verifier, CredentialVerifier)
    listener = server._credential_listener
    assert isinstance(listener, CredentialListener)
    assert listener._channel == "brain_client_credentials"
    assert listener._on_notification == server._credential_verifier.notify
    assert listener._on_connect == server._credential_verifier.run_listener_reconnected
    runtime = build_metrics_runtime(settings=settings(), engine=MagicMock())
    server = runtime._resources.server_factory(None, None)
    assert server._credential_verifier is None
    assert server._credential_listener is None


@pytest.mark.parametrize("method", ["active_rows", "disposition_by_digest"])
async def test_registry_queries_are_in_read_only_transactions(method: str) -> None:
    from brain_v42.metrics.credential_registry import ReadOnlyCredentialRegistry

    session = AsyncMock()
    session.__aenter__.return_value = session
    factory = MagicMock(return_value=session)
    registry = ReadOnlyCredentialRegistry(factory)
    repo = registry._repository
    expected = [] if method == "active_rows" else (None, "unknown")
    setattr(repo, method, AsyncMock(return_value=expected))
    args = (datetime.now(UTC),) if method == "active_rows" else (b"x" * 32, datetime.now(UTC))
    assert await getattr(registry, method)(*args) == expected
    assert str(session.execute.await_args.args[0]) == "SET TRANSACTION READ ONLY"
    getattr(repo, method).assert_awaited_once_with(*args, session=session)
    session.commit.assert_not_called()


async def test_registry_tasks_refresh_before_serving_and_close_with_the_app() -> None:
    from brain_v42.metrics.server import MetricsServer

    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    async def task(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped[index].set()

    core = MagicMock(refresh=AsyncMock(return_value=False), run_refresh_loop=lambda: task(0))
    listener = MagicMock(run=lambda: task(1))
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        receiver_auth="credentials",
        credential_verifier=core,
        credential_listener=listener,
    )
    context = server._credential_registry_lifecycle(server._build_app())
    await anext(context)
    core.refresh.assert_awaited_once()
    for event in started:
        await asyncio.wait_for(event.wait(), 1)
    await context.aclose()
    assert all(event.is_set() for event in stopped)
