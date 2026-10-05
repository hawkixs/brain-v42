"""AgentTraceNet — the server closes the tracers its connections left behind.

Ticket 09d2b56e. A tracer is closed when its transport terminates (DELETE, idle
eviction, shutdown: the hook in ``brain_v42.mcp.server``). A connection that
never terminates cleanly — a crashed process, a killed server — never reaches
that hook. This loop is its net: the 4 h observation rule of the Dream sweep,
run by the server that opened the tracers, so that closing them no longer waits
for a Dream night (none ran while Dream was suspended, and 1,616 tracers piled
up).

It touches tracers only (``close_inactive_agent_traces`` guards on
``nature = 'agent'``): human sessions keep their explicit commands and the
7-day Dream rule.

Follows the PlanIndexRefresher idiom: asyncio task with start()/stop(), owned by
the lifecycle's cleanup stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from brain_v42.models.brain_session import AGENT_INACTIVE_AFTER

if TYPE_CHECKING:
    from brain_v42.config import Settings

logger = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 900

InactiveTraceCloser = Callable[..., Awaitable[list[UUID]]]


def agent_trace_net_is_armed(settings: Settings) -> bool:
    """Tracers exist only over stateful HTTP with auto-open on; so does their net."""
    return (
        settings.brain_session_auto_open_enabled
        and settings.brain_mcp_transport == "http"
        and not settings.mcp_http_stateless
    )


class AgentTraceNet:
    """Periodically closes the tracers unobserved for ``AGENT_INACTIVE_AFTER``."""

    def __init__(
        self,
        close_inactive: InactiveTraceCloser,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        inactive_after: timedelta = AGENT_INACTIVE_AFTER,
    ) -> None:
        self._close_inactive = close_inactive
        self._interval = interval_seconds
        self._inactive_after = inactive_after
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the loop. A second call is a no-op, not a second loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("agent_trace_net.started", interval=self._interval)

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
        logger.info("agent_trace_net.stopped")

    async def sweep_once(self) -> int:
        """Close the stale tracers once; returns how many were closed."""
        closed = await self._close_inactive(inactive_after=self._inactive_after)
        logger.info("agent_trace_net.sweep_done", closed=len(closed))
        return len(closed)

    async def _run_loop(self) -> None:
        """Sweep, then sleep: a restart is exactly when the previous process's
        tracers became orphans, so the first sweep does not wait an interval."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A database blip must not end the net; the next tick tries again.
                logger.warning("agent_trace_net.sweep_failed", exc_info=True)
            await asyncio.sleep(self._interval)
