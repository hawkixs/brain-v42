"""``RunResult`` / ``TokenUsage`` -- re-exported from the shared runtime.

The shapes moved to :mod:`headless_agents.result` (Brain ticket b2a2d1a5);
the Dream-side providers keep importing them from here. ``tokens`` is
``None``, not a zeroed :class:`TokenUsage`, when the rail's own telemetry
does not measure a count -- the same "absent is not zero" contract migration
049 exists to preserve for ``dream_runs``.
"""

from __future__ import annotations

from headless_agents.result import RunResult as RunResult
from headless_agents.result import TokenUsage as TokenUsage
