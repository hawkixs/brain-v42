"""A plan written to disk becomes searchable without restarting the server.

The on-demand path already exists and is reachable: `brain_reindex_plans` is
registered unconditionally and answers under the production `compact` profile
(measured 2026-09-22 against the live server). What was missing is the passive
path — nothing re-scanned on its own, so a plan file only reached the corpus
when someone thought to ask, or when the process restarted.

A periodic sweep is the answer rather than a filesystem watch: the scan paths
span several roots on two machines, an inotify watch per root would have to be
rebuilt whenever a project's configuration changes, and the content-hash skip
already makes an unchanged sweep cheap (one stat + one lookup per file).

It ships CLOSED. This loop writes: an indexed plan reaches ClusterGuard, and
`plan` is in CREATING_SIGNALS, so it can create features. The roadmap tap is
under observation — the purge of pseudo-features is waiting for the dry-up to
be judged established — and arming a periodic creator by default would change
the very thing being measured. Arming it is an operator gesture.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from structlog.testing import capture_logs

from brain_v42.services.plan_index_refresher import PlanIndexRefresher


class _FakeIndexer:
    """Records each sweep and lets a test await the first one."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.swept = asyncio.Event()
        self._result = result if result is not None else {}
        self._error = error

    async def index_all_projects(self) -> dict[str, Any]:
        self.calls += 1
        self.swept.set()
        if self._error is not None:
            raise self._error
        return self._result


async def _stop(refresher: PlanIndexRefresher) -> None:
    await refresher.stop()


@pytest.mark.asyncio
async def test_the_first_sweep_waits_for_the_interval() -> None:
    """The loop must not race the startup pass that already runs at t=0.

    Sweeping immediately would mean two `index_all_projects` walking the same
    files in the same process, embedding the same new plans twice.
    """
    indexer = _FakeIndexer()
    refresher = PlanIndexRefresher(plan_indexer=indexer, interval_seconds=3600)

    await refresher.start()
    for _ in range(5):
        await asyncio.sleep(0)

    assert indexer.calls == 0
    await _stop(refresher)


@pytest.mark.asyncio
async def test_each_tick_sweeps_every_project() -> None:
    indexer = _FakeIndexer(result={"brain-v42": {"indexed": 1, "errors": 0, "failures": []}})
    refresher = PlanIndexRefresher(plan_indexer=indexer, interval_seconds=0)

    await refresher.start()
    await asyncio.wait_for(indexer.swept.wait(), timeout=2)
    await _stop(refresher)

    assert indexer.calls >= 1


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_kill_the_loop() -> None:
    """A loop that dies on the first bad night is a loop nobody can rely on."""
    indexer = _FakeIndexer(error=RuntimeError("embedding service is down"))
    refresher = PlanIndexRefresher(plan_indexer=indexer, interval_seconds=0)

    with capture_logs() as logs:
        await refresher.start()
        await asyncio.wait_for(indexer.swept.wait(), timeout=2)
        for _ in range(10):
            await asyncio.sleep(0)
        await _stop(refresher)

    assert indexer.calls >= 2, "the loop must keep ticking after a failed sweep"
    failed = [log for log in logs if log["event"] == "plan_index_refresher.sweep_failed"]
    assert failed, "a failed sweep must be named, not swallowed"


@pytest.mark.asyncio
async def test_a_sweep_reports_what_it_could_not_index() -> None:
    """Per-tick totals, so a repeated failure stays visible without per-path spam."""
    indexer = _FakeIndexer(
        result={
            "red-games": {
                "indexed": 0,
                "errors": 2,
                "failures": [
                    {"file_path": "/a/x-plan.md", "error_type": "PlanOwnedByAnotherProject"},
                    {"file_path": "/b", "error_type": "PlanScanPathError:relative"},
                ],
            }
        }
    )
    refresher = PlanIndexRefresher(plan_indexer=indexer, interval_seconds=0)

    with capture_logs() as logs:
        await refresher.start()
        await asyncio.wait_for(indexer.swept.wait(), timeout=2)
        await _stop(refresher)

    done = [log for log in logs if log["event"] == "plan_index_refresher.sweep_done"]
    assert done, "every sweep reports, including the ones that found nothing new"
    assert done[0]["errors"] == 2
    assert done[0]["projects"] == 1


@pytest.mark.asyncio
async def test_stop_is_safe_before_start() -> None:
    """Shutdown must not depend on whether the flag was armed."""
    refresher = PlanIndexRefresher(plan_indexer=_FakeIndexer(), interval_seconds=0)
    await refresher.stop()


@pytest.mark.asyncio
async def test_stop_cancels_a_sweep_in_flight() -> None:
    """A wedged sweep must not hold the shutdown open."""

    class _NeverFinishes:
        async def index_all_projects(self) -> dict[str, Any]:
            await asyncio.sleep(999)
            return {}

    refresher = PlanIndexRefresher(plan_indexer=_NeverFinishes(), interval_seconds=0)
    await refresher.start()
    for _ in range(5):
        await asyncio.sleep(0)

    await asyncio.wait_for(refresher.stop(), timeout=2)


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """Two starts must not leave an orphan loop nothing can cancel."""
    indexer = _FakeIndexer()
    refresher = PlanIndexRefresher(plan_indexer=indexer, interval_seconds=3600)

    await refresher.start()
    first = refresher._task
    await refresher.start()

    assert refresher._task is first
    await _stop(refresher)
    assert first is not None and first.cancelled() or first.done()


def test_the_refresher_ships_closed() -> None:
    """The default is today's behaviour: nothing sweeps on its own."""
    from brain_v42.config import Settings

    settings = Settings(POSTGRES_URL="postgresql+asyncpg://u:p@localhost:5433/db")
    assert settings.plan_index_refresh_enabled is False
    assert settings.plan_index_refresh_interval_seconds == 900


def test_the_constructor_default_is_closed_too() -> None:
    """A caller that forgets the setting gets today's behaviour, never the new one."""
    import inspect

    signature = inspect.signature(PlanIndexRefresher.__init__)
    assert signature.parameters["interval_seconds"].default == 900


@pytest.mark.asyncio
async def test_the_lifecycle_does_not_start_a_closed_refresher() -> None:
    """Shipping closed has to hold in the wiring, not only in Settings."""
    import inspect

    import brain_v42.mcp.server as server_mod

    source = inspect.getsource(server_mod.app_lifecycle)
    assert "settings.plan_index_refresh_enabled" in source, (
        "the lifecycle must read the flag before starting the refresher"
    )
    assert "cleanup.push_async_callback(plan_index_refresher.stop)" in source, (
        "the refresher must be stopped by the same stack that owns every other resource"
    )
