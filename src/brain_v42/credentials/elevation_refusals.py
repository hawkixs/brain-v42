"""Keep business refusals distinct from transport failures for the watcher."""

from threading import Lock
from types import MappingProxyType

import structlog

ELEVATION_REFUSAL_STATUSES = MappingProxyType(
    {
        "session_not_open": 409,
        "session_not_operator": 409,
        "no_attributed_connection": 409,
        "no_elevatable_connection": 409,
        "invalid_window": 422,
    }
)
_counts: dict[str, int] = {}
_lock = Lock()
_logger = structlog.get_logger(__name__)


def emit_elevation_refusal(
    reason: str,
    *,
    status: int,
    session_id: str,
    requesting_client_id: str,
    peer: str | None,
) -> None:
    """Freeze the event keys and increment exactly once, without request payloads."""
    if reason not in ELEVATION_REFUSAL_STATUSES or status != ELEVATION_REFUSAL_STATUSES[reason]:
        raise ValueError("invalid elevation refusal")
    with _lock:
        _counts[reason] = _counts.get(reason, 0) + 1
    _logger.warning(
        "elevation.refused",
        reason=reason,
        status=status,
        session_id=session_id,
        requesting_client_id=requesting_client_id,
        peer=peer,
    )


def elevation_refusal_counts() -> dict[str, int]:
    """Return a snapshot so metrics consumers cannot alter cumulative counts."""
    with _lock:
        return dict(_counts)


def reset_elevation_refusal_counts() -> None:
    """Isolate tests without adding synthetic observations."""
    with _lock:
        _counts.clear()
