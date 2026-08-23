"""Le rang dense se calcule APRÈS `NOT attisdropped` — prouvé sur colonne morte.

Ce module est le seul endroit du dépôt où l'empreinte de colonnes du contrat DR
est jouée contre une base qui porte un TROU d'`attnum`. Sans ce cas, le module
serait vert et INERTE : sur une base sans colonne morte, `row_number()` sur les
colonnes vivantes et `attnum` rendent la MÊME suite, donc le bug — recalculer le
rang avant le filtre — survivrait à tous les tests. Le témoin négatif
`test_a_hole_free_pair_cannot_tell_the_two_expressions_apart` épingle exactement
cette inertie, pour qu'on ne puisse pas croire qu'un cas plat suffisait.

Mesuré le 2026-08-23 sur trois bases à la tête `046` : 24 colonnes mortes sur 8
tables en production, 11 sur 6 tables sur une base bâtie à neuf par `alembic
upgrade head`, ZÉRO sur une base sortie de `pg_restore`. Une empreinte ordinale
mesure donc l'historique de l'INSTANCE, pas le schéma.

Le SQL joué n'est pas retapé ici : il est EXTRAIT des deux actifs versionnés. Un
test qui recopierait l'expression prouverait la conformité de sa propre copie.
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

#: Le rang dense du contrat, verbatim. La mutation de contrôle le remplace par
#: `attnum` — c'est-à-dire par le rang tel qu'il serait calculé AVANT le filtre.
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
    """Deux tables de forme VIVANTE identique, dont l'une porte un trou d'attnum."""
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
    """Mutation de contrôle dans les DEUX SENS, sur le même cas à colonne morte."""
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

            # SENS 1 — le contrat tel qu'il est LIVRÉ : le trou ne se voit pas.
            # Cette assertion vient AVANT toute inspection du texte : une garde
            # structurelle placée plus haut masquerait la preuve sémantique en
            # échouant la première, et le module ne dirait plus que le contrat
            # est conforme — seulement qu'il ressemble à ce qu'on attendait.
            contract = await _fingerprints(connection, cte)
            assert contract[HOLED] == contract[DENSE], asset

            # SENS 2 — rang recalculé AVANT le filtre : le trou grave l'instance.
            assert DENSE_RANK in cte, asset
            mutated = cte.replace(DENSE_RANK, RANK_BEFORE_THE_FILTER)
            broken = await _fingerprints(connection, mutated)
            assert broken[HOLED] != broken[DENSE], asset

            # SENS 1 rejoué après la mutation : le vert revient sans autre geste.
            assert (await _fingerprints(connection, cte))[HOLED] == contract[DENSE], asset
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("asset", ASSETS)
@pytest.mark.asyncio
async def test_a_hole_free_pair_cannot_tell_the_two_expressions_apart(
    engine: AsyncEngine, asset: str
) -> None:
    """Le témoin d'INERTIE : sans colonne morte, le bug passe les deux tests."""
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
