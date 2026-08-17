"""Structured audit events for successful Codex-originated mutations."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def log_write(operation: str, actor_project: str, **context: Any) -> None:
    logger.info(
        "codex_gateway.write",
        operation=operation,
        origin="codex",
        actor_project=actor_project,
        **context,
    )
