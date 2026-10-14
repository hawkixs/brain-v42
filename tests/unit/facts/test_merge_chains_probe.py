"""Pin the merge-chain measurement and its PostgreSQL trust boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import check_value_schema

_TABLES = ("learnings", "decisions", "snippets", "runbooks", "adrs", "features")


async def test_probe_counts_each_table_with_one_read_only_self_join_query() -> None:
    """A single snapshot must count pointers to merged targets, including features."""
    from brain_v42.facts.probes.merge_chains import MergeChainsProbe

    counts = dict.fromkeys(_TABLES, 0) | {"learnings": 212, "features": 11}
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {"table_name": table, "chain_count": count} for table, count in counts.items()
    ]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    probe = MergeChainsProbe()

    value = await probe.measure(SimpleNamespace(session=session))  # type: ignore[arg-type]

    assert value == counts
    check_value_schema(value, probe.value_schema)
    session.execute.assert_awaited_once()
    sql = str(session.execute.call_args.args[0]).lower()
    branches = sql.split("union all")
    assert len(branches) == 6
    for table, branch in zip(_TABLES, branches, strict=True):
        assert branch.strip().startswith("select")
        assert "count(*)" in branch
        assert f"from {table} as child" in branch
        assert f"join {table} as target" in branch
        assert "child.merged_into = target.id" in branch
        assert "target.merged_into is not null" in branch
    assert "for update" not in sql
    assert "for share" not in sql
    assert probe.name == "merge_chains"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.PRODUCTION
    assert probe.ttl.total_seconds() == 15
    assert probe.timeout.total_seconds() == 3
    assert probe.briefing is True
    assert probe.policies == {"expected": 0}
    assert probe.value_schema == dict.fromkeys(_TABLES, "int")


async def test_probe_does_not_turn_a_failed_database_read_into_zeroes() -> None:
    """The registry must receive the failure to publish an unreadable observation."""
    from brain_v42.facts.probes.merge_chains import MergeChainsProbe

    session = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("database unavailable")))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await MergeChainsProbe().measure(SimpleNamespace(session=session))  # type: ignore[arg-type]
