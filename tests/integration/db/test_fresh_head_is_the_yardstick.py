"""A FRESH database built by `alembic upgrade head` is the yardstick — two
consumers must reproduce it: the SQLAlchemy metadata and the DR asset.

Two tickets, one cause: nothing compared a repository artefact against the schema
the alembic chain REALLY produces.

* `8f59f6b7` — `tables.py` still carried the XOR removed by 047 AND was missing
  046's `closed_inactive` branch: a database built by `create_all()` refused rows
  production accepts. No live path uses `create_all()` on METADATA today
  (surveyed: the only calls are tests' private MetaData) — the defect was dormant,
  not absent.
* `23be2271` — the DR contract tests guarantee test↔asset, never asset↔real
  schema: v4 expected 128 indexes when production had 130, green; production is at
  131 while the v5 asset says 130. The gap GROWS at every migration because it
  only appears on a live replay — a manual gesture.

The shared remedy: build a disposable database here through the alembic chain,
then require parity. The asset's KNOWN gaps are PINNED one by one with their exact
value and their ticket — never tolerated as a band: a drift that grows (049 adds
an index) breaks the pin, a drift that heals (the asset brought up to date) breaks
it too, and the exception is removed instead of surviving.

**The module has pointed at the v7 asset since 2026-09-02**, and both halves of
that sentence were verified the same day: the v7 mint made the three gaps v5
carried PASS, the loop required their removal one by one, and `PINNED_ASSET_DRIFT`
went back to empty. Measured: 23 checks out of 30 pass against a fresh database,
the seven failures being six data checks and the extension inventory. Zero
structural gap between the asset and the chain — for the first time since this
module has existed.

The `-pgrestore` twin is replayed here TOO, and it is deliberately half a test: a
fresh database is not a restoration, so it says nothing about the
`pg_dump`/`pg_restore` round-trip. What it does say, and nothing else did, is that
the twin's fingerprints do describe the 049 schema — canonicalisation included,
the six constraints DERIVED from 049 included. It diverges from it by exactly one
index, pinned.

The disposable databases live in the SAME server as `BRAIN_V42_TEST_DB_URL`, like
`brain_test` itself; they are created and destroyed by the module. They never
touch `brain`: `conftest` refuses that name before any connection, and each
database carries a unique `brain_fresh_*` name.
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

#: The contract checks that attest the DATA carried by a restoration. A fresh
#: database is empty by construction: they cannot pass here and that is not a
#: drift. Exemption by KIND, not by id — a data check added tomorrow is exempted
#: for the same measurable reason.
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})

#: The KNOWN gaps between the current DR asset and the alembic chain at head.
#: **EMPTY since 2026-09-02**, and that is a measured result, not a relaxation: the
#: v7 mint brought the asset back up to the chain. The v5 asset carried three, all
#: pre-measured on 2026-08-29 in anticipation of this batch — `catalog_counts` (v5
#: froze 130 indexes, 048 added a 131st), `brain_runtime_032_036_037` (047 + 048 on
#: `brain_session_artifacts`) and `table_shape` {2 columns, 8 constraints, 1 index},
#: the focus's "yardstick {2,8,1}". All three fell away at once, as 049's comment
#: announced, and the loop below REQUIRED their removal one by one instead of
#: absorbing them.
#:
#: The dictionary stays, empty, because it is the mechanism and not the data that
#: has value: the next migration shipped before its re-mint will fill it. Empty, it
#: makes nothing vacuous — `unexplained` then carries the whole load, and it
#: requires that EVERY non-data failure be known.
PINNED_ASSET_DRIFT: dict[str, dict[str, Any]] = {}

#: The `-pgrestore` twin's only gap against a FRESH database, and it is structural:
#: `idx_dream_promotions_source_materialized` is pinned by the twin in the form
#: `pg_restore` RE-SERIALISES, which the alembic chain does not produce — measured
#: on 2026-09-02, and it is indeed 1 index and 0 for the rest. Pinning it here
#: rather than exempting it as a band is what makes the measurement useful: if a
#: second index started to diverge, this test would redden.
PINNED_TWIN_DRIFT: dict[str, Any] = {
    "table_index_mismatches": 1,
    "table_column_mismatches": 0,
    "table_constraint_mismatches": 0,
}

#: 2ed0d4e0: the BASE asset pins the inventory production DECLARES (`plpgsql 1.0,
#: vector 0.8.2`); any fresh database declares its image's build — 0.8.4 (measured
#: here on 2026-09-02, the cluster's `default_version`) or 0.8.5 (the pinned compose
#: target). The observed value therefore depends on the server hosting the
#: disposable database, not on the alembic chain: a closed band, not a single value.
#: The TWIN, for its part, only requires the NAMES (the v6 mint's names-only rule)
#: and therefore passes this check — which is why it does not appear in
#: `PINNED_TWIN_DRIFT`.
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
    """A pristine database brought to head by the alembic chain — the yardstick."""
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
    """A pristine database built by `METADATA.create_all()` — the bench to compare."""
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
    """Every CHECK of the public schema, keyed (table, name) — never a filter.

    The first version filtered on `conrelid = brain_sessions`: the PR 44 review
    measured 18 CHECKs present in the chain and absent from create_all() on 12 OTHER
    tables — ticket 8f59f6b7's class moved one table over, not closed. The
    all-tables comparison of CHECKs fits in one query; triggers and functions stay
    out of scope, and rightly so (they have no place in METADATA).
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
    """Full CHECK parity, ALL tables: the alembic chain on one side, `create_all()`
    on the other, compared through `pg_get_constraintdef` — the same reading that
    established the divergence in production.

    Born on `brain_sessions` alone (047's XOR, 046's branch); the PR 44 review
    proved that ticket 8f59f6b7's class also lived on 12 other tables (18 missing
    CHECKs: a create_all() bench accepted `learnings.confidence = '42'` or a
    malformed `project_key` that production refuses). The census the ticket asked
    for is THIS test, with no filter: every CHECK added by a future migration will
    have to exist in METADATA — or be exempted HERE, per table, with its reason
    written down.
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
    """The two rows production accepts and the bench refused.

    1. a `closed_inactive` session (046) — the enum admitted it, the branch
       constraint refused it;
    2. an `ended` closure with an empty ledger and NO `nothing_to_capture_reason`
       (047) — the XOR refused it.
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
    """Replay an attestation asset READ-ONLY, return its failures alone.

    `SET TRANSACTION READ ONLY` then rollback: a contract that wrote would no longer
    be a contract, and this disposable database owes its cleanliness to the alembic
    chain alone — not to the fact that nobody looked.
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
    """Replays `brain-v42-v7.sql` against the yardstick: asset↔real schema, at last.

    Every check of the receipt must pass, except:
    * the DATA checks (`DATA_CHECK_KINDS`) — a fresh database is empty;
    * the PINNED gaps of `PINNED_ASSET_DRIFT`, at their exact value;
    * `extension_versions`, whose observed value is the build of the server hosting
      the disposable database, not a property of the alembic chain.

    Measured on 2026-09-02 on v7: **23 checks pass out of 30**, and the seven
    failures are SIX data checks and the extension. Zero structural gap — this test
    does not declare it, it requires it.

    A migration that adds an index without bringing the asset up to date makes an
    unknown failure appear; bringing the asset up to date makes a pinned check pass,
    and the pin MUST then be removed. Both directions redden, and that is what made
    v5's three pins fall away in this batch. The ticket's hole — "the gap only
    appears on a live replay, a manual gesture" — is closed by this automatic
    replay.
    """
    failures = await _replay(fresh_head_db_url, V7_SQL)

    # The receipt does not carry `kind`; each check's nature lives in the JSON
    # contract, the same source as red-backup's DSL engine.
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

    # The pins are EXACT: any variation — worsening (049 adds an index: observed
    # 132) or healing (asset re-minted: the check passes) — must redden here to be
    # acted upon, never absorbed.
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
    """The `-pgrestore` twin measured where it CAN be measured without a restore.

    The twin exists to be replayed against a RESTORED target, and the v7 batch did
    not replay it there — no bench was stood up, that is written in the runbook and
    this test does not replace it. But a fresh database built by the alembic chain
    is not nothing: it carries the 049 schema for real, so it can say whether the
    twin's fingerprints describe THAT schema, canonicalisation included.

    It does say so, and that is the half-proof the mint lacked: the twin's 118
    constraints and 32 column fingerprints — 049's six `ck_*_freshness_source`
    included, whose values were DERIVED and not read off a restore — land exactly,
    `0` and `0`. What does not land exactly is ONE index and one only,
    `idx_dream_promotions_source_materialized`, which the twin pins in the form
    `pg_restore` re-serialises and the alembic chain never produces. That is its
    reason to exist, not a defect: this test pins it at its exact value so that a
    SECOND divergent index cannot pass for it.

    What this test still does not prove: the `pg_dump`/`pg_restore` round-trip
    itself. That needs a bench.
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

    # The twin only requires the extension NAMES: unlike the base asset, it MUST
    # pass this check on a fresh database. If it fails, the v6 mint's names-only
    # rule has been lost by the v7 mint.
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
