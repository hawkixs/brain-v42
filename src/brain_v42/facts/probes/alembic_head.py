"""Read the Alembic stamp as a fact bound to a verified PostgreSQL snapshot.

The service reader is deliberately shared: the probe must use the registry's
source session so its value and PostgreSQL identity come from one snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession
from brain_v42.services.schema_state_service import read_current_revision


class AlembicHeadProbe:
    """Publish the stamped revision, refusing to present an unstamped database as one."""

    name: str = "alembic_head"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=60)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = True
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {"revision": "string"}

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Read within the source transaction so revision and identity share one snapshot."""
        revision = await read_current_revision(cast(PostgresSourceSession, source).session)
        if revision is None:
            raise ValueError("alembic_version has no row")
        return {"revision": revision}
