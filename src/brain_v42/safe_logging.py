"""Shared exception rendering: keep frames and types, never locals or messages.

With ``rich`` installed, ``structlog.dev.ConsoleRenderer`` renders ``exc_info``
through a rich formatter that shows every frame's local variables. During a
Postgres outage the asyncpg connect frame holds ``password=...``, which then
lands in clear in the systemd journal. Driver exception messages can also carry
credentials, SQL and bound parameters. Both renderers use the same sanitizing
transformer, including causes and exception groups, so neither messages nor
frame locals reach the output, whether or not rich is present.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from collections.abc import Sequence
from typing import Any, Literal, TextIO
from urllib.parse import urlsplit, urlunsplit

import structlog
from structlog.types import EventDict, ExcInfo, Processor

_REDACTED_MESSAGE = "<message redacted>"


class _SafeExceptionTransformer(structlog.tracebacks.ExceptionDictTransformer):
    """Remove messages from every nested exception before either renderer sees it."""

    def __call__(self, exc_info: ExcInfo) -> list[dict[str, Any]]:
        stacks = super().__call__(exc_info)
        self._redact(stacks)
        return stacks

    def _redact(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self._redact(item)
        elif isinstance(value, dict):
            if "exc_value" in value:
                value["exc_value"] = _REDACTED_MESSAGE
                # Notes and SyntaxError payloads are also exception messages.
                value["exc_notes"] = []
                syntax = value.get("syntax_error")
                if syntax:
                    # `line` is the offending source text, as revealing as the message.
                    syntax["msg"] = _REDACTED_MESSAGE
                    syntax["text"] = _REDACTED_MESSAGE
                    syntax["line"] = _REDACTED_MESSAGE
            for item in value.values():
                self._redact(item)


_SAFE_EXCEPTION_TRANSFORMER = _SafeExceptionTransformer(show_locals=False)


def _write_exception_stacks(sio: TextIO, stacks: list[dict[str, Any]]) -> None:
    """Render only frame coordinates; source lines may themselves contain secrets."""
    previous: dict[str, Any] | None = None
    for stack in reversed(stacks):
        if previous is not None:
            relation = (
                "The above exception was the direct cause of the following exception:"
                if previous["is_cause"]
                else "During handling of the above exception, another exception occurred:"
            )
            sio.write(f"\n{relation}\n\n")
        sio.write("Traceback (most recent call last):\n")
        for frame in stack["frames"]:
            sio.write(f'  File "{frame["filename"]}", line {frame["lineno"]}, in {frame["name"]}\n')
        sio.write(f"{stack['exc_type']}: {stack['exc_value']}\n")
        for group in stack.get("exceptions", []):
            # One chain per sub-exception; accept a bare stack too, whatever the version.
            _write_exception_stacks(sio, [group] if isinstance(group, dict) else group)
        previous = stack


def _safe_console_traceback(sio: TextIO, exc_info: ExcInfo) -> None:
    """Use the JSON sanitizer for standalone console consumers too."""
    sio.write("\n")
    _write_exception_stacks(sio, _SAFE_EXCEPTION_TRANSFORMER(exc_info))


def _safe_text_traceback(exc_info: ExcInfo) -> str:
    """Render the sanitized traceback as one string, for chains that pre-format."""
    sio = io.StringIO()
    _write_exception_stacks(sio, _SAFE_EXCEPTION_TRANSFORMER(exc_info))
    return sio.getvalue().rstrip("\n")


#: Drop-in replacement for ``structlog.processors.format_exc_info``: the stock
#: processor formats with the stdlib, message included, before any renderer runs.
safe_format_exc_info = structlog.processors.ExceptionRenderer(_safe_text_traceback)


def redact_url(url: str | None) -> str | None:
    """Remove URL credentials and parameters before exposing endpoints; path segments are kept."""
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.hostname:
            return "<unparseable url>"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (ValueError, TypeError):
        return "<unparseable url>"


def safe_console_renderer(**kwargs: Any) -> structlog.dev.ConsoleRenderer:
    """Keep traceback diagnostics without disclosing locals or exception messages."""
    return structlog.dev.ConsoleRenderer(exception_formatter=_safe_console_traceback, **kwargs)


def build_logging_processors(
    log_format: Literal["console", "json"], *, colors: bool | None = None
) -> list[Processor]:
    """Keep both service formats aligned without exposing locals or messages."""
    processors: list[Processor] = [
        # UTC for the JSON lines a follower parses; the console keeps its local time.
        structlog.processors.TimeStamper(fmt="iso", utc=log_format == "json"),
        structlog.processors.add_log_level,
    ]
    if log_format == "json":
        processors.extend(
            [
                structlog.processors.ExceptionRenderer(_SAFE_EXCEPTION_TRANSFORMER),
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


def configure_console_logging(processors: Sequence[Processor]) -> None:
    """Sanitize stdlib tracebacks on stderr without changing structlog's output stream."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            pass_foreign_args=True,
            foreign_pre_chain=[
                _sanitize_access_event,
                structlog.stdlib.add_logger_name,
                *build_logging_processors("console")[:-1],
            ],
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


def configure_json_logging(processors: Sequence[Processor]) -> None:
    """Share exception redaction with stdlib and keep one stderr wire format."""
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


def configure_cli_logging(log_format: Literal["console", "json"]) -> None:
    """Give a one-shot CLI the wire format of the container it runs in.

    The maintenance CLIs never configured structlog, so under BRAIN_LOG_FORMAT=json
    their engine lines still came out as coloured console text (ticket 8d053014).
    The console default is left untouched; report lines printed to stdout are not
    logs and stay plain text.
    """
    if log_format == "json":
        configure_json_logging(build_logging_processors("json"))
