"""PlanIndexRefresher — re-scan plan files on a period, not on a restart.

Until this loop existed, `index_all_projects()` ran exactly once, as a
background task at startup. A plan written to disk therefore waited for the
next restart of the MCP server before anything could find it -- and `plan` is
the third most-read type in the corpus.

Periodic rather than a filesystem watch: the configured scan paths span
several roots, an inotify watch per root would have to be rebuilt whenever a
project's `plan_scan_paths` changes, and the content-hash skip already makes
an unchanged sweep cheap -- one read and one lookup per file, no embedding.

The explicit path is NOT replaced by this one. `brain_reindex_plans` stays the
way to say "now", and it is the only way while this loop ships closed.

Follows the MetricsFlusher / DecayFlusher idiom: asyncio task with
start()/stop(), owned by the lifecycle's cleanup stack.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from brain_v42.services.plan_indexer import PlanIndexer

logger = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 900


class PlanIndexRefresher:
    """Periodically re-scans every project that configures plan scan paths."""

    def __init__(
        self,
        plan_indexer: PlanIndexer,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._plan_indexer = plan_indexer
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the periodic sweep. A second call is a no-op, not a second loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("plan_index_refresher.started", interval=self._interval)

    async def stop(self) -> None:
        """Cancel the loop and wait for it, including a sweep in flight."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("plan_index_refresher.stopped")

    async def _run_loop(self) -> None:
        """Sleep first, then sweep.

        The startup pass already covers t=0. Sweeping immediately would put two
        `index_all_projects` on the same files in the same process.
        """
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad sweep -- an unreachable embedding service, a database
                # blip -- must not end the loop. The next tick tries again.
                logger.warning("plan_index_refresher.sweep_failed", exc_info=True)

    async def _sweep(self) -> None:
        """Run one sweep and report its totals in a single line.

        Totals, not one line per rejected path: this runs on a period, so a
        path that fails once fails every tick, and a per-path warning would
        turn a standing configuration error into a log flood. The paths
        themselves stay named in each project's `failures` list, which
        `brain_reindex_plans` prints on demand.
        """
        results: dict[str, Any] = await self._plan_indexer.index_all_projects()
        indexed = sum(stats.get("indexed", 0) for stats in results.values())
        errors = sum(stats.get("errors", 0) for stats in results.values())
        logger.info(
            "plan_index_refresher.sweep_done",
            projects=len(results),
            indexed=indexed,
            errors=errors,
        )
