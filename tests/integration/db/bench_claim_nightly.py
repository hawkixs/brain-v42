"""Benchmark gate of ADR 27 lot C, PR 3 (plan T3.1, spec §7.2 and §4.4).

Not a test module: the name does not match `test_*.py`, so pytest never collects
it. It is run by hand, against a disposable database it creates and drops:

    BRAIN_V42_TEST_DB_URL=<bootstrap url> python -m tests.integration.db.bench_claim_nightly

It seeds the projected size of spec §7.2 -- 10 000 claims over 10 projects,
1 000 000 verdicts, 2 000 `dream_runs` rows -- runs `ANALYZE`, then prints
`EXPLAIN (ANALYZE, BUFFERS)` and warm timings for three queries:

1. the run query the probe ships (`LEFT JOIN knowledge_claims c ON true`, then
   one `LATERAL ... LIMIT 1` verdict probe per claim);
2. `eligible_now`, a `count(*)` over the spec §4.1 predicate;
3. the nightly selection at cap 200 (evidence only, bounded by the step budget);
4. the spec §7.2 run query verbatim (evidence only: why (1) is not verbatim).

The gate (1) and (2): no `Seq Scan` on `knowledge_claim_verdicts`, the unique
index `uq_knowledge_claim_verdicts_idempotency` used in (1), and a median of five
warm runs under 300 ms. The script exits 1 when the gate fails, so a slower plan
cannot pass by being read too quickly.

Why (1) departs from the spec's text (measured 2026-09-30 on this seed): with a
plain `LEFT JOIN knowledge_claim_verdicts v ON v.claim_id = c.id AND ...`, the
planner prefers a hash right join over a sequential scan of the whole ledger
(about 150 ms, growing with every verdict ever written) to 10 000 unique-index
probes (about 25 ms). A plain `LATERAL` is pulled up into the same plan. The
`LIMIT 1` stops the pull-up; it drops nothing, because
`(claim_id, issuer_identity, idempotency_key)` is unique, so each claim matches at
most one verdict either way.

The seed is set-based SQL on purpose: a million ORM inserts would measure the
seeding, not the queries. Every row still goes through the insert gate trigger of
migration 055, so the claims are shaped like real ones.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.facts.nightly import ISSUER_PREFIX, KEY_PREFIX
from brain_v42.repositories.pg_claim_nightly import (
    LAST_WET_VERIFY_RUN_SQL,
    _eligible_query,
    _ranked_query,
)
from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database

PROJECTS = 10
CLAIMS = 10_000
VERDICTS = 1_000_000
DREAM_RUNS = 2_000
FACTS = 20
GATE_MS = 300.0
WARM_RUNS = 5

#: The spec §7.2 run query, verbatim except that the two literal prefixes come
#: from the constants the probe imports (spec §7.2 "Coupling"). Evidence only.
SPEC_RUN_QUERY = f"""
WITH r AS (
  SELECT id, run_date, status FROM dream_runs
   WHERE phase = 'verify' AND project_key = '*' AND phase_dry_run = false
   ORDER BY run_date DESC, id ASC LIMIT 1)
SELECT r.id, r.run_date, r.status,
       count(v.id) FILTER (WHERE v.verdict = 'holds')      AS holds,
       count(v.id) FILTER (WHERE v.verdict = 'falsified')  AS falsified,
       count(v.id) FILTER (WHERE v.verdict = 'unreadable') AS unreadable
  FROM r
  LEFT JOIN knowledge_claims c ON true
  LEFT JOIN knowledge_claim_verdicts v
         ON v.claim_id = c.id
        AND v.issuer_identity = '{ISSUER_PREFIX}' || r.id
        AND v.idempotency_key = '{KEY_PREFIX}' || to_char(r.run_date, 'YYYY-MM-DD')
 GROUP BY r.id, r.run_date, r.status
"""

#: Gate the exact statement the production repository executes.
RUN_QUERY = LAST_WET_VERIFY_RUN_SQL

SEED = [
    # Ten projects, each with its anchors.
    f"""
    INSERT INTO project_contexts (project_key, name, description)
    SELECT 'bench-' || p, 'bench-' || p, 'claim nightly benchmark'
      FROM generate_series(1, {PROJECTS}) p
    """,
    f"""
    INSERT INTO knowledge_fact_definitions
           (fact_name, definition_version, target, ttl_seconds, timeout_seconds,
            policies, value_schema, digest)
    SELECT 'bench_fact_' || f, 1, 'production', 60, 3, '{{}}', '{{"lag": "int"}}', repeat('a', 64)
      FROM generate_series(1, {FACTS}) f
    """,
    f"""
    INSERT INTO brain_entities (entity_type, entity_key, project_key, scope_kind, lifecycle)
    SELECT 'learning', 'bench-entity-' || n, 'bench-' || (1 + n % {PROJECTS}), 'project', 'active'
      FROM generate_series(1, {CLAIMS}) n
    """,
    # One claim per anchor; validity from one hour to thirty days.
    f"""
    INSERT INTO knowledge_claims
           (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name,
            definition_version, target, expected, expected_resolved, validity_seconds,
            provenance, declared_by, declared_at, recorded_at)
    SELECT e.id, 'learning', e.project_key, md5(e.entity_key) || md5(e.entity_key),
           'The benchmark lag remains below five.',
           'bench_fact_' || (1 + (hashtext(e.entity_key) & 2147483647) % {FACTS}), 1,
           'production',
           '{{"path": "/lag", "op": "lte", "value": 5}}',
           '{{"path": "/lag", "op": "lte", "value": 5}}',
           3600 * (1 + (hashtext(e.entity_key || 'v') & 2147483647) % 720),
           'declared', 'bench', now() - interval '400 days', now() - interval '400 days'
      FROM brain_entities e
     WHERE e.entity_key LIKE 'bench-entity-%'
    """,
    # Five percent retired: they keep their verdicts, and query (1) still walks them.
    """
    UPDATE knowledge_claims SET retired_at = now() - interval '1 day'
     WHERE seq % 20 = 0
    """,
    # Two thousand runs: every phase, a verify row per night since 2026 began,
    # wet and dry. Global phases use project '*', pool phases a project.
    f"""
    INSERT INTO dream_runs (run_date, phase, status, project_key, phase_dry_run)
    SELECT current_date - (n / 8),
           (ARRAY['verify','extract','sweep','promote','reorg','resonance','audit','roadmap'])
             [1 + n % 8],
           (ARRAY['done','done','done','partial','fail','timeout'])[1 + n % 6],
           CASE WHEN n % 8 IN (0, 1, 2, 7) THEN '*' ELSE 'bench-' || (1 + n % {PROJECTS}) END,
           (n % 16 = 8)
      FROM generate_series(0, {DREAM_RUNS - 1}) n
    """,
    # A hundred verdicts per claim, spread over the last 400 days, in seq order
    # of emission. Issuers mix MCP humans, robots, and nightly runs whose key
    # matches their run date or not (a key-bound count must ignore the latter).
    f"""
    INSERT INTO knowledge_claim_verdicts
           (claim_id, verdict, reason, measurement, measurement_digest, observation_id,
            issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint,
            idempotency_key, emitted_at)
    SELECT c.id,
           (ARRAY['holds','holds','holds','holds','falsified','unreadable'])[1 + (k + c.seq) % 6],
           NULL,
           '{{"fact": "bench", "definition_version": 1, "target": "production"}}',
           NULL, gen_random_uuid(),
           CASE k % 4
             WHEN 0 THEN 'mcp:bench-' || (k % 7)
             WHEN 1 THEN 'test:bench-seed'
             ELSE '{ISSUER_PREFIX}' || (1000 + k)
           END,
           CASE WHEN k % 4 = 0 THEN 'human' ELSE 'robot' END,
           repeat('a', 64), repeat('b', 64),
           CASE k % 4
             WHEN 2 THEN '{KEY_PREFIX}' || to_char(current_date - (100 - k), 'YYYY-MM-DD')
             WHEN 3 THEN 'dream-verify:v0:' || k
             ELSE 'bench-' || c.seq || '-' || k
           END,
           now() - (100 - k) * interval '4 days' - (c.seq % 3600) * interval '1 second'
      FROM (SELECT id, seq FROM knowledge_claims ORDER BY seq) c
      CROSS JOIN generate_series(1, {VERDICTS // CLAIMS}) k
     ORDER BY k, c.seq
    """,
]


def _render(query: sa.Select) -> str:
    """The SQLAlchemy selection with its bound values inlined, for EXPLAIN."""
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _seed(connection: AsyncConnection) -> dict[str, int]:
    for statement in SEED:
        await connection.execute(sa.text(statement))
    # The latest wet verify run gets verdicts under its own issuer and key, on
    # a fifth of the claims, so query (1) counts real rows, not only NULLs.
    await connection.execute(
        sa.text(
            f"""
            WITH r AS (
              SELECT id, run_date FROM dream_runs
               WHERE phase = 'verify' AND project_key = '*' AND phase_dry_run = false
               ORDER BY run_date DESC, id ASC LIMIT 1)
            INSERT INTO knowledge_claim_verdicts
                   (claim_id, verdict, reason, measurement, measurement_digest, observation_id,
                    issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint,
                    idempotency_key, emitted_at)
            SELECT c.id, (ARRAY['holds','falsified','unreadable'])[1 + c.seq % 3], NULL,
                   '{{"fact": "bench"}}', NULL, gen_random_uuid(),
                   '{ISSUER_PREFIX}' || r.id, 'robot', repeat('a', 64), repeat('b', 64),
                   '{KEY_PREFIX}' || to_char(r.run_date, 'YYYY-MM-DD'), now()
              FROM r CROSS JOIN knowledge_claims c
             WHERE c.seq % 5 = 0
            """
        )
    )
    for table in ("knowledge_claims", "knowledge_claim_verdicts", "dream_runs"):
        await connection.execute(sa.text(f"ANALYZE {table}"))
    counts = {}
    for table in ("knowledge_claims", "knowledge_claim_verdicts", "dream_runs"):
        counts[table] = int(await connection.scalar(sa.text(f"SELECT count(*) FROM {table}")))
    return counts


async def _explain(
    connection: AsyncConnection, sql: str | sa.TextClause, params: Mapping[str, str]
) -> tuple[list[str], dict[str, Any]]:
    statement = sql.text if isinstance(sql, sa.TextClause) else sql
    text_plan = (
        (await connection.execute(sa.text(f"EXPLAIN (ANALYZE, BUFFERS) {statement}"), params))
        .scalars()
        .all()
    )
    json_plan = await connection.scalar(
        sa.text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"), params
    )
    plan = json_plan if isinstance(json_plan, list) else json.loads(json_plan)
    return list(text_plan), plan[0]


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    stack, nodes = [plan["Plan"]], []
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node.get("Plans", []))
    return nodes


async def _timings(
    connection: AsyncConnection, sql: str | sa.TextClause, params: Mapping[str, str]
) -> list[float]:
    statement = sql if isinstance(sql, sa.TextClause) else sa.text(sql)
    await connection.execute(statement, params)  # warm-up, not counted
    samples = []
    for _ in range(WARM_RUNS):
        started = time.perf_counter()
        (await connection.execute(statement, params)).all()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


async def _bench(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            started = time.perf_counter()
            counts = await _seed(connection)
            print(f"seed: {counts} in {time.perf_counter() - started:.1f} s")

        now = datetime.now(UTC)
        eligible_now = _render(
            sa.select(sa.func.count()).select_from(_eligible_query(now).subquery("eligible"))
        )
        async with engine.connect() as connection:
            names = sorted(
                (
                    await connection.execute(
                        sa.text("SELECT fact_name FROM knowledge_fact_definitions")
                    )
                ).scalars()
            )
        selection = _render(_ranked_query(now, 200, eligible_fact_names=names))

        queries = [
            (
                "(1) run query",
                RUN_QUERY,
                True,
                True,
                {"issuer_prefix": ISSUER_PREFIX, "key_prefix": KEY_PREFIX},
            ),
            ("(2) eligible_now", eligible_now, True, False, {}),
            ("(3) selection at cap 200", selection, False, False, {}),
            ("(4) spec run query, verbatim", SPEC_RUN_QUERY, False, True, {}),
        ]
        passed = True
        async with engine.connect() as connection:
            for label, sql, gated, needs_unique, params in queries:
                text_plan, plan = await _explain(connection, sql, params)
                samples = await _timings(connection, sql, params)
                nodes = _nodes(plan)
                seq_scans = [
                    node
                    for node in nodes
                    if node["Node Type"] == "Seq Scan"
                    and node.get("Relation Name") == "knowledge_claim_verdicts"
                ]
                uses_unique = any(
                    node.get("Index Name") == "uq_knowledge_claim_verdicts_idempotency"
                    for node in nodes
                )
                median = statistics.median(samples)
                print(
                    f"\n=== {label} ===\n{(sql.text if isinstance(sql, sa.TextClause) else sql).strip()}\n"
                )
                print("\n".join(text_plan))
                print(
                    f"\nwarm runs (ms): {', '.join(f'{s:.1f}' for s in samples)}"
                    f" | median {median:.1f} | worst {max(samples):.1f}"
                )
                print(
                    f"seq scan on knowledge_claim_verdicts: {bool(seq_scans)}"
                    f" | uq_knowledge_claim_verdicts_idempotency used: {uses_unique}"
                )
                if gated:
                    ok = not seq_scans and median < GATE_MS and (uses_unique or not needs_unique)
                    print(f"gate: {'PASS' if ok else 'FAIL'}")
                    passed = passed and ok
        return passed
    finally:
        await engine.dispose()


def main() -> int:
    with fresh_head_database(_get_integration_db_url_or_skip(), prefix="brain_claimbench") as url:
        passed = asyncio.run(_bench(url))
    print(f"\nGATE: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
