"""Tests for EmbeddingSupervisor state machine.

Focus: the transitions, not the HTTP layer. Docker + nvidia-smi are
abstracted behind protocols so the tests can drive the state machine
deterministically without spawning subprocesses.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ["pytest_asyncio"]


# ── Fixtures ─────────────────────────────────────────────────────────────


def make_supervisor(**overrides):
    """Build an EmbeddingSupervisor with async mocks for docker + GPU probe."""
    from services.embedding_supervisor.main import EmbeddingSupervisor

    docker = AsyncMock()
    docker.start = AsyncMock()
    docker.stop = AsyncMock()
    docker.wait_healthy = AsyncMock(return_value=True)

    gpu = AsyncMock()
    gpu.free_mib = AsyncMock(return_value=10_000)  # plenty free

    kwargs = {
        "docker_client": docker,
        "gpu_probe": gpu,
        "target_container": "embedding-qodo",
        "target_url": "http://127.0.0.1:8003",
        "idle_timeout_sec": 900.0,
        "gpu_min_free_mib": 6000,
        "start_timeout_sec": 60.0,
    }
    kwargs.update(overrides)
    return EmbeddingSupervisor(**kwargs), docker, gpu


# ── Initial state ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state_is_idle_stopped() -> None:
    sup, _, _ = make_supervisor()
    assert sup.state == "IDLE_STOPPED"


# ── Auto-start happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_ready_starts_container_when_gpu_free() -> None:
    sup, docker, gpu = make_supervisor()
    await sup.ensure_ready()
    assert sup.state == "READY"
    docker.start.assert_awaited_once_with("embedding-qodo")
    docker.wait_healthy.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_ready_noop_when_already_ready() -> None:
    sup, docker, _ = make_supervisor()
    await sup.ensure_ready()
    docker.start.reset_mock()
    await sup.ensure_ready()
    docker.start.assert_not_awaited()  # idempotent


# ── GPU-busy gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_ready_raises_gpu_busy_when_free_below_threshold() -> None:
    from services.embedding_supervisor.main import GpuBusy

    sup, docker, gpu = make_supervisor()
    gpu.free_mib.return_value = 3000  # less than the 6000 threshold
    with pytest.raises(GpuBusy):
        await sup.ensure_ready()
    assert sup.state == "IDLE_STOPPED"
    docker.start.assert_not_awaited()


# ── Concurrency — one starter, many waiters ────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_ensure_ready_triggers_a_single_start() -> None:
    sup, docker, _ = make_supervisor()
    # Slow the start so the two callers observe STARTING
    start_gate = asyncio.Event()

    async def slow_start(_):
        await start_gate.wait()

    docker.start.side_effect = slow_start

    t1 = asyncio.create_task(sup.ensure_ready())
    t2 = asyncio.create_task(sup.ensure_ready())
    await asyncio.sleep(0.05)  # let both reach the await ready_event point
    assert sup.state == "STARTING"
    start_gate.set()
    await t1
    await t2
    assert sup.state == "READY"
    # docker.start was called exactly once despite two callers
    assert docker.start.await_count == 1


# ── Start failure propagates ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_failure_returns_state_to_idle_stopped() -> None:
    from services.embedding_supervisor.main import StartFailed

    sup, docker, _ = make_supervisor()
    docker.start.side_effect = RuntimeError("docker daemon down")
    with pytest.raises(StartFailed):
        await sup.ensure_ready()
    assert sup.state == "IDLE_STOPPED"


@pytest.mark.asyncio
async def test_wait_healthy_false_returns_state_to_idle_stopped() -> None:
    from services.embedding_supervisor.main import StartFailed

    sup, docker, _ = make_supervisor()
    docker.wait_healthy.return_value = False
    with pytest.raises(StartFailed):
        await sup.ensure_ready()
    assert sup.state == "IDLE_STOPPED"


# ── Idle shutdown ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_shutdown_stops_container_after_timeout() -> None:
    sup, docker, _ = make_supervisor(idle_timeout_sec=0.01)
    await sup.ensure_ready()
    # Artificially age the last-request timestamp
    sup.last_request_ts = time.time() - 10.0
    await sup.check_idle_once()
    assert sup.state == "IDLE_STOPPED"
    docker.stop.assert_awaited_once_with("embedding-qodo")


@pytest.mark.asyncio
async def test_idle_shutdown_does_nothing_on_recent_request() -> None:
    sup, docker, _ = make_supervisor(idle_timeout_sec=900.0)
    await sup.ensure_ready()
    sup.last_request_ts = time.time()  # just now
    await sup.check_idle_once()
    assert sup.state == "READY"
    docker.stop.assert_not_awaited()


# ── Manual stop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_from_ready_goes_to_idle_stopped() -> None:
    sup, docker, _ = make_supervisor()
    await sup.ensure_ready()
    await sup.stop()
    assert sup.state == "IDLE_STOPPED"
    docker.stop.assert_awaited_once_with("embedding-qodo")


@pytest.mark.asyncio
async def test_stop_when_already_idle_is_noop() -> None:
    sup, docker, _ = make_supervisor()
    await sup.stop()
    assert sup.state == "IDLE_STOPPED"
    docker.stop.assert_not_awaited()


# ── on_request_done bumps the idle timer ──────────────────────────────


@pytest.mark.asyncio
async def test_on_request_done_updates_last_request_ts() -> None:
    sup, _, _ = make_supervisor()
    sup.last_request_ts = 0.0
    await sup.on_request_done()
    assert sup.last_request_ts > 0.0


# ── mark_lost — self-healing on out-of-band container stop ────────


@pytest.mark.asyncio
async def test_mark_lost_from_ready_returns_to_idle_stopped() -> None:
    """User stops the container via Docker Desktop -> supervisor recovers."""
    sup, _, _ = make_supervisor()
    await sup.ensure_ready()
    assert sup.state == "READY"
    await sup.mark_lost()
    assert sup.state == "IDLE_STOPPED"


@pytest.mark.asyncio
async def test_mark_lost_then_ensure_ready_restarts() -> None:
    """After mark_lost, the next ensure_ready spins up the container again."""
    sup, docker, _ = make_supervisor()
    await sup.ensure_ready()
    docker.start.reset_mock()
    await sup.mark_lost()
    await sup.ensure_ready()
    assert sup.state == "READY"
    docker.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_lost_noop_when_not_ready() -> None:
    """mark_lost does nothing if we weren't in READY state."""
    sup, _, _ = make_supervisor()
    # State is IDLE_STOPPED
    await sup.mark_lost()
    assert sup.state == "IDLE_STOPPED"


# ── Status snapshot ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_snapshot_has_expected_keys() -> None:
    sup, _, _ = make_supervisor()
    snap = await sup.status()
    for key in ("state", "last_request_ts", "gpu_free_mib", "target_container"):
        assert key in snap
    assert snap["state"] == "IDLE_STOPPED"
    assert snap["target_container"] == "embedding-qodo"
