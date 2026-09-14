"""``RunSpec`` -- the common input shape across the providers.

Not every field applies to every provider -- ``reasoning_effort`` is
Codex-only, ``max_turns`` and ``raw_log`` are Claude-only, ``workspace`` is
Codex-only. Fields irrelevant to a given rail are simply left at their
default. ``raw_log`` exists as its own field rather than reusing ``events_log``
or ``report_log`` because Claude does not distinguish them: ``run_claude``
mixes stdout/stderr into ONE file, and a fallback between the two would invent
a mapping the caller did not make.

``deadline`` is a ``time.monotonic()`` instant. When set, the effective
timeout of the run is the smaller of ``timeout_seconds`` and the time left
until the deadline, so a caller running a chain under one wall budget can hand
the same deadline to every link and never overshoot it.

``environment`` is the child environment to run under; ``None`` inherits the
runtime's own. A caller that scopes a bearer builds it with
:func:`headless_agents.capability.scoped_environment`; a caller that isolates a
run builds it with :func:`headless_agents.sandbox.sandbox_environment`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .profile import CapabilityProfile


@dataclass(frozen=True, kw_only=True)
class RunSpec:
    """One agent invocation: a prompt, a profile, and where its output goes."""

    prompt: str
    name: str = "run"
    model: str = ""
    profile: CapabilityProfile = field(default_factory=CapabilityProfile)
    reasoning_effort: str = "medium"
    max_turns: int = 1
    timeout_seconds: float = 300.0
    deadline: float | None = None
    report_log: Path | None = None
    events_log: Path | None = None
    stderr_log: Path | None = None
    raw_log: Path | None = None
    workspace: Path | None = None
    executable: str | None = None
    environment: Mapping[str, str] | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def effective_timeout_seconds(self, *, now: float | None = None) -> float:
        """``timeout_seconds`` capped by the time left until ``deadline``."""
        if self.deadline is None:
            return self.timeout_seconds
        remaining = self.deadline - (time.monotonic() if now is None else now)
        return max(0.0, min(self.timeout_seconds, remaining))
