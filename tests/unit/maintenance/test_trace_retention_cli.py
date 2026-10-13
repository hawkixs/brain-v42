from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from brain_v42.maintenance import trace_retention as cli
from brain_v42.repositories.pg_trace_retention import TraceRetentionReport


def test_defaults_and_argument_floors() -> None:
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert (args.trace_older_than_days, args.outbox_older_than_days) == (30, 30)
    assert (args.batch_size, args.max_batches, args.execute) == (5000, 100, False)
    with pytest.raises(SystemExit):
        parser.parse_args(["--trace-older-than-days", "29"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--outbox-older-than-days", "6"])


@pytest.mark.parametrize("execute", [False, True])
def test_reports_are_rendered(monkeypatch, capsys, execute) -> None:
    report = TraceRetentionReport(datetime(2026, 9, 1, tzinfo=UTC), 4, 2, not execute)
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    traces = AsyncMock(return_value=report)
    outbox = AsyncMock(return_value=report)
    dispose = AsyncMock()
    monkeypatch.setattr(cli.PgTraceRetention, "purge_agent_traces", traces)
    monkeypatch.setattr(cli.PgTraceRetention, "purge_delivered_outbox", outbox)
    monkeypatch.setattr(cli, "dispose_engine", dispose)
    assert cli.main(["--execute"] if execute else []) == 0
    for purge in (traces, outbox):
        assert purge.await_args.kwargs["dry_run"] is (not execute)
        assert purge.await_args.kwargs["older_than"].days == 30
        assert purge.await_args.kwargs["batch_size"] == 5000
        assert purge.await_args.kwargs["max_batches"] == 100
    dispose.assert_awaited()
    assert capsys.readouterr().out.splitlines() == [
        f"trace retention [{'EXECUTE' if execute else 'DRY'}] cutoff=2026-09-01T00:00:00+00:00 candidates=4 deleted=2",
        f"graph outbox retention [{'EXECUTE' if execute else 'DRY'}] cutoff=2026-09-01T00:00:00+00:00 candidates=4 deleted=2",
    ]


def test_outbox_failure_keeps_trace_report_visible(monkeypatch, capsys) -> None:
    report = TraceRetentionReport(datetime(2026, 9, 1, tzinfo=UTC), 4, 2, False)
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(cli.PgTraceRetention, "purge_agent_traces", AsyncMock(return_value=report))
    monkeypatch.setattr(
        cli.PgTraceRetention,
        "purge_delivered_outbox",
        AsyncMock(side_effect=RuntimeError("private")),
    )
    dispose = AsyncMock()
    monkeypatch.setattr(cli, "dispose_engine", dispose)
    assert cli.main(["--execute"]) == 1
    captured = capsys.readouterr()
    assert "trace retention [EXECUTE]" in captured.out
    assert "RuntimeError" in captured.err and "private" not in captured.err
    dispose.assert_awaited()


def test_a_failed_engine_disposal_keeps_both_reports_visible(monkeypatch, capsys) -> None:
    """red-rail review of #357 (F-357-1): the outbox report was printed by main(),
    after _run's finally. A dispose_engine() failure there lost it although the
    outbox deletions were already committed."""
    report = TraceRetentionReport(datetime(2026, 9, 1, tzinfo=UTC), 4, 2, False)
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(cli.PgTraceRetention, "purge_agent_traces", AsyncMock(return_value=report))
    monkeypatch.setattr(
        cli.PgTraceRetention, "purge_delivered_outbox", AsyncMock(return_value=report)
    )
    monkeypatch.setattr(cli, "dispose_engine", AsyncMock(side_effect=OSError("private")))
    assert cli.main(["--execute"]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "trace retention [EXECUTE] cutoff=2026-09-01T00:00:00+00:00 candidates=4 deleted=2",
        "graph outbox retention [EXECUTE] cutoff=2026-09-01T00:00:00+00:00 candidates=4 deleted=2",
    ]
    assert "OSError" in captured.err and "private" not in captured.err


@pytest.mark.parametrize(
    "arguments", [["--trace-older-than-days", "invalid"], ["--outbox-older-than-days", "6"]]
)
def test_invalid_arguments_return_two_without_opening_database(monkeypatch, arguments) -> None:
    factory = AsyncMock()
    monkeypatch.setattr(cli, "get_session_factory", factory)
    assert cli.main(arguments) == 2
    factory.assert_not_called()


def test_trace_failure_reports_type_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(
        cli.PgTraceRetention, "purge_agent_traces", AsyncMock(side_effect=RuntimeError("secret"))
    )
    dispose = AsyncMock()
    monkeypatch.setattr(cli, "dispose_engine", dispose)
    assert cli.main([]) == 1
    assert capsys.readouterr().err.strip().endswith("RuntimeError")
    dispose.assert_awaited()
