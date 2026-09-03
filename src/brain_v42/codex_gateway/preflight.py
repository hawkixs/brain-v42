"""Prove the four clauses of `/ready` against a database, without root.

`scripts/rotate_codex_gateway_credentials.py` already proves this contract — and
proves MORE of it, binding every declared column as `codex_ro`. But it proves it
at step three of a coordinated credential rotation, behind
`privileged_installer.preflight(config.shrik_env)`. Measured on 2026-09-03:
`/usr/local/sbin/brain-shrik-env-control` is not installed on this host, so the
CLI exits on `non-interactive Shrik privilege is unavailable` before opening a
single connection. The contract proof had been unreachable for 33 days while the
four clauses were, in fact, true the whole time.

This module is the read-only half of that proof, detached from everything that
needs a privilege: no rotation, no bearer, no `codex_ro` password, no sudo.

WHY READING AS THE APPLICATION ROLE IS NOT A SHORTCUT

The four clauses are CATALOG facts — `to_regclass`, `pg_class.reloptions`,
`pg_trigger`. They do not depend on who asks, and `/ready` itself evaluates them
as the gateway's own role, never as `codex_ro`. What genuinely requires the
`codex_ro` password is a different proof (column-level grants, scope
boundedness), and it stays where it is. No `codex_ro` credential is reachable
from this repository anyway: `.secrets/` does not exist and `deploy/
codex-gateway.env.example` carries the gateway bearer, not a database password.

WHY THERE IS NO LIST OF NAMES IN THIS FILE

Every view, barrier and trigger name is read out of `_CODEX_CONTRACT_READY`, the
string `/ready` executes. The rotation CLI keeps a copy because it deliberately
imports nothing from `brain_v42` — it rewrites the `.env` files `Settings` reads.
This module sits inside the package and has no such excuse, and b3331691 already
paid for what a divergent copy costs: a preflight that returned `ok` while the
gateway was broken.

The authority decides. The decomposition only explains. When the two disagree the
report says `unexplained` and refuses — a green explanation must never outrank a
red verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.codex_gateway.composition import _CODEX_CONTRACT_READY

_VIEW_ARRAYS = re.compile(r"ARRAY\[(.*?)\]::text\[\]")
_QUOTED_VIEW = re.compile(r"'(codex_\w+)'")
_TRIGGER_PAIR = re.compile(r"tgname = '(\w+)'\s+AND tgrelid = to_regclass\('public\.(\w+)'\)")

_SECURITY_BARRIER = "security_barrier"
_VIEWS = "views"


class ContractShapeError(RuntimeError):
    """The readiness SQL no longer has the shape these names are read from."""


@dataclass(frozen=True, slots=True)
class ContractNames:
    """The contract, as the authority spells it. Parallel tuples for `unnest`."""

    views: tuple[str, ...]
    barrier_views: tuple[str, ...]
    triggers: tuple[str, ...]
    trigger_tables: tuple[str, ...]


def contract_names(authority: str | None = None) -> ContractNames:
    """Read the names out of the readiness SQL, or refuse.

    Fail-closed on shape, and that is the whole point of the exception: a parser
    that returned what it managed to find would silently shrink the contract to
    the clauses it still recognised, and the preflight would go green because it
    had forgotten what to check.
    """
    flat = " ".join((authority if authority is not None else str(_CODEX_CONTRACT_READY)).split())
    arrays = _VIEW_ARRAYS.findall(flat)
    if len(arrays) < 2:
        raise ContractShapeError(
            "the readiness authority no longer declares two text[] arrays — "
            "realign brain_v42.codex_gateway.preflight with composition.py"
        )
    views = tuple(_QUOTED_VIEW.findall(arrays[0]))
    barrier_views = tuple(_QUOTED_VIEW.findall(arrays[1]))
    pairs = _TRIGGER_PAIR.findall(flat)
    if not views or not barrier_views or not pairs:
        raise ContractShapeError(
            "the readiness authority yielded an empty clause — "
            f"views={len(views)} barriers={len(barrier_views)} triggers={len(pairs)}"
        )
    return ContractNames(
        views=views,
        barrier_views=barrier_views,
        triggers=tuple(name for name, _table in pairs),
        trigger_tables=tuple(table for _name, table in pairs),
    )


_DIAGNOSIS = sa.text(
    """
        SELECT
            COALESCE((
                SELECT array_agg(required.name ORDER BY required.name)
                FROM unnest(:views) AS required(name)
                WHERE to_regclass('public.' || required.name) IS NULL
            ), ARRAY[]::text[]) AS missing_views,
            COALESCE((
                SELECT array_agg(scoped.name ORDER BY scoped.name)
                FROM unnest(:barrier_views) AS scoped(name)
                JOIN pg_class AS contract_view
                  ON contract_view.oid = to_regclass('public.' || scoped.name)
                WHERE NOT (
                    'security_barrier=true' = ANY(
                        COALESCE(contract_view.reloptions, ARRAY[]::text[])
                    )
                )
            ), ARRAY[]::text[]) AS unbarriered,
            COALESCE((
                SELECT array_agg(guard.name ORDER BY guard.name)
                FROM unnest(:triggers, :trigger_tables) AS guard(name, table_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = guard.name
                      AND tgrelid = to_regclass('public.' || guard.table_name)
                      AND tgenabled IN ('O', 'A')
                      AND NOT tgisinternal
                )
            ), ARRAY[]::text[]) AS inactive_triggers
        """
).bindparams(
    sa.bindparam("views", type_=ARRAY(sa.Text)),
    sa.bindparam("barrier_views", type_=ARRAY(sa.Text)),
    sa.bindparam("triggers", type_=ARRAY(sa.Text)),
    sa.bindparam("trigger_tables", type_=ARRAY(sa.Text)),
)


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """`ready` is the authority's word. Everything else explains it."""

    ready: bool
    clauses: dict[str, bool]
    missing: dict[str, list[str]] = field(default_factory=dict)
    unexplained: bool = False

    @property
    def exit_code(self) -> int:
        return 0 if self.ready and not self.unexplained else 1

    def as_json(self) -> str:
        return json.dumps(
            {
                "ready": self.ready,
                "clauses": self.clauses,
                "missing": self.missing,
                "unexplained": self.unexplained,
            },
            indent=2,
            sort_keys=True,
        )


class _ContractConnection(Protocol):
    """The two calls this needs — narrow enough for a fake to be honest."""

    async def scalar(self, statement: Any, /) -> Any: ...

    async def execute(self, statement: Any, /) -> Any: ...


async def inspect_contract(connection: _ContractConnection) -> PreflightReport:
    """Run the authority, then name what it refused. Two queries, no writes."""
    names = contract_names()
    ready = bool(await connection.scalar(_CODEX_CONTRACT_READY))
    row = (
        await connection.execute(
            _DIAGNOSIS.bindparams(
                views=list(names.views),
                barrier_views=list(names.barrier_views),
                triggers=list(names.triggers),
                trigger_tables=list(names.trigger_tables),
            )
        )
    ).one()

    missing_views = list(row.missing_views or ())
    unbarriered = list(row.unbarriered or ())
    inactive = set(row.inactive_triggers or ())

    clauses: dict[str, bool] = {
        _VIEWS: not missing_views,
        _SECURITY_BARRIER: not unbarriered,
    }
    missing: dict[str, list[str]] = {}
    if missing_views:
        missing[_VIEWS] = missing_views
    if unbarriered:
        missing[_SECURITY_BARRIER] = unbarriered
    for name, table in zip(names.triggers, names.trigger_tables, strict=True):
        clauses[name] = name not in inactive
        if name in inactive:
            missing[name] = [f"public.{table}"]

    return PreflightReport(
        ready=ready,
        clauses=clauses,
        missing=missing,
        unexplained=ready != all(clauses.values()),
    )


async def check_database(url: str) -> PreflightReport:
    """Open ONE read-only transaction and inspect it. Never writes, by statement."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            # First statement of the transaction, so the guard is the database's
            # and not this module's good intentions.
            await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            return await inspect_contract(connection)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brain_v42.codex_gateway.preflight",
        description="Prove the four clauses of the Codex gateway /ready contract, read-only.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "SQLAlchemy async URL. OMIT IT for production: the default comes from "
            "POSTGRES_URL, and a URL passed here carries its password into the "
            "shell history. Use it for a throwaway database only."
        ),
    )
    arguments = parser.parse_args(argv)

    url = arguments.url
    if url is None:
        from brain_v42.config import get_settings  # noqa: PLC0415 - optional dependency on env

        url = get_settings().postgres_url

    report = asyncio.run(check_database(url))
    print(report.as_json())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
