"""``RunResult`` -- the common output shape across the providers.

``tokens`` is ``None``, not a zeroed :class:`TokenUsage`, when the rail's own
telemetry does not measure a count: absent is not zero, and a consumer that
persists these figures must be able to tell the two apart. A provider that
cannot observe cached-input tokens must report ``cached=None`` there, never
``0``.

``model`` is the label the caller asked for; ``model_reported`` is what the
CLI's envelope says actually answered, when it says anything (only ``claude``
names its model today). The two differ whenever a CLI silently substitutes,
which is exactly the case a consumer wants to display rather than normalise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """Token counts a provider measured for one run. ``None`` = not measured."""

    input: int | None = None
    output: int | None = None
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
    model_reported: str | None = None
    cost_usd: float | None = None
