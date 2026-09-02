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

**Le module pointe l'actif v7 depuis le 2026-09-02**, et les deux moitiés de
cette phrase se sont vérifiées le même jour : le mint v7 a fait PASSER les trois
écarts que v5 portait, la boucle a exigé leur retrait un par un, et
`PINNED_ASSET_DRIFT` est reparti vide. Mesuré : 23 checks sur 30 passent contre
une base neuve, les sept échecs étant six checks de données et l'inventaire des
extensions. Zéro écart structurel entre l'actif et la chaîne — pour la première
fois depuis que ce module existe.

Le jumeau `-pgrestore` est rejoué ici LUI AUSSI, et c'est délibérément un
demi-test : une base neuve n'est pas une restauration, donc il ne dit rien de
l'aller-retour `pg_dump`/`pg_restore`. Ce qu'il dit, et que rien d'autre ne
disait, c'est que les empreintes du jumeau décrivent bien le schéma 049 —
canonicalisation comprise, les six contraintes DÉRIVÉES de la 049 incluses. Il
en diverge par exactement un index, épinglé.

Les bases jetables vivent dans le MÊME serveur que `BRAIN_V42_TEST_DB_URL`,
comme `brain_test` lui-même ; elles sont créées et détruites par le module.
Elles ne touchent jamais `brain` : `conftest` refuse ce nom avant toute
connexion, et chaque base porte un nom `brain_fresh_*` unique.
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
V7_SQL = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v7.sql"
V7_JSON = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v7.json"
V7_PGRESTORE = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v7-pgrestore.sql"

#: Les checks du contrat qui attestent des DONNÉES transportées par une
#: restauration. Une base neuve est vide par construction : ils ne peuvent pas
#: passer ici et ce n'est pas une dérive. Exemption par KIND, pas par id — un
#: check de données ajouté demain est exempté pour la même raison mesurable.
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})

#: Les écarts CONNUS entre l'actif DR courant et la chaîne alembic à head.
#: **VIDE depuis le 2026-09-02**, et c'est un résultat mesuré, pas un
#: relâchement : le mint v7 a remis l'actif au niveau de la chaîne. L'actif v5
#: en portait trois, tous pré-mesurés le 2026-08-29 en prévision de ce lot —
#: `catalog_counts` (v5 figeait 130 index, la 048 en a ajouté un 131e),
#: `brain_runtime_032_036_037` (047 + 048 sur `brain_session_artifacts`) et
#: `table_shape` {2 colonnes, 8 contraintes, 1 index}, le « yardstick {2,8,1} »
#: du focus. Les trois sont retombés d'un coup, comme le commentaire de la 049
#: l'annonçait, et la boucle plus bas a EXIGÉ leur retrait un par un au lieu de
#: les absorber.
#:
#: Le dictionnaire reste, vide, parce que c'est le mécanisme et non la donnée
#: qui a de la valeur : la prochaine migration livrée avant son re-mint le
#: remplira. Vide, il ne rend rien vacuant — c'est `unexplained` qui porte
#: alors toute la charge, et il exige que TOUT échec non-données soit connu.
PINNED_ASSET_DRIFT: dict[str, dict[str, Any]] = {}

#: Le seul écart du jumeau `-pgrestore` contre une base NEUVE, et il est
#: structurel : `idx_dream_promotions_source_materialized` est épinglé par le
#: jumeau sous sa forme RE-SÉRIALISÉE par `pg_restore`, que la chaîne alembic
#: ne produit pas — mesuré le 2026-09-02, et c'est bien 1 index et 0 le reste.
#: L'épingler ici plutôt que l'exempter en bande est ce qui rend la mesure
#: utile : si un deuxième index se mettait à diverger, ce test rougirait.
PINNED_TWIN_DRIFT: dict[str, Any] = {
    "table_index_mismatches": 1,
    "table_column_mismatches": 0,
    "table_constraint_mismatches": 0,
}

#: 2ed0d4e0 : l'actif de BASE épingle l'inventaire que la prod DÉCLARE
#: (`plpgsql 1.0, vector 0.8.2`) ; toute base neuve déclare le build de son
#: image — 0.8.4 (mesuré ici le 2026-09-02, `default_version` du cluster) ou
#: 0.8.5 (cible compose épinglée). L'observé dépend donc du serveur qui porte
#: la base jetable, pas de la chaîne alembic : bande fermée, pas valeur unique.
#: Le JUMEAU, lui, n'exige que les NOMS (règle names-only du mint v6) et passe
#: donc ce check — c'est pour ça qu'il n'apparaît pas dans `PINNED_TWIN_DRIFT`.
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


async def _replay(url: str, asset: Path) -> dict[str, dict[str, Any]]:
    """Rejouer un actif d'attestation en LECTURE SEULE, rendre ses seuls échecs.

    `SET TRANSACTION READ ONLY` puis rollback : un contrat qui écrirait ne
    serait plus un contrat, et cette base jetable ne doit sa propreté qu'à la
    chaîne alembic — pas au fait que personne n'ait regardé.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                raw = await connection.scalar(sa.text(asset.read_text(encoding="utf-8")))
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    receipt = json.loads(str(raw))
    return {check["id"]: check for check in receipt["checks"] if check["status"] != "pass"}


@pytest.mark.asyncio
async def test_the_recovery_asset_passes_against_a_fresh_head_database(
    fresh_head_db_url: str,
) -> None:
    """Rejoue `brain-v42-v7.sql` contre l'étalon : actif↔schéma réel, enfin.

    Chaque check du reçu doit passer, sauf :
    * les checks de DONNÉES (`DATA_CHECK_KINDS`) — une base neuve est vide ;
    * les écarts ÉPINGLÉS de `PINNED_ASSET_DRIFT`, à leur valeur exacte ;
    * `extension_versions`, dont l'observé est le build du serveur qui porte la
      base jetable, pas une propriété de la chaîne alembic.

    Mesuré le 2026-09-02 sur v7 : **23 checks passent sur 30**, et les sept
    échecs sont SIX checks de données et l'extension. Zéro écart structurel —
    ce test ne le déclare pas, il l'exige.

    Une migration qui ajoute un index sans remise à niveau de l'actif fait
    apparaître un échec inconnu ; une remise à niveau de l'actif fait passer un
    check épinglé, et l'épingle DOIT alors se retirer. Les deux sens rougissent,
    et c'est ce qui a fait retomber les trois épingles de v5 dans ce lot. Le trou
    du ticket — « l'écart n'apparaît qu'au rejeu live, un geste manuel » — est
    fermé par ce rejeu automatique.
    """
    failures = await _replay(fresh_head_db_url, V7_SQL)

    # Le reçu ne porte pas `kind` ; la nature de chaque check vit dans le
    # contrat JSON, la même source que le moteur DSL de red-backup.
    contract = json.loads(V7_JSON.read_text(encoding="utf-8"))
    kinds = {check["id"]: check.get("kind") for check in contract["checks"]}
    unexplained = {
        check_id: failure
        for check_id, failure in failures.items()
        if kinds.get(check_id) not in DATA_CHECK_KINDS
        and check_id not in PINNED_ASSET_DRIFT
        and check_id != "extension_versions"
    }
    assert not unexplained, (
        "the v7 asset and the alembic chain disagree beyond the pinned drift:\n"
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

    extensions = failures.get("extension_versions")
    assert extensions is not None, (
        "extension_versions now passes: the asset was re-minted — remove its pin"
    )
    assert extensions["expected"] == "plpgsql 1.0, vector 0.8.2"
    assert extensions["observed"] in {
        f"plpgsql 1.0, vector {version}" for version in RESTORE_BUILD_VECTOR_VERSIONS
    }


@pytest.mark.asyncio
async def test_the_pgrestore_twin_diverges_from_a_fresh_head_by_exactly_one_index(
    fresh_head_db_url: str,
) -> None:
    """Le jumeau `-pgrestore` mesuré là où on PEUT le mesurer sans restauration.

    Le jumeau existe pour être rejoué contre une cible RESTAURÉE, et le lot v7
    ne l'y a pas rejoué — aucun banc n'a été monté, c'est écrit dans le runbook
    et ce test ne le remplace pas. Mais une base neuve bâtie par la chaîne
    alembic n'est pas rien : elle porte le schéma 049 pour de vrai, donc elle
    peut dire si les empreintes du jumeau décrivent CE schéma, canonicalisation
    comprise.

    Elle le dit, et c'est la moitié de preuve qui manquait au mint : les 118
    contraintes et les 32 empreintes de colonnes du jumeau — les six
    `ck_*_freshness_source` de la 049 incluses, dont les valeurs ont été
    DÉRIVÉES et non lues sur un restore — tombent juste, `0` et `0`. Ce qui ne
    tombe pas juste est UN index et un seul,
    `idx_dream_promotions_source_materialized`, que le jumeau épingle sous la
    forme que `pg_restore` re-sérialise et que la chaîne alembic ne produit
    jamais. C'est sa raison d'être, pas un défaut : ce test l'épingle à sa
    valeur exacte pour qu'un SECOND index divergent ne passe pas pour lui.

    Ce que ce test ne prouve toujours pas : l'aller-retour `pg_dump`/`pg_restore`
    lui-même. Il faut un banc pour ça.
    """
    failures = await _replay(fresh_head_db_url, V7_PGRESTORE)

    contract = json.loads(V7_JSON.read_text(encoding="utf-8"))
    kinds = {check["id"]: check.get("kind") for check in contract["checks"]}
    unexplained = {
        check_id: failure
        for check_id, failure in failures.items()
        if kinds.get(check_id) not in DATA_CHECK_KINDS and check_id != "table_shape"
    }
    assert not unexplained, (
        "the v7 -pgrestore twin disagrees with the alembic chain somewhere other "
        "than its one re-serialized index:\n" + json.dumps(unexplained, indent=2, default=str)
    )

    # Le jumeau n'exige que les NOMS des extensions : contrairement à l'actif de
    # base, il DOIT passer ce check sur une base neuve. S'il échoue, la règle
    # names-only du mint v6 a été perdue par le mint v7.
    assert "extension_versions" not in failures, (
        "the twin now judges extension VERSIONS — the names-only rule was lost"
    )

    shape = failures.get("table_shape")
    assert shape is not None, (
        "the twin now matches a fresh head exactly: either pg_restore stopped "
        "re-serializing idx_dream_promotions_source_materialized, or the twin was "
        "minted from a non-restored source — re-measure before removing this pin"
    )
    assert shape["observed"] == PINNED_TWIN_DRIFT, (
        "the twin's divergence from a fresh head MOVED since 2026-09-02:\n"
        f"pinned:   {json.dumps(PINNED_TWIN_DRIFT, sort_keys=True)}\n"
        f"observed: {json.dumps(shape['observed'], sort_keys=True)}"
    )
