"""``RunResult`` -- the common output shape across the three agent providers.

``tokens`` is ``None``, not a zeroed :class:`TokenUsage`, when the rail's own
telemetry does not measure a count -- the same "absent is not zero" contract
migration 049 exists to preserve for ``dream_runs`` (see ``CLAUDE.md``,
``freshness_source`` / thinking-token discussion). A provider that cannot
observe cached-input tokens (Claude's OTEL stream, today) must report
``cached=None`` there, never ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """Token counts a provider measured for one run. ``None`` = not measured."""

    fresh: int | None = None
    cached: int | None = None
    thinking: int | None = None


@dataclass(frozen=True, kw_only=True)
class RunResult:
    """The outcome of one :meth:`AgentProvider.run` call."""

    exit_code: int
    provider: str
    model: str
    report_path: Path | None
    events_log: Path | None
    tokens: TokenUsage | None
    duration_seconds: float
    tool_call_completed: bool
