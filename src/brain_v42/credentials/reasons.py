"""Centralise refusal observations so each refusal is counted and logged once."""

from threading import Lock
from types import MappingProxyType

import structlog

from brain_v42.credentials.redact import sanitize_label

TRANSPORT_STATUSES = MappingProxyType(
    {
        "missing_token": 401,
        "unknown_token": 401,
        "revoked_token": 401,
        "expired_token": 401,
        "agent_mismatch": 403,
        "foreign_client_attach": 403,
        "rate_limited": 429,
        "registry_unavailable": 503,
    }
)
TOOL_STATUSES = MappingProxyType({"family_denied": 403, "issuer_not_allowed": 403})
REFUSAL_STATUSES = MappingProxyType({**TRANSPORT_STATUSES, **TOOL_STATUSES})
_counts: dict[str, int] = {}
_lock = Lock()
_logger = structlog.get_logger(__name__)


def emit_refusal(
    reason: str,
    *,
    status: int,
    client_id: str | None = None,
    requesting_client_id: str | None = None,
    owner_client_id: str | None = None,
    session_id: str | None = None,
    declared_agent: str | None = None,
    peer: str | None = None,
    path: str | None = None,
    tool: str | None = None,
) -> None:
    """Reject malformed observations before either the counter or watcher sees them."""
    if reason not in REFUSAL_STATUSES:
        raise ValueError("unknown refusal reason")
    if status != REFUSAL_STATUSES[reason]:
        raise ValueError("incorrect refusal status")
    if reason == "foreign_client_attach" and requesting_client_id is None:
        raise ValueError("foreign client attach requires requesting_client_id")
    context = {
        "client_id": None if client_id is None else client_id[:128],
        "requesting_client_id": None
        if requesting_client_id is None
        else requesting_client_id[:128],
        "owner_client_id": None if owner_client_id is None else owner_client_id[:128],
        "session_id": None if session_id is None else session_id[:128],
        "declared_agent": sanitize_label(declared_agent),
        "peer": None if peer is None else peer[:128],
        "path": None if path is None else path[:128],
        "tool": None if tool is None else tool[:128],
    }
    with _lock:
        _counts[reason] = _counts.get(reason, 0) + 1
    _logger.warning(
        "mcp_auth.refused",
        reason=reason,
        status=status,
        **{key: value for key, value in context.items() if value is not None},
    )


def refusal_counts() -> dict[str, int]:
    """Return a copy so observers cannot mutate the process's counters."""
    with _lock:
        return dict(_counts)


def reset_refusal_counts() -> None:
    """Isolate tests without manufacturing zero-valued observations."""
    with _lock:
        _counts.clear()
