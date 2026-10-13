"""Operator-armed bounded retention for traces and delivered graph events."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import timedelta

from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.repositories.pg_trace_retention import PgTraceRetention, TraceRetentionReport


def _window(floor: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        days = int(value)
        if days < floor:
            raise argparse.ArgumentTypeError(f"retention window must be at least {floor} days")
        return days

    parse.__name__ = f"at_least_{floor}_days"
    return parse


def _batch(value: str) -> int:
    size = int(value)
    if not 1 <= size <= 50_000:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 50000")
    return size


def _positive(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("batch count must be positive")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retain old agent traces and delivered graph outbox rows."
    )
    parser.add_argument("--execute", action="store_true", help="delete candidates (default: dry)")
    parser.add_argument("--trace-older-than-days", type=_window(30), default=30)
    parser.add_argument("--outbox-older-than-days", type=_window(7), default=30)
    parser.add_argument("--batch-size", type=_batch, default=5000)
    parser.add_argument("--max-batches", type=_positive, default=100)
    return parser


def _render(label: str, report: TraceRetentionReport) -> str:
    mode = "DRY" if report.dry_run else "EXECUTE"
    return (
        f"{label} [{mode}] cutoff={report.cutoff.isoformat()} "
        f"candidates={report.candidates} deleted={report.deleted}"
    )


async def _run(args: argparse.Namespace) -> tuple[TraceRetentionReport, TraceRetentionReport]:
    try:
        retention = PgTraceRetention(get_session_factory())
        options = {
            "batch_size": args.batch_size,
            "max_batches": args.max_batches,
            "dry_run": not args.execute,
        }
        traces = await retention.purge_agent_traces(
            older_than=timedelta(days=args.trace_older_than_days), **options
        )
        print(_render("trace retention", traces))
        outbox = await retention.purge_delivered_outbox(
            older_than=timedelta(days=args.outbox_older_than_days), **options
        )
        return traces, outbox
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code or 0)
    try:
        traces, outbox = asyncio.run(_run(args))
    except Exception as error:
        print(f"trace retention failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(_render("graph outbox retention", outbox))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
