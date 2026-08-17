"""Entrypoint for ``python -m brain_v42.automation``."""

from __future__ import annotations

import asyncio
import signal

from brain_v42.automation.runtime import build_automation_runtime


async def main(stop_event: asyncio.Event | None = None) -> int:
    """Run automation until the supplied or signal-driven stop event."""
    runtime = build_automation_runtime()
    effective_stop = stop_event
    if effective_stop is None:
        effective_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, effective_stop.set)
    return await runtime.run(effective_stop)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
