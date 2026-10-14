"""Entrypoint for the independently managed metrics lifecycle."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import cast

import structlog

from brain_v42.automation.dedup import run_dedup_loop
from brain_v42.config import get_settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.runtime import (
    build_metrics_runtime,
    build_sidecar_structlog_processors,
    run_cleanup_loop,
)
from brain_v42.safe_logging import configure_console_logging, configure_json_logging

# Temporary rollback facades for internal imports kept stable during ARC1 lot 1.
_dedup_loop = run_dedup_loop
_cleanup_loop = run_cleanup_loop


class _PendingRecentLogs:
    """Stands in for the collector until the runtime that owns it is built.

    Building the runtime logs (the embedding factory says which backend it built),
    but the collector the sidecar's log chain mirrors into is created by that very
    build. Logging used to be configured afterwards, so every start began with
    console lines a JSON follower cannot parse (ticket c206929f). The chain is now
    configured first against this stand-in, which keeps the early lines and hands
    them to the real collector once attached.
    """

    _MAX_PENDING = 100

    def __init__(self) -> None:
        self._target: MetricsCollector | None = None
        self._pending: list[tuple[str, str]] = []

    def push_recent_log(self, level: str, msg: str) -> None:
        if self._target is not None:
            self._target.push_recent_log(level, msg)
        elif len(self._pending) < self._MAX_PENDING:
            self._pending.append((level, msg))

    def attach(self, collector: MetricsCollector) -> None:
        self._target = collector
        for level, msg in self._pending:
            collector.push_recent_log(level, msg)
        self._pending.clear()


def _configure_logging(collector: MetricsCollector) -> None:
    """Expose JSON on stderr for watchers, preserving the console's stdout stream."""
    log_format = get_settings().brain_log_format
    processors = build_sidecar_structlog_processors(collector, log_format=log_format)
    if log_format == "json":
        configure_json_logging(processors)
    else:
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(0),
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
        configure_console_logging(processors)
    structlog.get_logger(__name__).info(
        "logging.configured", renderer=log_format, service="brain-v42-metrics", pid=os.getpid()
    )


async def main(stop_event: asyncio.Event | None = None) -> int:
    """Run metrics until an explicit or signal-driven stop event."""
    recent_logs = _PendingRecentLogs()
    # RecentLogProcessor only calls push_recent_log, which the stand-in provides.
    _configure_logging(cast(MetricsCollector, recent_logs))
    runtime = build_metrics_runtime()
    recent_logs.attach(runtime._resources.collector)
    effective_stop = stop_event
    if effective_stop is None:
        effective_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, effective_stop.set)
    return await runtime.run(effective_stop)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
