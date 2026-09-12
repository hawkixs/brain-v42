"""``RunSpec`` -- the common input shape across the three agent providers.

The three rails (Codex, agy, Claude) predate this abstraction and keep their
own, differently-shaped ``run_*`` functions in
:mod:`brain_v42.agents.providers` -- moved unchanged from ``scripts/dream/``,
because their existing unit tests call them by their exact old keyword
signatures. ``RunSpec`` is the adapter layer on top: each
:class:`~brain_v42.agents.protocol.AgentProvider` implementation reads the
fields it needs off one ``RunSpec`` and ignores the rest, so a caller that does
not care which rail it is talking to (a future PR reviewer service, for
instance) can build one value and hand it to any provider.

Not every field applies to every provider -- ``reasoning_effort`` is
Codex-only, ``max_turns`` is Claude-only, ``workspace`` is Codex-only. Fields
irrelevant to a given rail are simply left at their default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class RunSpec:
    """One agent invocation: a phase, a prompt, and where its output goes."""

    phase: str
    prompt: str
    project_key: str | None = None
    model: str = ""
    reasoning_effort: str = "medium"
    max_turns: int = 1
    timeout_seconds: float = 300.0
    report_log: Path | None = None
    events_log: Path | None = None
    stderr_log: Path | None = None
    workspace: Path | None = None
    mcp_url: str | None = None
    executable: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
