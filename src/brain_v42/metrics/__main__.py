"""Entrypoint for the independently managed metrics lifecycle."""

from __future__ import annotations

import asyncio
import signal

import structlog

from brain_v42.automation.dedup import run_dedup_loop
from brain_v42.metrics.runtime import (
    build_metrics_runtime,
    build_sidecar_structlog_processors,
    run_cleanup_loop,
)

# Temporary rollback facades for internal imports kept stable during ARC1 lot 1.
_dedup_loop = run_dedup_loop
_cleanup_loop = run_cleanup_loop


async def main(stop_event: asyncio.Event | None = None) -> int:
    """Run metrics until an explicit or signal-driven stop event."""
    runtime = build_metrics_runtime()
    structlog.configure(
        processors=build_sidecar_structlog_processors(runtime._resources.collector),
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    effective_stop = stop_event
    if effective_stop is None:
        effective_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, effective_stop.set)
    return await runtime.run(effective_stop)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
