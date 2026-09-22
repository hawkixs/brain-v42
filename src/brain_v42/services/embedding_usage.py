"""Request-scoped provider-reported embedding usage.

The capture deliberately holds only aggregate integers.  Provider payloads and
the text that produced them never leave the embedding request boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class EmbeddingUsage:
    total_tokens: int = 0
    reported_requests: int = 0


_current_usage: ContextVar[EmbeddingUsage | None] = ContextVar("embedding_usage", default=None)


@contextmanager
def capture_embedding_usage() -> Iterator[EmbeddingUsage]:
    """Make one independent usage accumulator available to an embedding call."""
    usage = EmbeddingUsage()
    token = _current_usage.set(usage)
    try:
        yield usage
    finally:
        _current_usage.reset(token)


def record_embedding_usage(total_tokens: int | None) -> None:
    """Add a provider-reported total to the active request capture, if any."""
    if (
        total_tokens is None
        or isinstance(total_tokens, bool)
        or not isinstance(total_tokens, int)
        or total_tokens < 0
    ):
        return
    usage = _current_usage.get()
    if usage is not None:
        usage.total_tokens += total_tokens
        usage.reported_requests += 1
