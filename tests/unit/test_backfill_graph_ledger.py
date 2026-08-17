"""CLI contracts for the legacy graph ledger backfill."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts import backfill_graph_ledger

from brain_v42.services.legacy_graph_import import LegacyGraphImportReport


def test_parse_args_defaults_to_dry_run_with_bounded_caps() -> None:
    args = backfill_graph_ledger.parse_args([])

    assert args.apply is False
    assert args.max_nodes == 20_000
    assert args.max_relations == 100_000
    assert args.batch_size == 500
    assert args.timeout == 30.0
    assert args.allow_skips is False
    assert args.writers_off_confirmed is False


def test_parse_args_requires_explicit_apply_switch_for_writes() -> None:
    args = backfill_graph_ledger.parse_args(
        [
            "--apply",
            "--writers-off-confirmed",
            "--allow-skips",
            "--max-nodes",
            "25",
            "--max-relations",
            "50",
            "--batch-size",
            "10",
            "--timeout",
            "12.5",
        ]
    )

    assert args.apply is True
    assert args.max_nodes == 25
    assert args.max_relations == 50
    assert args.batch_size == 10
    assert args.timeout == 12.5
    assert args.allow_skips is True
    assert args.writers_off_confirmed is True


def test_parse_args_rejects_an_unbounded_postgres_batch() -> None:
    with pytest.raises(SystemExit):
        backfill_graph_ledger.parse_args(["--batch-size", "1001"])


def test_parse_args_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(SystemExit):
        backfill_graph_ledger.parse_args(["--timeout", "0"])


def test_report_displays_only_canonical_project_identities(capsys) -> None:
    report = LegacyGraphImportReport(
        applied=False,
        candidate_entities=2,
        candidate_relations=1,
        canonical_project_keys=("brain-v42",),
    )

    backfill_graph_ledger._print_report(report)

    output = capsys.readouterr().out
    assert "projects=brain-v42" in output
    assert "brain_v42" not in output


@pytest.mark.asyncio
async def test_run_dry_run_never_constructs_a_postgres_engine(monkeypatch, capsys) -> None:
    settings = MagicMock(
        neo4j_url="bolt://neo4j:7687",
        neo4j_user="neo4j",
        neo4j_password="secret",
        neo4j_timeout=5.0,
        postgres_url="postgresql+asyncpg://secret",
    )
    driver = AsyncMock()
    snapshot = MagicMock(status="ok")
    report = MagicMock(
        applied=False,
        candidate_entities=4,
        candidate_relations=7,
        imported_entities=0,
        imported_relations=0,
        acknowledged_entities=0,
        acknowledged_relations=0,
        skipped_nodes=0,
        skipped_relations=0,
        truncated_nodes=False,
        truncated_relations=False,
        canonical_project_keys=("brain-v42",),
    )
    reader = MagicMock()
    reader.read = AsyncMock(return_value=snapshot)
    importer = MagicMock()
    importer.import_snapshot = AsyncMock(return_value=report)
    engine_factory = MagicMock()

    monkeypatch.setattr(backfill_graph_ledger, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(
        backfill_graph_ledger, "create_neo4j_driver", MagicMock(return_value=driver)
    )
    monkeypatch.setattr(
        backfill_graph_ledger,
        "LegacyGraphSnapshotReader",
        MagicMock(return_value=reader),
    )
    monkeypatch.setattr(
        backfill_graph_ledger,
        "LegacyGraphImporter",
        MagicMock(return_value=importer),
    )
    monkeypatch.setattr(backfill_graph_ledger, "create_async_engine", engine_factory)

    exit_code = await backfill_graph_ledger.run(backfill_graph_ledger.parse_args([]))

    assert exit_code == 0
    engine_factory.assert_not_called()
    importer.import_snapshot.assert_awaited_once_with(
        snapshot,
        apply=False,
        allow_skips=False,
    )
    driver.close.assert_awaited_once()
    output = capsys.readouterr().out
    assert "DRY-RUN" in output
    assert "secret" not in output


@pytest.mark.asyncio
async def test_run_apply_disposes_both_databases(monkeypatch) -> None:
    settings = MagicMock(
        neo4j_url="bolt://neo4j:7687",
        neo4j_user="neo4j",
        neo4j_password="secret",
        neo4j_timeout=5.0,
        postgres_url="postgresql+asyncpg://secret",
    )
    driver = AsyncMock()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    snapshot = MagicMock(status="ok")
    report = MagicMock(
        applied=True,
        candidate_entities=4,
        candidate_relations=7,
        imported_entities=2,
        imported_relations=3,
        acknowledged_entities=4,
        acknowledged_relations=7,
        skipped_nodes=0,
        skipped_relations=0,
        truncated_nodes=False,
        truncated_relations=False,
        canonical_project_keys=("brain-v42",),
    )
    reader = MagicMock()
    reader.read = AsyncMock(return_value=snapshot)
    importer = MagicMock()
    importer.import_snapshot = AsyncMock(return_value=report)
    importer_type = MagicMock(return_value=importer)

    monkeypatch.setattr(backfill_graph_ledger, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(
        backfill_graph_ledger, "create_neo4j_driver", MagicMock(return_value=driver)
    )
    monkeypatch.setattr(
        backfill_graph_ledger,
        "LegacyGraphSnapshotReader",
        MagicMock(return_value=reader),
    )
    monkeypatch.setattr(backfill_graph_ledger, "LegacyGraphImporter", importer_type)
    monkeypatch.setattr(
        backfill_graph_ledger, "create_async_engine", MagicMock(return_value=engine)
    )
    monkeypatch.setattr(backfill_graph_ledger, "async_sessionmaker", MagicMock())

    exit_code = await backfill_graph_ledger.run(
        backfill_graph_ledger.parse_args(["--apply", "--writers-off-confirmed"])
    )

    assert exit_code == 0
    importer_type.assert_called_once()
    importer.import_snapshot.assert_awaited_once_with(
        snapshot,
        apply=True,
        allow_skips=False,
    )
    driver.close.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_refuses_apply_without_writers_off_confirmation(monkeypatch, capsys) -> None:
    driver_factory = MagicMock()
    monkeypatch.setattr(backfill_graph_ledger, "create_neo4j_driver", driver_factory)

    exit_code = await backfill_graph_ledger.run(backfill_graph_ledger.parse_args(["--apply"]))

    assert exit_code == 2
    driver_factory.assert_not_called()
    assert "writers" in capsys.readouterr().err.lower()


@pytest.mark.asyncio
async def test_run_redacts_unexpected_runtime_error_details(monkeypatch, capsys) -> None:
    settings = MagicMock(
        neo4j_url="bolt://neo4j:7687",
        neo4j_user="neo4j",
        neo4j_password="password-secret",
        neo4j_timeout=5.0,
    )
    driver = AsyncMock()
    reader = MagicMock()
    reader.read = AsyncMock(side_effect=RuntimeError("password-secret"))
    monkeypatch.setattr(backfill_graph_ledger, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(
        backfill_graph_ledger, "create_neo4j_driver", MagicMock(return_value=driver)
    )
    monkeypatch.setattr(
        backfill_graph_ledger,
        "LegacyGraphSnapshotReader",
        MagicMock(return_value=reader),
    )

    exit_code = await backfill_graph_ledger.run(backfill_graph_ledger.parse_args([]))

    assert exit_code == 2
    driver.close.assert_awaited_once()
    error = capsys.readouterr().err
    assert "RuntimeError" in error
    assert "password-secret" not in error


@pytest.mark.asyncio
async def test_run_redacts_driver_construction_errors(monkeypatch, capsys) -> None:
    settings = MagicMock(
        neo4j_url="bolt://neo4j:7687",
        neo4j_user="neo4j",
        neo4j_password="driver-secret",
    )
    monkeypatch.setattr(backfill_graph_ledger, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(
        backfill_graph_ledger,
        "create_neo4j_driver",
        MagicMock(side_effect=RuntimeError("driver-secret")),
    )

    exit_code = await backfill_graph_ledger.run(backfill_graph_ledger.parse_args([]))

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "RuntimeError" in error
    assert "driver-secret" not in error
