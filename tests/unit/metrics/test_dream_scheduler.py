"""Scheduler measurements must distinguish operator suspension from unreadable state."""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _read(mode: str, runner: AsyncMock) -> dict[str, Any]:
    assert find_spec("brain_v42.metrics.dream_scheduler") is not None
    from brain_v42.metrics.dream_scheduler import read_dream_scheduler_state

    return await read_dream_scheduler_state(mode, runner=runner)


@pytest.mark.asyncio
async def test_none_declares_absence_without_running_systemctl() -> None:
    runner = AsyncMock()
    assert await _read("none", runner) == {
        "state": "absent",
        "source": "declared_none",
        "timer": {"unit_file_state": None, "active_state": None, "next_elapse": None},
        "service": {"active_state": None},
        "error_type": None,
    }
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unit_file_state", "active_state", "load_state", "expected"),
    [
        ("enabled", "active", "loaded", "armed"),
        ("disabled", "inactive", "loaded", "suspended"),
        ("masked", "inactive", "masked", "suspended"),
        ("enabled", "inactive", "loaded", "suspended"),
        ("enabled", "active", "not-found", "absent"),
    ],
)
async def test_timer_state_controls_scheduler(
    unit_file_state: str, active_state: str, load_state: str, expected: str
) -> None:
    runner = AsyncMock(
        side_effect=[
            (
                0,
                f"UnitFileState={unit_file_state}\nActiveState={active_state}\n"
                f"LoadState={load_state}\nNextElapseUSecRealtime=Tue 2026-10-13 03:00:00 UTC\n",
            ),
            (0, "ActiveState=inactive\n"),
        ]
    )
    result = await _read("systemd_user", runner)
    assert result == {
        "state": expected,
        "source": "systemd_user",
        "timer": {
            "unit_file_state": unit_file_state,
            "active_state": active_state,
            "next_elapse": "Tue 2026-10-13 03:00:00 UTC",
        },
        "service": {"active_state": "inactive"},
        "error_type": None,
    }
    assert runner.await_args_list[0].args == (
        "systemctl",
        "--user",
        "show",
        "brain-v42-dream.timer",
        "--property=UnitFileState,ActiveState,NextElapseUSecRealtime,LoadState",
    )
    assert runner.await_args_list[1].args == (
        "systemctl",
        "--user",
        "show",
        "brain-v42-dream.service",
        "--property=ActiveState",
    )


@pytest.mark.asyncio
async def test_empty_properties_become_null() -> None:
    runner = AsyncMock(side_effect=[(0, "UnitFileState=\nActiveState=\n"), (0, "ActiveState=\n")])
    result = await _read("systemd_user", runner)
    assert result["timer"] == {"unit_file_state": None, "active_state": None, "next_elapse": None}
    assert result["service"] == {"active_state": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FileNotFoundError(), TimeoutError(), RuntimeError()])
async def test_runner_errors_are_not_measured(error: Exception) -> None:
    result = await _read("systemd_user", AsyncMock(side_effect=error))
    assert result == {
        "state": "not_measured",
        "source": "systemd_user",
        "timer": {"unit_file_state": None, "active_state": None, "next_elapse": None},
        "service": {"active_state": None},
        "error_type": type(error).__name__,
    }


@pytest.mark.asyncio
async def test_systemctl_timeout_kills_and_reaps_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.metrics.dream_scheduler import read_dream_scheduler_state

    process = MagicMock(returncode=None)
    reaped = False

    async def communicate() -> tuple[bytes, None]:
        nonlocal reaped
        if not process.kill.called:
            await asyncio.sleep(60)
        process.returncode = -9
        reaped = True
        return b"", None

    process.communicate = AsyncMock(side_effect=communicate)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: real_timeout(0.01))

    result = await read_dream_scheduler_state("systemd_user")

    assert result["state"] == "not_measured"
    assert result["error_type"] == "TimeoutError"
    spawn.assert_awaited_once()
    process.kill.assert_called_once_with()
    assert process.communicate.await_count == 2
    assert reaped
    assert process.returncode == -9


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_unit", [0, 1])
async def test_nonzero_exit_is_not_measured(failed_unit: int) -> None:
    responses = [(0, "LoadState=not-found\n"), (0, "ActiveState=inactive\n")]
    responses[failed_unit] = (3, "")
    result = await _read("systemd_user", AsyncMock(side_effect=responses))
    assert result["state"] == "not_measured"
    assert result["error_type"] == "systemctl_exit_3"


@pytest.mark.asyncio
async def test_server_passes_scheduler_mode_to_both_blocks() -> None:
    from brain_v42.metrics.server import MetricsServer

    collector = MagicMock()
    collector.get_metrics.return_value = {"embedding_service": {}}
    collector.collect_db_stats = AsyncMock(return_value={})
    collector.collect_search_quality = AsyncMock(return_value={})
    collector.collect_process_metrics = AsyncMock(return_value={"active_processes": 0})
    collector.collect_dream_metrics = AsyncMock(return_value={})
    collector.collect_nightly_ops = AsyncMock(return_value={})
    collector.collect_ticket_counts = AsyncMock(return_value={})
    collector.collect_session_slots = AsyncMock(return_value={})
    server = MetricsServer(
        collector, MagicMock(healthcheck=AsyncMock(return_value=True)), dream_scheduler="none"
    )
    await server._handle_metrics(MagicMock())
    collector.collect_dream_metrics.assert_awaited_once_with(scheduler_mode="none")
    collector.collect_nightly_ops.assert_awaited_once_with(scheduler_mode="none")


def test_runtime_passes_configured_scheduler_to_server() -> None:
    from brain_v42.config import Settings
    from brain_v42.metrics.runtime import build_metrics_runtime

    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/test",
        metrics_dream_scheduler="none",
        _env_file=None,
    )
    with (
        patch("brain_v42.metrics.runtime.build_embedding_service"),
        patch("brain_v42.metrics.runtime.create_neo4j_driver", return_value=None),
        patch("brain_v42.metrics.runtime.BrainGraphMetricsServer") as server,
    ):
        runtime = build_metrics_runtime(settings=settings, engine=MagicMock())
        runtime._resources.server_factory(None, None)
    assert server.call_args.kwargs["dream_scheduler"] == "none"
