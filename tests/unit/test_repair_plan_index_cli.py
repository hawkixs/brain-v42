"""Tests for the bounded plan-index repair operator CLI."""

from __future__ import annotations

import importlib
import json
import logging
import stat
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.maintenance.plan_index_repair import (
    TARGET_PROJECT_KEYS,
    PhaseResult,
    ProjectReindexStats,
    ReindexEvidence,
    RepairSnapshot,
    VerificationReport,
    write_private_json,
)

RUN_ID = "11111111-1111-4111-8111-111111111111"


def _cli() -> ModuleType:
    try:
        return importlib.import_module("scripts.repair_plan_index")
    except ModuleNotFoundError as exc:
        pytest.fail(f"repair CLI is missing: {type(exc).__name__}")


def test_parse_args_defaults_to_inventory_with_explicit_output() -> None:
    args = _cli().parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            "manifest.json",
            "--snapshot-output",
            "snapshot.json",
        ]
    )

    assert args.command == "inventory"
    assert args.run_id == RUN_ID
    assert str(args.manifest) == "manifest.json"
    assert str(args.snapshot_output) == "snapshot.json"


def test_inventory_requires_snapshot_output_and_rejects_attestations() -> None:
    cli = _cli()

    with pytest.raises(SystemExit) as missing_output:
        cli.parse_args(["--run-id", RUN_ID, "--manifest", "manifest.json"])
    assert missing_output.value.code == 2

    with pytest.raises(SystemExit) as unsafe_attestation:
        cli.parse_args(
            [
                "inventory",
                "--run-id",
                RUN_ID,
                "--manifest",
                "manifest.json",
                "--snapshot-output",
                "snapshot.json",
                "--writers-off-confirmed",
            ]
        )
    assert unsafe_attestation.value.code == 2


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (
            "apply-paths",
            [
                "--snapshot",
                "snapshot.json",
                "--snapshot-sha256",
                "a" * 64,
                "--backup-receipt",
                "backup.json",
                "--backup-receipt-sha256",
                "b" * 64,
                "--postgres-restore-tested",
                "--writers-off-confirmed",
            ],
        ),
        (
            "verify",
            [
                "--snapshot",
                "snapshot.json",
                "--snapshot-sha256",
                "a" * 64,
                "--reindex-evidence",
                "evidence.json",
                "--reindex-evidence-sha256",
                "b" * 64,
                "--verification-output",
                "verification.json",
            ],
        ),
        (
            "finalize",
            [
                "--snapshot",
                "snapshot.json",
                "--snapshot-sha256",
                "a" * 64,
                "--backup-receipt",
                "backup.json",
                "--backup-receipt-sha256",
                "b" * 64,
                "--verification-report",
                "verification.json",
                "--verification-report-sha256",
                "c" * 64,
                "--postgres-restore-tested",
                "--writers-off-confirmed",
            ],
        ),
        (
            "rollback-before-finalize",
            [
                "--snapshot",
                "snapshot.json",
                "--snapshot-sha256",
                "a" * 64,
                "--backup-receipt",
                "backup.json",
                "--backup-receipt-sha256",
                "b" * 64,
                "--postgres-restore-tested",
                "--writers-off-confirmed",
            ],
        ),
    ],
)
def test_mutating_and_verification_phases_are_separate(
    command: str,
    arguments: list[str],
) -> None:
    args = _cli().parse_args([command, "--run-id", RUN_ID, *arguments])

    assert args.command == command
    assert not hasattr(args, "wet")
    assert not hasattr(args, "force")
    assert not hasattr(args, "skip_backup")


@pytest.mark.parametrize("flag", ["--wet", "--force", "--skip-backup", "--apply-all"])
def test_parser_rejects_unsafe_or_combined_flags(flag: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _cli().parse_args(
            [
                "apply-paths",
                "--run-id",
                RUN_ID,
                "--snapshot",
                "snapshot.json",
                "--snapshot-sha256",
                "a" * 64,
                "--backup-receipt",
                "backup.json",
                "--backup-receipt-sha256",
                "b" * 64,
                "--postgres-restore-tested",
                "--writers-off-confirmed",
                flag,
            ]
        )

    assert exc.value.code == 2


@pytest.mark.parametrize(
    "raw",
    [
        "11111111111141118111111111111111",
        "{11111111-1111-4111-8111-111111111111}",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper(),
        "not-a-uuid",
    ],
)
def test_run_id_must_be_a_canonical_full_uuid(raw: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _cli().parse_args(
            [
                "--run-id",
                raw,
                "--manifest",
                "manifest.json",
                "--snapshot-output",
                "snapshot.json",
            ]
        )

    assert exc.value.code == 2


def _snapshot(tmp_path: Path) -> tuple[Path, str, RepairSnapshot]:
    snapshot = RepairSnapshot(
        version=1,
        mutation_timestamp="2026-07-31T00:00:00+00:00",
        database_identity_hash="d" * 64,
        alembic_revision="037",
        contexts=(),
        local_files=(),
        indexed_plans=(),
        feature_links=(),
        polluted_plan_ids=(),
        missing_canonical_files=(),
        collisions=(),
    )
    path = tmp_path / "snapshot.json"
    return path, write_private_json(path, snapshot.to_dict()), snapshot


def _receipt(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "backup-receipt.json"
    digest = write_private_json(path, {"backup": "verified"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return path, digest


def _evidence(tmp_path: Path, snapshot_digest: str) -> tuple[Path, str, ReindexEvidence]:
    evidence = ReindexEvidence(
        version=1,
        snapshot_sha256=snapshot_digest,
        projects=tuple(
            ProjectReindexStats(
                project_key=project_key,
                indexed=0,
                skipped=0,
                linked=0,
                errors=0,
                chunks_created=0,
            )
            for project_key in sorted(TARGET_PROJECT_KEYS)
        ),
    )
    path = tmp_path / "reindex-evidence.json"
    return path, write_private_json(path, evidence.to_dict()), evidence


def _report(
    tmp_path: Path,
    snapshot_digest: str,
    evidence_digest: str,
    evidence: ReindexEvidence,
) -> tuple[Path, str, VerificationReport]:
    report = VerificationReport(
        version=1,
        snapshot_sha256=snapshot_digest,
        evidence_sha256=evidence_digest,
        evidence=evidence,
        canonical_plans=(),
    )
    path = tmp_path / "verification-report.json"
    return path, write_private_json(path, report.to_dict()), report


def _proof_argv(
    command: str,
    snapshot_path: Path,
    snapshot_digest: str,
    receipt_path: Path,
    receipt_digest: str,
) -> list[str]:
    return [
        command,
        "--run-id",
        RUN_ID,
        "--snapshot",
        str(snapshot_path),
        "--snapshot-sha256",
        snapshot_digest,
        "--backup-receipt",
        str(receipt_path),
        "--backup-receipt-sha256",
        receipt_digest,
        "--postgres-restore-tested",
        "--writers-off-confirmed",
    ]


def _store() -> MagicMock:
    store = MagicMock()
    store.inventory = AsyncMock()
    store.apply_paths = AsyncMock(return_value=PhaseResult("applied", 7))
    store.verify = AsyncMock()
    store.finalize = AsyncMock(return_value=PhaseResult("finalized", 3))
    store.rollback_before_finalize = AsyncMock(return_value=PhaseResult("rolled_back", 4))
    return store


def _assert_only_awaited(store: MagicMock, expected: str) -> None:
    for method in (
        "inventory",
        "apply_paths",
        "verify",
        "finalize",
        "rollback_before_finalize",
    ):
        assertion = (
            store.__getattr__(method).assert_awaited_once
            if method == expected
            else store.__getattr__(method).assert_not_awaited
        )
        assertion()


@pytest.mark.asyncio
async def test_default_inventory_writes_private_snapshot_and_compact_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    output = tmp_path / "inventory-output.json"
    args = cli.parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--snapshot-output",
            str(output),
        ]
    )
    _, _, snapshot = _snapshot(tmp_path)
    store = _store()
    store.inventory.return_value = snapshot
    dispose = AsyncMock()
    manifest = MagicMock(projects=(object(),))
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
        patch.object(cli, "load_manifest", return_value=manifest),
        patch.object(cli, "discover_local_files", return_value=()),
    ):
        assert await cli.run_from_args(args) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert captured.out == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert "contexts" not in captured.out
    assert payload["command"] == "inventory"
    assert set(payload) == {
        "artifact_sha256",
        "command",
        "local_file_count",
        "polluted_plan_count",
        "project_count",
        "run_id",
        "status",
    }
    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    _assert_only_awaited(store, "inventory")
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("apply-paths", "apply_paths"),
        ("rollback-before-finalize", "rollback_before_finalize"),
    ],
)
async def test_proof_commands_validate_real_private_receipt_before_store(
    tmp_path: Path,
    command: str,
    method: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, receipt_digest = _receipt(tmp_path)
    args = cli.parse_args(
        _proof_argv(
            command,
            snapshot_path,
            snapshot_digest,
            receipt_path,
            receipt_digest,
        )
    )
    store = _store()
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
    ):
        assert await cli.run_from_args(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"affected_rows", "command", "run_id", "status"}
    _assert_only_awaited(store, method)
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verify_loads_real_evidence_and_writes_private_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    evidence_path, evidence_digest, evidence = _evidence(tmp_path, snapshot_digest)
    output = tmp_path / "verification-output.json"
    args = cli.parse_args(
        [
            "verify",
            "--run-id",
            RUN_ID,
            "--snapshot",
            str(snapshot_path),
            "--snapshot-sha256",
            snapshot_digest,
            "--reindex-evidence",
            str(evidence_path),
            "--reindex-evidence-sha256",
            evidence_digest,
            "--verification-output",
            str(output),
        ]
    )
    store = _store()
    report = VerificationReport(
        version=1,
        snapshot_sha256=snapshot_digest,
        evidence_sha256=evidence_digest,
        evidence=evidence,
        canonical_plans=(),
    )
    store.verify.return_value = report
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
    ):
        assert await cli.run_from_args(args) == 0

    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "artifact_sha256",
        "canonical_plan_count",
        "command",
        "polluted_plan_count",
        "run_id",
        "status",
    }
    _assert_only_awaited(store, "verify")
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_finalize_loads_real_report_after_real_mutation_proof(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, receipt_digest = _receipt(tmp_path)
    evidence_path, evidence_digest, evidence = _evidence(tmp_path, snapshot_digest)
    report_path, report_digest, _ = _report(
        tmp_path,
        snapshot_digest,
        evidence_digest,
        evidence,
    )
    args = cli.parse_args(
        [
            *_proof_argv(
                "finalize",
                snapshot_path,
                snapshot_digest,
                receipt_path,
                receipt_digest,
            ),
            "--verification-report",
            str(report_path),
            "--verification-report-sha256",
            report_digest,
        ]
    )
    store = _store()
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
    ):
        assert await cli.run_from_args(args) == 0

    assert evidence_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"affected_rows", "command", "run_id", "status"}
    _assert_only_awaited(store, "finalize")
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("apply-paths", "apply_paths"),
        ("finalize", "finalize"),
        ("rollback-before-finalize", "rollback_before_finalize"),
    ],
)
async def test_bad_backup_receipt_stops_every_mutating_store_call(
    tmp_path: Path,
    command: str,
    method: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, receipt_digest = _receipt(tmp_path)
    argv = _proof_argv(
        command,
        snapshot_path,
        snapshot_digest,
        receipt_path,
        "f" * 64 if receipt_digest != "f" * 64 else "e" * 64,
    )
    if command == "finalize":
        evidence_path, evidence_digest, evidence = _evidence(tmp_path, snapshot_digest)
        report_path, report_digest, _ = _report(
            tmp_path,
            snapshot_digest,
            evidence_digest,
            evidence,
        )
        argv.extend(
            [
                "--verification-report",
                str(report_path),
                "--verification-report-sha256",
                report_digest,
            ]
        )
        assert evidence_path.exists()
    args = cli.parse_args(argv)
    store = _store()
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
    ):
        assert await cli.run_from_args(args) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "reason_code": "private_file_digest_mismatch",
        "status": "blocked",
    }
    assert "backup" not in captured.err
    _assert_only_awaited(store, "")
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unexpected_errors_mask_secret_and_dispose_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli()
    args = cli.parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--snapshot-output",
            str(tmp_path / "snapshot.json"),
        ]
    )
    store = _store()
    store.inventory.side_effect = RuntimeError("SECRET_SENTINEL")
    dispose = AsyncMock(side_effect=RuntimeError("DISPOSE_SENTINEL"))
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
        patch.object(cli, "load_manifest", return_value=MagicMock(projects=())),
        patch.object(cli, "discover_local_files", return_value=()),
    ):
        assert await cli.run_from_args(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SECRET_SENTINEL" not in captured.err
    assert "DISPOSE_SENTINEL" not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "error_type": "RuntimeError",
        "run_id": RUN_ID,
        "status": "failed",
    }
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_safety_result_is_preserved_when_dispose_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, _ = _receipt(tmp_path)
    args = cli.parse_args(
        _proof_argv(
            "apply-paths",
            snapshot_path,
            snapshot_digest,
            receipt_path,
            "a" * 64,
        )
    )
    dispose = AsyncMock(side_effect=RuntimeError("DISPOSE_SENTINEL"))
    with patch.object(
        cli,
        "_runtime_dependencies",
        return_value=(MagicMock(), MagicMock(), dispose),
    ):
        assert await cli.run_from_args(args) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "reason_code": "private_file_digest_mismatch",
        "status": "blocked",
    }
    assert "DISPOSE_SENTINEL" not in captured.err
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "error_type"),
    [
        (object(), "TypeError"),
        (PhaseResult("unexpected_status", 0), "ValueError"),
    ],
)
async def test_unexpected_phase_result_is_masked_to_type_and_run_id(
    tmp_path: Path,
    result: object,
    error_type: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, receipt_digest = _receipt(tmp_path)
    args = cli.parse_args(
        _proof_argv(
            "apply-paths",
            snapshot_path,
            snapshot_digest,
            receipt_path,
            receipt_digest,
        )
    )
    store = _store()
    store.apply_paths.return_value = result
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
    ):
        assert await cli.run_from_args(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_type": error_type,
        "run_id": RUN_ID,
        "status": "failed",
    }
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_invalid_mutation_proof_type_stops_store_before_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    snapshot_path, snapshot_digest, _ = _snapshot(tmp_path)
    receipt_path, receipt_digest = _receipt(tmp_path)
    args = cli.parse_args(
        _proof_argv(
            "apply-paths",
            snapshot_path,
            snapshot_digest,
            receipt_path,
            receipt_digest,
        )
    )
    store = _store()
    dispose = AsyncMock()
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(MagicMock(), MagicMock(), dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
        patch.object(cli, "validate_mutation_proof", return_value=(object(), object())),
    ):
        assert await cli.run_from_args(args) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "error_type": "TypeError",
        "run_id": RUN_ID,
        "status": "failed",
    }
    _assert_only_awaited(store, "")
    dispose.assert_awaited_once_with()


def test_runtime_dependencies_import_settings_factory_and_async_disposer() -> None:
    cli = _cli()
    from brain_v42 import config
    from brain_v42.db import engine

    settings = MagicMock()
    factory = MagicMock()
    dispose = AsyncMock()
    with (
        patch.object(config, "get_settings", return_value=settings) as get_settings,
        patch.object(engine, "get_session_factory", return_value=factory) as get_factory,
        patch.object(engine, "dispose_engine", dispose),
    ):
        settings_loader, factory_loader, actual_dispose = cli._runtime_dependencies()

    get_settings.assert_not_called()
    get_factory.assert_not_called()
    assert settings_loader() is settings
    assert factory_loader() is factory
    get_settings.assert_called_once_with()
    get_factory.assert_called_once_with()
    assert actual_dispose is dispose


@pytest.mark.asyncio
async def test_settings_initialization_failure_disposes_once_without_leaking_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    from brain_v42 import config
    from brain_v42.db import engine

    args = cli.parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--snapshot-output",
            str(tmp_path / "snapshot.json"),
        ]
    )
    dispose = AsyncMock(side_effect=RuntimeError("DISPOSE_SENTINEL"))
    with (
        patch.object(config, "get_settings", side_effect=RuntimeError("INIT_SENTINEL")),
        patch.object(engine, "dispose_engine", dispose),
    ):
        assert await cli.run_from_args(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "INIT_SENTINEL" not in captured.err
    assert "DISPOSE_SENTINEL" not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "error_type": "RuntimeError",
        "run_id": RUN_ID,
        "status": "failed",
    }
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_session_factory_failure_disposes_once_without_leaking_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli()
    from brain_v42 import config
    from brain_v42.db import engine

    args = cli.parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--snapshot-output",
            str(tmp_path / "snapshot.json"),
        ]
    )
    dispose = AsyncMock(side_effect=RuntimeError("DISPOSE_SENTINEL"))
    with (
        patch.object(config, "get_settings", return_value=MagicMock()) as get_settings,
        patch.object(
            engine,
            "get_session_factory",
            side_effect=RuntimeError("FACTORY_SENTINEL"),
        ),
        patch.object(engine, "dispose_engine", dispose),
    ):
        assert await cli.run_from_args(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FACTORY_SENTINEL" not in captured.err
    assert "DISPOSE_SENTINEL" not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "error_type": "RuntimeError",
        "run_id": RUN_ID,
        "status": "failed",
    }
    get_settings.assert_called_once_with()
    dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_logs_are_confined_before_init_and_during_disposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import structlog

    cli = _cli()
    sentinel = "postgresql+asyncpg://operator:secret@db.internal:5432/brain"
    args = cli.parse_args(
        [
            "--run-id",
            RUN_ID,
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--snapshot-output",
            str(tmp_path / "inventory-output.json"),
        ]
    )
    _, _, snapshot = _snapshot(tmp_path)
    store = _store()
    store.inventory.return_value = snapshot

    def emit_runtime_logs() -> None:
        logger = logging.getLogger("repair-plan-index-test")
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning(sentinel)
            structlog.get_logger("repair-plan-index-test").warning(sentinel)
        finally:
            logger.removeHandler(handler)
            handler.close()

    def noisy_settings() -> MagicMock:
        emit_runtime_logs()
        return MagicMock()

    def noisy_factory() -> MagicMock:
        emit_runtime_logs()
        return MagicMock()

    async def noisy_dispose() -> None:
        emit_runtime_logs()

    manifest = MagicMock(projects=(object(),))
    with (
        patch.object(
            cli,
            "_runtime_dependencies",
            return_value=(noisy_settings, noisy_factory, noisy_dispose),
        ),
        patch.object(cli, "RepairStore", return_value=store),
        patch.object(cli, "load_manifest", return_value=manifest),
        patch.object(cli, "discover_local_files", return_value=()),
    ):
        assert await cli.run_from_args(args) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert captured.err == ""
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert "Traceback" not in captured.out + captured.err
