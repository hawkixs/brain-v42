"""Une base NEUVE bâtie par `alembic upgrade head` est l'étalon — deux
consommateurs doivent la reproduire : les métadonnées SQLAlchemy et l'actif DR.

Deux tickets, une seule cause : rien ne comparait un artefact du dépôt au
schéma que la chaîne alembic produit RÉELLEMENT.

* `8f59f6b7` — `tables.py` portait encore le XOR retiré par la 047 ET il
  manquait la branche `closed_inactive` de la 046 : une base bâtie par
  `create_all()` refusait des lignes que la production accepte. Aucun chemin
  vivant n'utilise `create_all()` sur METADATA aujourd'hui (recensé : les
  seuls appels sont des MetaData privées de tests) — le défaut était dormant,
  pas absent.
* `23be2271` — les tests du contrat DR garantissent test↔actif, jamais
  actif↔schéma réel : v4 attendait 128 index quand la prod en avait 130, vert ;
  la prod est à 131 pendant que l'actif v5 dit 130. L'écart GRANDIT à chaque
  migration parce qu'il n'apparaît qu'au rejeu live — un geste manuel.

Le remède commun : bâtir ici une base jetable par la chaîne alembic, puis
exiger la parité. Les écarts CONNUS de l'actif sont ÉPINGLÉS un par un avec
leur valeur exacte et leur ticket — jamais tolérés en bande : une dérive qui
grandit (049 ajoute un index) casse l'épingle, une dérive qui guérit (l'actif
remis à niveau) la casse aussi, et l'exception se retire au lieu de survivre.

Les bases jetables vivent dans le MÊME serveur que `BRAIN_V42_TEST_DB_URL`,
comme `brain_test` lui-même ; elles sont créées et détruites par le module.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).parents[3]
V5_SQL = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v5.sql"
V5_JSON = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v5.json"

#: Les checks du contrat qui attestent des DONNÉES transportées par une
#: restauration. Une base neuve est vide par construction : ils ne peuvent pas
#: passer ici et ce n'est pas une dérive. Exemption par KIND, pas par id — un
#: check de données ajouté demain est exempté pour la même raison mesurable.
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})

#: Les écarts CONNUS entre l'actif v5 — frappé le 2026-08-22, AVANT les
#: migrations 047 (contrainte terminale réécrite) et 048 (colonne
#: `attribution_mode` + index partiel sur `brain_session_artifacts`) — et la
#: chaîne alembic à head. Chaque valeur `observed` est épinglée EXACTEMENT,
#: mesurée le 2026-08-29, et portée par `23be2271` (le mécanisme) et le lot
#: DR v5 à venir (la remise à niveau). Retirer une entrée exige que l'actif
#: ait été re-frappé ; la laisser bouger d'un cran fait rougir ce test —
#: c'est sa raison d'être.
PINNED_ASSET_DRIFT: dict[str, dict[str, Any]] = {
    # 23be2271 : v5 fige 130 index ; la 048 en a ajouté un 131e.
    "catalog_counts": {
        "indexes": 131,
        "foreign_keys": 26,
        "invalid_indexes": 0,
        "unvalidated_constraints": 0,
    },
    "brain_runtime_032_036_037": {
        "artifact_column_mismatches": 2,
        "artifact_constraint_mismatches": 1,
        "artifact_index_mismatches": 1,
        "artifact_project_mismatches": 0,
        "ended_snapshot_mismatches": 0,
        "focus_revision_violations": 0,
        "runtime_trigger_mismatches": 0,
        "session_column_mismatches": 0,
        "session_constraint_mismatches": 3,
        "view_column_mismatches": 0,
        "view_definition_mismatches": 0,
        "view_option_mismatches": 0,
    },
    "table_shape": {
        # 049 (re-mesuré le 2026-08-29) : +1 colonne-md5 (dream_runs gagne
        # closed_inactive_count et thinking_tokens — un seul md5 par table),
        # +6 contraintes (les six CHECK ck_*_freshness_source re-signés avec
        # manual_update/plan_reindex). Le re-mint de l'actif (v7, lot DR)
        # accompagne le rollout de la 049 et fera retomber ces épingles.
        "table_column_mismatches": 2,
        "table_constraint_mismatches": 8,
        "table_index_mismatches": 1,
    },
}

#: 2ed0d4e0 : l'actif épingle la version que la prod DÉCLARE (0.8.2) ; toute
#: base neuve déclare le build de son image — 0.8.4 (runtime de prod) ou
#: 0.8.5 (cible compose épinglée). L'observé dépend donc du serveur qui porte
#: la base jetable, pas de la chaîne alembic : bande fermée, pas valeur unique.
RESTORE_BUILD_VECTOR_VERSIONS = frozenset({"0.8.4", "0.8.5"})


def _database_url_or_skip() -> str:
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    return url


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _swap_database(url: str, database: str) -> str:
    base, _, _old = url.rpartition("/")
    return f"{base}/{database}"


def _run_sql(dsn: str, statements: list[str]) -> None:
    async def run() -> None:
        connection = await asyncpg.connect(dsn)
        try:
            for statement in statements:
                await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def _create_database(admin_url: str, database: str) -> None:
    _run_sql(_asyncpg_dsn(admin_url), [f'CREATE DATABASE "{database}"'])


def _drop_database(admin_url: str, database: str) -> None:
    _run_sql(_asyncpg_dsn(admin_url), [f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'])


def _alembic_upgrade_head(db_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr}\n{result.stdout}")


@pytest.fixture(scope="module")
def fresh_head_db_url() -> Iterator[str]:
    """Une base vierge amenée à head par la chaîne alembic — l'étalon."""
    admin_url = _database_url_or_skip()
    database = f"brain_fresh_head_{uuid.uuid4().hex[:12]}"
    _create_database(admin_url, database)
    url = _swap_database(admin_url, database)
    try:
        _alembic_upgrade_head(url)
        yield url
    finally:
        _drop_database(admin_url, database)


@pytest.fixture(scope="module")
def create_all_db_url() -> Iterator[str]:
    """Une base vierge bâtie par `METADATA.create_all()` — le banc à comparer."""
    admin_url = _database_url_or_skip()
    database = f"brain_fresh_meta_{uuid.uuid4().hex[:12]}"
    _create_database(admin_url, database)
    url = _swap_database(admin_url, database)
    try:
        _run_sql(_asyncpg_dsn(url), ["CREATE EXTENSION IF NOT EXISTS vector"])

        async def build() -> None:
            from brain_v42.db.tables import METADATA

            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(METADATA.create_all)
            finally:
                await engine.dispose()

        asyncio.run(build())
        yield url
    finally:
        _drop_database(admin_url, database)


async def _fetch_constraints(url: str) -> dict[tuple[str, str], str]:
    """Tous les CHECK du schéma public, clés (table, nom) — jamais un filtre.

    La première version filtrait `conrelid = brain_sessions` : la review de la
    PR 44 a mesuré 18 CHECK présents dans la chaîne et absents de create_all()
    sur 12 AUTRES tables — la classe du ticket 8f59f6b7 déplacée d'une table,
    pas fermée. La comparaison toutes-tables des CHECK tient en une requête ;
    triggers et fonctions restent hors périmètre, eux à raison (ils n'ont pas
    de place dans METADATA).
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT conrelid::regclass::text AS tbl, conname, "
                        "pg_get_constraintdef(oid) AS definition "
                        "FROM pg_constraint "
                        "WHERE contype = 'c' "
                        "AND connamespace = 'public'::regnamespace"
                    )
                )
            ).mappings()
            return {(str(row["tbl"]), str(row["conname"])): str(row["definition"]) for row in rows}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_all_reproduces_every_check_constraint_of_the_chain(
    fresh_head_db_url: str, create_all_db_url: str
) -> None:
    """Parité intégrale des CHECK, TOUTES tables : chaîne alembic d'un côté,
    `create_all()` de l'autre, comparés par `pg_get_constraintdef` — la même
    lecture qui a établi la divergence en production.

    Née sur `brain_sessions` seule (XOR 047, branche 046) ; la review de la
    PR 44 a prouvé que la classe du ticket 8f59f6b7 vivait aussi sur 12 autres
    tables (18 CHECK absents : un banc create_all() acceptait
    `learnings.confidence = '42'` ou un `project_key` malformé que la prod
    refuse). Le recensement demandé par le ticket est CE test, sans filtre :
    tout CHECK ajouté par une migration future devra exister dans METADATA —
    ou être exempté ICI, par table, avec sa raison écrite.
    """
    from_chain = await _fetch_constraints(fresh_head_db_url)
    from_metadata = await _fetch_constraints(create_all_db_url)

    missing = sorted(set(from_chain) - set(from_metadata))
    assert not missing, (
        "CHECK présents dans la chaîne alembic et absents de create_all() — "
        "le banc accepte ce que la prod refuse :\n"
        + "\n".join(f"  {table} :: {name}" for table, name in missing)
    )
    extra = sorted(set(from_metadata) - set(from_chain))
    assert not extra, (
        "CHECK présents dans create_all() et absents de la chaîne — le banc "
        "refuse ce que la prod accepte :\n"
        + "\n".join(f"  {table} :: {name}" for table, name in extra)
    )
    for key in sorted(from_chain):
        assert from_metadata[key] == from_chain[key], (
            f"constraint {key} diverges between create_all() and the alembic chain"
        )


@pytest.mark.asyncio
async def test_a_create_all_bench_accepts_what_production_accepts(
    create_all_db_url: str,
) -> None:
    """Les deux lignes que la production accepte et que le banc refusait.

    1. une session `closed_inactive` (046) — l'énum l'admettait, la contrainte
       de branches la refusait ;
    2. une fermeture `ended` au ledger vide SANS `nothing_to_capture_reason`
       (047) — le XOR la refusait.
    """
    engine = create_async_engine(create_all_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO project_contexts (project_key, name, description) "
                    "VALUES ('bench-parity', 'bench', 'create_all parity bench')"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_sessions "
                    "(project_key, client_key, status, started_focus_revision, "
                    " nature, ended_at) "
                    "VALUES ('bench-parity', 'sweeper', 'closed_inactive', 0, "
                    " 'agent', NOW())"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_sessions "
                    "(project_key, client_key, status, started_focus_revision, "
                    " summary, next_focus, focus_outcome, focus_at_end, ended_at) "
                    "VALUES ('bench-parity', 'closer', 'ended', 0, "
                    " 'work done', 'next thing', 'applied', 'next thing', NOW())"
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_recovery_asset_passes_against_a_fresh_head_database(
    fresh_head_db_url: str,
) -> None:
    """Rejoue `brain-v42-v5.sql` contre l'étalon : actif↔schéma réel, enfin.

    Chaque check du reçu doit passer, sauf :
    * les checks de DONNÉES (`DATA_CHECK_KINDS`) — une base neuve est vide ;
    * les écarts ÉPINGLÉS de `PINNED_ASSET_DRIFT`, à leur valeur exacte.

    Une migration qui ajoute un index sans remise à niveau de l'actif casse
    l'épingle `catalog_counts` (observé 132 ≠ 131) ; une remise à niveau de
    l'actif la casse aussi (le check passe) et l'épingle se retire. Le trou
    du ticket — « l'écart n'apparaît qu'au rejeu live, un geste manuel » —
    est fermé par ce rejeu automatique.
    """
    sql = V5_SQL.read_text(encoding="utf-8")
    engine = create_async_engine(fresh_head_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                raw = await connection.scalar(sa.text(sql))
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    receipt = json.loads(str(raw))
    failures: dict[str, dict[str, Any]] = {}
    for check in receipt["checks"]:
        if check["status"] != "pass":
            failures[check["id"]] = check

    # Le reçu ne porte pas `kind` ; la nature de chaque check vit dans le
    # contrat JSON, la même source que le moteur DSL de red-backup.
    contract = json.loads(V5_JSON.read_text(encoding="utf-8"))
    kinds = {check["id"]: check.get("kind") for check in contract["checks"]}
    unexplained = {
        check_id: failure
        for check_id, failure in failures.items()
        if kinds.get(check_id) not in DATA_CHECK_KINDS
        and check_id not in PINNED_ASSET_DRIFT
        and check_id != "extension_vector"
    }
    assert not unexplained, (
        "the v5 asset and the alembic chain disagree beyond the pinned drift:\n"
        + json.dumps(unexplained, indent=2, default=str)
    )

    # Les épingles sont EXACTES : toute variation — aggravation (049 ajoute un
    # index : observé 132) ou guérison (actif re-frappé : le check passe) —
    # doit rougir ici pour être actée, jamais absorbée.
    for check_id, pinned_observed in PINNED_ASSET_DRIFT.items():
        failure = failures.get(check_id)
        assert failure is not None, (
            f"{check_id} now passes: the asset was re-minted — remove its pin "
            "from PINNED_ASSET_DRIFT"
        )
        assert failure["observed"] == pinned_observed, (
            f"{check_id}: the drift MOVED since it was pinned on 2026-08-29 — "
            "re-measure, then update the asset (DR v5 lot) or this pin:\n"
            f"pinned:   {json.dumps(pinned_observed, sort_keys=True)}\n"
            f"observed: {json.dumps(failure['observed'], sort_keys=True)}"
        )

    vector = failures.get("extension_vector")
    assert vector is not None, (
        "extension_vector now passes: the asset was re-minted — remove its pin"
    )
    assert vector["expected"] == "0.8.2"
    assert vector["observed"] in RESTORE_BUILD_VECTOR_VERSIONS
