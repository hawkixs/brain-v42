"""The claim inventory is ONE bounded aggregate over a representative ledger.

The briefing line must cost the same whether the ledger holds ten claims or ten
thousand per read, and must never fan out per claim. This module owns a
disposable database: it commits append-only claim rows (migration 055 refuses
every DELETE), so they cannot live in a shared database.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import (
    brain_entities,
    knowledge_claims,
    knowledge_fact_definitions,
    project_contexts,
)
from brain_v42.repositories.pg_claim_inventory import (
    ClaimInventory,
    claim_inventory_statement,
    read_claim_inventory,
)
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration

#: Entries x claims per entry. 500 x 4 = 2000 rows: a ledger far above the
#: current volume, so a plan that scaled per claim would show.
_ENTRIES = 500
_CLAIMS_PER_ENTRY = 4
#: Generous on purpose: this guards the SHAPE (one scan, no loops), not a benchmark.
_EXECUTION_BOUND_MS = 250.0


# Session scope, not module: these fixtures shadow session-scoped conftest fixtures
# that consume `engine`; a module scope raises ScopeMismatch (see test_claim_write_path).
@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """Committed claim rows cannot be cleaned from a shared database."""
    with fresh_head_database(
        _get_integration_db_url_or_skip(), prefix="brain_claim_inventory"
    ) as url:
        yield url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    disposable_engine = create_async_engine(migration_database_url, poolclass=NullPool)
    try:
        yield disposable_engine
    finally:
        await disposable_engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def ledger(engine: AsyncEngine) -> ClaimInventory:
    """Seed a representative ledger and return the counts it must produce."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    project = f"inv-{uuid4().hex[:14]}"
    fact_name = f"claim_inventory_{uuid4().hex}"
    provenances = ("extracted", "declared", "measured", "extracted")
    expected = {"extracted": 0, "declared": 0, "measured": 0}
    created_7d = 0
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(project_contexts).values(
                project_key=project, name=project, description="claim inventory fixture"
            )
        )
        await session.execute(
            sa.insert(knowledge_fact_definitions).values(
                fact_name=fact_name,
                definition_version=1,
                target="production",
                ttl_seconds=30,
                timeout_seconds=1,
                policies={},
                value_schema={"lag": "int"},
                digest="a" * 64,
            )
        )
        rows: list[dict[str, Any]] = []
        for entry in range(_ENTRIES):
            anchor = (
                await session.execute(
                    sa.insert(brain_entities)
                    .values(
                        entity_type="learning",
                        entity_key=f"claim-inventory-{uuid4()}",
                        source_uuid=uuid4(),
                        project_key=project,
                        scope_kind="project",
                        lifecycle="active",
                    )
                    .returning(brain_entities.c.id)
                )
            ).scalar_one()
            for index in range(_CLAIMS_PER_ENTRY):
                # A third of the rows are retired, a third are older than 7 days.
                retired = (entry + index) % 3 == 0
                old = (entry + index) % 3 == 1
                recorded_at = now - timedelta(days=30 if old else 1)
                provenance = provenances[index]
                if not retired:
                    expected[provenance] += 1
                if not old:
                    created_7d += 1
                rows.append(
                    {
                        "entity_ref_id": anchor,
                        "entity_type": "learning",
                        "project_key": project,
                        "claim_key": f"{entry * _CLAIMS_PER_ENTRY + index + 1:064x}",
                        "statement": f"Lag claim {entry}-{index}",
                        "fact_name": fact_name,
                        "definition_version": 1,
                        "target": "production",
                        "expected": {"path": "/lag", "op": "lte", "value": 5},
                        "expected_resolved": {"path": "/lag", "op": "lte", "value": 5},
                        "validity_seconds": 60,
                        "provenance": provenance,
                        "declared_by": "tester",
                        "declared_at": now,
                        "recorded_at": recorded_at,
                        "retired_at": now if retired else None,
                    }
                )
        await session.execute(sa.insert(knowledge_claims), rows)
    return ClaimInventory(
        extracted=expected["extracted"],
        declared=expected["declared"],
        measured=expected["measured"],
        created_7d=created_7d,
    )


def _plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


async def test_claim_inventory_query_is_bounded(
    engine: AsyncEngine, ledger: ClaimInventory
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []

    def collect(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", collect)
    try:
        async with factory() as session:
            inventory = await read_claim_inventory(session)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", collect)

    # One SELECT, the whole answer: no per-claim, per-fact or per-project query.
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert inventory == ledger
    assert inventory.active == ledger.extracted + ledger.declared + ledger.measured

    compiled = claim_inventory_statement().compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    async with factory() as session:
        raw = (
            await session.execute(
                sa.text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")  # noqa: S608
            )
        ).scalar_one()
    document = json.loads(raw) if isinstance(raw, str) else raw
    plan = document[0]["Plan"]
    nodes = list(_plan_nodes(plan))
    claims_scans = [n for n in nodes if n.get("Relation Name") == "knowledge_claims"]

    # One aggregate over one scan of the table: no join, no subplan, no nested loop.
    assert plan["Node Type"] == "Aggregate"
    assert len(claims_scans) == 1
    assert {n["Node Type"] for n in nodes} <= {
        "Aggregate",
        "Seq Scan",
        "Index Only Scan",
        "Index Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
    }
    assert all(n.get("Actual Loops", 1) == 1 for n in nodes)
    assert document[0]["Execution Time"] < _EXECUTION_BOUND_MS
    # Every counted row is read once: the scan cannot touch more than the table holds.
    assert claims_scans[0]["Actual Rows"] + claims_scans[0].get("Rows Removed by Filter", 0) <= (
        _ENTRIES * _CLAIMS_PER_ENTRY
    )
    print(  # noqa: T201 - measured shape, kept for the PR report (pytest -s)
        "claim inventory plan:",
        plan["Node Type"],
        claims_scans[0]["Node Type"],
        f"{document[0]['Execution Time']:.3f} ms",
        "shared hit/read:",
        plan.get("Shared Hit Blocks"),
        plan.get("Shared Read Blocks"),
    )
