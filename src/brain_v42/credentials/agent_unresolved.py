"""Count every accepted project-actor fallback independently of warning deduplication."""

from threading import Lock
from typing import Literal

_counts: dict[str, int] = {}
_lock = Lock()


def count_agent_unresolved(reason: Literal["not_kebab", "unknown_project"]) -> None:
    """Keep reason cardinality bounded without retaining request identities."""
    if reason not in {"not_kebab", "unknown_project"}:
        raise ValueError("invalid agent unresolved reason")
    with _lock:
        _counts[reason] = _counts.get(reason, 0) + 1


def agent_unresolved_counts() -> dict[str, int]:
    """Return a snapshot so metrics consumers cannot alter cumulative counts."""
    with _lock:
        return dict(_counts)


def reset_agent_unresolved_counts() -> None:
    """Isolate tests without adding synthetic observations."""
    with _lock:
        _counts.clear()
