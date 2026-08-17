# Perf Tests & Data Migration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a standalone benchmark script (baseline, compare, load, quality modes) and execute the Neo4j→PG data migration with validation and switchover.

**Architecture:** Single `scripts/benchmark.py` with 4 modes using direct repo/service access (no MCP layer). Migration uses existing `scripts/migrate_neo4j_to_pg.py` with validation steps.

**Tech Stack:** Python 3.12, asyncio, tabulate, neo4j-driver, SQLAlchemy async, ONNX Runtime, structlog

---

### Task 1: Add benchmark dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add tabulate and neo4j to dev dependencies**

In `pyproject.toml`, add `tabulate` and `neo4j` to the `[dependency-groups] dev` list:

```toml
[dependency-groups]
dev = [
    "mypy>=1.19.1",
    "pytest>=9.0.2",
    "pytest-asyncio>=1.3.0",
    "pytest-cov>=7.0.0",
    "ruff>=0.15.4",
    "tabulate>=0.9",
    "neo4j>=5.0",
]
```

**Step 2: Install dependencies**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && uv sync`
Expected: Dependencies installed successfully.

**Step 3: Create results directory**

Run: `mkdir -p /home/hawixs/hawkixs_infra/git_repo/brain_v42/results && echo "results/" >> /home/hawixs/hawkixs_infra/git_repo/brain_v42/.gitignore`

**Step 4: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "chore: add benchmark dependencies (tabulate, neo4j)"
```

---

### Task 2: Benchmark scaffold — CLI parsing and shared utilities

**Files:**
- Create: `scripts/benchmark.py`

**Step 1: Write the benchmark scaffold**

```python
#!/usr/bin/env python3
"""benchmark.py — Performance & quality benchmark for brain_v42.

Usage:
    python scripts/benchmark.py --mode baseline
    python scripts/benchmark.py --mode compare --neo4j-uri bolt://localhost:7688
    python scripts/benchmark.py --mode load --concurrency 10 --duration 30
    python scripts/benchmark.py --mode quality
    python scripts/benchmark.py --mode all --neo4j-uri bolt://localhost:7688
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from tabulate import tabulate

# ─── Setup ────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger("benchmark")

# Default PG URL
DEFAULT_PG_URL = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
DEFAULT_NEO4J_URI = "bolt://localhost:7688"
DEFAULT_NEO4J_USER = "neo4j"


# ─── Timing utilities ────────────────────────────────────────────────────────


class Timer:
    """Simple context manager for timing operations."""

    def __init__(self) -> None:
        self.start: float = 0
        self.end: float = 0
        self.elapsed_ms: float = 0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.end = time.perf_counter()
        self.elapsed_ms = (self.end - self.start) * 1000


def compute_stats(times_ms: list[float]) -> dict[str, float]:
    """Compute latency statistics from a list of ms values."""
    if not times_ms:
        return {"min": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}
    sorted_t = sorted(times_ms)
    n = len(sorted_t)
    return {
        "min": round(sorted_t[0], 2),
        "avg": round(statistics.mean(sorted_t), 2),
        "p50": round(sorted_t[n // 2], 2),
        "p95": round(sorted_t[int(n * 0.95)], 2) if n >= 20 else round(sorted_t[-1], 2),
        "p99": round(sorted_t[int(n * 0.99)], 2) if n >= 100 else round(sorted_t[-1], 2),
        "max": round(sorted_t[-1], 2),
        "count": n,
    }


# ─── brain_v42 service factory ───────────────────────────────────────────────


def setup_pg_services(pg_url: str) -> dict[str, Any]:
    """Initialize brain_v42 services for benchmarking (repos + services + embedding)."""
    os.environ["POSTGRES_URL"] = pg_url

    from brain_v42.db.engine import get_session_factory
    from brain_v42.repositories.pg_adr import PgADRRepo
    from brain_v42.repositories.pg_decision import PgDecisionRepo
    from brain_v42.repositories.pg_learning import PgLearningRepo
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo
    from brain_v42.repositories.pg_runbook import PgRunbookRepo
    from brain_v42.repositories.pg_snippet import PgSnippetRepo
    from brain_v42.services.adr_service import ADRService
    from brain_v42.services.brain_service import BrainService
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.embedding_service import EmbeddingService
    from brain_v42.services.learning_service import LearningService
    from brain_v42.services.project_context_service import ProjectContextService
    from brain_v42.services.runbook_service import RunbookService
    from brain_v42.services.snippet_service import SnippetService

    session_factory = get_session_factory()

    # Repos
    decision_repo = PgDecisionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)
    snippet_repo = PgSnippetRepo(session_factory)
    runbook_repo = PgRunbookRepo(session_factory)
    adr_repo = PgADRRepo(session_factory)
    project_context_repo = PgProjectContextRepo(session_factory)

    # Embedding
    embedding_svc = EmbeddingService()

    # Async embedding wrapper (services expect async embed())
    class AsyncEmbedding:
        def __init__(self, svc: EmbeddingService) -> None:
            self._svc = svc

        async def embed(self, text: str) -> list[float]:
            return self._svc.embed_text(text)

    async_embedding = AsyncEmbedding(embedding_svc)

    # Services
    decision_svc = DecisionService(decision_repo, async_embedding)
    learning_svc = LearningService(learning_repo, async_embedding)
    snippet_svc = SnippetService(snippet_repo, async_embedding)
    runbook_svc = RunbookService(runbook_repo, async_embedding)
    adr_svc = ADRService(adr_repo, async_embedding)
    project_context_svc = ProjectContextService(project_context_repo)
    brain_svc = BrainService(
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc, embedding_svc
    )

    return {
        "session_factory": session_factory,
        "embedding_svc": embedding_svc,
        "decision_svc": decision_svc,
        "learning_svc": learning_svc,
        "snippet_svc": snippet_svc,
        "runbook_svc": runbook_svc,
        "adr_svc": adr_svc,
        "project_context_svc": project_context_svc,
        "brain_svc": brain_svc,
        # Repos for direct access
        "decision_repo": decision_repo,
        "learning_repo": learning_repo,
        "snippet_repo": snippet_repo,
        "runbook_repo": runbook_repo,
        "adr_repo": adr_repo,
        "project_context_repo": project_context_repo,
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="brain_v42 benchmark suite")
    parser.add_argument(
        "--mode",
        choices=["baseline", "compare", "load", "quality", "all"],
        required=True,
        help="Benchmark mode",
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help=f"PostgreSQL URL (default: POSTGRES_URL env or {DEFAULT_PG_URL})",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=DEFAULT_NEO4J_URI,
        help=f"Neo4j bolt URI for compare mode (default: {DEFAULT_NEO4J_URI})",
    )
    parser.add_argument("--neo4j-user", default=DEFAULT_NEO4J_USER)
    parser.add_argument("--neo4j-password", default=None, help="Neo4j password (for compare mode)")
    parser.add_argument("--concurrency", type=int, default=10, help="Load test concurrency")
    parser.add_argument("--duration", type=int, default=30, help="Load test duration (seconds)")
    parser.add_argument("--iterations", type=int, default=50, help="Baseline iterations per op")
    parser.add_argument("--output", default=None, help="JSON output path (auto-generated if empty)")
    return parser.parse_args()


def save_results(results: dict[str, Any], output_path: str | None) -> str:
    """Save benchmark results to JSON. Returns the path used."""
    if output_path is None:
        results_dir = Path(__file__).parent.parent / "results"
        results_dir.mkdir(exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = str(results_dir / f"{ts}_benchmark.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    return output_path


# ─── Mode stubs (filled in subsequent tasks) ─────────────────────────────────


async def run_baseline(services: dict[str, Any], iterations: int) -> dict[str, Any]:
    """Baseline latency benchmark."""
    raise NotImplementedError("Task 3")


async def run_compare(
    services: dict[str, Any],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
) -> dict[str, Any]:
    """Comparative benchmark: Neo4j vs PostgreSQL."""
    raise NotImplementedError("Task 4")


async def run_load(
    services: dict[str, Any], concurrency: int, duration: int
) -> dict[str, Any]:
    """Load test with concurrent workers."""
    raise NotImplementedError("Task 5")


async def run_quality(services: dict[str, Any]) -> dict[str, Any]:
    """Search quality validation."""
    raise NotImplementedError("Task 6")


# ─── Main ─────────────────────────────────────────────────────────────────────


async def main() -> int:
    args = parse_args()
    pg_url = args.postgres_url or os.environ.get("POSTGRES_URL", DEFAULT_PG_URL)

    logger.info("benchmark.start", mode=args.mode, pg_url=pg_url)

    services = setup_pg_services(pg_url)
    all_results: dict[str, Any] = {"mode": args.mode, "timestamp": datetime.now(UTC).isoformat()}

    modes = (
        ["baseline", "compare", "load", "quality"] if args.mode == "all" else [args.mode]
    )

    for mode in modes:
        logger.info("benchmark.mode_start", mode=mode)
        if mode == "baseline":
            all_results["baseline"] = await run_baseline(services, args.iterations)
        elif mode == "compare":
            if not args.neo4j_password:
                logger.error("compare mode requires --neo4j-password")
                return 1
            all_results["compare"] = await run_compare(
                services, args.neo4j_uri, args.neo4j_user, args.neo4j_password
            )
        elif mode == "load":
            all_results["load"] = await run_load(services, args.concurrency, args.duration)
        elif mode == "quality":
            all_results["quality"] = await run_quality(services)

    output_path = save_results(all_results, args.output)
    logger.info("benchmark.done", output=output_path)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

**Step 2: Verify scaffold runs**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode baseline 2>&1 | head -5`
Expected: Should fail with NotImplementedError("Task 3") — confirming the scaffold works.

**Step 3: Commit**

```bash
git add scripts/benchmark.py
git commit -m "feat: benchmark script scaffold with CLI and utilities"
```

---

### Task 3: Implement baseline mode

**Files:**
- Modify: `scripts/benchmark.py` — replace `run_baseline()` stub

**Step 1: Implement run_baseline()**

Replace the `run_baseline` function with:

```python
async def run_baseline(services: dict[str, Any], iterations: int) -> dict[str, Any]:
    """Baseline latency benchmark on brain_v42 (PostgreSQL).

    Tests: embedding, list, FTS search, semantic search, global search.
    All on real data already in the DB.
    """
    results: dict[str, Any] = {}
    embedding_svc = services["embedding_svc"]

    # 1. Embedding generation latency
    logger.info("baseline.embedding")
    embed_times: list[float] = []
    test_texts = [
        "PostgreSQL pgvector HNSW index configuration",
        "Python async SQLAlchemy session management",
        "ONNX Runtime inference optimization",
        "MCP tool registration pattern",
        "Docker container networking and port mapping",
    ]
    for text in test_texts * (iterations // 5 or 1):
        with Timer() as t:
            embedding_svc.embed_text(text)
        embed_times.append(t.elapsed_ms)
    results["embedding"] = compute_stats(embed_times)

    # 2. List operations (per entity type)
    entity_services = {
        "decision": services["decision_svc"],
        "learning": services["learning_svc"],
        "snippet": services["snippet_svc"],
        "runbook": services["runbook_svc"],
        "adr": services["adr_svc"],
    }

    for name, svc in entity_services.items():
        logger.info("baseline.list", entity=name)
        list_times: list[float] = []
        for _ in range(iterations):
            with Timer() as t:
                await svc.list_all(limit=20)
            list_times.append(t.elapsed_ms)
        results[f"list_{name}"] = compute_stats(list_times)

    # 3. FTS search
    search_queries = ["PostgreSQL", "Docker", "MCP", "test", "deploy"]
    for name, svc in entity_services.items():
        logger.info("baseline.fts", entity=name)
        fts_times: list[float] = []
        for q in search_queries * (iterations // 5 or 1):
            with Timer() as t:
                await svc.search(q, limit=10)
            fts_times.append(t.elapsed_ms)
        results[f"fts_{name}"] = compute_stats(fts_times)

    # 4. Semantic search
    for name, svc in entity_services.items():
        logger.info("baseline.semantic", entity=name)
        sem_times: list[float] = []
        for q in search_queries * (iterations // 5 or 1):
            with Timer() as t:
                await svc.semantic_search(q, limit=10)
            sem_times.append(t.elapsed_ms)
        results[f"semantic_{name}"] = compute_stats(sem_times)

    # 5. Global search (BrainService)
    brain_svc = services["brain_svc"]
    logger.info("baseline.global_search")
    global_times: list[float] = []
    for q in search_queries * (iterations // 5 or 1):
        with Timer() as t:
            await brain_svc.search(q, limit=20)
        global_times.append(t.elapsed_ms)
    results["global_search"] = compute_stats(global_times)

    # 6. what_do_i_know_about
    logger.info("baseline.what_do_i_know")
    wdik_times: list[float] = []
    for q in search_queries * (iterations // 5 or 1):
        with Timer() as t:
            await brain_svc.what_do_i_know_about(q, limit=10)
        wdik_times.append(t.elapsed_ms)
    results["what_do_i_know"] = compute_stats(wdik_times)

    # Print table
    table_data = []
    for op, stats in results.items():
        table_data.append([
            op, stats["count"], stats["min"], stats["avg"],
            stats["p50"], stats["p95"], stats["max"],
        ])
    print("\n=== BASELINE RESULTS (ms) ===")
    print(tabulate(
        table_data,
        headers=["Operation", "N", "Min", "Avg", "P50", "P95", "Max"],
        tablefmt="grid",
    ))

    return results
```

**Step 2: Run baseline**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode baseline --iterations 10`
Expected: Table with latency stats for each operation.

**Step 3: Commit**

```bash
git add scripts/benchmark.py
git commit -m "feat: benchmark baseline mode — latency per operation"
```

---

### Task 4: Implement compare mode

**Files:**
- Modify: `scripts/benchmark.py` — replace `run_compare()` stub

**Step 1: Implement run_compare()**

Replace the `run_compare` function with:

```python
async def run_compare(
    services: dict[str, Any],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
) -> dict[str, Any]:
    """Comparative benchmark: Neo4j (old brain) vs PostgreSQL (brain_v42).

    Compares: list, FTS-equivalent, count operations.
    Neo4j uses Cypher queries directly. PG uses repo methods.
    """
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    results: dict[str, Any] = {}
    iterations = 20

    entity_map = {
        "Decision": ("decision_svc", "decisions"),
        "Learning": ("learning_svc", "learnings"),
        "Snippet": ("snippet_svc", "snippets"),
        "Runbook": ("runbook_svc", "runbooks"),
        "ADR": ("adr_svc", "adrs"),
    }

    search_queries = ["PostgreSQL", "Docker", "deploy"]

    try:
        for neo4j_label, (svc_key, _table) in entity_map.items():
            svc = services[svc_key]
            op_key = neo4j_label.lower()

            # --- List: Neo4j ---
            logger.info("compare.list_neo4j", entity=neo4j_label)
            neo4j_list_times: list[float] = []
            for _ in range(iterations):
                with Timer() as t:
                    async with driver.session() as session:
                        result = await session.run(
                            f"MATCH (n:{neo4j_label}) RETURN n LIMIT 20"
                        )
                        await result.data()
                neo4j_list_times.append(t.elapsed_ms)

            # --- List: PG ---
            logger.info("compare.list_pg", entity=neo4j_label)
            pg_list_times: list[float] = []
            for _ in range(iterations):
                with Timer() as t:
                    await svc.list_all(limit=20)
                pg_list_times.append(t.elapsed_ms)

            # --- FTS: Neo4j (CONTAINS) ---
            logger.info("compare.search_neo4j", entity=neo4j_label)
            neo4j_search_times: list[float] = []
            for q in search_queries * (iterations // 3 or 1):
                with Timer() as t:
                    async with driver.session() as session:
                        result = await session.run(
                            f"MATCH (n:{neo4j_label}) "
                            f"WHERE n.title CONTAINS $q OR n.description CONTAINS $q "
                            f"RETURN n LIMIT 10",
                            q=q,
                        )
                        await result.data()
                neo4j_search_times.append(t.elapsed_ms)

            # --- FTS: PG (tsvector) ---
            logger.info("compare.search_pg", entity=neo4j_label)
            pg_search_times: list[float] = []
            for q in search_queries * (iterations // 3 or 1):
                with Timer() as t:
                    await svc.search(q, limit=10)
                pg_search_times.append(t.elapsed_ms)

            neo4j_list_stats = compute_stats(neo4j_list_times)
            pg_list_stats = compute_stats(pg_list_times)
            neo4j_search_stats = compute_stats(neo4j_search_times)
            pg_search_stats = compute_stats(pg_search_times)

            results[op_key] = {
                "list_neo4j": neo4j_list_stats,
                "list_pg": pg_list_stats,
                "list_speedup": (
                    round(neo4j_list_stats["avg"] / pg_list_stats["avg"], 1)
                    if pg_list_stats["avg"] > 0 else 0
                ),
                "search_neo4j": neo4j_search_stats,
                "search_pg": pg_search_stats,
                "search_speedup": (
                    round(neo4j_search_stats["avg"] / pg_search_stats["avg"], 1)
                    if pg_search_stats["avg"] > 0 else 0
                ),
            }

        # Print comparison table
        table_data = []
        for entity, data in results.items():
            table_data.append([
                f"{entity} list",
                f"{data['list_neo4j']['avg']:.1f}",
                f"{data['list_pg']['avg']:.1f}",
                f"{data['list_speedup']}x",
            ])
            table_data.append([
                f"{entity} search",
                f"{data['search_neo4j']['avg']:.1f}",
                f"{data['search_pg']['avg']:.1f}",
                f"{data['search_speedup']}x",
            ])

        print("\n=== COMPARE: Neo4j vs PostgreSQL (avg ms) ===")
        print(tabulate(
            table_data,
            headers=["Operation", "Neo4j (ms)", "PG (ms)", "Speedup"],
            tablefmt="grid",
        ))

    finally:
        await driver.close()

    return results
```

**Step 2: Run compare mode**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode compare --neo4j-password <PASSWORD>`

Note: Get the Neo4j password from the datalake_v2 docker-compose or .env file.

Expected: Side-by-side comparison table with speedup ratios.

**Step 3: Commit**

```bash
git add scripts/benchmark.py
git commit -m "feat: benchmark compare mode — Neo4j vs PG side-by-side"
```

---

### Task 5: Implement load mode

**Files:**
- Modify: `scripts/benchmark.py` — replace `run_load()` stub

**Step 1: Implement run_load()**

Replace the `run_load` function with:

```python
async def run_load(
    services: dict[str, Any], concurrency: int, duration: int
) -> dict[str, Any]:
    """Load test: concurrent operations for a fixed duration.

    Mix: 70% reads (list), 20% search (FTS), 10% writes (not implemented — read-only).
    """
    import random

    brain_svc = services["brain_svc"]
    entity_services = [
        services["decision_svc"],
        services["learning_svc"],
        services["snippet_svc"],
        services["runbook_svc"],
        services["adr_svc"],
    ]
    queries = ["PostgreSQL", "Docker", "MCP", "Python", "async", "deploy", "test"]

    total_ops = 0
    errors = 0
    all_times: list[float] = []
    stop_event = asyncio.Event()

    async def worker(worker_id: int) -> tuple[int, int, list[float]]:
        """Single worker: loop until stop_event, pick random operation."""
        local_ops = 0
        local_errors = 0
        local_times: list[float] = []

        while not stop_event.is_set():
            roll = random.random()
            try:
                with Timer() as t:
                    if roll < 0.70:
                        # List operation
                        svc = random.choice(entity_services)
                        await svc.list_all(limit=20)
                    elif roll < 0.90:
                        # FTS search
                        svc = random.choice(entity_services)
                        await svc.search(random.choice(queries), limit=10)
                    else:
                        # Global search
                        await brain_svc.search(random.choice(queries), limit=10)
                local_times.append(t.elapsed_ms)
                local_ops += 1
            except Exception:
                local_errors += 1

        return local_ops, local_errors, local_times

    # Launch workers
    logger.info("load.start", concurrency=concurrency, duration=duration)
    worker_tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]

    # Wait for duration
    await asyncio.sleep(duration)
    stop_event.set()

    # Collect results
    for ops, errs, times in await asyncio.gather(*worker_tasks):
        total_ops += ops
        errors += errs
        all_times.extend(times)

    throughput = total_ops / duration if duration > 0 else 0
    stats = compute_stats(all_times)

    results = {
        "concurrency": concurrency,
        "duration_s": duration,
        "total_ops": total_ops,
        "errors": errors,
        "throughput_ops_per_sec": round(throughput, 1),
        "latency": stats,
    }

    print(f"\n=== LOAD TEST ({concurrency} workers, {duration}s) ===")
    print(tabulate(
        [
            ["Total Operations", total_ops],
            ["Errors", errors],
            ["Throughput", f"{throughput:.1f} ops/sec"],
            ["Avg Latency", f"{stats['avg']:.1f} ms"],
            ["P50 Latency", f"{stats['p50']:.1f} ms"],
            ["P95 Latency", f"{stats['p95']:.1f} ms"],
            ["P99 Latency", f"{stats['p99']:.1f} ms"],
            ["Max Latency", f"{stats['max']:.1f} ms"],
        ],
        tablefmt="grid",
    ))

    return results
```

**Step 2: Run load test (short)**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode load --concurrency 5 --duration 10`
Expected: Load test summary with throughput and latency percentiles.

**Step 3: Commit**

```bash
git add scripts/benchmark.py
git commit -m "feat: benchmark load mode — concurrent workers with latency percentiles"
```

---

### Task 6: Implement quality mode

**Files:**
- Modify: `scripts/benchmark.py` — replace `run_quality()` stub

**Step 1: Implement run_quality()**

Replace the `run_quality` function with:

```python
async def run_quality(services: dict[str, Any]) -> dict[str, Any]:
    """Search quality validation: precision, noise, FTS vs semantic comparison.

    Uses real data in the DB. Defines test queries with expected keywords
    that should appear in relevant results.
    """
    brain_svc = services["brain_svc"]
    results: dict[str, Any] = {}

    # ── 1. Precision test ────────────────────────────────────────────────
    # Queries with expected keywords that SHOULD appear in top-5 results
    precision_queries = [
        {
            "query": "pgvector HNSW index",
            "expected_keywords": ["pgvector", "hnsw", "vector", "index", "embedding"],
        },
        {
            "query": "Docker deployment container",
            "expected_keywords": ["docker", "container", "deploy", "compose"],
        },
        {
            "query": "Neo4j migration PostgreSQL",
            "expected_keywords": ["neo4j", "migration", "postgres", "migrate"],
        },
        {
            "query": "MCP tool registration",
            "expected_keywords": ["mcp", "tool", "register", "server"],
        },
        {
            "query": "Python async SQLAlchemy",
            "expected_keywords": ["python", "async", "sqlalchemy", "session"],
        },
    ]

    precision_results: list[dict[str, Any]] = []
    for pq in precision_queries:
        response = await brain_svc.search(pq["query"], limit=5)
        hits = 0
        result_summaries: list[dict[str, Any]] = []
        for r in response.results:
            item_text = json.dumps(r.item).lower()
            matched = any(kw in item_text for kw in pq["expected_keywords"])
            if matched:
                hits += 1
            result_summaries.append({
                "type": r.type,
                "score": round(r.score, 4),
                "relevant": matched,
                "title": r.item.get("title", r.item.get("topic", "?"))[:60],
            })
        precision = hits / max(len(response.results), 1)
        precision_results.append({
            "query": pq["query"],
            "results_count": len(response.results),
            "relevant_count": hits,
            "precision_at_5": round(precision, 2),
            "results": result_summaries,
        })

    avg_precision = (
        statistics.mean(p["precision_at_5"] for p in precision_results)
        if precision_results else 0
    )
    results["precision"] = {
        "queries": precision_results,
        "avg_precision_at_5": round(avg_precision, 2),
    }

    # ── 2. Noise test ────────────────────────────────────────────────────
    # Vague queries — check that scores decay properly and no flood of results
    noise_queries = ["test", "code", "config", "data", "error"]
    noise_results: list[dict[str, Any]] = []

    for q in noise_queries:
        response = await brain_svc.search(q, limit=20)
        scores = [r.score for r in response.results]
        low_score_count = sum(1 for s in scores if s < 0.3)
        noise_results.append({
            "query": q,
            "total_results": len(response.results),
            "scores_range": (
                f"{min(scores):.3f} - {max(scores):.3f}" if scores else "N/A"
            ),
            "low_score_count": low_score_count,
            "noise_ratio": (
                round(low_score_count / max(len(scores), 1), 2) if scores else 0
            ),
        })

    results["noise"] = {"queries": noise_results}

    # ── 3. FTS vs Semantic comparison ────────────────────────────────────
    comparison_queries = ["PostgreSQL", "deployment", "Python testing"]
    fts_vs_sem: list[dict[str, Any]] = []

    entity_services = {
        "decision": services["decision_svc"],
        "learning": services["learning_svc"],
        "snippet": services["snippet_svc"],
    }

    for q in comparison_queries:
        for name, svc in entity_services.items():
            fts_results_raw = await svc.search(q, limit=10)
            sem_results_raw = await svc.semantic_search(q, limit=10)

            # Extract IDs for overlap comparison
            fts_ids = set()
            for item in fts_results_raw:
                if hasattr(item, "id"):
                    fts_ids.add(str(item.id))
                elif isinstance(item, dict):
                    fts_ids.add(str(item.get("id")))

            sem_ids = set()
            sem_scores: list[float] = []
            for item, score in sem_results_raw:
                if hasattr(item, "id"):
                    sem_ids.add(str(item.id))
                elif isinstance(item, dict):
                    sem_ids.add(str(item.get("id")))
                sem_scores.append(score)

            overlap = len(fts_ids & sem_ids)
            fts_vs_sem.append({
                "query": q,
                "entity": name,
                "fts_count": len(fts_results_raw),
                "semantic_count": len(sem_results_raw),
                "overlap": overlap,
                "semantic_score_range": (
                    f"{min(sem_scores):.3f} - {max(sem_scores):.3f}"
                    if sem_scores else "N/A"
                ),
            })

    results["fts_vs_semantic"] = fts_vs_sem

    # ── 4. Cutoff recommendation ─────────────────────────────────────────
    # Collect all semantic scores to find natural cutoff
    all_scores: list[float] = []
    for pq in precision_results:
        for r in pq["results"]:
            all_scores.append(r["score"])
    for nr in noise_results:
        # Re-run to get scores (we already have them in noise_results conceptually)
        response = await brain_svc.search(nr["query"], limit=20)
        for r in response.results:
            all_scores.append(r.score)

    if all_scores:
        sorted_scores = sorted(all_scores, reverse=True)
        p25 = sorted_scores[int(len(sorted_scores) * 0.25)]
        p50 = sorted_scores[int(len(sorted_scores) * 0.50)]
        p75 = sorted_scores[int(len(sorted_scores) * 0.75)]
        results["cutoff_recommendation"] = {
            "strict": round(p25, 3),
            "moderate": round(p50, 3),
            "permissive": round(p75, 3),
            "note": "strict=top 25% only, moderate=top 50%, permissive=top 75%",
        }
    else:
        results["cutoff_recommendation"] = {"note": "No scores collected"}

    # ── Print summary ────────────────────────────────────────────────────
    print("\n=== QUALITY: Precision ===")
    prec_table = [
        [p["query"][:40], p["results_count"], p["relevant_count"], p["precision_at_5"]]
        for p in precision_results
    ]
    print(tabulate(
        prec_table,
        headers=["Query", "Results", "Relevant", "P@5"],
        tablefmt="grid",
    ))
    print(f"\nAverage Precision@5: {avg_precision:.2f}")

    print("\n=== QUALITY: Noise ===")
    noise_table = [
        [n["query"], n["total_results"], n["scores_range"], n["noise_ratio"]]
        for n in noise_results
    ]
    print(tabulate(
        noise_table,
        headers=["Query", "Results", "Score Range", "Noise Ratio"],
        tablefmt="grid",
    ))

    print("\n=== QUALITY: FTS vs Semantic ===")
    fts_table = [
        [f["query"][:20], f["entity"], f["fts_count"], f["semantic_count"],
         f["overlap"], f["semantic_score_range"]]
        for f in fts_vs_sem
    ]
    print(tabulate(
        fts_table,
        headers=["Query", "Entity", "FTS", "Semantic", "Overlap", "Score Range"],
        tablefmt="grid",
    ))

    if "cutoff_recommendation" in results and "strict" in results["cutoff_recommendation"]:
        cr = results["cutoff_recommendation"]
        print(f"\n=== CUTOFF RECOMMENDATION ===")
        print(f"  Strict  (top 25%): >= {cr['strict']}")
        print(f"  Moderate (top 50%): >= {cr['moderate']}")
        print(f"  Permissive (top 75%): >= {cr['permissive']}")

    return results
```

**Step 2: Run quality check**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode quality`
Expected: Precision, noise, and FTS vs semantic comparison tables.

**Step 3: Commit**

```bash
git add scripts/benchmark.py
git commit -m "feat: benchmark quality mode — precision, noise, FTS vs semantic"
```

---

### Task 7: Run migration — dry-run

**Files:**
- None (existing script)

**Step 1: Check Neo4j password**

Run: `grep -r NEO4J_PASSWORD /home/hawixs/hawkixs_infra/git_repo/datalake_v2/.env 2>/dev/null || grep -r NEO4J /home/hawixs/hawkixs_infra/git_repo/datalake_v2/docker-compose*.yml 2>/dev/null | head -5`

Note: The Neo4j password is needed for the migration. Check datalake_v2 config files.

**Step 2: Install neo4j driver in brain_v42 venv**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && uv pip install neo4j>=5.0`

**Step 3: Dry-run migration**

Run:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
python scripts/migrate_neo4j_to_pg.py \
    --neo4j-uri bolt://localhost:7688 \
    --neo4j-password <PASSWORD> \
    --postgres-url postgresql+asyncpg://brain:brain@localhost:5433/brain \
    --dry-run
```

Expected: Summary table showing count per entity type (Decision ~75, Learning ~100, Snippet ~20, Runbook ~4, ADR ~1, ProjectContext ~15). No data written.

**Step 4: Verify brain_v42 PG is empty (or count existing rows)**

Run:
```bash
PGPASSWORD=brain psql -h localhost -p 5433 -U brain -d brain -c "
SELECT 'decisions' AS t, COUNT(*) FROM decisions
UNION ALL SELECT 'learnings', COUNT(*) FROM learnings
UNION ALL SELECT 'snippets', COUNT(*) FROM snippets
UNION ALL SELECT 'runbooks', COUNT(*) FROM runbooks
UNION ALL SELECT 'adrs', COUNT(*) FROM adrs
UNION ALL SELECT 'project_contexts', COUNT(*) FROM project_contexts;
"
```

Expected: All tables should have 0 rows (or some if tests left data behind).

---

### Task 8: Run migration — real execution

**Files:**
- None (existing script)

**Step 1: Backup PG**

Run:
```bash
PGPASSWORD=brain pg_dump -h localhost -p 5433 -U brain brain | gzip > /home/hawixs/backups/brain_v42_pre_migration_$(date +%Y%m%d_%H%M%S).sql.gz
```

**Step 2: Run migration with --regen-embeddings**

Run:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
python scripts/migrate_neo4j_to_pg.py \
    --neo4j-uri bolt://localhost:7688 \
    --neo4j-password <PASSWORD> \
    --postgres-url postgresql+asyncpg://brain:brain@localhost:5433/brain \
    --regen-embeddings
```

Expected: Migration summary with ~215 entities inserted, 0 errors.

**Step 3: Validate counts**

Run:
```bash
PGPASSWORD=brain psql -h localhost -p 5433 -U brain -d brain -c "
SELECT 'decisions' AS t, COUNT(*) FROM decisions
UNION ALL SELECT 'learnings', COUNT(*) FROM learnings
UNION ALL SELECT 'snippets', COUNT(*) FROM snippets
UNION ALL SELECT 'runbooks', COUNT(*) FROM runbooks
UNION ALL SELECT 'adrs', COUNT(*) FROM adrs
UNION ALL SELECT 'project_contexts', COUNT(*) FROM project_contexts;
"
```

Expected: Counts should match the dry-run summary.

**Step 4: Verify embeddings are populated**

Run:
```bash
PGPASSWORD=brain psql -h localhost -p 5433 -U brain -d brain -c "
SELECT 'decisions' AS t, COUNT(*) AS total, COUNT(embedding) AS with_embedding FROM decisions
UNION ALL SELECT 'learnings', COUNT(*), COUNT(embedding) FROM learnings
UNION ALL SELECT 'snippets', COUNT(*), COUNT(embedding) FROM snippets
UNION ALL SELECT 'runbooks', COUNT(*), COUNT(embedding) FROM runbooks
UNION ALL SELECT 'adrs', COUNT(*), COUNT(embedding) FROM adrs;
"
```

Expected: `total` = `with_embedding` for all tables (all have embeddings).

**Step 5: Quick semantic search test**

Run:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
python -c "
import asyncio, os
os.environ['POSTGRES_URL'] = 'postgresql+asyncpg://brain:brain@localhost:5433/brain'
from brain_v42.services.embedding_service import EmbeddingService
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.services.decision_service import DecisionService
from brain_v42.db.engine import get_session_factory

class AE:
    def __init__(self, s): self._s = s
    async def embed(self, t): return self._s.embed_text(t)

async def test():
    sf = get_session_factory()
    es = EmbeddingService()
    ds = DecisionService(PgDecisionRepo(sf), AE(es))
    results = await ds.semantic_search('PostgreSQL pgvector', limit=3)
    for item, score in results:
        print(f'{score:.3f} | {item.title}')

asyncio.run(test())
"
```

Expected: Top-3 relevant results about pgvector with scores > 0.3.

---

### Task 9: Run benchmarks on migrated data

**Files:**
- None (existing scripts)

**Step 1: Baseline benchmark**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode baseline --iterations 20`

**Step 2: Quality check**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode quality`

**Step 3: Compare mode** (if Neo4j password known)

Run:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
python scripts/benchmark.py --mode compare \
    --neo4j-uri bolt://localhost:7688 \
    --neo4j-password <PASSWORD>
```

**Step 4: Load test**

Run: `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && python scripts/benchmark.py --mode load --concurrency 10 --duration 30`

**Step 5: Review results and adjust**

Check `results/` directory for JSON output. Review quality scores.
If precision@5 < 0.5 or noise_ratio > 0.5: investigate embedding quality or add min_score cutoff.

---

### Task 10: Switchover — disable old brain MCP

**Files:**
- Modify: `~/.claude/.mcp.json` (or global MCP config)

**Step 1: Locate and read MCP config**

Run: `cat ~/.claude/.mcp.json 2>/dev/null || cat ~/.claude/mcp.json 2>/dev/null`

**Step 2: Remove or comment out mcp-brain entry**

Remove the `mcp-brain` server entry (old datalake_v2 brain), keeping only `brain-v42`.

**Step 3: Verify brain-v42 tools still work**

Run a quick test via Claude Code: `brain_get_project_context(project_key="brain_v42")` — should return data from the newly migrated PG database.

**Step 4: Update brain_v42 project context**

Use brain-v42 MCP tool:
```
brain_update_project_focus(
    project_key="brain_v42",
    current_focus="Post-migration: all data migrated from Neo4j, old mcp-brain disabled, brain-v42 is sole brain"
)
```

**Step 5: Commit benchmark script**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add scripts/benchmark.py pyproject.toml
git commit -m "feat: complete benchmark suite — baseline, compare, load, quality modes"
```
