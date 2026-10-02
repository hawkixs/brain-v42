#!/usr/bin/env python3
"""Block a service start until PostgreSQL answers, within a hard deadline.

The long-running brain-v42 units are systemd *user* units: the user manager cannot order them
after docker.service (a system unit), so after a reboot they can start before the database
container is serving. This gate runs as ExecStartPre. It exits non-zero once the deadline
passes so the unit's restart backoff takes over, instead of the service crash-looping against
a database that is not there yet.

The connection URL is read from the environment (systemd's EnvironmentFile already loaded it)
and never from the command line or the logs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MAX_WAIT_SECONDS = 120
DEFAULT_WAIT_SECONDS = 90
DEFAULT_INTERVAL_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 5.0


def resolve_dsn(names: Sequence[str], environ: Mapping[str, str]) -> str:
    """Return the first non-empty URL among ``names``, without the SQLAlchemy driver suffix."""
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            try:
                url = make_url(value).set(drivername="postgresql")
                if url.port is not None and not 1 <= url.port <= 65535:
                    raise ValueError("port out of range")
                return url.render_as_string(hide_password=False)
            except (ValueError, ArgumentError) as exc:
                raise ValueError(f"{name} is invalid ({type(exc).__name__})") from None
    raise ValueError(f"none of {', '.join(names)} is set")


async def _query(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        await connection.fetchval("SELECT 1", timeout=CONNECT_TIMEOUT_SECONDS)
    finally:
        await connection.close()


def probe_postgres(dsn: str) -> None:
    """Raise unless a connection can be opened and answers a trivial query."""
    asyncio.run(_query(dsn))


def wait_for_postgres(
    dsn: str,
    *,
    timeout: float,
    interval: float,
    probe: Callable[[str], None] = probe_postgres,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Probe until ready; False once ``timeout`` seconds have elapsed. Never sleeps past it."""
    deadline = monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            probe(dsn)
        except Exception as exc:
            # Only the exception type: the driver message can carry host and user names.
            print(
                f"postgres not ready (attempt {attempt}, {type(exc).__name__})",
                file=sys.stderr,
                flush=True,
            )
        else:
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(interval, remaining))


def _wait_seconds(raw: str) -> float:
    seconds = float(raw)
    if not 0 < seconds <= MAX_WAIT_SECONDS:
        raise argparse.ArgumentTypeError(f"must be within (0, {MAX_WAIT_SECONDS}] seconds")
    return seconds


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url-env",
        action="append",
        required=True,
        help="Environment variable holding the URL; repeat to give a fallback order.",
    )
    parser.add_argument("--timeout", type=_wait_seconds, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    try:
        dsn = resolve_dsn(args.url_env, os.environ if environ is None else environ)
    except ValueError as exc:
        names = ", ".join(args.url_env)
        print(
            f"postgres readiness gate misconfigured ({type(exc).__name__}); check {names}",
            file=sys.stderr,
        )
        return 2
    if wait_for_postgres(dsn, timeout=args.timeout, interval=args.interval):
        return 0
    print(f"postgres not ready after {args.timeout:g}s", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
