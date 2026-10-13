from datetime import timedelta

import pytest

from brain_v42.repositories.pg_trace_retention import PgTraceRetention


@pytest.mark.parametrize("method,days", [("purge_agent_traces", 29), ("purge_delivered_outbox", 6)])
async def test_minimum_window_is_enforced(method: str, days: int) -> None:
    with pytest.raises(ValueError):
        await getattr(PgTraceRetention(None), method)(
            older_than=timedelta(days=days), batch_size=1, max_batches=1, dry_run=True
        )


@pytest.mark.parametrize(
    "field,value", [("batch_size", 0), ("batch_size", 50001), ("max_batches", 0), ("dry_run", 1)]
)
async def test_invalid_batch_arguments_are_refused(field: str, value: object) -> None:
    kwargs = {"older_than": timedelta(days=30), "batch_size": 1, "max_batches": 1, "dry_run": True}
    kwargs[field] = value
    with pytest.raises(ValueError):
        await PgTraceRetention(None).purge_agent_traces(**kwargs)
