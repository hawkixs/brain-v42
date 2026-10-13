"""Measure prospective author binding without imposing an authorization policy.

Ticket c17ef5b1, phase 1: the ticket and delivery write tools take a declared
project that is not bound to the transport actor yet. This observer only counts
and logs the writes that a binding would affect; it never refuses.
"""

import structlog

from brain_v42.credentials.author_binding import (
    author_binding_counts,
    count_author_binding,
    reset_author_binding_counts,
)
from brain_v42.credentials.redact import sanitize_label
from brain_v42.provenance import get_current_actor_project, get_current_principal

__all__ = ["author_binding_counts", "observe_declared_project", "reset_author_binding_counts"]

_logger = structlog.get_logger(__name__)


def observe_declared_project(tool: str, declared: str | None) -> None:
    """Keep candidate counts and logs independent of the write's success or failure."""
    try:
        client_id = get_current_principal()
        actor_project = get_current_actor_project()
        if client_id is None:
            cause = "unauthenticated"
        elif actor_project is None:
            cause = "unresolved_actor"
        elif declared is not None and declared != actor_project:
            cause = "mismatch"
        else:
            return
        safe_tool = sanitize_label(tool)
        count_author_binding(f"{safe_tool}:{cause}")
        # Shared-token and stdio writes are all unauthenticated: count them, log quietly.
        log = _logger.debug if cause == "unauthenticated" else _logger.info
        log(
            "author_binding.candidate",
            tool=safe_tool,
            cause=cause,
            declared_project=sanitize_label(declared),
            actor_project=sanitize_label(actor_project),
            client_id=sanitize_label(client_id),
        )
    except Exception as exc:
        # Observability must never alter tool returns or existing refusals.
        try:
            _logger.warning("author_binding.failed", error_type=type(exc).__name__)
        except Exception:
            pass
