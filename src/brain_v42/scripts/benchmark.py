#!/usr/bin/env python3
"""brain_v42 benchmark script — measures PostgreSQL performance.

Modes:
    baseline  — Latency/throughput of brain_v42 PG operations
    compare   — Side-by-side PG vs Neo4j for equivalent operations
    load      — Sustained concurrency/throughput under load
    quality   — Semantic search quality (precision/recall) comparison

Usage:
    python scripts/benchmark.py --mode baseline
    python scripts/benchmark.py --mode compare --neo4j-uri bolt://localhost:7688
    python scripts/benchmark.py --mode load --concurrency 10 --duration 30
    python scripts/benchmark.py --mode quality
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_PG_URL = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
DEFAULT_NEO4J_URI = "bolt://localhost:7688"
DEFAULT_NEO4J_USER = "neo4j"

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


# ── Timer context manager ────────────────────────────────────────────────────


class Timer:
    """Context manager that records elapsed time in seconds.

    Usage:
        with Timer() as t:
            do_work()
        print(t.elapsed)  # float, seconds
    """

    def __init__(self) -> None:
        self.elapsed: float = 0.0
        self._start: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed = time.perf_counter() - self._start


# ── Statistics ────────────────────────────────────────────────────────────────


def compute_stats(latencies: list[float]) -> dict[str, float]:
    """Compute latency statistics from a list of durations (seconds).

    Returns dict with keys: min, avg, p50, p95, p99, max, count.
    All values are in milliseconds except count.
    """
    if not latencies:
        return {
            "min": 0.0,
            "avg": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "count": 0,
        }

    # Convert seconds to milliseconds
    ms = [lat * 1000 for lat in latencies]
    ms_sorted = sorted(ms)
    n = len(ms_sorted)

    def percentile(p: float) -> float:
        """Compute the p-th percentile (0-100) from a sorted list."""
        k = (n - 1) * (p / 100.0)
        f = int(k)
        c = f + 1
        if c >= n:
            return ms_sorted[-1]
        return ms_sorted[f] + (k - f) * (ms_sorted[c] - ms_sorted[f])

    return {
        "min": ms_sorted[0],
        "avg": statistics.mean(ms),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "max": ms_sorted[-1],
        "count": n,
    }


# ── PG service factory ───────────────────────────────────────────────────────


def setup_pg_services(pg_url: str) -> dict[str, Any]:
    """Initialize all brain_v42 repositories, services, and embedding service.

    This wires up the full brain_v42 stack pointing at the given PG URL,
    suitable for benchmarking.

    Returns dict with keys:
        engine, session_factory, embedding_svc,
        decision_svc, learning_svc, snippet_svc, runbook_svc, adr_svc,
        project_context_svc, brain_svc
    """
    # Import here to avoid top-level dependency on brain_v42 internals
    from brain_v42.repositories.pg_adr import PgADRRepo
    from brain_v42.repositories.pg_decision import PgDecisionRepo
    from brain_v42.repositories.pg_learning import PgLearningRepo
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo
    from brain_v42.repositories.pg_runbook import PgRunbookRepo
    from brain_v42.repositories.pg_snippet import PgSnippetRepo
    from brain_v42.services.adr_service import ADRService
    from brain_v42.services.brain_service import BrainService
    from brain_v42.services.decision_service import DecisionService
    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
    from brain_v42.services.learning_service import LearningService
    from brain_v42.services.project_context_service import ProjectContextService
    from brain_v42.services.runbook_service import RunbookService
    from brain_v42.services.snippet_service import SnippetService

    # Engine + session factory (bypass brain_v42.db.engine singletons)
    engine = create_async_engine(
        pg_url,
        pool_size=5,
        max_overflow=10,
        pool_timeout=10,
        pool_pre_ping=True,
        echo=False,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Also initialize the global singleton so repos that use get_session_factory() work
    import brain_v42.db.engine as db_engine

    db_engine._engine = engine
    db_engine._session_factory = session_factory

    # Embedding service (GPU, async httpx)
    embedding_svc = GPUEmbeddingService()

    # Repositories — all six receive session_factory explicitly (consistent DI)
    decision_repo = PgDecisionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)
    snippet_repo = PgSnippetRepo(session_factory)
    runbook_repo = PgRunbookRepo(session_factory)
    adr_repo = PgADRRepo(session_factory)
    project_context_repo = PgProjectContextRepo(session_factory)

    # Services — project_context_repo wires the fail-closed project-existence
    # guard on create() (see brain_v42.services.project_guard).
    decision_svc = DecisionService(
        repo=decision_repo, embedding_svc=embedding_svc, project_context_repo=project_context_repo
    )
    learning_svc = LearningService(
        pg_repo=learning_repo,
        embedding_svc=embedding_svc,
        project_context_repo=project_context_repo,
    )
    snippet_svc = SnippetService(
        repo=snippet_repo, embedding_svc=embedding_svc, project_context_repo=project_context_repo
    )
    runbook_svc = RunbookService(
        pg_repo=runbook_repo,
        embedding_svc=embedding_svc,
        project_context_repo=project_context_repo,
    )
    adr_svc = ADRService(
        pg_repo=adr_repo, embedding_svc=embedding_svc, project_context_repo=project_context_repo
    )
    project_context_svc = ProjectContextService(pg_repo=project_context_repo)

    brain_svc = BrainService(
        decision_svc=decision_svc,
        learning_svc=learning_svc,
        snippet_svc=snippet_svc,
        runbook_svc=runbook_svc,
        adr_svc=adr_svc,
        embedding_svc=embedding_svc,
    )

    return {
        "engine": engine,
        "session_factory": session_factory,
        "embedding_svc": embedding_svc,
        "decision_svc": decision_svc,
        "learning_svc": learning_svc,
        "snippet_svc": snippet_svc,
        "runbook_svc": runbook_svc,
        "adr_svc": adr_svc,
        "project_context_svc": project_context_svc,
        "brain_svc": brain_svc,
    }


# ── Results I/O ──────────────────────────────────────────────────────────────


def save_results(data: dict[str, Any], output: str | None = None) -> Path:
    """Save benchmark results as JSON.

    Args:
        data: The results dictionary to serialize.
        output: Optional output file path. If None, auto-generates a timestamped
                filename in the results/ directory.

    Returns:
        Path to the written file.
    """
    if output:
        path = Path(output)
    else:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        mode = data.get("mode", "benchmark")
        path = RESULTS_DIR / f"{mode}_{ts}.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"Results saved to {path}")
    return path


# ── Mode stubs ────────────────────────────────────────────────────────────────


async def run_baseline(args: argparse.Namespace) -> dict[str, Any]:
    """Run baseline benchmarks — PG latency/throughput for all operations.

    Measures latency for:
    1. Embedding generation (ONNX)
    2. List operations per entity service
    3. Full-text search per entity service
    4. Semantic search per entity service
    5. Global brain search
    6. what_do_i_know_about
    """
    from tabulate import tabulate

    TEST_QUERIES = ["PostgreSQL", "Docker", "MCP", "Python", "deploy"]

    svcs = setup_pg_services(args.postgres_url)
    embedding_svc = svcs["embedding_svc"]
    decision_svc = svcs["decision_svc"]
    learning_svc = svcs["learning_svc"]
    snippet_svc = svcs["snippet_svc"]
    runbook_svc = svcs["runbook_svc"]
    adr_svc = svcs["adr_svc"]
    brain_svc = svcs["brain_svc"]

    iterations = args.iterations
    # For operations that cycle through TEST_QUERIES, use iterations // 5
    # so total calls roughly match the requested iteration count
    query_iters = max(1, iterations // len(TEST_QUERIES))

    # Values are float-only stats from compute_stats() OR a single
    # {"error": str(...)} sentinel when every iteration of an op failed --
    # a genuine union, not an oversight (see the ERR row rendering below).
    all_stats: dict[str, dict[str, Any]] = {}

    # ── 1. Embedding generation ──────────────────────────────────────────────
    print("  [1/6] Embedding generation...")
    embed_latencies: list[float] = []
    for i in range(iterations):
        text = TEST_QUERIES[i % len(TEST_QUERIES)]
        with Timer() as t:
            await embedding_svc.embed(text)
        embed_latencies.append(t.elapsed)
    all_stats["embed_text"] = compute_stats(embed_latencies)

    # ── 2. List operations ───────────────────────────────────────────────────
    print("  [2/6] List operations...")
    list_ops: dict[str, Any] = {
        "list_decisions": lambda: decision_svc.list_all(limit=20),
        "list_learnings": lambda: learning_svc.list_all(limit=20),
        "list_snippets": lambda: snippet_svc.list_snippets(limit=20),
        "list_runbooks": lambda: runbook_svc.search("", limit=20),  # no generic list_all
        "list_adrs": lambda: adr_svc.list_all(limit=20),
    }
    for op_name, op_fn in list_ops.items():
        latencies: list[float] = []
        err: Exception | None = None
        for _ in range(iterations):
            try:
                with Timer() as t:
                    await op_fn()
                latencies.append(t.elapsed)
            except Exception as exc:
                print(f"    ⚠ {op_name}: {exc}")
                err = exc
                break
        if latencies:
            all_stats[op_name] = compute_stats(latencies)
        else:
            all_stats[op_name] = {"error": str(err)}

    # ── 3. FTS search ────────────────────────────────────────────────────────
    print("  [3/6] Full-text search...")
    fts_ops: dict[str, Any] = {
        "fts_decisions": lambda q: decision_svc.search(q, limit=10),
        "fts_learnings": lambda q: learning_svc.search(q, limit=10),
        # snippet_svc has no FTS search method
        "fts_runbooks": lambda q: runbook_svc.search(q, limit=10),
        "fts_adrs": lambda q: adr_svc.search(query=q, limit=10),
    }
    for op_name, op_fn in fts_ops.items():
        latencies = []
        err = None
        for _ in range(query_iters):
            for q in TEST_QUERIES:
                try:
                    with Timer() as t:
                        await op_fn(q)
                    latencies.append(t.elapsed)
                except Exception as exc:
                    print(f"    ⚠ {op_name}: {exc}")
                    err = exc
                    break
            if err:
                break
        all_stats[op_name] = compute_stats(latencies) if latencies else {"error": str(err)}

    # ── 4. Semantic search ───────────────────────────────────────────────────
    print("  [4/6] Semantic search...")
    sem_ops: dict[str, Any] = {
        "sem_decisions": lambda q: decision_svc.semantic_search(q, limit=10),
        "sem_learnings": lambda q: learning_svc.semantic_search(q, limit=10),
        "sem_snippets": lambda q: snippet_svc.semantic_search(q, limit=10),
        "sem_runbooks": lambda q: runbook_svc.semantic_search(q, limit=10),
        "sem_adrs": lambda q: adr_svc.semantic_search(q, limit=10),
    }
    for op_name, op_fn in sem_ops.items():
        latencies = []
        err = None
        for _ in range(query_iters):
            for q in TEST_QUERIES:
                try:
                    with Timer() as t:
                        await op_fn(q)
                    latencies.append(t.elapsed)
                except Exception as exc:
                    print(f"    ⚠ {op_name}: {exc}")
                    err = exc
                    break
            if err:
                break
        all_stats[op_name] = compute_stats(latencies) if latencies else {"error": str(err)}

    # ── 5. Global search ─────────────────────────────────────────────────────
    print("  [5/6] Global brain search...")
    global_latencies: list[float] = []
    for _ in range(query_iters):
        for q in TEST_QUERIES:
            with Timer() as t:
                await brain_svc.search(q, limit=20)
            global_latencies.append(t.elapsed)
    all_stats["brain_search"] = compute_stats(global_latencies)

    # ── 6. what_do_i_know_about ──────────────────────────────────────────────
    print("  [6/6] what_do_i_know_about...")
    wdika_latencies: list[float] = []
    for _ in range(query_iters):
        for q in TEST_QUERIES:
            with Timer() as t:
                await brain_svc.what_do_i_know_about(q, limit=10)
            wdika_latencies.append(t.elapsed)
    all_stats["what_do_i_know_about"] = compute_stats(wdika_latencies)

    # ── Print formatted table ────────────────────────────────────────────────
    print()
    headers = ["Operation", "N", "Min(ms)", "Avg(ms)", "P50(ms)", "P95(ms)", "Max(ms)"]
    rows = []
    for op_name, stats in all_stats.items():
        if "error" in stats:
            rows.append([op_name, "ERR", "-", "-", "-", "-", stats["error"][:40]])
        else:
            rows.append(
                [
                    op_name,
                    int(stats["count"]),
                    f"{stats['min']:.2f}",
                    f"{stats['avg']:.2f}",
                    f"{stats['p50']:.2f}",
                    f"{stats['p95']:.2f}",
                    f"{stats['max']:.2f}",
                ]
            )

    print(tabulate(rows, headers=headers, tablefmt="github"))
    print()

    # ── Cleanup engine ───────────────────────────────────────────────────────
    engine = svcs["engine"]
    await engine.dispose()

    return {"operations": all_stats}


async def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    """Run comparison benchmarks — PG vs Neo4j side-by-side.

    For each entity type (Decision, Learning, Snippet, Runbook, ADR):
      - List: Neo4j MATCH vs PG service list
      - Search: Neo4j CONTAINS vs PG FTS (tsvector)

    Prints a side-by-side table with speedup ratios.
    """
    from tabulate import tabulate

    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        print("ERROR: neo4j driver not installed. Install with: pip install neo4j")
        return {"error": "neo4j driver not installed"}

    SEARCH_QUERIES = ["PostgreSQL", "Docker", "deploy"]
    ITERATIONS = 20

    # ── Entity definitions ───────────────────────────────────────────────────
    # Each entry: (label, neo4j_label, cypher_search_fields, pg_list_fn, pg_search_fn)
    # cypher_search_fields: list of property names to CONTAINS-search in Neo4j

    # ── Setup PG services ────────────────────────────────────────────────────
    svcs = setup_pg_services(args.postgres_url)
    decision_svc = svcs["decision_svc"]
    learning_svc = svcs["learning_svc"]
    snippet_svc = svcs["snippet_svc"]
    runbook_svc = svcs["runbook_svc"]
    adr_svc = svcs["adr_svc"]

    # Explicit Any: each dict mixes str, list[str] and Callable values, so
    # mypy's inferred `dict[str, object]` would make every access below
    # (`.lower()`, iterating search_fields, calling pg_list/pg_search)
    # a static error despite each individual access being safe by
    # construction (fixed literal keys, fixed shapes).
    entities: list[dict[str, Any]] = [
        {
            "name": "Decision",
            "neo4j_label": "Decision",
            "search_fields": ["title", "description"],
            "pg_list": lambda: decision_svc.list_all(limit=20),
            "pg_search": lambda q: decision_svc.search(q, limit=10),
        },
        {
            "name": "Learning",
            "neo4j_label": "Learning",
            "search_fields": ["title", "insight"],
            "pg_list": lambda: learning_svc.list_all(limit=20),
            "pg_search": lambda q: learning_svc.search(q, limit=10),
        },
        {
            "name": "Snippet",
            "neo4j_label": "Snippet",
            "search_fields": ["title", "description"],
            "pg_list": lambda: snippet_svc.list_snippets(limit=20),
            "pg_search": lambda q: snippet_svc.semantic_search(q, limit=10),
        },
        {
            "name": "Runbook",
            "neo4j_label": "Runbook",
            "search_fields": ["title", "description"],
            "pg_list": lambda: runbook_svc.search("", limit=20),
            "pg_search": lambda q: runbook_svc.search(q, limit=10),
        },
        {
            "name": "ADR",
            "neo4j_label": "ADR",
            "search_fields": ["title", "description"],
            "pg_list": lambda: adr_svc.list_all(limit=20),
            "pg_search": lambda q: adr_svc.search(query=q, limit=10),
        },
    ]

    # ── Connect to Neo4j ─────────────────────────────────────────────────────
    print(f"  Connecting to Neo4j at {args.neo4j_uri}...")
    neo4j_driver = AsyncGraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password) if args.neo4j_password else None,
    )

    try:
        # Verify connectivity
        async with neo4j_driver.session() as session:
            await session.run("RETURN 1")
        print("  Neo4j connected OK")
    except Exception as e:
        print(f"  ERROR: Cannot connect to Neo4j: {e}")
        await neo4j_driver.close()
        engine = svcs["engine"]
        await engine.dispose()
        return {"error": f"Neo4j connection failed: {e}"}

    results: dict[str, Any] = {}
    rows: list[list[Any]] = []

    try:
        for entity in entities:
            name = entity["name"]
            neo4j_label = entity["neo4j_label"]
            search_fields = entity["search_fields"]
            pg_list_fn = entity["pg_list"]
            pg_search_fn = entity["pg_search"]

            print(f"\n  [{name}]")

            # ── List — Neo4j ─────────────────────────────────────────────
            print(f"    List Neo4j ({ITERATIONS} iters)...")
            neo4j_list_latencies: list[float] = []
            for _ in range(ITERATIONS):
                with Timer() as t:
                    async with neo4j_driver.session() as session:
                        result = await session.run(f"MATCH (n:{neo4j_label}) RETURN n LIMIT 20")
                        await result.consume()
                neo4j_list_latencies.append(t.elapsed)

            # ── List — PG ────────────────────────────────────────────────
            print(f"    List PG ({ITERATIONS} iters)...")
            pg_list_latencies: list[float] = []
            for _ in range(ITERATIONS):
                with Timer() as t:
                    await pg_list_fn()
                pg_list_latencies.append(t.elapsed)

            # ── Search — Neo4j ───────────────────────────────────────────
            # Build a safe Cypher WHERE clause that handles missing properties
            # using coalesce() to default to empty string
            where_parts = [f"coalesce(n.{field}, '') CONTAINS $q" for field in search_fields]
            where_clause = " OR ".join(where_parts)
            search_cypher = f"MATCH (n:{neo4j_label}) WHERE {where_clause} RETURN n LIMIT 10"

            print(f"    Search Neo4j ({ITERATIONS * len(SEARCH_QUERIES)} iters)...")
            neo4j_search_latencies: list[float] = []
            for q in SEARCH_QUERIES:
                for _ in range(ITERATIONS):
                    with Timer() as t:
                        async with neo4j_driver.session() as session:
                            result = await session.run(search_cypher, q=q)
                            await result.consume()
                    neo4j_search_latencies.append(t.elapsed)

            # ── Search — PG ──────────────────────────────────────────────
            print(f"    Search PG ({ITERATIONS * len(SEARCH_QUERIES)} iters)...")
            pg_search_latencies: list[float] = []
            for q in SEARCH_QUERIES:
                for _ in range(ITERATIONS):
                    with Timer() as t:
                        await pg_search_fn(q)
                    pg_search_latencies.append(t.elapsed)

            # ── Compute stats ────────────────────────────────────────────
            neo4j_list_stats = compute_stats(neo4j_list_latencies)
            pg_list_stats = compute_stats(pg_list_latencies)
            neo4j_search_stats = compute_stats(neo4j_search_latencies)
            pg_search_stats = compute_stats(pg_search_latencies)

            # Speedup ratio (>1 means PG is faster)
            list_speedup = (
                neo4j_list_stats["avg"] / pg_list_stats["avg"]
                if pg_list_stats["avg"] > 0
                else float("inf")
            )
            search_speedup = (
                neo4j_search_stats["avg"] / pg_search_stats["avg"]
                if pg_search_stats["avg"] > 0
                else float("inf")
            )

            # Store results
            results[f"{name.lower()}_list"] = {
                "neo4j": neo4j_list_stats,
                "pg": pg_list_stats,
                "speedup": list_speedup,
            }
            results[f"{name.lower()}_search"] = {
                "neo4j": neo4j_search_stats,
                "pg": pg_search_stats,
                "speedup": search_speedup,
            }

            # Table rows
            rows.append(
                [
                    f"{name} List",
                    f"{neo4j_list_stats['avg']:.2f}",
                    f"{pg_list_stats['avg']:.2f}",
                    f"{list_speedup:.1f}x",
                ]
            )
            rows.append(
                [
                    f"{name} Search",
                    f"{neo4j_search_stats['avg']:.2f}",
                    f"{pg_search_stats['avg']:.2f}",
                    f"{search_speedup:.1f}x",
                ]
            )

    finally:
        await neo4j_driver.close()
        engine = svcs["engine"]
        await engine.dispose()

    # ── Print formatted table ────────────────────────────────────────────────
    print()
    headers = ["Operation", "Neo4j (ms)", "PG (ms)", "Speedup"]
    print(tabulate(rows, headers=headers, tablefmt="github"))
    print()

    return {"operations": results}


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    """Run load benchmarks — sustained concurrency/throughput.

    Spawns `args.concurrency` async workers that loop for `args.duration`
    seconds performing random operations:
      - 70% list (random entity service, list with limit=20)
      - 20% search (random entity service, search with random query)
      - 10% global search (brain_svc.search with random query)

    Collects latency per operation and reports aggregate stats.
    """
    import random

    from tabulate import tabulate

    RANDOM_QUERIES = ["PostgreSQL", "Docker", "MCP", "Python", "async", "deploy", "test"]

    svcs = setup_pg_services(args.postgres_url)
    decision_svc = svcs["decision_svc"]
    learning_svc = svcs["learning_svc"]
    snippet_svc = svcs["snippet_svc"]
    runbook_svc = svcs["runbook_svc"]
    adr_svc = svcs["adr_svc"]
    brain_svc = svcs["brain_svc"]

    # Define operation pools matching baseline patterns
    list_ops = [
        lambda: decision_svc.list_all(limit=20),
        lambda: learning_svc.list_all(limit=20),
        lambda: snippet_svc.list_snippets(limit=20),
        lambda: runbook_svc.search("", limit=20),
        lambda: adr_svc.list_all(limit=20),
    ]

    search_ops = [
        lambda q: decision_svc.search(q, limit=10),
        lambda q: learning_svc.search(q, limit=10),
        lambda q: snippet_svc.semantic_search(q, limit=10),
        lambda q: runbook_svc.search(q, limit=10),
        lambda q: adr_svc.search(query=q, limit=10),
    ]

    # Shared state
    stop_event = asyncio.Event()
    all_latencies: list[float] = []
    error_count = 0
    op_count = 0
    latency_lock = asyncio.Lock()

    async def worker(worker_id: int) -> None:
        nonlocal error_count, op_count
        local_latencies: list[float] = []
        local_errors = 0
        local_ops = 0

        while not stop_event.is_set():
            try:
                roll = random.random()
                with Timer() as t:
                    if roll < 0.70:
                        # 70% — list operation
                        await random.choice(list_ops)()
                    elif roll < 0.90:
                        # 20% — search operation
                        q = random.choice(RANDOM_QUERIES)
                        await random.choice(search_ops)(q)
                    else:
                        # 10% — global brain search
                        q = random.choice(RANDOM_QUERIES)
                        await brain_svc.search(q, limit=20)
                local_latencies.append(t.elapsed)
                local_ops += 1
            except Exception:
                local_errors += 1
                local_ops += 1

        # Merge into shared state
        async with latency_lock:
            all_latencies.extend(local_latencies)
            error_count += local_errors
            op_count += local_ops

    # Run workers for the requested duration
    concurrency = args.concurrency
    duration = args.duration
    print(f"  Starting {concurrency} workers for {duration}s...")

    with Timer() as total_timer:
        workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]

        await asyncio.sleep(duration)
        stop_event.set()

        # Let workers finish their current operation
        await asyncio.gather(*workers)

    wall_time = total_timer.elapsed
    throughput = op_count / wall_time if wall_time > 0 else 0.0
    stats = compute_stats(all_latencies)

    # ── Print formatted table ────────────────────────────────────────────────
    print()
    headers = ["Metric", "Value"]
    rows = [
        ["Total Operations", str(op_count)],
        ["Errors", str(error_count)],
        ["Throughput (ops/sec)", f"{throughput:.2f}"],
        ["Avg Latency (ms)", f"{stats['avg']:.2f}"],
        ["P50 Latency (ms)", f"{stats['p50']:.2f}"],
        ["P95 Latency (ms)", f"{stats['p95']:.2f}"],
        ["P99 Latency (ms)", f"{stats['p99']:.2f}"],
        ["Max Latency (ms)", f"{stats['max']:.2f}"],
    ]
    print(tabulate(rows, headers=headers, tablefmt="github"))
    print()

    # ── Cleanup engine ───────────────────────────────────────────────────────
    engine = svcs["engine"]
    await engine.dispose()

    return {
        "total_ops": op_count,
        "errors": error_count,
        "wall_time_s": wall_time,
        "throughput_ops_sec": throughput,
        "concurrency": concurrency,
        "duration_s": duration,
        "latency": stats,
    }


async def run_quality(args: argparse.Namespace) -> dict[str, Any]:
    """Run quality benchmarks — semantic search precision/recall.

    Validates search quality across 4 dimensions:
    1. Precision@5 — do top-5 results contain expected keywords?
    2. Noise analysis — how noisy are vague queries?
    3. FTS vs Semantic comparison — overlap and score ranges
    4. Cutoff recommendation — percentile-based score thresholds
    """
    from tabulate import tabulate

    svcs = setup_pg_services(args.postgres_url)
    decision_svc = svcs["decision_svc"]
    learning_svc = svcs["learning_svc"]
    snippet_svc = svcs["snippet_svc"]
    brain_svc = svcs["brain_svc"]

    all_semantic_scores: list[float] = []

    # ── 1. Precision@5 ──────────────────────────────────────────────────────
    print("  [1/4] Precision@5...")

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

    precision_rows: list[list[Any]] = []
    precision_data: list[dict[str, Any]] = []

    for pq in precision_queries:
        query = pq["query"]
        expected = pq["expected_keywords"]
        response = await brain_svc.search(query, limit=5)
        results = response.results
        total = len(results)

        # Count results that contain at least one expected keyword
        relevant = 0
        for r in results:
            item_text = json.dumps(r.item).lower()
            if any(kw.lower() in item_text for kw in expected):
                relevant += 1

        # Collect scores for cutoff analysis
        for r in results:
            all_semantic_scores.append(r.score)

        p_at_5 = relevant / total if total > 0 else 0.0
        precision_rows.append([query, total, relevant, f"{p_at_5:.2%}"])
        precision_data.append(
            {
                "query": query,
                "results": total,
                "relevant": relevant,
                "precision_at_5": p_at_5,
            }
        )

    print()
    print("  === PRECISION@5 ===")
    print(
        tabulate(
            precision_rows,
            headers=["Query", "Results", "Relevant", "P@5"],
            tablefmt="github",
        )
    )
    print()

    # ── 2. Noise analysis ───────────────────────────────────────────────────
    print("  [2/4] Noise analysis...")

    noise_queries = ["test", "code", "config", "data", "error"]
    noise_rows: list[list[Any]] = []
    noise_data: list[dict[str, Any]] = []

    for nq in noise_queries:
        response = await brain_svc.search(nq, limit=20)
        results = response.results
        total = len(results)

        if total > 0:
            scores = [r.score for r in results]
            score_min = min(scores)
            score_max = max(scores)
            low_score_count = sum(1 for s in scores if s < 0.3)
            noise_ratio = low_score_count / total

            # Collect scores for cutoff analysis
            all_semantic_scores.extend(scores)

            score_range = f"{score_min:.3f}-{score_max:.3f}"
        else:
            score_range = "N/A"
            low_score_count = 0
            noise_ratio = 0.0

        noise_rows.append([nq, total, score_range, f"{noise_ratio:.2%}"])
        noise_data.append(
            {
                "query": nq,
                "results": total,
                "score_range": score_range,
                "low_score_count": low_score_count,
                "noise_ratio": noise_ratio,
            }
        )

    print()
    print("  === NOISE ANALYSIS ===")
    print(
        tabulate(
            noise_rows,
            headers=["Query", "Results", "Score Range", "Noise Ratio"],
            tablefmt="github",
        )
    )
    print()

    # ── 3. FTS vs Semantic comparison ────────────────────────────────────────
    print("  [3/4] FTS vs Semantic comparison...")

    comparison_queries = ["PostgreSQL", "deployment", "Python testing"]
    # entity name -> (fts_fn or None, semantic_fn)
    # snippet_svc has no FTS search, only semantic
    entity_configs: dict[str, dict[str, Any]] = {
        "decision": {
            "fts_fn": lambda q: decision_svc.search(q, limit=10),
            "sem_fn": lambda q: decision_svc.semantic_search(q, limit=10),
        },
        "learning": {
            "fts_fn": lambda q: learning_svc.search(q, limit=10),
            "sem_fn": lambda q: learning_svc.semantic_search(q, limit=10),
        },
        "snippet": {
            "fts_fn": None,  # no FTS on snippets
            "sem_fn": lambda q: snippet_svc.semantic_search(q, limit=10),
        },
    }

    comparison_rows: list[list[Any]] = []
    comparison_data: list[dict[str, Any]] = []

    for cq in comparison_queries:
        for entity_name, config in entity_configs.items():
            fts_fn = config["fts_fn"]
            sem_fn = config["sem_fn"]

            # Semantic search (always available)
            sem_results = await sem_fn(cq)
            sem_count = len(sem_results)
            sem_ids = {str(entity.id) for entity, _score in sem_results}
            sem_scores = [score for _entity, score in sem_results]

            if sem_scores:
                sem_range = f"{min(sem_scores):.3f}-{max(sem_scores):.3f}"
            else:
                sem_range = "N/A"

            # FTS search (not available for snippet)
            if fts_fn is not None:
                fts_results = await fts_fn(cq)
                fts_count = len(fts_results)
                fts_ids = {str(entity.id) for entity in fts_results}
            else:
                fts_count = -1  # signals "N/A"
                fts_ids = set()

            # Overlap
            if fts_fn is not None and fts_count > 0 and sem_count > 0:
                overlap = len(fts_ids & sem_ids)
            else:
                overlap = 0

            fts_display = str(fts_count) if fts_count >= 0 else "N/A"
            overlap_display = str(overlap) if fts_count >= 0 else "N/A"

            comparison_rows.append(
                [
                    cq,
                    entity_name,
                    fts_display,
                    sem_count,
                    overlap_display,
                    sem_range,
                ]
            )
            comparison_data.append(
                {
                    "query": cq,
                    "entity": entity_name,
                    "fts_count": fts_count if fts_count >= 0 else None,
                    "semantic_count": sem_count,
                    "overlap": overlap if fts_count >= 0 else None,
                    "score_range": sem_range,
                }
            )

    print()
    print("  === FTS vs SEMANTIC ===")
    print(
        tabulate(
            comparison_rows,
            headers=["Query", "Entity", "FTS", "Semantic", "Overlap", "Score Range"],
            tablefmt="github",
        )
    )
    print()

    # ── 4. Cutoff recommendation ─────────────────────────────────────────────
    print("  [4/4] Cutoff recommendation...")

    cutoff_data: dict[str, float | str] = {}
    if all_semantic_scores:
        # Sort descending — p25 means top 25% boundary
        sorted_desc = sorted(all_semantic_scores, reverse=True)
        n = len(sorted_desc)

        def percentile_desc(p: float) -> float:
            """Compute percentile from a descending-sorted list.

            p=25 means the score at the 25th percentile boundary of top scores,
            i.e. index at 25% of n.
            """
            idx = int(n * (p / 100.0))
            idx = min(idx, n - 1)
            return sorted_desc[idx]

        strict = percentile_desc(25)
        moderate = percentile_desc(50)
        permissive = percentile_desc(75)

        cutoff_data = {
            "strict_p25": strict,
            "moderate_p50": moderate,
            "permissive_p75": permissive,
            "total_scores": n,
            "score_min": min(all_semantic_scores),
            "score_max": max(all_semantic_scores),
        }

        cutoff_rows = [
            ["Strict (top 25%)", f"{strict:.4f}"],
            ["Moderate (top 50%)", f"{moderate:.4f}"],
            ["Permissive (top 75%)", f"{permissive:.4f}"],
            ["Total scores analyzed", str(n)],
            ["Score range", f"{min(all_semantic_scores):.4f} - {max(all_semantic_scores):.4f}"],
        ]
    else:
        cutoff_data = {
            "strict_p25": "N/A",
            "moderate_p50": "N/A",
            "permissive_p75": "N/A",
            "total_scores": 0,
        }
        cutoff_rows = [
            ["Strict (top 25%)", "N/A"],
            ["Moderate (top 50%)", "N/A"],
            ["Permissive (top 75%)", "N/A"],
            ["Total scores analyzed", "0"],
        ]

    print()
    print("  === CUTOFF RECOMMENDATION ===")
    print(
        tabulate(
            cutoff_rows,
            headers=["Threshold", "Value"],
            tablefmt="github",
        )
    )
    print()

    # ── Cleanup engine ───────────────────────────────────────────────────────
    engine = svcs["engine"]
    await engine.dispose()

    return {
        "precision": precision_data,
        "noise": noise_data,
        "fts_vs_semantic": comparison_data,
        "cutoff": cutoff_data,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

MODE_DISPATCH = {
    "baseline": run_baseline,
    "compare": run_compare,
    "load": run_load,
    "quality": run_quality,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the benchmark script."""
    parser = argparse.ArgumentParser(
        description="brain_v42 benchmark — PostgreSQL performance testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=list(MODE_DISPATCH.keys()),
        required=True,
        help="Benchmark mode to run",
    )
    parser.add_argument(
        "--postgres-url",
        default=DEFAULT_PG_URL,
        help=f"PostgreSQL async URL (default: {DEFAULT_PG_URL})",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=DEFAULT_NEO4J_URI,
        help=f"Neo4j bolt URI (default: {DEFAULT_NEO4J_URI})",
    )
    parser.add_argument(
        "--neo4j-user",
        default=DEFAULT_NEO4J_USER,
        help=f"Neo4j username (default: {DEFAULT_NEO4J_USER})",
    )
    parser.add_argument(
        "--neo4j-password",
        default="",
        help="Neo4j password (default: empty)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of concurrent workers for load mode (default: 5)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Duration in seconds for load mode (default: 30)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Number of iterations per operation (default: 100)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: auto-generated in results/)",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """Main entry point — parse args and dispatch to the correct mode."""
    args = parse_args(argv)

    print(f"brain_v42 benchmark — mode: {args.mode}")
    print(f"  PostgreSQL: {args.postgres_url}")
    if args.mode == "compare":
        print(f"  Neo4j: {args.neo4j_uri}")
    if args.mode == "load":
        print(f"  Concurrency: {args.concurrency}, Duration: {args.duration}s")
    print(f"  Iterations: {args.iterations}")
    print()

    handler = MODE_DISPATCH[args.mode]
    results = await handler(args)

    results["mode"] = args.mode
    results["timestamp"] = datetime.now(UTC).isoformat()
    results["config"] = {
        "postgres_url": args.postgres_url,
        "iterations": args.iterations,
        "concurrency": args.concurrency,
        "duration": args.duration,
    }

    save_results(results, output=args.output)


if __name__ == "__main__":
    asyncio.run(main())
