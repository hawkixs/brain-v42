"""Configure safe gateway logging before uvicorn installs any handlers."""

from __future__ import annotations

from typing import Literal

from brain_v42.safe_logging import (
    configure_console_logging,
    configure_json_logging,
    configure_stderr_logging,
)


def configure_gateway_logging(log_format: Literal["console", "json"]) -> None:
    """Route gateway logs to sanitized stderr so stdout remains available to callers."""
    configure_stderr_logging(
        log_format,
        json_configurer=configure_json_logging,
        console_configurer=configure_console_logging,
    )
