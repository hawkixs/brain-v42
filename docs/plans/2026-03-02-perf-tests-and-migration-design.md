# Design: Performance Tests & Data Migration

**Date**: 2026-03-02
**Status**: Approved
**Scope**: brain_v42 — post-M5 (production-ready)

## Context

brain_v42 is production-ready (M1-M5 done, 800 tests, 95% coverage). Two tasks remain:
1. Validate the performance of the new system (baseline, comparison with the old one, load testing, search quality)
2. Migrate data from the old brain (Neo4j/datalake_v2) to the new one (PostgreSQL/brain_v42)

## Task 1: Benchmark script

### File

`scripts/benchmark.py` — standalone script with 4 modes.

### Mode: baseline

Measures the latency of each brain-v42 operation on PostgreSQL:
- **CRUD**: create, get_by_id, update, list_all, delete × 6 entity types (decisions, learnings, snippets, runbooks, adrs, project_contexts)
- **FTS**: full-text search via tsvector
- **Semantic search**: vector search via pgvector HNSW
- **Global search**: brain_service.search() and what_do_i_know_about()
- **Embedding**: ONNX generation time (all-MiniLM-L6-v2)

Metrics per operation: min, avg, p50, p95, p99, max latency.

### Mode: compare

Comparative benchmark old (Neo4j) vs new (PostgreSQL):
- Same operations run on both backends
- Old: direct Neo4j call via neo4j-driver
- New: direct PG repo call via SQLAlchemy async
- Output: side-by-side table with performance ratio

### Mode: load

Load test with concurrent workers:
- asyncio.gather() with N workers
- Operation mix: 70% read, 20% search, 10% write
- Parameters: --concurrency (default 10), --duration (default 30s)
- Metrics: throughput (ops/sec), p50, p95, p99, error rate

### Mode: quality

Search quality validation:
- **Precision test**: known queries with expected results, measures precision@5
- **Noise test**: vague queries, checks that the number of results stays reasonable and that similarity scores decay properly
- **FTS vs Semantic**: same query through both engines, compares overlap and ranking
- **Cutoff recommendation**: suggests an optimal cosine distance threshold
- Metrics: precision@5, MRR (Mean Reciprocal Rank), noise ratio

### CLI

```bash
# Baseline brain-v42
python scripts/benchmark.py --mode baseline

# Comparative old vs new
python scripts/benchmark.py --mode compare --neo4j-uri bolt://localhost:7687

# Load test
python scripts/benchmark.py --mode load --concurrency 10 --duration 30

# Quality check
python scripts/benchmark.py --mode quality

# All modes
python scripts/benchmark.py --mode all --neo4j-uri bolt://localhost:7687
```

### Output

- Console: formatted tables (tabulate)
- File: `results/<timestamp>_benchmark.json`

### Dependencies

- `tabulate` (console formatting)
- `neo4j` (driver for compare mode)

## Task 2: Data migration

### Existing script

`scripts/migrate_neo4j_to_pg.py` (feature 640, already merged).

### Data volume (old brain)

| Type | Count |
|------|-------|
| Decisions | ~75 |
| Learnings | ~100 |
| Snippets | ~20 |
| Runbooks | 4 |
| ADRs | 1 |
| Project Contexts | ~15 |
| **Total** | **~215** |

### Execution plan

#### 1. Pre-migration
- Verify brain_v42 PG is up (port 5433)
- Verify Neo4j is up (port 7687)
- Backup PG: `pg_dump`
- Dry-run: `python scripts/migrate_neo4j_to_pg.py --dry-run`

#### 2. Migration
- Run with `--regen-embeddings` (sentence-transformers 768d → ONNX 384d)
- Batch size: 50 (default)
- ON CONFLICT DO NOTHING (idempotent)
- UUIDs preserved

#### 3. Post-migration validation
- Count by type: old vs new (must match)
- Spot-check: 3-5 entries per type
- Verify non-null embeddings
- Test semantic search on migrated data

#### 4. Cutover
- Disable mcp-brain in the global MCP config
- Keep only brain-v42
- Update the project_context
- Optional: stop datalake_v2 containers

### Points of attention

- **Incompatible embeddings**: The old one uses sentence-transformers (768 dims), the new one uses ONNX all-MiniLM-L6-v2 (384 dims). `--regen-embeddings` is mandatory.
- **Idempotent**: ON CONFLICT DO NOTHING allows a safe re-run.
- **UUIDs preserved**: Cross-references remain valid.

## Execution order

1. Write the benchmark script
2. Run the migration (dry-run then real)
3. Run the benchmarks (baseline on real data)
4. Run the quality check
5. Run compare (old vs new)
6. Full cutover to brain-v42
