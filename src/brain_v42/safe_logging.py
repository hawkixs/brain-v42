"""The one ConsoleRenderer factory: tracebacks are printed, frame locals never.

With ``rich`` installed, ``structlog.dev.ConsoleRenderer`` renders ``exc_info``
through a rich formatter that shows every frame's local variables. During a
Postgres outage the asyncpg connect frame holds ``password=...``, which then
lands in clear in the systemd journal. ``plain_traceback`` keeps the traceback
and drops the locals, whether or not rich is present.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Sequence
from typing import Any, Literal

import structlog
from structlog.types import EventDict, Processor


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


def _sanitize_access_event(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Keep access paths while dropping request queries and optional header fields."""
    record = event_dict.get("_record")
    args = event_dict.pop("positional_args", ())
    if isinstance(record, logging.LogRecord) and record.name == "uvicorn.access":
        if isinstance(args, tuple) and len(args) == 5:
            client, method, target, version, status = args
            path = str(target).split("?", 1)[0]
            event_dict["event"] = f'{client} - "{method} {path} HTTP/{version}" {status}'
        else:
            # Unknown access formats must not expose an unparsed request or headers.
            event_dict["event"] = "http.access"
    elif isinstance(record, logging.LogRecord) and record.name == "aiohttp.access":
        request = re.search(r'"(\S+) (\S+) HTTP/(\S+)" (\d{3})', event_dict["event"])
        if request is not None:
            method, target, version, status = request.groups()
            path = target.split("?", 1)[0]
            event_dict["event"] = f'"{method} {path} HTTP/{version}" {status}'
        else:
            event_dict["event"] = "http.access"
    return event_dict


def configure_json_logging(processors: Sequence[Processor]) -> None:
    """Share the no-locals JSON chain with stdlib and keep one stderr wire format."""
    # Keep foreign formatter metadata out of the sidecar's structured recent-log
    # buffer: the LogRecord still holds the original request, including its query.
    pre_chain: list[Processor] = [
        _sanitize_access_event,
        structlog.stdlib.add_logger_name,
        *build_logging_processors("json")[:-1],
    ]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            pass_foreign_args=True,
            foreign_pre_chain=pre_chain,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                processors[-1],
            ],
        )
    )
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers.clear()
        log.propagate = True
        log.setLevel(logging.NOTSET)
    structlog.configure(
        processors=[
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.add_logger_name,
            *processors[:-1],
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
