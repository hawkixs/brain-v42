"""Operator-armed confirmation retention; dry by default and independent of Dream."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.repositories.pg_delivery_retention import (
    ConfirmationRetentionReport,
    PgDeliveryConfirmationRetention,
)


def _window(value: str) -> int:
    days = int(value)
    if days < 7:
        raise argparse.ArgumentTypeError("retention window must be at least seven days")
    return days


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
        description="Retain referenced delivery proof and purge old successes."
    )
    parser.add_argument("--execute", action="store_true", help="delete candidates (default: dry)")
    parser.add_argument("--older-than-days", type=_window, default=14)
    parser.add_argument("--batch-size", type=_batch, default=5000)
    parser.add_argument("--max-batches", type=_positive, default=100)
    return parser


def render_report(report: ConfirmationRetentionReport) -> str:
    mode = "DRY" if report.dry_run else "EXECUTE"
    return (
        f"delivery confirmation retention [{mode}] cutoff={report.cutoff.isoformat()} "
        f"candidates={report.candidates} deleted={report.deleted}"
    )


async def _run(args: argparse.Namespace) -> ConfirmationRetentionReport:
    # This CLI uses the shared session factory; timeouts come from its engine configuration.
    try:
        return await PgDeliveryConfirmationRetention(get_session_factory()).purge(
            older_than=timedelta(days=args.older_than_days),
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            dry_run=not args.execute,
        )
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code or 0)
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        # Database exceptions can contain connection details or stored proof bodies.
        print(f"delivery confirmation retention failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
