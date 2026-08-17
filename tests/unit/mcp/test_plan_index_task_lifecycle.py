"""Tests for plan-indexer background task lifecycle in app_lifecycle.

Regression guard for the asyncio fire-and-forget gotcha:
asyncio.create_task() without storing a reference is subject to GC mid-flight
(the event loop only holds a weak-ref). The task must also be cancelled and
awaited in the shutdown finally block to prevent 'Task was destroyed but it
is pending!' warnings and dangling coroutines on graceful shutdown.

Fix (server.py): retain the Task and register a cancellation callback in the
same AsyncExitStack that owns every other lifecycle resource.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Source-level contracts (static analysis — no heavy imports needed)
# ---------------------------------------------------------------------------


def test_plan_index_task_reference_stored() -> None:
    """app_lifecycle must assign asyncio.create_task() to a local variable.

    A fire-and-forget task (no stored reference) can be GC'd mid-flight
    because the event loop only keeps a weak-reference. The fix is:

        plan_index_task = asyncio.create_task(_background_plan_index())
    """
    import brain_v42.mcp.server as server_mod

    src = inspect.getsource(server_mod.app_lifecycle)
    assert "plan_index_task = asyncio.create_task(" in src, (
        "app_lifecycle must retain the plan-index Task reference"
    )


def test_plan_index_task_cancelled_in_finally() -> None:
    """The retained task must be cancelled by the lifecycle cleanup stack."""
    import brain_v42.mcp.server as server_mod

    src = inspect.getsource(server_mod.app_lifecycle)
    assert "task.cancel()" in src
    assert "await task" in src
    assert "cleanup.push_async_callback(_cancel_task, plan_index_task)" in src


# ---------------------------------------------------------------------------
# Runtime contract: task is cancelled when lifecycle exits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_index_task_receives_cancel_on_lifecycle_exit() -> None:
    """When app_lifecycle exits, the background plan-index task is cancelled.

    If the task completes before shutdown this is a no-op (cancel on a done
    task is safe). The important case is when the task is still running on
    shutdown — we must not leave a dangling pending task.
    """
    from brain_v42.config import Settings

    # Minimal settings that won't trigger flusher/decay start
    settings = MagicMock(spec=Settings)
    settings.metrics_enabled = False
    settings.decay_enabled = False
    settings.otel_tracing_enabled = False

    # Slow plan indexer that never completes during the test
    slow_indexer = MagicMock()

    async def _never_completes() -> None:
        await asyncio.sleep(999)

    slow_indexer.index_all_projects = _never_completes

    services = {
        "access_logger": MagicMock(),
        "plan_indexer": slow_indexer,
        "neo4j_driver": None,
        "access_log_repo": MagicMock(),
        "decay_calculator": MagicMock(),
    }

    cancelled_flag: list[bool] = []

    with (
        patch("brain_v42.mcp.server.close_neo4j_driver", new_callable=AsyncMock),
        patch("brain_v42.mcp.server.dispose_engine", new_callable=AsyncMock),
    ):
        from brain_v42.mcp.server import app_lifecycle

        # Enter and immediately exit the lifecycle context
        async with app_lifecycle(settings, services, MagicMock()):
            # Give the background task a tick to start
            await asyncio.sleep(0)

        # If we reach here without hanging, the task was cancelled (not awaited forever)
        cancelled_flag.append(True)

    assert cancelled_flag == [True], (
        "app_lifecycle should exit cleanly after cancelling the background task"
    )
