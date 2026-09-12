"""``AgentProvider`` -- the common protocol implemented by the three adapters.

Structural (``typing.Protocol``), not a base class: ``providers/codex.py``,
``providers/agy.py`` and ``providers/claude.py`` each expose a class that
satisfies this shape by wrapping their rail's pre-existing, differently-shaped
``build_*_command`` / ``run_*`` functions -- moved unchanged from
``scripts/dream/`` so their unit tests keep passing unmodified. The protocol
is the seam a second consumer (a PR reviewer service) can code against without
caring which rail answers.

``child_environment`` may return ``None``: under the historical "capability
enforcement disabled" rollback path, all three rails inherit the ambient
environment rather than building a scoped one -- see
``brain_v42.agents.capability.build_child_environment``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from .result import RunResult
from .spec import RunSpec


@runtime_checkable
class AgentProvider(Protocol):
    """One headless agent rail: build a command, scope its environment, run it."""

    name: str

    def build_command(self, spec: RunSpec) -> list[str]:
        """The argv for one invocation of this rail, given ``spec``."""
        ...

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        """The scoped child environment, or ``None`` to inherit ``environ``."""
        ...

    def prepare_home(self, spec: RunSpec) -> Path | None:
        """An ephemeral HOME to run under, or ``None`` if the rail needs none."""
        ...

    def tool_call_completed(self, events_log: Path) -> bool:
        """Did a Brain MCP tool call SUCCEED anywhere in this rail's event log?

        The exact predicate that authorises a provider switchover -- see
        ``brain_v42.agents.capability.PROVIDER_FALLBACK_EXIT_CODE``.
        """
        ...

    def run(self, spec: RunSpec) -> RunResult:
        """Run one phase end to end and return its :class:`RunResult`."""
        ...
