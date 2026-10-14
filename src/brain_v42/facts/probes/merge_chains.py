"""Measure merge chains so a recurrence is visible without following dead survivors."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

import sqlalchemy as sa

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import PostgresSourceSession

_TABLES = ("learnings", "decisions", "snippets", "runbooks", "adrs", "features")
# Only this closed table list is interpolated. One statement keeps all six
# counts on the same snapshot without locking rows or changing merge pointers.
_MERGE_CHAINS_SQL = sa.text(
    " UNION ALL ".join(
        f"SELECT '{table}' AS table_name, count(*) AS chain_count "  # noqa: S608  # nosec B608 - fragment = `table`, iterated from the module tuple `_TABLES` (six literal table names); no caller input reaches it
        f"FROM {table} AS child JOIN {table} AS target ON child.merged_into = target.id "
        "WHERE target.merged_into IS NOT NULL"
        for table in _TABLES
    )
)


class MergeChainsProbe:
    """Expose each table's broken survivor pointers under the verified production identity."""

    name: str = "merge_chains"
    definition_version: int = 1
    target: FactTarget = FactTarget.PRODUCTION
    ttl: timedelta = timedelta(seconds=15)
    timeout: timedelta = timedelta(seconds=3)
    briefing: bool = True
    policies: Mapping[str, int] = {"expected": 0}
    value_schema: Mapping[str, ValueType] = dict.fromkeys(_TABLES, "int")

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Let failed reads propagate so the registry never reports synthetic zeroes."""
        result = await cast(PostgresSourceSession, source).session.execute(_MERGE_CHAINS_SQL)
        return {row["table_name"]: row["chain_count"] for row in result.mappings().all()}
