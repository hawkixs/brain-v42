"""The one ConsoleRenderer factory: tracebacks are printed, frame locals never.

With ``rich`` installed, ``structlog.dev.ConsoleRenderer`` renders ``exc_info``
through a rich formatter that shows every frame's local variables. During a
Postgres outage the asyncpg connect frame holds ``password=...``, which then
lands in clear in the systemd journal. ``plain_traceback`` keeps the traceback
and drops the locals, whether or not rich is present.
"""

from __future__ import annotations

from typing import Any

import structlog


def safe_console_renderer(**kwargs: Any) -> structlog.dev.ConsoleRenderer:
    """Build a ConsoleRenderer whose tracebacks never include frame locals."""
    return structlog.dev.ConsoleRenderer(
        exception_formatter=structlog.dev.plain_traceback, **kwargs
    )
