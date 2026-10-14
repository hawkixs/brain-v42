"""Configure safe gateway logging before uvicorn installs any handlers."""

from __future__ import annotations

import sys
from typing import Literal

import structlog

from brain_v42.safe_logging import (
    build_logging_processors,
    configure_console_logging,
    configure_json_logging,
)


def configure_gateway_logging(log_format: Literal["console", "json"]) -> None:
    """Route gateway logs to sanitized stderr so stdout remains available to callers."""
    processors = build_logging_processors(log_format, colors=False)
    if log_format == "json":
        configure_json_logging(processors)
    else:
        structlog.configure(
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            processors=processors,
        )
        configure_console_logging(processors)
