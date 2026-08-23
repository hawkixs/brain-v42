"""Exécute les deux attestations v5 dans une transaction PostgreSQL en lecture seule.

Jumeau de `test_recovery_contract_v4_execution`, et il n'était pas décoratif : le
lot `8eaefe36` a fait passer les deux actifs de ~2 700 à ~3 600 lignes de SQL que
RIEN n'exécutait en CI. Un `pytest tests/unit` entier reste vert sur un contrat
qui ne se parse pas — les modules statiques lisent du texte, pas un plan.

Le périmètre du snapshot est DÉRIVÉ de `table_set` plutôt qu'énuméré : la preuve
« l'attestation n'écrit rien » doit couvrir la 33ᵉ table le jour où elle entre,
sans qu'un rédacteur ait à y penser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]
RECOVERY = ROOT / "ops" / "recovery"
V5_JSON = RECOVERY / "brain-v42-v5.json"

CHECK_ID = "table_shape"

#: Les trois sous-compteurs de la check row `table_shape`, posée par `8eaefe36`.
SHAPE_COUNTERS = (
    "table_column_mismatches",
    "table_constraint_mismatches",
    "table_index_mismatches",
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _contract() -> dict[str, Any]:
    return json.loads(V5_JSON.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)


async def _database_snapshot(
    connection: AsyncConnection, relations: tuple[str, ...]
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for relation in relations:
        snapshot[relation] = str(
            await connection.scalar(
                sa.text(
                    "SELECT COALESCE(jsonb_agg(to_jsonb(observed) "
                    "ORDER BY to_jsonb(observed)::text), '[]'::jsonb)::text "
                    f"FROM public.{relation} AS observed"
                )
            )
        )
    return snapshot


@pytest.mark.parametrize("asset", ["brain-v42-v5.sql", "brain-v42-v5-pgrestore.sql"])
@pytest.mark.asyncio
async def test_v5_assets_execute_read_only_without_mutation(
    engine: AsyncEngine, asset: str
) -> None:
    expected = _contract()
    relations = tuple(
        next(check for check in expected["checks"] if check["id"] == "table_set")["tables"]
    )
    assert len(relations) == 32
    sql = (RECOVERY / asset).read_text(encoding="utf-8")

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            assert await connection.scalar(sa.text("SHOW transaction_read_only")) == "on"
            before = await _database_snapshot(connection, relations)
            raw = await connection.scalar(sa.text(sql))
            result = json.loads(str(raw), object_pairs_hook=_reject_duplicate_keys)
            after = await _database_snapshot(connection, relations)

            assert set(result) == {"checks", "contract_id", "schema_version"}
            assert result["contract_id"] == "brain-v42/postgresql-recovery/v5"
            assert result["schema_version"] == 5
            observed_ids = [check["id"] for check in result["checks"]]
            assert len(observed_ids) == len(set(observed_ids)) == 30
            assert set(observed_ids) == {check["id"] for check in expected["checks"]}
            assert after == before
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("asset", ["brain-v42-v5.sql", "brain-v42-v5-pgrestore.sql"])
@pytest.mark.asyncio
async def test_the_receipt_reports_the_three_shape_counters(
    engine: AsyncEngine, asset: str
) -> None:
    """Le reçu doit RENDRE les trois compteurs, pas seulement les calculer.

    Un compteur qui n'atteint pas le reçu est le mode de panne maison : vert,
    et invisible. Le même test garde l'autre moitié de l'arbitrage : que
    `catalog_counts` soit RESTÉ à ses quatre entiers, parce que le moteur DSL de
    `red-backup` le modélise `extra="forbid"` et refuserait un cinquième champ.
    """
    sql = (RECOVERY / asset).read_text(encoding="utf-8")
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            raw = await connection.scalar(sa.text(sql))
            result = json.loads(str(raw), object_pairs_hook=_reject_duplicate_keys)
        finally:
            await transaction.rollback()

    shape = next(check for check in result["checks"] if check["id"] == CHECK_ID)
    for counter in SHAPE_COUNTERS:
        assert shape["expected"][counter] == 0, counter
        assert isinstance(shape["observed"][counter], int), counter

    catalog_counts = next(check for check in result["checks"] if check["id"] == "catalog_counts")
    assert set(catalog_counts["expected"]) == {
        "foreign_keys",
        "indexes",
        "invalid_indexes",
        "unvalidated_constraints",
    }
