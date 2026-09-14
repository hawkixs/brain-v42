"""``AgentProvider`` -- the common protocol implemented by the adapters.

Structural (``typing.Protocol``), not a base class: ``providers/codex.py``,
``providers/agy.py`` and ``providers/claude.py`` each expose a class that
satisfies this shape. The protocol is the seam a consumer can code against
without caring which rail answers.

``child_environment`` may return ``None``: a rail that inherits the ambient
environment rather than building a scoped one says so this way.
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

    def tool_call_completed(self, spec: RunSpec) -> bool:
        """Did a call on ``spec.profile.mcp`` SUCCEED anywhere in this run's event log?

        The exact predicate that authorises a provider switchover -- see
        ``headless_agents.capability.PROVIDER_FALLBACK_EXIT_CODE``. A spec
        without a server can never have completed one.
        """
        ...

    def run(self, spec: RunSpec) -> RunResult:
        """Run one invocation end to end and return its :class:`RunResult`."""
        ...
