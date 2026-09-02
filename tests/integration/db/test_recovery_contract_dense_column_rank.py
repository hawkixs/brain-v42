"""The dense rank is computed AFTER `NOT attisdropped` — proved on a dropped column.

This module is the repository's only place where the DR contract's column
fingerprint is played against a database carrying an `attnum` HOLE. Without that
case, the module would be green and INERT: on a database with no dropped column,
`row_number()` over the live columns and `attnum` return the SAME sequence, so the
bug — recomputing the rank before the filter — would survive every test. The
negative witness `test_a_hole_free_pair_cannot_tell_the_two_expressions_apart` pins
exactly that inertia, so that nobody can believe a flat case was enough.

Measured on 2026-08-23 across three databases at head `046`: 24 dropped columns
over 8 tables in production, 11 over 6 tables on a database built afresh by
`alembic upgrade head`, ZERO on a database out of `pg_restore`. An ordinal
fingerprint therefore measures the INSTANCE's history, not the schema.

The SQL played is not retyped here: it is EXTRACTED from the two versioned assets.
A test that copied the expression would prove its own copy's conformity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]
RECOVERY = ROOT / "ops" / "recovery"
ASSETS = ("brain-v42-v5.sql", "brain-v42-v5-pgrestore.sql")

HOLED = "zz_dense_rank_probe_holed"
DENSE = "zz_dense_rank_probe_dense"

#: The contract's dense rank, verbatim. The control mutation replaces it with
#: `attnum` — that is, with the rank as it would be computed BEFORE the filter.
DENSE_RANK = """         row_number() OVER (
             PARTITION BY observed_column.table_name
             ORDER BY attribute_record.attnum
         ) AS dense_position,"""
RANK_BEFORE_THE_FILTER = "         attribute_record.attnum AS dense_position,"

_COLUMNS = "id integer NOT NULL, label text, weight numeric(10, 2)"


def _observed_cte(asset: str) -> str:
    sql = (RECOVERY / asset).read_text(encoding="utf-8")
    start = sql.index("\nobserved_table_columns")
    return sql[start + 1 : sql.index("\n),\n", start)]


def _probe_query(cte: str) -> sa.TextClause:
    return sa.text(
        f"WITH {cte}\n)\n"
        "SELECT table_name, definition_md5 FROM observed_table_columns "
        "WHERE table_name IN (:holed, :dense) ORDER BY table_name"
    ).bindparams(holed=HOLED, dense=DENSE)


async def _fingerprints(connection: AsyncConnection, cte: str) -> dict[str, str]:
    rows = (await connection.execute(_probe_query(cte))).all()
    return {str(name): str(digest) for name, digest in rows}


async def _build_twins(connection: AsyncConnection, *, holed: bool) -> None:
    """Two tables with an identical LIVE shape, one of which carries an attnum hole."""
    if holed:
        await connection.execute(
            sa.text(
                f"CREATE TABLE public.{HOLED} (id integer NOT NULL, doomed text, "
                "label text, weight numeric(10, 2))"
            )
        )
        await connection.execute(sa.text(f"ALTER TABLE public.{HOLED} DROP COLUMN doomed"))
    else:
        await connection.execute(sa.text(f"CREATE TABLE public.{HOLED} ({_COLUMNS})"))
    await connection.execute(sa.text(f"CREATE TABLE public.{DENSE} ({_COLUMNS})"))


@pytest.mark.parametrize("asset", ASSETS)
@pytest.mark.asyncio
async def test_a_dead_column_does_not_move_the_contract_fingerprint(
    engine: AsyncEngine, asset: str
) -> None:
    """A control mutation in BOTH DIRECTIONS, on the same dropped-column case."""
    cte = _observed_cte(asset)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _build_twins(connection, holed=True)

            live = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_catalog.pg_attribute AS a "
                    "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
                    "WHERE c.relname = :holed AND a.attnum > 0 AND a.attisdropped"
                ).bindparams(holed=HOLED)
            )
            assert live == 1, "le cas ne porte pas de colonne morte, il ne prouve rien"

            # DIRECTION 1 — the contract as SHIPPED: the hole is invisible. This
            # assertion comes BEFORE any inspection of the text: a structural guard
            # placed higher would mask the semantic proof by failing first, and the
            # module would no longer say the contract is conforming — only that it
            # looks like what was expected.
            contract = await _fingerprints(connection, cte)
            assert contract[HOLED] == contract[DENSE], asset

            # DIRECTION 2 — rank recomputed BEFORE the filter: the hole engraves the instance.
            assert DENSE_RANK in cte, asset
            mutated = cte.replace(DENSE_RANK, RANK_BEFORE_THE_FILTER)
            broken = await _fingerprints(connection, mutated)
            assert broken[HOLED] != broken[DENSE], asset

            # DIRECTION 1 replayed after the mutation: the green comes back with no other move.
            assert (await _fingerprints(connection, cte))[HOLED] == contract[DENSE], asset
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("asset", ASSETS)
@pytest.mark.asyncio
async def test_a_hole_free_pair_cannot_tell_the_two_expressions_apart(
    engine: AsyncEngine, asset: str
) -> None:
    """The INERTIA witness: with no dropped column, the bug passes both tests."""
    cte = _observed_cte(asset)
    mutated = cte.replace(DENSE_RANK, RANK_BEFORE_THE_FILTER)
    assert mutated != cte, asset

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _build_twins(connection, holed=False)
            assert await _fingerprints(connection, cte) == await _fingerprints(
                connection, mutated
            ), asset
        finally:
            await transaction.rollback()
