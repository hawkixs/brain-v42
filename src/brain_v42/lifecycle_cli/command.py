"""Translate operator flags into pipeline calls, refusing local gates before writes."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, Protocol, TextIO
from uuid import uuid4

import structlog

from brain_v42.config import Settings
from brain_v42.repositories.pg_knowledge_lifecycle import RunNotFound
from brain_v42.services.knowledge_lifecycle_evidence import (
    BackupProof,
    Mode,
    PipelineRefusal,
    RunReport,
)

from .adapters import SystemClock, switch_refusal

log = structlog.get_logger(__name__)


class CommandPipeline(Protocol):
    async def start(
        self,
        mode: str,
        scope: Mapping[str, Any],
        ceiling_usd: float,
        backup_proof: BackupProof | None = None,
        *,
        run_id: str | None = None,
    ) -> RunReport: ...

    async def resume(self, run_id: str, backup_proof: BackupProof | None = None) -> RunReport: ...

    async def invalidate(
        self, run_id: str, reason: str, backup_proof: BackupProof | None = None
    ) -> RunReport: ...

    async def report(self, run_id: str) -> RunReport: ...


class ArgumentRefusal(ValueError):
    """Make argument errors render the same safe report as other refusals."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ArgumentRefusal("invalid_arguments")


def _ceiling(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Ceiling must be a finite non-negative USD amount"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("Ceiling must be a finite non-negative USD amount")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = Parser(
        prog="python -m brain_v42.lifecycle_cli",
        description="Evaluate knowledge lifecycle evidence without displaying entry text.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "resume", "invalidate", "report"):
        sub = commands.add_parser(
            command,
            help={
                "run": "Create and evaluate a new run",
                "resume": "Resume durable run checkpoints",
                "invalidate": "Invalidate a run and compensate applied states",
                "report": "Read a durable run report",
            }[command],
        )
        sub.add_argument("--json", action="store_true", help="Render the report as JSON")
        if command == "run":
            sub.add_argument("--mode", required=True, choices=("dry-run", "apply"))
            sub.add_argument("--scope", required=True, help="all or a project key")
            sub.add_argument("--ceiling-usd", required=True, type=_ceiling)
        else:
            sub.add_argument("--run", required=True, help="Lifecycle run identifier")
        if command == "invalidate":
            sub.add_argument("--reason", required=True, help="Reason for invalidating this run")
        if command != "report":
            sub.add_argument(
                "--backup-run-id",
                help="Declared sealed backup identifier (required for apply and invalidate)",
            )
            sub.add_argument(
                "--backup-sealed-at", help="Declared ISO-8601 UTC seal time, at most one hour old"
            )
    args = parser.parse_args(argv)
    if args.command == "run":
        args.mode = Mode.DRY_RUN if args.mode == "dry-run" else Mode.APPLY
        if not args.scope.strip():
            raise ArgumentRefusal("invalid_arguments")
        args.scope = {} if args.scope == "all" else {"project_key": args.scope}
    elif not args.run.strip() or (args.command == "invalidate" and not args.reason.strip()):
        raise ArgumentRefusal("invalid_arguments")
    return args


def _backup(args: argparse.Namespace, now: datetime, required: bool) -> BackupProof | None:
    run_id, sealed = args.backup_run_id, args.backup_sealed_at
    if not run_id and not sealed and not required:
        return None
    if not run_id or not run_id.strip() or not sealed:
        raise PipelineRefusal("backup_proof_missing")
    try:
        at = datetime.fromisoformat(sealed.replace("Z", "+00:00"))
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError as exc:
        raise PipelineRefusal("backup_requires_utc") from exc
    if not timedelta(0) <= now.astimezone(UTC) - at <= timedelta(hours=1):
        raise PipelineRefusal("backup_proof_expired")
    return BackupProof(run_id, at)


def render_report(report: RunReport, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        stdout.write(report.to_json() + "\n")
        return
    stdout.write(
        f"Run {report.run_id or '(not created)'}: {report.status}; mode={report.mode or 'unknown'}\n"
    )
    for name in (
        "applied_by_state",
        "pending_by_state",
        "compensated_by_state",
        "deferred_by_cause",
    ):
        stdout.write(f"{name}: {getattr(report, name)}\n")
    stdout.write(f"Cost: ${report.real_cost_usd:.6f}; worst case: ${report.worst_case_usd:.6f}\n")
    if report.backup_proof:
        stdout.write(report.backup_proof + "\n")
    if report.failure_reason:
        stdout.write(f"Reason: {report.failure_reason}\n")


async def execute(
    argv: list[str],
    settings: Settings,
    pipeline: CommandPipeline,
    read_mode: Callable[[str], Awaitable[str]] | None = None,
    *,
    now: Callable[[], datetime] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """A refused invocation must not enter orchestration, even for terminal apply runs."""
    output = stdout if stdout is not None else sys.stdout
    as_json = "--json" in argv
    run_id, mode = "", ""
    try:
        args = parse_args(argv)
        run_id = getattr(args, "run", "")
        if args.command == "report":
            report = await pipeline.report(run_id)
        else:
            refusal = switch_refusal(settings, Mode.DRY_RUN)
            if refusal:
                raise PipelineRefusal(refusal)
            if args.command == "run":
                mode = args.mode
            elif args.command == "invalidate":
                mode = Mode.APPLY
            else:
                if read_mode is None:
                    raise PipelineRefusal("run_mode_reader_missing")
                mode = await read_mode(run_id)
            refusal = switch_refusal(settings, mode)
            if refusal:
                raise PipelineRefusal(refusal)
            proof = _backup(args, (now or SystemClock().now)(), required=mode == Mode.APPLY)
            if args.command == "run":
                run_id = "lifecycle-" + uuid4().hex
                report = await pipeline.start(
                    mode, args.scope, args.ceiling_usd, proof, run_id=run_id
                )
            elif args.command == "resume":
                report = await pipeline.resume(run_id, proof)
            else:
                report = await pipeline.invalidate(run_id, args.reason, proof)
        code = 3 if report.status == "failed" or report.failure_reason else 0
    except (ArgumentRefusal, PipelineRefusal, RunNotFound) as exc:
        reason = (
            "invalid_arguments"
            if isinstance(exc, ArgumentRefusal)
            else "run_not_found"
            if isinstance(exc, RunNotFound)
            else str(exc)
        )
        report = RunReport(run_id, mode, "refused", failure_reason=reason)
        code = 4 if reason == "run_locked" else 2
    except Exception as exc:
        # Exception messages may contain URLs, entry text or provider credentials.
        log.error("lifecycle.command_failed", error_type=type(exc).__name__)
        report = RunReport(run_id, mode, "failed", failure_reason="command_failed")
        if run_id:
            try:
                report = replace(await pipeline.report(run_id), failure_reason="command_failed")
            except Exception:
                pass
        code = 3
    render_report(report, as_json, output)
    return code
