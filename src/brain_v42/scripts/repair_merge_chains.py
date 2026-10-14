"""Preview historical merge chains or atomically repair an explicitly approved count."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import asdict

import sqlalchemy as sa

from brain_v42.config import get_settings
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.maintenance.merge_chain_repair import (
    InventoryReport,
    MergeChainRepairError,
    inventory,
    repair,
)


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected count must be non-negative")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Require a measured count before opening any writable transaction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="repair (default: dry run)")
    parser.add_argument("--expect", type=_nonnegative, help="approved in-project chained count")
    args = parser.parse_args(argv)
    if args.execute and args.expect is None:
        parser.error("--execute requires --expect N")
    return args


def _inventory_payload(report: InventoryReport) -> dict[str, object]:
    tables = []
    for table in report.tables:
        payload = asdict(table)
        payload["depth_histogram"] = dict(table.depth_histogram)
        tables.append(payload)
    return {"tables": tables, "in_project_chained_count": report.in_project_chained_count}


@contextlib.contextmanager
def _confine_runtime_output() -> Iterator[None]:
    """Keep runtime logs and database exception details outside the output contract."""
    previous_disable = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous_disable)


async def run_from_args(args: argparse.Namespace) -> int:
    """Emit one JSON result only after the transaction has completed successfully."""
    code = 0
    payload: dict[str, object]
    with _confine_runtime_output():
        try:
            # Settings, including POSTGRES_URL, remain the only database authority.
            get_settings()
            async with get_session_factory()() as session, session.begin():
                if args.execute:
                    result = await repair(session, expected=args.expect)
                    payload = {
                        "dry_run": False,
                        "changed_count": result.changed_count,
                        "before": _inventory_payload(result.before),
                        "after": _inventory_payload(result.after),
                    }
                else:
                    await session.execute(sa.text("SET TRANSACTION READ ONLY"))
                    payload = {"dry_run": True, **_inventory_payload(await inventory(session))}
        except MergeChainRepairError as error:
            code = 2
            payload = {"status": "refused", "reason": error.reason}
        except Exception as error:  # noqa: BLE001 — never expose a DSN or row contents
            code = 1
            payload = {"status": "failed", "error_type": type(error).__name__}
        finally:
            try:
                await dispose_engine()
            except Exception as error:  # noqa: BLE001
                if code == 0:
                    code = 1
                    payload = {"status": "failed", "error_type": type(error).__name__}
    print(json.dumps(payload, default=str, sort_keys=True), file=sys.stderr if code else sys.stdout)
    return code


def main(argv: list[str] | None = None) -> int:
    """Parse before database initialization so incomplete execution requests fail closed."""
    return asyncio.run(run_from_args(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
