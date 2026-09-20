"""Read the shipped Alembic head only when every revision file is trustworthy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.release import shipped_alembic_head_strict


class AlembicHeadShippedProbe:
    """Publish the shipped migration head without turning a partial read into a value.

    A bad revision file must propagate as an error: an older head would falsely
    claim the release can apply only the migrations it happened to parse.
    """

    name: str = "alembic_head_shipped"
    definition_version: int = 1
    target: FactTarget = FactTarget.LIVE_RELEASE
    ttl: timedelta = timedelta(days=3650)
    timeout: timedelta = timedelta(seconds=1)
    briefing: bool = True
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {"revision": "string"}

    def __init__(self, head: Callable[[], str] = shipped_alembic_head_strict) -> None:
        """Keep a narrow test seam around the strict reader, not around probe behavior."""
        self._head = head

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Return the one strict shipped head; a malformed release propagates upward."""
        return {"revision": self._head()}
