#!/usr/bin/env python3
"""Run one explicit, fail-closed phase of the bounded plan-index repair."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from brain_v42.maintenance.plan_index_repair import (
    MutationProof,
    PhaseResult,
    ReindexEvidence,
    RepairSafetyError,
    RepairSnapshot,
    VerificationReport,
    discover_local_files,
    load_manifest,
    load_reindex_evidence,
    load_snapshot,
    load_verification_report,
    validate_mutation_proof,
    write_private_json,
)
from brain_v42.maintenance.plan_index_repair_store import RepairStore

_COMMANDS = (
    "inventory",
    "apply-paths",
    "verify",
    "finalize",
    "rollback-before-finalize",
)
_PROOF_ARGUMENTS = (
    "snapshot",
    "snapshot_sha256",
    "backup_receipt",
    "backup_receipt_sha256",
    "postgres_restore_tested",
    "writers_off_confirmed",
)
_EXPECTED_PHASE_STATUSES: Mapping[str, frozenset[str]] = {
    "apply-paths": frozenset({"applied", "already_applied"}),
    "finalize": frozenset({"finalized"}),
    "rollback-before-finalize": frozenset(
        {"rolled_back", "already_rolled_back", "backup_restore_required"}
    ),
}


@dataclass(frozen=True, slots=True)
class CommandSummary:
    """Content-safe result for one operator phase."""

    run_id: str
    command: str
    phase_fields: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        """Return the fixed, content-safe JSON schema."""
        if (
            "status" not in self.phase_fields
            or {
                "command": self.command,
                "run_id": self.run_id,
            }.keys()
            & self.phase_fields.keys()
        ):
            raise TypeError("invalid command summary")
        return {"command": self.command, "run_id": self.run_id, **self.phase_fields}


def _canonical_uuid(raw: str) -> str:
    try:
        parsed = UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a canonical UUID") from exc
    if raw != str(parsed):
        raise argparse.ArgumentTypeError("must be a canonical UUID")
    return raw


def _sha256(raw: str) -> str:
    if len(raw) != 64 or any(character not in "0123456789abcdef" for character in raw):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 digest")
    return raw


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse exactly one repair command and its closed evidence set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=_COMMANDS, default="inventory")
    parser.add_argument("--run-id", required=True, type=_canonical_uuid)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256", type=_sha256)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--backup-receipt-sha256", type=_sha256)
    parser.add_argument("--postgres-restore-tested", action="store_true")
    parser.add_argument("--writers-off-confirmed", action="store_true")
    parser.add_argument("--reindex-evidence", type=Path)
    parser.add_argument("--reindex-evidence-sha256", type=_sha256)
    parser.add_argument("--verification-output", type=Path)
    parser.add_argument("--verification-report", type=Path)
    parser.add_argument("--verification-report-sha256", type=_sha256)
    args = parser.parse_args(argv)

    required_by_command = {
        "inventory": ("manifest", "snapshot_output"),
        "apply-paths": _PROOF_ARGUMENTS,
        "verify": (
            "snapshot",
            "snapshot_sha256",
            "reindex_evidence",
            "reindex_evidence_sha256",
            "verification_output",
        ),
        "finalize": (
            *_PROOF_ARGUMENTS,
            "verification_report",
            "verification_report_sha256",
        ),
        "rollback-before-finalize": _PROOF_ARGUMENTS,
    }
    phase_arguments = {
        "manifest",
        "snapshot_output",
        *_PROOF_ARGUMENTS,
        "reindex_evidence",
        "reindex_evidence_sha256",
        "verification_output",
        "verification_report",
        "verification_report_sha256",
    }
    required = set(required_by_command[args.command])
    missing = sorted(name for name in required if not getattr(args, name))
    if missing:
        parser.error(
            f"{args.command} requires: "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )
    forbidden = sorted(name for name in phase_arguments - required if getattr(args, name))
    if forbidden:
        parser.error(
            f"{args.command} does not accept: "
            + ", ".join(f"--{name.replace('_', '-')}" for name in forbidden)
        )
    return args


def _runtime_dependencies() -> tuple[
    Callable[[], Any],
    Callable[[], Any],
    Callable[[], Awaitable[None]],
]:
    from brain_v42.config import get_settings  # noqa: PLC0415
    from brain_v42.db.engine import (  # noqa: PLC0415
        dispose_engine,
        get_session_factory,
    )

    return get_settings, get_session_factory, dispose_engine


def _proof_from_args(args: argparse.Namespace) -> tuple[RepairSnapshot, MutationProof]:
    result = validate_mutation_proof(
        snapshot_path=args.snapshot,
        snapshot_sha256=args.snapshot_sha256,
        backup_receipt_path=args.backup_receipt,
        backup_receipt_sha256=args.backup_receipt_sha256,
        postgres_restore_tested=args.postgres_restore_tested,
        writers_off_confirmed=args.writers_off_confirmed,
    )
    if not isinstance(result, tuple) or len(result) != 2:
        raise TypeError("unexpected mutation proof")
    snapshot, proof = result
    if not isinstance(snapshot, RepairSnapshot) or not isinstance(proof, MutationProof):
        raise TypeError("unexpected mutation proof")
    return snapshot, proof


def _phase_summary(run_id: str, command: str, result: object) -> CommandSummary:
    if not isinstance(result, PhaseResult):
        raise TypeError("unexpected phase result")
    if result.status not in _EXPECTED_PHASE_STATUSES[command]:
        raise ValueError("unexpected phase status")
    return CommandSummary(
        run_id=run_id,
        command=command,
        phase_fields=result.to_dict(),
    )


async def _dispatch(args: argparse.Namespace, store: RepairStore) -> CommandSummary:
    if args.command == "inventory":
        manifest = load_manifest(args.manifest)
        local_files = discover_local_files(manifest)
        snapshot = await store.inventory(manifest, local_files)
        if not isinstance(snapshot, RepairSnapshot):
            raise TypeError("unexpected inventory result")
        digest = write_private_json(args.snapshot_output, snapshot.to_dict())
        return CommandSummary(
            run_id=args.run_id,
            command=args.command,
            phase_fields={
                "status": "snapshotted",
                "project_count": len(manifest.projects),
                "local_file_count": len(local_files),
                "polluted_plan_count": len(snapshot.polluted_plan_ids),
                "artifact_sha256": digest,
            },
        )

    if args.command == "apply-paths":
        snapshot, proof = _proof_from_args(args)
        result = await store.apply_paths(snapshot, proof)
        return _phase_summary(args.run_id, args.command, result)

    if args.command == "verify":
        snapshot = load_snapshot(args.snapshot, args.snapshot_sha256)
        evidence = load_reindex_evidence(
            args.reindex_evidence,
            args.reindex_evidence_sha256,
        )
        if not isinstance(evidence, ReindexEvidence):
            raise TypeError("unexpected reindex evidence")
        report = await store.verify(snapshot, evidence)
        if not isinstance(report, VerificationReport):
            raise TypeError("unexpected verification result")
        digest = write_private_json(args.verification_output, report.to_dict())
        return CommandSummary(
            run_id=args.run_id,
            command=args.command,
            phase_fields={
                "status": "verified",
                "canonical_plan_count": len(report.canonical_plans),
                "polluted_plan_count": len(snapshot.polluted_plan_ids),
                "artifact_sha256": digest,
            },
        )

    if args.command == "finalize":
        snapshot, proof = _proof_from_args(args)
        report = load_verification_report(
            args.verification_report,
            args.verification_report_sha256,
        )
        result = await store.finalize(snapshot, proof, report)
        return _phase_summary(args.run_id, args.command, result)

    if args.command == "rollback-before-finalize":
        snapshot, proof = _proof_from_args(args)
        result = await store.rollback_before_finalize(snapshot, proof)
        return _phase_summary(args.run_id, args.command, result)

    raise RepairSafetyError("unsupported_repair_command")


def _safe_run_id(value: object) -> str:
    if not isinstance(value, str):
        return "invalid"
    try:
        return value if value == str(UUID(value)) else "invalid"
    except (AttributeError, TypeError, ValueError):
        return "invalid"


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@contextlib.contextmanager
def _confine_runtime_output() -> Iterator[None]:
    """Suppress runtime logs until the CLI emits its one JSON result."""
    previous_disable = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            yield
    finally:
        logging.disable(previous_disable)


async def run_from_args(args: argparse.Namespace) -> int:
    """Run one phase and emit only bounded, content-safe JSON."""
    run_id = _safe_run_id(getattr(args, "run_id", None))
    dispose: Callable[[], Awaitable[None]] | None = None
    write_to_stderr = False
    exit_code = 0
    payload: Mapping[str, object]
    with _confine_runtime_output():
        try:
            get_settings, get_session_factory, dispose = _runtime_dependencies()
            get_settings()
            session_factory = get_session_factory()
            summary = await _dispatch(args, RepairStore(session_factory))
            payload = summary.to_dict()
        except RepairSafetyError as exc:
            write_to_stderr = True
            exit_code = 2
            payload = {"status": "blocked", "reason_code": exc.reason_code}
        except Exception as exc:  # noqa: BLE001
            write_to_stderr = True
            exit_code = 1
            payload = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "run_id": run_id,
            }
        finally:
            if dispose is not None:
                try:
                    await dispose()
                except Exception:  # noqa: BLE001
                    pass

    print(_json(payload), file=sys.stderr if write_to_stderr else sys.stdout)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse and run one repair command."""
    return asyncio.run(run_from_args(parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
