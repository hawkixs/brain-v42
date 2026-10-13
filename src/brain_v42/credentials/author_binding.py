"""Count prospective author-binding candidates; the observer lives in the MCP layer."""

from threading import Lock

_counts: dict[str, int] = {}
_lock = Lock()


def count_author_binding(key: str) -> None:
    """Count one `<tool>:<cause>` candidate; the key is built from bounded labels."""
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1


def author_binding_counts() -> dict[str, int]:
    """Return a snapshot so metrics consumers cannot alter cumulative counts."""
    with _lock:
        return dict(_counts)


def reset_author_binding_counts() -> None:
    """Isolate tests without adding synthetic observations."""
    with _lock:
        _counts.clear()
