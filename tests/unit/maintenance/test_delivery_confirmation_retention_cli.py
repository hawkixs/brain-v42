"""Retention is explicitly armed, bounded and reports its destructive effect."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from brain_v42.maintenance import delivery_confirmation_retention as cli
from brain_v42.repositories.pg_delivery_retention import ConfirmationRetentionReport


@pytest.mark.parametrize("execute", [False, True])
def test_default_is_dry_and_execute_is_explicit(monkeypatch, capsys, execute: bool) -> None:
    report = ConfirmationRetentionReport(
        cutoff=datetime(2026, 9, 1, tzinfo=UTC), candidates=9, deleted=0, dry_run=not execute
    )
    purge = AsyncMock(return_value=report)
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(cli.PgDeliveryConfirmationRetention, "purge", purge)
    monkeypatch.setattr(cli, "dispose_engine", AsyncMock())
    assert cli.main(["--execute"] if execute else []) == 0
    assert purge.await_args.kwargs["dry_run"] is (not execute)
    assert purge.await_args.kwargs["older_than"].days == 14
    assert purge.await_args.kwargs["batch_size"] == 5000
    assert purge.await_args.kwargs["max_batches"] == 100
    output = capsys.readouterr().out
    assert "cutoff=2026-09-01" in output and "candidates=9" in output and "deleted=0" in output


def test_runtime_failure_returns_one_without_echoing_exception(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(
        cli.PgDeliveryConfirmationRetention, "purge", AsyncMock(side_effect=RuntimeError("private"))
    )
    monkeypatch.setattr(cli, "dispose_engine", AsyncMock())
    assert cli.main([]) == 1
    assert "private" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["--older-than-days", "6"],
        ["--batch-size", "0"],
        ["--batch-size", "50001"],
        ["--max-batches", "0"],
        ["--older-than-days", "invalid"],
    ],
)
def test_invalid_arguments_return_two_before_opening_database(monkeypatch, arguments) -> None:
    factory = AsyncMock()
    monkeypatch.setattr(cli, "get_session_factory", factory)
    assert cli.main(arguments) == 2
    factory.assert_not_called()
