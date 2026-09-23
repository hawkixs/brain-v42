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

``run_dir`` names one directory per run: every log the caller leaves unset
takes its fixed name inside it (:data:`RUN_DIR_LOG_NAMES`), its name is the
run's id, and the provider writes ``result.json`` there (see
:mod:`headless_agents.run_record`). Explicit log paths still win, so a caller
that names its logs runs exactly as before.

``context`` is the resolved context bundle (:mod:`headless_agents.context`);
each rail delivers it through its preamble channel.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .context import ContextBundle
from .profile import CapabilityProfile

#: The name each log takes inside ``RunSpec.run_dir`` when the caller names
#: none. A reader of a run directory relies on them: they are part of the
#: contract, like the schema of ``result.json``.
RUN_DIR_LOG_NAMES: Final[Mapping[str, str]] = {
    "report_log": "report.log",
    "events_log": "events.jsonl",
    "stderr_log": "stderr.log",
    "raw_log": "raw.log",
}


def _in_run_dir(current: Path | None, run_dir: Path, field_name: str) -> Path:
    """``current`` when the caller named it, else its fixed name inside ``run_dir``."""
    return current if current is not None else run_dir / RUN_DIR_LOG_NAMES[field_name]


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
    run_dir: Path | None = None
    workspace: Path | None = None
    executable: str | None = None
    environment: Mapping[str, str] | None = None
    context: ContextBundle | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def effective_timeout_seconds(self, *, now: float | None = None) -> float:
        """``timeout_seconds`` capped by the time left until ``deadline``."""
        if self.deadline is None:
            return self.timeout_seconds
        remaining = self.deadline - (time.monotonic() if now is None else now)
        return max(0.0, min(self.timeout_seconds, remaining))

    def with_run_dir_defaults(self) -> RunSpec:
        """This spec with every UNSET log path moved into ``run_dir``.

        Explicit paths win: a caller that names a log keeps it. Without a
        ``run_dir`` the spec itself is returned, unchanged.
        """
        run_dir = self.run_dir
        if run_dir is None:
            return self
        return replace(
            self,
            report_log=_in_run_dir(self.report_log, run_dir, "report_log"),
            events_log=_in_run_dir(self.events_log, run_dir, "events_log"),
            stderr_log=_in_run_dir(self.stderr_log, run_dir, "stderr_log"),
            raw_log=_in_run_dir(self.raw_log, run_dir, "raw_log"),
        )
