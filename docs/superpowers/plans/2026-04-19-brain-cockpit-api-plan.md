# Brain-v42 Cockpit API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans-parallel` to dispatch tasks in batches via TeamCreate.

**Goal:** Ship `GET /api/cockpit` on the brain-v42 sidecar (port 9200) matching the handoff contract, in two tiers: Phase 1 (Tier 1 fields live, Tier 2 & 3 stubbed) then Phase 2 (Tier 2 buckets + cost + 24h histories).
**Test command:** `uv run pytest tests/unit -v`
**Tech Stack:** Python 3.12, aiohttp, asyncpg, SQLAlchemy 2.0 async, structlog, pytest-asyncio
**Spec source:** `/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor/docs/specs/2026-04-19-brain-cockpit-api-contract.md`
**Reference shapes:** `projects/red-monitor/docs/design_handoff_red_monitor/src/mock-data.jsx` → `brainExtras`

## Orientation

The existing metrics sidecar (`src/brain_v42/metrics/server.py`) exposes `GET /metrics`. **Do not touch that endpoint** — red-monitor's puller consumes it on a 15s cadence. Add a new route `GET /api/cockpit` polled at 2s by red-monitor, backed by a new `CockpitCollector` class that reuses the existing `MetricsCollector` for in-memory state and adds rolling-window derivations plus fresh DB queries.

**Constraint recap:** stdlib + asyncpg + structlog only (no prometheus_client, no t-digest). All heavy computes (percentiles, PG queries) behind a 1s in-memory cache so p95 response time stays < 100ms. Degraded mode: if tables/columns are missing, return `null`/`0`/`[]` instead of crashing.

---

## Batch 1: In-memory foundations (parallel)

Three tasks adding disjoint modules/state. No shared file, no shared test file.

### Task 1.1: Rolling-window state on MetricsCollector

**Files:**
- Modify: `src/brain_v42/metrics/collector.py` (extend `MetricsCollector` class with timestamped latency deques + percentile/rate methods)
- Create: `tests/unit/test_metrics_cockpit_window.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics_cockpit_window.py`:

```python
"""Tests for MetricsCollector rolling-window percentiles + rate derivation."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_tool_percentiles_returns_zero_without_samples(collector: MetricsCollector) -> None:
    p = collector.tool_percentiles(window_s=300)
    assert p == {"p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_tool_percentiles_computed_from_recent_samples(collector: MetricsCollector) -> None:
    for latency in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        collector.record_tool_call("brain_search", latency_ms=float(latency))
    p = collector.tool_percentiles(window_s=300)
    assert p["p50"] == pytest.approx(55.0, abs=5.0)
    assert p["p95"] == pytest.approx(95.0, abs=5.0)
    assert p["p99"] == pytest.approx(99.0, abs=5.0)


def test_tool_percentiles_drops_samples_outside_window(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = time.time()
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0 - 3600)
    collector.record_tool_call("brain_search", latency_ms=1000.0)
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0)
    collector.record_tool_call("brain_search", latency_ms=10.0)
    p = collector.tool_percentiles(window_s=300)
    assert p["p95"] < 100.0


def test_retrieval_percentiles_uses_search_latencies(collector: MetricsCollector) -> None:
    for latency in [20, 40, 60, 80, 100, 200, 300]:
        collector.record_search_latency(float(latency))
    p = collector.retrieval_percentiles(window_s=86400)
    assert p["p50"] == pytest.approx(80.0, abs=20.0)
    assert p["p95"] == pytest.approx(300.0, abs=20.0)


def test_compute_rates_returns_zero_without_snapshots(collector: MetricsCollector) -> None:
    r = collector.compute_rates(window_s=60)
    assert r["rps"] == 0.0
    assert r["err_rate"] == 0.0


def test_compute_rates_derives_rps_from_snapshots(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = time.time()
    # Snapshot 1 at t0-60s with 100 calls, 2 errors
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0 - 60)
    for _ in range(100):
        collector.record_tool_call("brain_search", latency_ms=10.0)
    for _ in range(2):
        collector.record_tool_call("brain_search", latency_ms=10.0, error=True)
    collector.snapshot_counters()
    # Snapshot 2 at t0 with 160 total calls, 3 total errors
    monkeypatch.setattr("brain_v42.metrics.collector.time.time", lambda: t0)
    for _ in range(58):
        collector.record_tool_call("brain_search", latency_ms=10.0)
    collector.record_tool_call("brain_search", latency_ms=10.0, error=True)
    collector.snapshot_counters()
    r = collector.compute_rates(window_s=60)
    assert r["rps"] == pytest.approx(59.0 / 60, abs=0.1)  # 59 new calls over 60s
    assert r["err_rate"] == pytest.approx(1.0 / 59, abs=0.05)


def test_recent_log_buffer_keeps_last_50(collector: MetricsCollector) -> None:
    for i in range(80):
        collector.push_recent_log("info", f"msg {i}")
    entries = collector.get_recent_log()
    assert len(entries) == 50
    assert entries[0]["msg"] == "msg 30"
    assert entries[-1]["msg"] == "msg 79"
    assert "t" in entries[0]
    assert entries[0]["lvl"] == "info"
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_cockpit_window.py -v
```

Expected: `AttributeError: 'MetricsCollector' object has no attribute 'tool_percentiles'` (and similar for other new methods).

- [ ] **Step 3: Implement the new state on MetricsCollector**

In `src/brain_v42/metrics/collector.py`, extend `__init__` with:

```python
# ── rolling-window state (for /api/cockpit) ──
self._tool_latencies: deque[tuple[float, float]] = deque(maxlen=10000)  # (ts, ms)
self._search_latencies: deque[tuple[float, float]] = deque(maxlen=10000)
self._counter_snapshots: deque[tuple[float, int, int]] = deque(maxlen=20)  # (ts, calls, errors)
self._recent_log: deque[dict[str, Any]] = deque(maxlen=50)
```

Extend `record_tool_call()` to also push to `_tool_latencies`:

```python
self._tool_latencies.append((time.time(), latency_ms))
```

Add a new method `record_search_latency`:

```python
def record_search_latency(self, latency_ms: float) -> None:
    """Record one search/retrieval latency sample for percentile computation."""
    self._search_latencies.append((time.time(), latency_ms))
```

Add the percentile helper and public methods:

```python
@staticmethod
def _percentiles(
    samples: deque[tuple[float, float]], window_s: float
) -> dict[str, float]:
    cutoff = time.time() - window_s
    values = sorted(v for ts, v in samples if ts >= cutoff)
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    n = len(values)

    def pct(p: float) -> float:
        idx = min(n - 1, int(p * n))
        return round(values[idx], 1)

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}

def tool_percentiles(self, window_s: float = 300.0) -> dict[str, float]:
    """Return p50/p95/p99 of tool latencies within the last `window_s` seconds."""
    return self._percentiles(self._tool_latencies, window_s)

def retrieval_percentiles(self, window_s: float = 86400.0) -> dict[str, float]:
    """Return p50/p95 of retrieval latencies within the window (24h default)."""
    p = self._percentiles(self._search_latencies, window_s)
    return {"p50": p["p50"], "p95": p["p95"]}
```

Add counter snapshots + rate derivation:

```python
def snapshot_counters(self) -> None:
    """Capture total tool calls + errors at current time for rate derivation."""
    total_calls = sum(s["calls"] for s in self._tool_stats.values())
    total_errors = sum(s["errors"] for s in self._tool_stats.values())
    self._counter_snapshots.append((time.time(), total_calls, total_errors))

def compute_rates(self, window_s: float = 60.0) -> dict[str, float]:
    """Derive rps + err_rate from counter snapshots over the window.

    Uses the snapshot closest-to-but-not-past `window_s` seconds ago as the
    baseline. Returns zeros if no baseline exists yet.
    """
    if len(self._counter_snapshots) < 2:
        return {"rps": 0.0, "err_rate": 0.0}
    cutoff = time.time() - window_s
    baseline: tuple[float, int, int] | None = None
    for snap in self._counter_snapshots:
        if snap[0] <= cutoff:
            baseline = snap
        else:
            break
    if baseline is None:
        baseline = self._counter_snapshots[0]
    latest = self._counter_snapshots[-1]
    elapsed = latest[0] - baseline[0]
    if elapsed <= 0:
        return {"rps": 0.0, "err_rate": 0.0}
    delta_calls = latest[1] - baseline[1]
    delta_errors = latest[2] - baseline[2]
    return {
        "rps": round(delta_calls / elapsed, 2),
        "err_rate": round(delta_errors / delta_calls, 4) if delta_calls else 0.0,
    }
```

Add recent-log buffer:

```python
def push_recent_log(self, level: str, message: str) -> None:
    """Append an entry to the recent-log ring buffer (max 50)."""
    self._recent_log.append({
        "t": datetime.now().strftime("%H:%M:%S"),
        "lvl": level,
        "msg": message,
    })

def get_recent_log(self) -> list[dict[str, Any]]:
    """Return a snapshot copy of the recent-log buffer (oldest → newest)."""
    return list(self._recent_log)
```

Also add `from datetime import datetime` if not already imported (check: the file currently imports `from datetime import UTC, datetime` — good).

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_cockpit_window.py -v
```

- [ ] **Step 5: Full suite still green**

```bash
uv run pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/collector.py tests/unit/test_metrics_cockpit_window.py
git commit -m "$(cat <<'EOF'
feat(metrics): rolling-window percentiles + rates + recent-log on collector

Foundation for the /api/cockpit sidecar endpoint. Adds timestamped
deques for tool+retrieval latencies, counter snapshots for rps/err_rate
derivation, and a 50-entry recent-log ring buffer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 1.2: Memory-stats DB helper

**Files:**
- Create: `src/brain_v42/metrics/memory_stats.py`
- Create: `tests/unit/test_metrics_memory_stats.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics_memory_stats.py`:

```python
"""Tests for the memory-stats DB helper used by the cockpit endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.memory_stats import collect_memory_stats


@pytest.fixture
def session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock()
    factory.return_value = session
    return factory


async def test_returns_null_shape_on_exception(session_factory: MagicMock) -> None:
    session_factory.return_value.execute.side_effect = Exception("boom")
    stats = await collect_memory_stats(session_factory)
    assert stats == {
        "episodes": 0,
        "episodic_mb": 0,
        "semantic_chunks": 0,
        "semantic_mb": 0,
        "lastCompaction": None,
        "freedLastCompaction": 0,
        "vectorIndex": None,
    }


async def test_aggregates_from_pg_queries(session_factory: MagicMock) -> None:
    # 4 expected queries in order: episodic count+size, semantic count+size,
    # consolidation_log summary, vector index def.
    exec_mock = session_factory.return_value.execute
    exec_mock.side_effect = [
        MagicMock(one=MagicMock(return_value=(14820, 482 * 1024 * 1024))),  # episodic
        MagicMock(one=MagicMock(return_value=(38420, 1240 * 1024 * 1024))),  # semantic
        MagicMock(one=MagicMock(return_value=(datetime(2026, 4, 19, 14, 31, 51, tzinfo=UTC), 38))),
        MagicMock(scalar=MagicMock(return_value="CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='200')")),
    ]
    stats = await collect_memory_stats(session_factory)
    assert stats["episodes"] == 14820
    assert stats["episodic_mb"] == 482
    assert stats["semantic_chunks"] == 38420
    assert stats["semantic_mb"] == 1240
    assert stats["lastCompaction"] == "14:31:51"
    assert stats["freedLastCompaction"] == 38
    assert stats["vectorIndex"] == "hnsw (m=16, ef=200)"


async def test_null_compaction_when_no_rows(session_factory: MagicMock) -> None:
    exec_mock = session_factory.return_value.execute
    exec_mock.side_effect = [
        MagicMock(one=MagicMock(return_value=(0, 0))),
        MagicMock(one=MagicMock(return_value=(0, 0))),
        MagicMock(one=MagicMock(return_value=(None, 0))),
        MagicMock(scalar=MagicMock(return_value=None)),
    ]
    stats = await collect_memory_stats(session_factory)
    assert stats["lastCompaction"] is None
    assert stats["freedLastCompaction"] == 0
    assert stats["vectorIndex"] is None
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_memory_stats.py -v
```

Expected: `ModuleNotFoundError: No module named 'brain_v42.metrics.memory_stats'`.

- [ ] **Step 3: Implement the helper**

Create `src/brain_v42/metrics/memory_stats.py`:

```python
"""Memory-stats DB helper for the /api/cockpit sidecar endpoint.

Derives `memory.*` fields from PostgreSQL system catalogs + consolidation_log.
Fully degraded: any SQL failure returns zeros / nulls (never raises).
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

# Episodic = short-term event log (decisions/learnings with recent access).
# Semantic = long-term compacted knowledge (snippets/runbooks/adrs + chunks).
_EPISODIC_TABLES = ("decisions", "learnings")
_SEMANTIC_TABLES = ("snippets", "runbooks", "adrs", "plan_chunks")

_NULL_STATS: dict[str, Any] = {
    "episodes": 0,
    "episodic_mb": 0,
    "semantic_chunks": 0,
    "semantic_mb": 0,
    "lastCompaction": None,
    "freedLastCompaction": 0,
    "vectorIndex": None,
}

_HNSW_PARAM_RE = re.compile(r"m\s*=\s*['\"]?(\d+)['\"]?.*?ef_construction\s*=\s*['\"]?(\d+)['\"]?", re.IGNORECASE)


async def collect_memory_stats(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Return the `memory` block of the cockpit payload.

    On any exception the dict is returned with zeros/nulls — callers never
    see a partial/inconsistent shape.
    """
    try:
        async with session_factory() as session:
            episodic_row = (
                await session.execute(
                    text(
                        "SELECT "
                        f"(SELECT COALESCE(SUM(n), 0) FROM (VALUES "
                        + ", ".join(f"((SELECT COUNT(*) FROM {t}))" for t in _EPISODIC_TABLES)
                        + ") AS v(n)), "
                        f"(SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) "
                        f" FROM pg_class c WHERE c.relname IN :ep_tables)"
                    ).bindparams(ep_tables=_EPISODIC_TABLES)
                )
            ).one()
            semantic_row = (
                await session.execute(
                    text(
                        "SELECT "
                        f"(SELECT COALESCE(SUM(n), 0) FROM (VALUES "
                        + ", ".join(f"((SELECT COUNT(*) FROM {t}))" for t in _SEMANTIC_TABLES)
                        + ") AS v(n)), "
                        f"(SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) "
                        f" FROM pg_class c WHERE c.relname IN :se_tables)"
                    ).bindparams(se_tables=_SEMANTIC_TABLES)
                )
            ).one()
            # Last compaction = most recent batch within a 5-minute cluster.
            compaction_row = (
                await session.execute(
                    text(
                        "SELECT MAX(created_at), "
                        "COUNT(*) FILTER ("
                        "  WHERE created_at >= ("
                        "    SELECT MAX(created_at) - INTERVAL '5 minutes' FROM consolidation_log"
                        "  )"
                        ") "
                        "FROM consolidation_log"
                    )
                )
            ).one()
            index_def = (
                await session.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE tablename = 'decisions' AND indexdef ILIKE '%hnsw%' "
                        "LIMIT 1"
                    )
                )
            ).scalar()
    except Exception:
        logger.warning("metrics.collect_memory_stats.failed", exc_info=True)
        return dict(_NULL_STATS)

    last_compaction_ts = compaction_row[0]
    last_compaction_str: str | None = (
        last_compaction_ts.strftime("%H:%M:%S") if last_compaction_ts is not None else None
    )

    vector_index_str: str | None = None
    if index_def:
        match = _HNSW_PARAM_RE.search(index_def)
        if match:
            vector_index_str = f"hnsw (m={match.group(1)}, ef={match.group(2)})"

    return {
        "episodes": int(episodic_row[0] or 0),
        "episodic_mb": int((episodic_row[1] or 0) // (1024 * 1024)),
        "semantic_chunks": int(semantic_row[0] or 0),
        "semantic_mb": int((semantic_row[1] or 0) // (1024 * 1024)),
        "lastCompaction": last_compaction_str,
        "freedLastCompaction": int(compaction_row[1] or 0),
        "vectorIndex": vector_index_str,
    }
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_memory_stats.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/memory_stats.py tests/unit/test_metrics_memory_stats.py
git commit -m "$(cat <<'EOF'
feat(metrics): memory-stats DB helper for cockpit endpoint

New module that queries pg_total_relation_size + consolidation_log +
pg_indexes to derive episodes/semantic_chunks/lastCompaction/vectorIndex
for the upcoming /api/cockpit payload. Fully degraded on SQL failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 1.3: Recent-log structlog processor

**Files:**
- Create: `src/brain_v42/metrics/recent_log.py`
- Create: `tests/unit/test_metrics_recent_log.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics_recent_log.py`:

```python
"""Tests for the recent-log structlog processor."""

from __future__ import annotations

from unittest.mock import MagicMock

from brain_v42.metrics.recent_log import RecentLogProcessor


def test_processor_pushes_entry_to_collector() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "tool.invoked", "tool": "brain_search", "level": "info"}
    proc(None, "info", event_dict)
    collector.push_recent_log.assert_called_once_with("info", "tool.invoked tool=brain_search")


def test_processor_formats_key_value_pairs_deterministically() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "thing", "b": 2, "a": 1, "level": "warn"}
    proc(None, "warn", event_dict)
    msg = collector.push_recent_log.call_args[0][1]
    assert msg == "thing a=1 b=2"


def test_processor_returns_event_dict_unchanged() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector)
    event_dict = {"event": "thing", "level": "info"}
    result = proc(None, "info", event_dict)
    assert result is event_dict


def test_processor_skips_below_threshold() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector, min_level="warn")
    proc(None, "info", {"event": "quiet", "level": "info"})
    collector.push_recent_log.assert_not_called()
    proc(None, "warn", {"event": "loud", "level": "warn"})
    collector.push_recent_log.assert_called_once()


def test_processor_truncates_long_messages() -> None:
    collector = MagicMock()
    proc = RecentLogProcessor(collector, max_len=40)
    proc(None, "info", {"event": "x" * 100, "level": "info"})
    msg = collector.push_recent_log.call_args[0][1]
    assert len(msg) <= 40
    assert msg.endswith("…")
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_recent_log.py -v
```

Expected: `ModuleNotFoundError: No module named 'brain_v42.metrics.recent_log'`.

- [ ] **Step 3: Implement the processor**

Create `src/brain_v42/metrics/recent_log.py`:

```python
"""Structlog processor that mirrors log events into the collector's recent-log buffer.

Wires structured log events into the in-memory deque exposed by
``MetricsCollector.get_recent_log()``. Intended to be inserted in the
structlog processor chain before the renderer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brain_v42.metrics.collector import MetricsCollector


_LEVEL_ORDER = {"debug": 0, "info": 1, "warn": 2, "warning": 2, "err": 3, "error": 3, "critical": 4}


class RecentLogProcessor:
    """Structlog processor that forwards events to the collector's buffer.

    Args:
        collector: target MetricsCollector.
        min_level: minimum level to forward (default "info").
        max_len: truncate messages to this many characters (default 200).
    """

    def __init__(
        self,
        collector: MetricsCollector,
        min_level: str = "info",
        max_len: int = 200,
    ) -> None:
        self._collector = collector
        self._min = _LEVEL_ORDER.get(min_level, 1)
        self._max_len = max_len

    def __call__(
        self, logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        level = event_dict.get("level", method_name)
        if _LEVEL_ORDER.get(level, 1) < self._min:
            return event_dict
        event = event_dict.get("event", "")
        extras = sorted(
            (k, v) for k, v in event_dict.items()
            if k not in ("event", "level", "timestamp", "logger")
        )
        parts = [event, *(f"{k}={v}" for k, v in extras)]
        msg = " ".join(p for p in parts if p)
        if len(msg) > self._max_len:
            msg = msg[: self._max_len - 1] + "…"
        self._collector.push_recent_log(level if level != "warning" else "warn", msg)
        return event_dict
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_recent_log.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/recent_log.py tests/unit/test_metrics_recent_log.py
git commit -m "$(cat <<'EOF'
feat(metrics): structlog processor for recent-log ring buffer

RecentLogProcessor forwards structured log events into the collector's
50-entry deque with level filtering and message truncation. Wired into
the MCP bootstrap in a later task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 2: CockpitCollector assembly (sequential)

### Task 2.1: CockpitCollector snapshot builder

**Files:**
- Create: `src/brain_v42/metrics/cockpit.py`
- Create: `tests/unit/test_metrics_cockpit_collector.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics_cockpit_collector.py`:

```python
"""Tests for CockpitCollector.snapshot() — Phase 1 scope."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.cockpit import CockpitCollector
from brain_v42.metrics.collector import MetricsCollector


_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1536,
)


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    for latency in [50, 100, 150, 200, 250]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.record_search(similarity_score=0.8, result_count=5)
    c.record_search_latency(40.0)
    c.push_recent_log("info", "bootstrap ok")
    return c


@pytest.fixture
def cockpit(collector: MetricsCollector) -> CockpitCollector:
    return CockpitCollector(
        collector=collector,
        session_factory=MagicMock(),
    )


async def test_snapshot_has_required_top_level_keys(cockpit: CockpitCollector) -> None:
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    for key in [
        "version", "pid", "uptime_s", "endpoint",
        "metrics", "activeConvs", "tools", "skills",
        "memory", "retrieval", "latencyBuckets", "cost",
        "handoff", "rpsHistory", "p95History", "errHistory", "costHistory",
        "recent",
    ]:
        assert key in snap, f"missing top-level key: {key}"


async def test_metrics_block_has_all_required_fields(cockpit: CockpitCollector) -> None:
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    m = snap["metrics"]
    assert set(m.keys()) >= {
        "rps", "p50", "p95", "p99", "err_rate",
        "cache_hit", "active_convs", "ctx_tokens", "memory_mb",
    }
    assert isinstance(m["p95"], (int, float))
    assert m["active_convs"] == 0  # Tier 3 stub


async def test_tools_block_has_p95_and_lastErr(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_tool_call("brain_search", latency_ms=500.0, error=True)
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    tools = snap["tools"]
    assert isinstance(tools, list)
    assert any(t["name"] == "brain_search" for t in tools)
    brain_search = next(t for t in tools if t["name"] == "brain_search")
    assert "p95" in brain_search
    assert "err" in brain_search
    assert "calls24h" in brain_search


async def test_tier3_stubs_are_empty_arrays(cockpit: CockpitCollector) -> None:
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    assert snap["activeConvs"] == []
    assert snap["skills"] == []
    assert snap["handoff"] == []


async def test_phase2_fields_are_stubbed_in_phase1(cockpit: CockpitCollector) -> None:
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    assert snap["latencyBuckets"] == []
    assert snap["rpsHistory"] == []
    assert snap["p95History"] == []
    assert snap["errHistory"] == []
    assert snap["costHistory"] == []
    assert snap["cost"] == {
        "today": 0.0, "yesterday": 0.0, "week": 0.0, "month": 0.0, "byModel": []
    }


async def test_snapshot_cached_within_ttl(cockpit: CockpitCollector) -> None:
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })) as mock_mem:
        await cockpit.snapshot()
        await cockpit.snapshot()
        await cockpit.snapshot()
    assert mock_mem.call_count == 1  # cached after first call


async def test_recent_section_matches_collector_buffer(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.push_recent_log("warn", "something odd")
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    msgs = [r["msg"] for r in snap["recent"]]
    assert "something odd" in msgs
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_cockpit_collector.py -v
```

Expected: `ModuleNotFoundError: No module named 'brain_v42.metrics.cockpit'`.

- [ ] **Step 3: Implement CockpitCollector**

Create `src/brain_v42/metrics/cockpit.py`:

```python
"""CockpitCollector — assembles the /api/cockpit payload.

Wraps MetricsCollector for in-memory state (rates, percentiles, recent-log)
and delegates DB-side fields to memory_stats + search_log queries. 1-second
in-memory cache keeps p95 response time under 100ms under polling load.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import time
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.config import get_settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.memory_stats import collect_memory_stats

logger = structlog.get_logger(__name__)

_CACHE_TTL_S = 1.0


class CockpitCollector:
    """Assembles the cockpit payload from the in-memory collector + DB queries."""

    def __init__(
        self,
        collector: MetricsCollector,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._started_at = time.time()
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, Any]:
        """Return the full cockpit payload (cached 1s)."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_ts < _CACHE_TTL_S:
            return self._cache
        async with self._cache_lock:
            if self._cache is not None and time.monotonic() - self._cache_ts < _CACHE_TTL_S:
                return self._cache
            self._cache = await self._build()
            self._cache_ts = time.monotonic()
            return self._cache

    async def _build(self) -> dict[str, Any]:
        settings = get_settings()
        try:
            version = importlib.metadata.version("brain_v42")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"

        tool_pct = self._collector.tool_percentiles(window_s=300.0)
        rates = self._collector.compute_rates(window_s=60.0)
        retrieval_pct = self._collector.retrieval_percentiles(window_s=86400.0)

        memory = await collect_memory_stats(self._session_factory)
        retrieval = await self._retrieval_stats(retrieval_pct)
        tools_block = self._tools_block()

        cache_hit_ratio = self._cache_hit_ratio()
        rss_bytes = self._total_rss_bytes()

        return {
            "version": version,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self._started_at),
            "endpoint": "stdio",
            "metrics": {
                "rps": rates["rps"],
                "p50": tool_pct["p50"],
                "p95": tool_pct["p95"],
                "p99": tool_pct["p99"],
                "err_rate": rates["err_rate"],
                "cache_hit": cache_hit_ratio,
                "active_convs": 0,  # Tier 3
                "ctx_tokens": 0,  # Tier 3
                "memory_mb": rss_bytes // (1024 * 1024),
            },
            "activeConvs": [],  # Tier 3
            "tools": tools_block,
            "skills": [],  # Tier 3
            "memory": memory,
            "retrieval": retrieval,
            "latencyBuckets": [],  # Phase 2
            "cost": {
                "today": 0.0, "yesterday": 0.0, "week": 0.0, "month": 0.0, "byModel": []
            },  # Phase 2
            "handoff": [],  # Tier 3
            "rpsHistory": [],   # Phase 2
            "p95History": [],   # Phase 2
            "errHistory": [],   # Phase 2
            "costHistory": [],  # Phase 2
            "recent": self._collector.get_recent_log(),
        }

    def _tools_block(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for name, stats in self._collector._tool_stats.items():
            if name.startswith("_"):
                continue
            calls = stats["calls"]
            avg = round(stats["total_latency"] / calls, 1) if calls else 0.0
            tools.append({
                "name": name,
                "calls24h": calls,
                "err": stats["errors"],
                "p95": avg,  # per-tool p95 improved in Phase 2 (requires per-tool deques)
                "lastErr": None,
            })
        return tools

    def _cache_hit_ratio(self) -> float:
        emb = self._collector._embedding_stats
        total = emb["total_requests"]
        if not total:
            return 0.0
        # Without a cache_hits counter (the embedding service does not expose
        # hit telemetry in Phase 1), surface 0.0 until extended in Phase 2.
        return 0.0

    def _total_rss_bytes(self) -> int:
        from brain_v42.metrics.collector import _get_rss_bytes
        return _get_rss_bytes()

    async def _retrieval_stats(self, pct: dict[str, float]) -> dict[str, Any]:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*), "
                            "COUNT(*) FILTER (WHERE result_count = 0) "
                            "FROM search_log "
                            "WHERE created_at > NOW() - INTERVAL '24 hours'"
                        )
                    )
                ).one()
                total, zero = int(row[0] or 0), int(row[1] or 0)
        except Exception:
            logger.warning("metrics.cockpit.retrieval_stats.failed", exc_info=True)
            total, zero = 0, 0

        rer_total = self._collector._reranker_stats["total_calls"]
        rer_ratio = rer_total / total if total else 0.0

        return {
            "p50": pct["p50"],
            "p95": pct["p95"],
            "hitRate": round(1 - (zero / total), 2) if total else 0.0,
            "rerankUsed": round(rer_ratio, 2),
            "queries24h": total,
        }
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_cockpit_collector.py -v
```

- [ ] **Step 5: Full suite green**

```bash
uv run pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/cockpit.py tests/unit/test_metrics_cockpit_collector.py
git commit -m "$(cat <<'EOF'
feat(metrics): CockpitCollector — Phase 1 snapshot builder

Assembles the /api/cockpit payload from MetricsCollector + memory_stats
+ search_log. 1s in-memory cache keeps poll cost minimal. Phase 2 fields
(latencyBuckets, cost, *History) stubbed to empty; Tier 3 fields
(activeConvs, skills, handoff) stubbed as well.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 3: Endpoint wiring + Phase-1 integration (sequential)

### Task 3.1: Register /api/cockpit route on MetricsServer

**Files:**
- Modify: `src/brain_v42/metrics/server.py` (add route + handler + CockpitCollector wiring)
- Modify: `src/brain_v42/mcp/server.py` or bootstrap module — attach RecentLogProcessor to structlog chain (only if MCP bootstrap owns structlog config; otherwise skip — many codebases only configure structlog at top level)
- Create: `tests/unit/test_cockpit_endpoint.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cockpit_endpoint.py`:

```python
"""Integration tests for GET /api/cockpit route."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1536,
)


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    c.record_tool_call("brain_search", latency_ms=80.0)
    return c


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.healthcheck = AsyncMock(return_value=True)
    return svc


async def test_cockpit_endpoint_returns_200_json(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 14820, "episodic_mb": 482, "semantic_chunks": 38420, "semantic_mb": 1240,
        "lastCompaction": "14:31:51", "freedLastCompaction": 38, "vectorIndex": "hnsw (m=16, ef=200)",
    })):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
    assert resp.status == 200
    data = await resp.json()
    # Spot-check schema conformance
    assert data["endpoint"] == "stdio"
    assert "metrics" in data
    assert data["memory"]["episodes"] == 14820
    assert isinstance(data["tools"], list)
    assert data["activeConvs"] == []


async def test_cockpit_endpoint_schema_contains_all_required_keys(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
        data = await resp.json()
    for key in [
        "version", "pid", "uptime_s", "endpoint", "metrics",
        "activeConvs", "tools", "skills", "memory", "retrieval",
        "latencyBuckets", "cost", "handoff",
        "rpsHistory", "p95History", "errHistory", "costHistory", "recent",
    ]:
        assert key in data


async def test_cockpit_endpoint_degraded_on_memory_stats_failure(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    with patch("brain_v42.metrics.cockpit.collect_memory_stats",
               new=AsyncMock(side_effect=Exception("db down"))):
        client = await aiohttp_client(server._build_app())
        resp = await client.get("/api/cockpit")
    # Must NOT 500 — degraded response expected.
    assert resp.status == 200
    data = await resp.json()
    assert data["memory"]["episodes"] == 0


async def test_metrics_route_unchanged_no_regression(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    server = MetricsServer(collector, mock_embedding_svc, port=0, host="127.0.0.1")
    collector.collect_db_stats = AsyncMock(
        return_value={
            "pool": {"active": 0, "idle": 0, "overflow": 0, "max": 15},
            "tables": {}, "db_size_bytes": 0, "dimension_mismatches": 0,
        }
    )
    client = await aiohttp_client(server._build_app())
    resp = await client.get("/metrics")
    assert resp.status == 200
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_cockpit_endpoint.py -v
```

Expected: 404 on `/api/cockpit` (route not yet registered).

- [ ] **Step 3: Add the route on MetricsServer**

In `src/brain_v42/metrics/server.py`:

Import CockpitCollector at top:

```python
from brain_v42.metrics.cockpit import CockpitCollector
```

Extend `__init__` to lazy-build a CockpitCollector — add at the end of `__init__`:

```python
self._cockpit: CockpitCollector | None = None
```

Extend `_build_app` to register the new route:

```python
def _build_app(self) -> web.Application:
    app = web.Application()
    app.router.add_get("/metrics", self._handle_metrics)
    app.router.add_get("/api/cockpit", self._handle_cockpit)
    if self._gitlab_ingestor:
        app.router.add_post("/gitlab/webhook", self._handle_webhook)
    return app
```

Add the handler:

```python
async def _handle_cockpit(self, request: web.Request) -> web.Response:
    """Handle GET /api/cockpit — 2s-poll cockpit payload."""
    if self._cockpit is None:
        # session_factory lives on the collector; no separate constructor
        # arg needed — keeps __init__ backward compatible.
        self._cockpit = CockpitCollector(
            collector=self._collector,
            session_factory=self._collector._session_factory,
        )
    payload = await self._cockpit.snapshot()
    return web.json_response(payload)
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_cockpit_endpoint.py -v
```

- [ ] **Step 5: Full suite green**

```bash
uv run pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/server.py tests/unit/test_cockpit_endpoint.py
git commit -m "$(cat <<'EOF'
feat(metrics): GET /api/cockpit route on sidecar (Phase 1 MUST)

Registers the cockpit endpoint beside /metrics. 2s-poll target for
red-monitor. Tier 1 fields live (metrics.rps/p50/p95/p99/err_rate,
tools, memory, retrieval, recent), Tier 2 & 3 stubbed per plan.
Degraded on DB failure — never 500s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 4: Phase 2 foundations (parallel)

Two tasks, disjoint surfaces: DB migration vs collector/flusher extension.

### Task 4.1: Migration 018 — metrics_timeseries table

**Files:**
- Create: `alembic/versions/018_metrics_timeseries.py`
- Create: `tests/unit/test_migration_018_metrics_timeseries.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_migration_018_metrics_timeseries.py`:

```python
"""Smoke test: migration 018 creates metrics_timeseries with correct shape."""

from __future__ import annotations

from alembic.script import ScriptDirectory
from alembic.config import Config


def test_migration_018_is_present() -> None:
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = [s for s in script.walk_revisions() if s.revision.startswith("018")]
    assert heads, "migration 018_metrics_timeseries not found"
    mig = heads[0]
    assert mig.down_revision == "017", f"expected 017 down_rev, got {mig.down_revision}"


def test_migration_018_creates_table_with_composite_pk() -> None:
    from alembic.versions import _018_metrics_timeseries as mig  # noqa: N813
    src = open(mig.__file__).read()
    assert "metrics_timeseries" in src
    assert "PRIMARY KEY (bucket_ts, metric)" in src or "primary_key=True" in src
    assert "bucket_ts" in src
    assert "metric" in src
    assert "value" in src
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_migration_018_metrics_timeseries.py -v
```

Expected: migration file missing.

- [ ] **Step 3: Implement the migration**

Create `alembic/versions/018_metrics_timeseries.py`:

```python
"""Add metrics_timeseries table for 24h histories (rps/p95/err_rate/cost).

Revision ID: 018
Revises: 017
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_timeseries",
        sa.Column("bucket_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(50), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.PrimaryKeyConstraint("bucket_ts", "metric"),
    )
    op.create_index(
        "idx_metrics_ts_metric",
        "metrics_timeseries",
        ["metric", sa.text("bucket_ts DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_metrics_ts_metric", table_name="metrics_timeseries")
    op.drop_table("metrics_timeseries")
```

Also register the table in `src/brain_v42/db/tables.py` (add to the `__all__` list and declare the SQLAlchemy Table object near the other telemetry tables):

```python
# ─── metrics_timeseries (24h history for cockpit) ───────────────────────────

metrics_timeseries = Table(
    "metrics_timeseries",
    METADATA,
    Column("bucket_ts", DateTime(timezone=True), primary_key=True),
    Column("metric", String(50), primary_key=True),
    Column("value", sa.Float, nullable=False),
    Index(
        "idx_metrics_ts_metric",
        "metric",
        sa.text("bucket_ts DESC"),
    ),
)
```

Then append `"metrics_timeseries"` to the `__all__` / table name list at the bottom.

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_migration_018_metrics_timeseries.py -v
uv run pytest tests/unit/test_alembic_env.py -v
```

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/018_metrics_timeseries.py src/brain_v42/db/tables.py tests/unit/test_migration_018_metrics_timeseries.py
git commit -m "$(cat <<'EOF'
feat(migrations): 018 metrics_timeseries for cockpit histories

Stores 30min-bucketed rps/p95/err_rate + 1h-bucketed cost for the
24h sparklines on the red-monitor cockpit. Composite PK (bucket_ts,
metric), idx on (metric, bucket_ts DESC) for history queries.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 4.2: Latency buckets + cost tracking on collector/flusher

**Files:**
- Modify: `src/brain_v42/metrics/collector.py` (add bucket counter + cost fields + recorder hooks)
- Modify: `src/brain_v42/metrics/flusher.py` (serialize cost_stats into process_metrics.tool_stats JSONB)
- Create: `tests/unit/test_metrics_cost_and_buckets.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics_cost_and_buckets.py`:

```python
"""Tests for Phase 2 latency-buckets + cost tracking on MetricsCollector."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


def test_bucket_counts_initialize_at_zero(collector: MetricsCollector) -> None:
    buckets = collector.latency_buckets()
    expected_ranges = {"< 100ms", "100-300ms", "300-600ms", "600ms-1s", "1-2s", "> 2s"}
    assert {b["range"] for b in buckets} == expected_ranges
    assert all(b["count"] == 0 for b in buckets)


def test_tool_call_increments_correct_bucket(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_tool_call("brain_search", latency_ms=150.0)
    collector.record_tool_call("brain_search", latency_ms=400.0)
    collector.record_tool_call("brain_search", latency_ms=700.0)
    collector.record_tool_call("brain_search", latency_ms=1500.0)
    collector.record_tool_call("brain_search", latency_ms=3000.0)
    buckets = {b["range"]: b["count"] for b in collector.latency_buckets()}
    assert buckets["< 100ms"] == 1
    assert buckets["100-300ms"] == 1
    assert buckets["300-600ms"] == 1
    assert buckets["600ms-1s"] == 1
    assert buckets["1-2s"] == 1
    assert buckets["> 2s"] == 1


def test_record_cost_tracks_per_model(collector: MetricsCollector) -> None:
    collector.record_cost(model="sonnet-4.5", cost_usd=0.82)
    collector.record_cost(model="sonnet-4.5", cost_usd=1.18)
    collector.record_cost(model="haiku-4.5", cost_usd=0.05)
    by_model = collector.cost_by_model()
    assert by_model["sonnet-4.5"] == pytest.approx(2.00)
    assert by_model["haiku-4.5"] == pytest.approx(0.05)


def test_cost_total_equals_sum_across_models(collector: MetricsCollector) -> None:
    collector.record_cost(model="a", cost_usd=3.0)
    collector.record_cost(model="b", cost_usd=5.0)
    assert collector.cost_total() == pytest.approx(8.0)


def test_flush_data_exposes_cost_and_buckets(collector: MetricsCollector) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_cost(model="sonnet-4.5", cost_usd=1.5)
    data = collector.get_flush_data()
    assert "latency_buckets" in data
    assert "cost_stats" in data
    assert data["cost_stats"]["total"] == pytest.approx(1.5)
    assert data["cost_stats"]["by_model"]["sonnet-4.5"] == pytest.approx(1.5)


def test_flusher_includes_cost_in_tool_stats_jsonb() -> None:
    # Flusher injects cost under tool_stats['_cost'] for cross-process aggregation.
    from brain_v42.metrics.flusher import MetricsFlusher
    collector = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    collector.record_cost(model="sonnet-4.5", cost_usd=2.0)
    MetricsFlusher(collector=collector, session_factory=MagicMock(), flush_interval=30.0)
    data = collector.get_flush_data()
    # cost_stats present in flush_data, flusher will serialize under _cost key.
    assert data["cost_stats"]["total"] == pytest.approx(2.0)
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_cost_and_buckets.py -v
```

- [ ] **Step 3: Implement buckets + cost on MetricsCollector**

In `src/brain_v42/metrics/collector.py`:

Add bucket boundary + state to `__init__`:

```python
# ── latency buckets (6 fixed ranges, Phase 2 cockpit field) ──
self._latency_buckets: dict[str, int] = {
    "< 100ms": 0, "100-300ms": 0, "300-600ms": 0,
    "600ms-1s": 0, "1-2s": 0, "> 2s": 0,
}
# ── cost tracking (Phase 2 cockpit field) ──
self._cost_by_model: dict[str, float] = {}
```

Helper for bucket lookup:

```python
@staticmethod
def _bucket_for(latency_ms: float) -> str:
    if latency_ms < 100:
        return "< 100ms"
    if latency_ms < 300:
        return "100-300ms"
    if latency_ms < 600:
        return "300-600ms"
    if latency_ms < 1000:
        return "600ms-1s"
    if latency_ms < 2000:
        return "1-2s"
    return "> 2s"
```

Extend `record_tool_call`:

```python
self._latency_buckets[self._bucket_for(latency_ms)] += 1
```

Add public methods:

```python
def latency_buckets(self) -> list[dict[str, Any]]:
    """Return fixed 6-bucket latency histogram for the cockpit payload."""
    return [{"range": r, "count": c} for r, c in self._latency_buckets.items()]

def record_cost(self, model: str, cost_usd: float) -> None:
    """Record one billable unit against a model (LLM API call, rerank, dream, …)."""
    self._cost_by_model[model] = self._cost_by_model.get(model, 0.0) + cost_usd

def cost_by_model(self) -> dict[str, float]:
    return dict(self._cost_by_model)

def cost_total(self) -> float:
    return sum(self._cost_by_model.values())
```

Extend `get_flush_data()` return dict with two new keys:

```python
"latency_buckets": dict(self._latency_buckets),
"cost_stats": {
    "total": self.cost_total(),
    "by_model": self.cost_by_model(),
},
```

In `src/brain_v42/metrics/flusher.py`, inside `_flush()`, after the `tool_json["_reranker"]` block, append cost injection:

```python
cost = flush_data.get("cost_stats", {})
if cost.get("total", 0.0) > 0:
    tool_json["_cost"] = {
        "total": cost["total"],
        "by_model": cost["by_model"],
    }
buckets = flush_data.get("latency_buckets", {})
if any(buckets.values()):
    tool_json["_buckets"] = buckets
```

This keeps all Phase 2 data inside `process_metrics.tool_stats` (already JSONB, already aggregated cross-process). No schema change needed on `process_metrics`.

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_cost_and_buckets.py -v
uv run pytest tests/unit/test_metrics_collector.py -v  # regression
uv run pytest tests/unit/test_metrics_server.py -v     # regression
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/collector.py src/brain_v42/metrics/flusher.py tests/unit/test_metrics_cost_and_buckets.py
git commit -m "$(cat <<'EOF'
feat(metrics): latency buckets + cost tracking (Phase 2 foundation)

Adds 6-bucket latency histogram (incremented in record_tool_call) and
per-model cost counter (record_cost API). Flusher serializes both under
process_metrics.tool_stats JSONB via _cost / _buckets pseudo-keys so
cross-process aggregation is free.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 5: Time-series flush (sequential)

### Task 5.1: Background task flushing rps/p95/err_rate/cost to metrics_timeseries

**Files:**
- Create: `src/brain_v42/metrics/timeseries_flusher.py`
- Create: `tests/unit/test_timeseries_flusher.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_timeseries_flusher.py`:

```python
"""Tests for the 30min/1h bucketed flush of rps/p95/err_rate/cost."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.timeseries_flusher import TimeseriesFlusher, _bucket_floor


def test_bucket_floor_rounds_down_to_30min() -> None:
    ts = datetime(2026, 4, 19, 14, 47, 38, tzinfo=UTC)
    assert _bucket_floor(ts, 1800) == datetime(2026, 4, 19, 14, 30, tzinfo=UTC)


def test_bucket_floor_rounds_down_to_hour() -> None:
    ts = datetime(2026, 4, 19, 14, 47, 38, tzinfo=UTC)
    assert _bucket_floor(ts, 3600) == datetime(2026, 4, 19, 14, 0, tzinfo=UTC)


@pytest.fixture
def collector() -> MetricsCollector:
    c = MetricsCollector(engine=MagicMock(), session_factory=MagicMock())
    for latency in [100, 200, 300]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.snapshot_counters()
    for latency in [150, 250]:
        c.record_tool_call("brain_search", latency_ms=float(latency))
    c.snapshot_counters()
    c.record_cost(model="sonnet-4.5", cost_usd=1.5)
    return c


@pytest.fixture
def session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock()
    factory.return_value = session
    return factory


async def test_flush_upserts_30min_metrics(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._flush_30min_metrics()
    calls = session_factory.return_value.execute.call_args_list
    # At least one INSERT per metric (rps, p95, err_rate)
    metrics_inserted = [
        c.args[1].get("metric") for c in calls if isinstance(c.args[1], dict)
    ]
    assert "rps" in metrics_inserted
    assert "p95" in metrics_inserted
    assert "err_rate" in metrics_inserted


async def test_flush_upserts_cost_bucket_hourly(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._flush_cost_bucket()
    calls = session_factory.return_value.execute.call_args_list
    metrics_inserted = [
        c.args[1].get("metric") for c in calls if isinstance(c.args[1], dict)
    ]
    assert "cost" in metrics_inserted


async def test_retention_deletes_rows_older_than_7_days(
    collector: MetricsCollector, session_factory: MagicMock
) -> None:
    flusher = TimeseriesFlusher(collector=collector, session_factory=session_factory)
    await flusher._retention_sweep()
    exec_calls = session_factory.return_value.execute.call_args_list
    # One of the calls must be the DELETE with a 7-day interval
    assert any(
        "DELETE FROM metrics_timeseries" in str(c.args[0])
        and "'7 days'" in str(c.args[0])
        for c in exec_calls
    )
```

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_timeseries_flusher.py -v
```

- [ ] **Step 3: Implement the flusher**

Create `src/brain_v42/metrics/timeseries_flusher.py`:

```python
"""TimeseriesFlusher — writes bucketed rps/p95/err_rate/cost to metrics_timeseries.

30-minute buckets for rps / p95 / err_rate, 1-hour buckets for cost.
Retention: 7 days (48 × 30min + 24 × 1h fits in ≈ 500 rows total).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector

logger = structlog.get_logger(__name__)

_30MIN = 1800
_1H = 3600


def _bucket_floor(ts: datetime, bucket_s: int) -> datetime:
    total = int(ts.timestamp())
    floored = (total // bucket_s) * bucket_s
    return datetime.fromtimestamp(floored, tz=UTC)


class TimeseriesFlusher:
    """Periodic flusher for bucketed cockpit histories."""

    def __init__(
        self,
        collector: MetricsCollector,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._last_cost_bucket: datetime | None = None
        self._last_retention: datetime | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("timeseries_flusher.started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("timeseries_flusher.stopped")

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(_30MIN)
                await self._flush_30min_metrics()
                now = datetime.now(UTC)
                if self._last_cost_bucket is None or (now - self._last_cost_bucket) >= timedelta(seconds=_1H):
                    await self._flush_cost_bucket()
                    self._last_cost_bucket = now
                if self._last_retention is None or (now - self._last_retention) >= timedelta(days=1):
                    await self._retention_sweep()
                    self._last_retention = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("timeseries_flusher.error")

    async def _flush_30min_metrics(self) -> None:
        rates = self._collector.compute_rates(window_s=1800.0)
        pct = self._collector.tool_percentiles(window_s=1800.0)
        bucket = _bucket_floor(datetime.now(UTC), _30MIN)
        async with self._session_factory() as session:
            for metric, value in (
                ("rps", rates["rps"]),
                ("p95", pct["p95"]),
                ("err_rate", rates["err_rate"]),
            ):
                await session.execute(
                    text(
                        "INSERT INTO metrics_timeseries (bucket_ts, metric, value) "
                        "VALUES (:bucket_ts, :metric, :value) "
                        "ON CONFLICT (bucket_ts, metric) DO UPDATE SET value = EXCLUDED.value"
                    ),
                    {"bucket_ts": bucket, "metric": metric, "value": float(value)},
                )
            await session.commit()

    async def _flush_cost_bucket(self) -> None:
        cost = self._collector.cost_total()
        bucket = _bucket_floor(datetime.now(UTC), _1H)
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO metrics_timeseries (bucket_ts, metric, value) "
                    "VALUES (:bucket_ts, :metric, :value) "
                    "ON CONFLICT (bucket_ts, metric) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"bucket_ts": bucket, "metric": "cost", "value": float(cost)},
            )
            await session.commit()

    async def _retention_sweep(self) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text("DELETE FROM metrics_timeseries WHERE bucket_ts < NOW() - INTERVAL '7 days'")
            )
            await session.commit()
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_timeseries_flusher.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/timeseries_flusher.py tests/unit/test_timeseries_flusher.py
git commit -m "$(cat <<'EOF'
feat(metrics): TimeseriesFlusher for 24h cockpit histories

30min bucketed rps/p95/err_rate + 1h bucketed cost, written to
metrics_timeseries via ON CONFLICT upsert. 7-day retention swept
once per day. Started alongside MetricsFlusher in the MCP bootstrap
in a later wiring step.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 6: Histories + buckets + cost on CockpitCollector (sequential)

### Task 6.1: Extend snapshot with Phase 2 fields

**Files:**
- Modify: `src/brain_v42/metrics/cockpit.py` (populate latencyBuckets/cost/*History from collector + metrics_timeseries)
- Modify: `tests/unit/test_metrics_cockpit_collector.py` (update Phase-2 stub tests → expect populated values)

- [ ] **Step 1: Write / update the failing tests**

Append to `tests/unit/test_metrics_cockpit_collector.py`:

```python
async def test_phase2_latency_buckets_populated(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_tool_call("brain_search", latency_ms=50.0)
    collector.record_tool_call("brain_search", latency_ms=400.0)
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    buckets = {b["range"]: b["count"] for b in snap["latencyBuckets"]}
    assert buckets["< 100ms"] >= 1
    assert buckets["300-600ms"] >= 1


async def test_phase2_cost_today_populated(
    cockpit: CockpitCollector, collector: MetricsCollector
) -> None:
    collector.record_cost(model="sonnet-4.5", cost_usd=2.0)
    # Histories block is optional here — focus on the `cost` block.
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    assert snap["cost"]["today"] == pytest.approx(2.0)
    assert snap["cost"]["byModel"] == [{"m": "sonnet-4.5", "v": 2.0, "pct": 1.0}]


async def test_phase2_histories_query_metrics_timeseries(
    cockpit: CockpitCollector,
) -> None:
    # Mock session to return fake history rows
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    cockpit._session_factory = MagicMock(return_value=session)
    rps_rows = [(2.1,), (2.4,), (2.7,)]
    p95_rows = [(312,), (298,), (280,)]
    err_rows = [(0.004,), (0.002,), (0.001,)]
    cost_rows = [(0.4,), (0.62,)]
    # memory_stats + retrieval_stats + 4× history queries
    session.execute.side_effect = [
        MagicMock(one=MagicMock(return_value=(0, 0))),  # retrieval (search_log summary)
        MagicMock(fetchall=MagicMock(return_value=rps_rows)),
        MagicMock(fetchall=MagicMock(return_value=p95_rows)),
        MagicMock(fetchall=MagicMock(return_value=err_rows)),
        MagicMock(fetchall=MagicMock(return_value=cost_rows)),
    ]
    with patch("brain_v42.metrics.cockpit.collect_memory_stats", new=AsyncMock(return_value={
        "episodes": 0, "episodic_mb": 0, "semantic_chunks": 0, "semantic_mb": 0,
        "lastCompaction": None, "freedLastCompaction": 0, "vectorIndex": None,
    })):
        snap = await cockpit.snapshot()
    assert snap["rpsHistory"] == [2.1, 2.4, 2.7]
    assert snap["p95History"] == [312.0, 298.0, 280.0]
    assert snap["errHistory"] == [0.004, 0.002, 0.001]
    assert snap["costHistory"] == [0.4, 0.62]
```

Also remove / update the earlier `test_phase2_fields_are_stubbed_in_phase1` — those stubs are no longer stubs.

- [ ] **Step 2: Run the tests, expect FAIL**

```bash
uv run pytest tests/unit/test_metrics_cockpit_collector.py -v
```

- [ ] **Step 3: Populate Phase 2 fields in CockpitCollector._build**

In `src/brain_v42/metrics/cockpit.py`:

Add a helper for history retrieval:

```python
async def _history(self, metric: str, limit: int) -> list[float]:
    """Fetch last N bucketed points for a metric, oldest → newest."""
    try:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT value FROM metrics_timeseries "
                        "WHERE metric = :metric AND bucket_ts > NOW() - INTERVAL '24 hours' "
                        "ORDER BY bucket_ts ASC "
                        "LIMIT :limit"
                    ),
                    {"metric": metric, "limit": limit},
                )
            ).fetchall()
            return [float(r[0]) for r in rows]
    except Exception:
        logger.warning("metrics.cockpit.history_failed", metric=metric, exc_info=True)
        return []
```

Replace the Phase-2 stub sections in `_build`:

```python
buckets = self._collector.latency_buckets()
total_cost = self._collector.cost_total()
by_model_raw = self._collector.cost_by_model()
by_model: list[dict[str, Any]] = [
    {"m": name, "v": round(v, 4), "pct": round(v / total_cost, 2) if total_cost else 0.0}
    for name, v in sorted(by_model_raw.items(), key=lambda kv: -kv[1])
]
rps_hist = await self._history("rps", limit=48)
p95_hist = await self._history("p95", limit=48)
err_hist = await self._history("err_rate", limit=48)
cost_hist = await self._history("cost", limit=24)
```

Then in the returned dict replace the stub entries:

```python
"latencyBuckets": buckets,
"cost": {
    "today": round(total_cost, 2),
    "yesterday": 0.0,  # derived from metrics_timeseries['cost'] (previous 24h) in follow-up
    "week": 0.0,       # deferred: needs 7-day cost rollup
    "month": 0.0,      # deferred
    "byModel": by_model,
},
"rpsHistory": rps_hist,
"p95History": p95_hist,
"errHistory": err_hist,
"costHistory": cost_hist,
```

- [ ] **Step 4: Run the tests, expect PASS**

```bash
uv run pytest tests/unit/test_metrics_cockpit_collector.py -v
uv run pytest tests/unit/test_cockpit_endpoint.py -v  # regression
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/metrics/cockpit.py tests/unit/test_metrics_cockpit_collector.py
git commit -m "$(cat <<'EOF'
feat(metrics): populate Phase 2 fields — buckets/cost/histories

latencyBuckets + cost.today + cost.byModel come straight from the
collector; rpsHistory/p95History/errHistory/costHistory are queried
from metrics_timeseries (24h window). cost.yesterday/week/month
deferred to follow-up — require 7-day rollup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 7: End-to-end validation + bootstrap wiring (sequential)

### Task 7.1: Wire TimeseriesFlusher into MCP bootstrap + e2e load test

**Files:**
- Modify: `src/brain_v42/mcp/server.py` or `src/brain_v42/__main__.py` (start TimeseriesFlusher alongside MetricsFlusher)
- Create: `tests/integration/test_cockpit_endpoint_e2e.py`

- [ ] **Step 1: Locate the bootstrap — inspect first**

```bash
grep -n "MetricsFlusher" src/brain_v42/**/*.py
```

Identify the `.start()` call site. Add `TimeseriesFlusher` alongside it (same session_factory + collector).

- [ ] **Step 2: Write the failing e2e test**

Create `tests/integration/test_cockpit_endpoint_e2e.py`:

```python
"""End-to-end: 100 requests in ~10s against /api/cockpit → assert p95 < 100ms.

Skipped unless PG + the metrics sidecar are reachable (see conftest.py
integration gate). Validates success criterion #3 from the spec.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

pytestmark = pytest.mark.integration


async def _hit(session: aiohttp.ClientSession, url: str) -> float:
    t0 = time.monotonic()
    async with session.get(url) as resp:
        await resp.json()
        assert resp.status == 200
    return (time.monotonic() - t0) * 1000


async def test_cockpit_p95_under_100ms_under_load() -> None:
    url = "http://127.0.0.1:9200/api/cockpit"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=2.0) as resp:
                if resp.status != 200:
                    pytest.skip(f"sidecar not ready (status {resp.status})")
        except Exception:
            pytest.skip("sidecar unreachable")

        latencies: list[float] = []
        for _ in range(10):
            batch = await asyncio.gather(*(_hit(session, url) for _ in range(10)))
            latencies.extend(batch)
            await asyncio.sleep(1.0)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 100, f"p95={p95:.1f}ms exceeds 100ms budget"


async def test_cockpit_schema_matches_contract() -> None:
    url = "http://127.0.0.1:9200/api/cockpit"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=2.0) as resp:
                if resp.status != 200:
                    pytest.skip(f"sidecar not ready (status {resp.status})")
                data = await resp.json()
        except Exception:
            pytest.skip("sidecar unreachable")

    required = {
        "version", "pid", "uptime_s", "endpoint", "metrics",
        "activeConvs", "tools", "skills", "memory", "retrieval",
        "latencyBuckets", "cost", "handoff",
        "rpsHistory", "p95History", "errHistory", "costHistory", "recent",
    }
    assert required <= set(data.keys())
    assert "episodes" in data["memory"]
    assert "p95" in data["metrics"]
    assert data["activeConvs"] == []
    assert data["skills"] == []
    assert data["handoff"] == []
```

- [ ] **Step 3: Wire TimeseriesFlusher into the bootstrap**

Based on the location found in Step 1, add the flusher alongside the existing `MetricsFlusher`. Example pattern (adjust to match actual bootstrap):

```python
from brain_v42.metrics.timeseries_flusher import TimeseriesFlusher

# ... after existing metrics_flusher.start() call:
timeseries_flusher = TimeseriesFlusher(
    collector=collector,
    session_factory=session_factory,
)
await timeseries_flusher.start()
# ... on shutdown:
await timeseries_flusher.stop()
```

- [ ] **Step 4: Run the e2e test against a live sidecar**

Start brain-v42 MCP + sidecar manually (`uv run python -m brain_v42.mcp.server` in another shell, verify `curl http://localhost:9200/api/cockpit | jq . | head -20`), then:

```bash
uv run pytest tests/integration/test_cockpit_endpoint_e2e.py -v
```

If the sidecar isn't up in CI, the test auto-skips — success criterion still validated locally.

- [ ] **Step 5: Log the decision in the brain**

```bash
# From the repo root, invoke via the MCP client the two cross-project
# decision logs mentioned in the spec §Communication entre sessions.
# Consumer expects: commit SHA + example curl response + degraded-field list.
```

Call `brain_log_decision(project_key="brain-v42", ...)` documenting:
- endpoint URL + final commit SHA of the merge
- degraded-mode field list (today's shipped: `cache_hit=0`, `active_convs=0`, `ctx_tokens=0`, `skills=[]`, `handoff=[]`, `cost.yesterday/week/month=0`, `tools[].lastErr=null`, `tools[].p95=avg_latency`)
- `curl http://localhost:9200/api/cockpit` response example (first 200 lines).

Then mirror the same content under `project_key="red-monitor"` so the consumer session sees it.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/mcp/server.py src/brain_v42/__main__.py tests/integration/test_cockpit_endpoint_e2e.py
git commit -m "$(cat <<'EOF'
feat(metrics): wire TimeseriesFlusher into bootstrap + cockpit e2e test

Cockpit endpoint is now fully online with 24h histories flushed every
30min (rps/p95/err_rate) + 1h (cost). Integration test asserts p95
<100ms under 100-req/10s load and validates schema conformance vs the
red-monitor handoff contract.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

## Out of scope (Phase 3 / future)

The contract defers these explicitly — do not implement here:

- `activeConvs[]` with real data — requires an MCP session model brain-v42 does not have today.
- `skills[]` — no skill-loader in brain-v42; red-lab owns skill telemetry.
- `handoff[]` — cross-agent instrumentation, belongs in red-lab.
- `ctx_tokens` — needs Claude API context accounting, not available from the MCP stdio side.
- `cache_hit` — requires embedding-service hit-rate exposure; Phase 3 follow-up once that telemetry is added upstream.
- `cost.yesterday / week / month` — 7-day cost rollup requires querying `metrics_timeseries` with windowed SUMs; cheap extension but deliberately deferred so the cockpit ships at Phase 2 without waiting on a 7-day seed.

## Success criteria (from spec §Success criteria)

1. ✅ `curl http://localhost:9200/api/cockpit | jq` returns valid JSON matching the schema. — covered by Task 7.1 e2e schema test.
2. ✅ Unit tests on collector (percentiles, rates, rollups). — covered by Tasks 1.1, 4.2, 6.1.
3. ✅ p95 response time < 100ms under 100-req/10s load. — covered by Task 7.1 e2e perf test.
4. ✅ Degraded-safe on missing tables/columns. — covered by Tasks 1.2 (null shape) + 3.1 (endpoint-level degraded test).
5. ✅ No regression on `/metrics`. — covered by Task 3.1 regression test.
