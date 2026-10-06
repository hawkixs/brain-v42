"""The one ConsoleRenderer factory: tracebacks are printed, frame locals never.

With ``rich`` installed, ``structlog.dev.ConsoleRenderer`` renders ``exc_info``
through a rich formatter that shows every frame's local variables. During a
Postgres outage the asyncpg connect frame holds ``password=...``, which then
lands in clear in the systemd journal. ``plain_traceback`` keeps the traceback
and drops the locals, whether or not rich is present.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from structlog.types import Processor


def safe_console_renderer(**kwargs: Any) -> structlog.dev.ConsoleRenderer:
    """Build a ConsoleRenderer whose tracebacks never include frame locals."""
    return structlog.dev.ConsoleRenderer(
        exception_formatter=structlog.dev.plain_traceback, **kwargs
    )


def build_logging_processors(
    log_format: Literal["console", "json"], *, colors: bool | None = None
) -> list[Processor]:
    """Keep both service log formats aligned without ever capturing frame locals."""
    processors: list[Processor] = [
        # UTC for the JSON lines a follower parses; the console keeps its local time.
        structlog.processors.TimeStamper(fmt="iso", utc=log_format == "json"),
        structlog.processors.add_log_level,
    ]
    if log_format == "json":
        processors.extend(
            [
                structlog.processors.ExceptionRenderer(
                    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
                ),
                structlog.processors.JSONRenderer(),
            ]
        )
    else:
        processors.append(
            safe_console_renderer() if colors is None else safe_console_renderer(colors=colors)
        )
    return processors
