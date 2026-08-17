"""Signal wiring tests for the two independently managed entrypoints."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_automation_and_metrics_entrypoints_are_importable() -> None:
    from brain_v42.automation.__main__ import main as automation_main
    from brain_v42.metrics.__main__ import main as metrics_main

    assert automation_main is not None
    assert metrics_main is not None


async def test_automation_none_stop_event_wires_both_signals_and_passes_same_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.__main__ as entrypoint

    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=37)
    build_runtime = MagicMock(return_value=runtime)
    loop = MagicMock()
    monkeypatch.setattr(entrypoint, "build_automation_runtime", build_runtime, raising=False)
    monkeypatch.setattr(entrypoint.asyncio, "get_running_loop", MagicMock(return_value=loop))

    result = (await asyncio.gather(entrypoint.main(None), return_exceptions=True))[0]

    assert result == 37
    build_runtime.assert_called_once_with()
    runtime.run.assert_awaited_once()
    stop_event = runtime.run.await_args.args[0]
    assert isinstance(stop_event, asyncio.Event)
    assert loop.add_signal_handler.call_count == 2
    assert {call.args[0] for call in loop.add_signal_handler.call_args_list} == {
        signal.SIGINT,
        signal.SIGTERM,
    }
    assert all(
        call.args[1].__self__ is stop_event for call in loop.add_signal_handler.call_args_list
    )


async def test_metrics_none_stop_event_wires_both_signals_and_passes_same_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.__main__ as entrypoint

    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=41)
    build_runtime = MagicMock(return_value=runtime)
    loop = MagicMock()
    configure_logging = MagicMock()
    monkeypatch.setattr(entrypoint, "build_metrics_runtime", build_runtime, raising=False)
    monkeypatch.setattr(entrypoint.asyncio, "get_running_loop", MagicMock(return_value=loop))
    monkeypatch.setattr(entrypoint.structlog, "configure", configure_logging)

    result = (await asyncio.gather(entrypoint.main(None), return_exceptions=True))[0]

    assert result == 41
    build_runtime.assert_called_once_with()
    configure_logging.assert_called_once()
    runtime.run.assert_awaited_once()
    stop_event = runtime.run.await_args.args[0]
    assert isinstance(stop_event, asyncio.Event)
    assert loop.add_signal_handler.call_count == 2
    assert {call.args[0] for call in loop.add_signal_handler.call_args_list} == {
        signal.SIGINT,
        signal.SIGTERM,
    }
    assert all(
        call.args[1].__self__ is stop_event for call in loop.add_signal_handler.call_args_list
    )
