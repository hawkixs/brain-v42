# Spec C MVP β — Cross-Project Briefing & Resonance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface cross-project knowledge in `brain_session_start` (top-domain insights from other projects) and detect cross-project decision resonance via a nightly DRY_RUN-able script — all behind a closed killswitch, zero migrations, zero new MCP tools.

**Architecture:** Two read paths. (1) Briefing: a new `CrossProjectBriefingService` queries Neo4j for the project's top domains + candidate entity ids from other projects, then PG for display briefs; `session_tools` renders an additive `### Cross-project` section. (2) Resonance: `scripts/dream/cross_project_resonance.py` fetches per-domain decision ids from Neo4j, computes cross-project cosine pairs PG-side via pgvector `<=>`, writes a markdown report, and (WET only, opt-in) emits insulated learnings.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async + asyncpg, pgvector, Neo4j async driver, Pydantic Settings, pytest + pytest-asyncio (AsyncMock).

**Source spec:** `docs/superpowers/specs/2026-05-01-spec-c-mvp-cross-project-design.md` (v2). This plan applies the 2026-06-12 drift review — deviations from the spec are intentional and listed below.

## Drift adaptations (binding — override the spec where they conflict)

| # | Spec said | Reality (verified 2026-06-12) | Plan does |
|---|-----------|-------------------------------|-----------|
| 1 | `dream_runs(kind, mode, pair_count, run_id UUID)` | `dream_runs(id INTEGER, run_date, phase VARCHAR(10), status VARCHAR(10), phase_dry_run BOOL, error_message, …)` (`db/tables.py:624`) | `phase='RESONANCE'` (9 chars), `phase_dry_run=(mode != "wet")`, integer run id, pair_count only in the markdown report. Status enum is `done\|timeout\|fail` (learning) — use `done`/`fail`, never `completed`/`blocked`. |
| 2 | `learnings.source_kind`, `learnings.dedup_key`, `learnings.dream_run_id` columns | None of the three exist; `learnings.metadata` JSONB + `tags` exist | Insulation = tag `EXCLUDE_FROM_PROMOTE` (single source of truth). `dedup_key` + `dream_run_id` stored in `metadata` JSONB. Idempotency check via `metadata->>'dedup_key'`. |
| 3 | `_format_session_briefing(ctx, decisions, learnings)` + `cross_entries` last param | Function now has 7 positional + 2 kw-only params, 9 sections (`session_tools.py:138`) | Add kw-only `cross_block: Any \| None = None`; section rendered after `_section_recap`, before `_section_drill_in_hint`. |
| 4 | Repos gain `fetch_brief_by_ids` ×5 | Touching 5 repos + 5 services for a display query is overkill | One new `CrossProjectBriefingService` does the Neo4j calls + per-table brief SQL (pattern: `DreamRunService` raw-SQL service, `self._sf`). |
| 5 | Cypher `ORDER BY e.created_at DESC` | Neo4j nodes only carry `{id, title, project_key}` — **no `created_at`** (verified live) | Neo4j returns up to 50 candidate ids (unordered); PG brief query returns `created_at`; Python sorts desc and truncates to `entries_max`. |
| 6 | Snippet display = `intent`, Runbook display = `name` | Columns are `snippets.title`, `runbooks.title` | All five tables use their short text column: `decisions.title`, `learnings.topic`, `snippets.title`, `runbooks.title`, `adrs.title`. |
| 7 | `ThresholdSpec(name, value, domain, rationale, calibrated)` | Real dataclass: `(name, value, location, scale, calibrated, last_calibrated, calibration_script, corpus_dependency, notes)` (`thresholds.py:24`) | Entry uses the real fields. |
| 8 | Baseline 1837 tests | 1877 pass / 26 skip | Non-regression bar = every existing test stays green. |
| 9 | `ResonancePair` returned by repo | Repo lives in `src/`, dataclass in `scripts/` — src must not import scripts | `PgDecisionRepo.fetch_cross_project_resonance_pairs` returns `list[dict]` rows; the script maps rows → `ResonancePair`. |

**Verified invariants:** `Project {project_key}` nodes exist (10+ projects), all 9 domains populated (615 classified entities), `thresholds.by_name` exists, `ALLOWED_DOMAINS` importable, `graph_service` available in `services` dict at `server.py:387` wiring site.

**Known gotcha to respect:** every new `GraphService` method MUST get a matching explicit wrapper in `InstrumentedGraphService` (`src/brain_v42/metrics/instrument.py:129`) — see existing learning; mirror `find_orphans_for_classification` body exactly.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/brain_v42/config.py` | Modify | +3 settings (killswitch, top_n, entries_max) |
| `src/brain_v42/thresholds.py` | Modify | +1 REGISTRY entry `cross_project_resonance_min` |
| `src/brain_v42/services/graph_service.py` | Modify | +3 read methods (active domains, cross entity ids, decision ids per domain) |
| `src/brain_v42/metrics/instrument.py` | Modify | +3 instrumented wrappers |
| `src/brain_v42/repositories/pg_decision.py` | Modify | +`fetch_cross_project_resonance_pairs` (pgvector pair SQL) |
| `src/brain_v42/services/cross_project_service.py` | Create | `CrossEntry`, `CrossProjectBlock`, `CrossProjectBriefingService` |
| `src/brain_v42/mcp/tools/session_tools.py` | Modify | `_section_cross_project`, `cross_block` param, fetch in tool |
| `src/brain_v42/mcp/server.py` | Modify | Build + inject `cross_project_svc` |
| `scripts/dream/promote_prepare.py` | Modify | Insulation filter in `_CANDIDATE_SQL` |
| `scripts/dream/cross_project_resonance.py` | Create | `ResonancePair`, report writer, CLI main |
| `tests/unit/test_config.py` | Modify | settings tests |
| `tests/unit/test_thresholds.py` | Modify | registry entry test |
| `tests/unit/services/test_graph_service.py` | Modify | 3 methods tests |
| `tests/unit/metrics/test_instrument.py` (or existing instrument test file — locate with `grep -rl InstrumentedGraphService tests/`) | Modify | wrapper delegation tests |
| `tests/unit/repositories/test_pg_decision.py` | Modify | pair SQL tests |
| `tests/unit/services/test_cross_project_service.py` | Create | service tests |
| `tests/unit/mcp/test_session_tools.py` | Modify | section + graceful tests |
| `tests/unit/test_promote_prepare.py` | Modify | insulation test |
| `tests/unit/test_cross_project_resonance.py` | Create | dataclass/report/main tests |
| `CLAUDE.md` | Modify | document 3 env vars |

**Verification commands before EVERY commit** (project rule):
```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/ruff format --check src/ tests/ scripts/
.venv/bin/mypy src/
```
(If `.venv/bin` doesn't exist, use `uv run pytest …` etc. — match whichever the repo's venv provides. `mypy src/` only covers `src/`; `scripts/` is not mypy-gated.)

---

### Task 1: Config — three cross-project settings

**Files:**
- Modify: `src/brain_v42/config.py` (after the `gitlab_webhook_secret` block, ~line 92)
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_config.py`. NOTE: the file's convention is to import `Settings` INSIDE each test method (deferred import) — follow it:

```python
class TestCrossProjectSettings:
    """Spec C MVP β — killswitch + briefing tuning (closed by default)."""

    def test_cross_project_disabled_by_default(self):
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://u:p@h:5432/db")
        assert s.brain_dream_cross_project_enabled is False

    def test_briefing_top_n_default_2(self):
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://u:p@h:5432/db")
        assert s.brain_cross_project_briefing_domains_top_n == 2

    def test_briefing_entries_max_default_5(self):
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://u:p@h:5432/db")
        assert s.brain_cross_project_briefing_entries_max == 5

    def test_cross_project_enabled_via_env(self, monkeypatch):
        from brain_v42.config import Settings

        monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "true")
        s = Settings(postgres_url="postgresql+asyncpg://u:p@h:5432/db")
        assert s.brain_dream_cross_project_enabled is True
```

(`Settings(...)` direct construction is fine here — it bypasses the `get_settings()` lru_cache entirely.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -q -k CrossProject`
Expected: 4 FAIL — `ValidationError`/`AttributeError` (unknown field).

- [ ] **Step 3: Implement** — in `src/brain_v42/config.py`, after the GitLab webhook block:

```python
    # --- Cross-project (Dream v3 Spec C MVP β) ---
    brain_dream_cross_project_enabled: bool = False
    """Master killswitch for cross-project briefing section + resonance script."""

    brain_cross_project_briefing_domains_top_n: int = 2
    """Top-N active domains of the current project surfaced in the briefing."""

    brain_cross_project_briefing_entries_max: int = 5
    """Cap on cross-project entries rendered in the briefing."""
```

- [ ] **Step 4: Run tests** — same command, expected: 4 PASS. Then the full gate (see header).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/config.py tests/unit/test_config.py
git commit -m "feat(config): cross-project killswitch + briefing tuning settings (Spec C)"
```

---

### Task 2: Threshold registry entry

**Files:**
- Modify: `src/brain_v42/thresholds.py` (append to `REGISTRY` tuple)
- Test: `tests/unit/test_thresholds.py`

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_thresholds.py`:

```python
def test_cross_project_resonance_threshold_present():
    spec = thresholds.by_name("cross_project_resonance_min")
    assert spec is not None
    assert spec.value == 0.80
    assert spec.scale == "cosine"
    assert spec.calibrated is False
```

(Adapt the import to the file's existing style — it may `from brain_v42 import thresholds` or import `by_name` directly.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_thresholds.py -q -k cross_project`
Expected: FAIL — `by_name` returns `None`.

- [ ] **Step 3: Implement** — append inside `REGISTRY` in `src/brain_v42/thresholds.py`:

```python
    ThresholdSpec(
        name="cross_project_resonance_min",
        value=0.80,
        location="scripts/dream/cross_project_resonance.py",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="cross-project decision-pair cosine distribution per domain",
        notes="Min cosine to surface a decision pair as cross-project resonance "
        "candidate. Deliberately below promote_dedup_cosine (0.85). "
        "Recalibrate after 5+ DRY_RUN nights (Spec C MVP β rollout).",
    ),
```

- [ ] **Step 4: Run tests** — expected PASS (and any existing registry-shape tests like `test_each_entry_is_threshold_spec` stay green). Full gate.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/thresholds.py tests/unit/test_thresholds.py
git commit -m "feat(thresholds): register cross_project_resonance_min=0.80 (Spec C)"
```

---

### Task 3: GraphService — three read methods + instrumented wrappers

**Files:**
- Modify: `src/brain_v42/services/graph_service.py` (after `find_orphans_for_classification`, before the `# ── internals` block)
- Modify: `src/brain_v42/metrics/instrument.py` (inside `InstrumentedGraphService`, after `find_orphans_for_classification` wrapper, before `healthcheck`)
- Test: `tests/unit/services/test_graph_service.py`, plus the instrument test file (`grep -rl "InstrumentedGraphService" tests/unit` to locate; add delegation tests following its existing pattern)

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/services/test_graph_service.py`. IMPORTANT: the existing tests mock at the driver/`session.run` level — there is NO existing `_run_read`-mocking helper. The `graph_service_with_read` fixture below is NEW and local to these test classes (define it at the bottom of the file, next to the new classes); mocking `svc._run_read` directly is intentional — these methods are thin Cypher wrappers and the driver-level plumbing is already covered by existing tests:

```python
class TestFetchActiveDomains:
    @pytest.mark.asyncio
    async def test_returns_domain_names(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = [{"domain": "ml"}, {"domain": "memory"}]
        result = await svc.fetch_active_domains("brain-v42", top_n=2)
        assert result == ["ml", "memory"]
        query = mock_read.call_args[0][0]
        assert "BELONGS_TO_DOMAIN" in query
        assert "ORDER BY n DESC" in query

    @pytest.mark.asyncio
    async def test_empty_on_no_domains(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = []
        assert await svc.fetch_active_domains("brain-v42") == []


class TestFetchCrossProjectEntityIds:
    @pytest.mark.asyncio
    async def test_excludes_current_project_in_cypher(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = []
        await svc.fetch_cross_project_entity_ids(["ml"], exclude_project_key="brain-v42")
        query, params = mock_read.call_args[0]
        assert "p.project_key <> $exclude" in query
        assert params["exclude"] == "brain-v42"
        assert params["limit"] == 50  # candidate cap; recency sort happens in PG

    @pytest.mark.asyncio
    async def test_returns_rows_verbatim(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        rows = [{"id": "abc", "labels": ["Decision"], "project_key": "red-shrik"}]
        mock_read.return_value = rows
        assert await svc.fetch_cross_project_entity_ids(["ml"], exclude_project_key="x") == rows


class TestFetchDecisionIdsInDomain:
    @pytest.mark.asyncio
    async def test_returns_ids_for_domain(self, graph_service_with_read):
        svc, mock_read = graph_service_with_read
        mock_read.return_value = [{"id": "11111111-1111-1111-1111-111111111111"}]
        result = await svc.fetch_decision_ids_in_domain("ml")
        assert result == ["11111111-1111-1111-1111-111111111111"]
        query = mock_read.call_args[0][0]
        assert "(e:Decision)" in query
```

If the existing test file has no reusable fixture for `_run_read` mocking, create one local to these classes:

```python
@pytest.fixture
def graph_service_with_read():
    svc = GraphService(driver=MagicMock())
    mock_read = AsyncMock()
    svc._run_read = mock_read
    return svc, mock_read
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/services/test_graph_service.py -q -k "FetchActive or FetchCross or FetchDecisionIds"`
Expected: FAIL — `AttributeError: fetch_active_domains`.

- [ ] **Step 3: Implement** — in `graph_service.py` (note: all three use `_run_read`, which already returns `[]` on Neo4j errors — fault-tolerance is inherited):

```python
    # ── Cross-project (Spec C MVP β) ──

    async def fetch_active_domains(self, project_key: str, top_n: int = 2) -> list[str]:
        """Top-N domains of a project, ranked by classified-entity count."""
        query = (
            "MATCH (e)-[:BELONGS_TO]->(:Project {project_key: $project_key}) "
            "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
            "WITH d.name AS domain, count(e) AS n "
            "ORDER BY n DESC LIMIT $top_n "
            "RETURN domain"
        )
        rows = await self._run_read(query, {"project_key": project_key, "top_n": top_n})
        return [r["domain"] for r in rows]

    async def fetch_cross_project_entity_ids(
        self, domains: list[str], exclude_project_key: str, limit: int = 50
    ) -> list[dict]:
        """Candidate entities from OTHER projects in the given domains.

        Nodes carry no created_at — recency ordering happens later in PG.
        limit caps the candidate set handed to the PG brief query.
        """
        query = (
            "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
            "WHERE d.name IN $domains "
            "MATCH (e)-[:BELONGS_TO]->(p:Project) "
            "WHERE p.project_key <> $exclude "
            "RETURN e.id AS id, labels(e) AS labels, p.project_key AS project_key "
            "LIMIT $limit"
        )
        return await self._run_read(
            query, {"domains": domains, "exclude": exclude_project_key, "limit": limit}
        )

    async def fetch_decision_ids_in_domain(self, domain: str) -> list[str]:
        """All Decision node ids classified in a domain (resonance candidate pool)."""
        query = (
            "MATCH (e:Decision)-[:BELONGS_TO_DOMAIN]->(:Domain {name: $domain}) "
            "RETURN e.id AS id"
        )
        rows = await self._run_read(query, {"domain": domain})
        return [r["id"] for r in rows]
```

- [ ] **Step 4: Add instrumented wrappers** — in `src/brain_v42/metrics/instrument.py`, inside `InstrumentedGraphService` (mirror `find_orphans_for_classification` exactly):

```python
    async def fetch_active_domains(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_active_domains(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def fetch_cross_project_entity_ids(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_cross_project_entity_ids(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)

    async def fetch_decision_ids_in_domain(self, *args: Any, **kwargs: Any) -> list:
        start = time.monotonic()
        error = False
        try:
            result = await self._inner.fetch_decision_ids_in_domain(*args, **kwargs)
            return result  # type: ignore[no-any-return]
        except Exception:
            error = True
            raise
        finally:
            self._collector.record_graph_query((time.monotonic() - start) * 1000, error=error)
```

Add delegation tests in the instrument test file following its existing per-method pattern (locate with `grep -rln "find_orphans_for_classification" tests/unit` and copy that test's shape for the three new methods).

- [ ] **Step 5: Run tests** — targeted then full gate. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/services/graph_service.py src/brain_v42/metrics/instrument.py \
        tests/unit/services/test_graph_service.py tests/unit/metrics/
git commit -m "feat(graph): cross-project domain + entity-id read methods (Spec C)"
```

---

### Task 4: PgDecisionRepo — pgvector pair computation

**Files:**
- Modify: `src/brain_v42/repositories/pg_decision.py` (after `search_vector`)
- Test: `tests/unit/repositories/test_pg_decision.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/repositories/test_pg_decision.py`. There is NO `repo_with_session` fixture — the file's real pattern (see its existing tests ~lines 100-133) is `_make_mock_session()` + `pytest.MonkeyPatch.context()` patching `repo.get_session`. Reuse the file's existing `_make_mock_session`/`_mock_get_session` helpers; if their result shape differs, adapt the plumbing but keep the assertions:

```python
class TestFetchCrossProjectResonancePairs:
    async def test_sql_excludes_intra_project_and_nulls(self):
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            await repo.fetch_cross_project_resonance_pairs(
                ids=[str(uuid4()), str(uuid4())], threshold=0.80
            )
        sql = str(session.execute.call_args[0][0])
        assert "a.project_key <> b.project_key" in sql
        assert "a.embedding IS NOT NULL" in sql
        assert "a.id < b.id" in sql
        assert "ANY(CAST(:ids AS uuid[]))" in sql

    async def test_returns_row_dicts(self):
        row = {
            "a_id": uuid4(), "b_id": uuid4(),
            "a_project": "brain-v42", "b_project": "red-shrik",
            "a_title": "t1", "b_title": "t2",
            "a_created_at": date(2026, 4, 1), "b_created_at": date(2026, 4, 2),
            "cosine": 0.91,
        }
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.fetch_cross_project_resonance_pairs(
                ids=[str(uuid4())], threshold=0.8
            )
        assert result[0]["cosine"] == 0.91

    async def test_empty_ids_short_circuits_without_query(self):
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            assert await repo.fetch_cross_project_resonance_pairs(ids=[], threshold=0.8) == []
        session.execute.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/repositories/test_pg_decision.py -q -k Resonance`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement** — in `pg_decision.py`:

```python
    # ── Cross-project resonance (Spec C MVP β) ─────────────────────────────

    async def fetch_cross_project_resonance_pairs(
        self, *, ids: list[str], threshold: float
    ) -> list[dict]:
        """All cross-project decision pairs above cosine threshold, via pgvector.

        Pair compute stays in PG (no embedding payload crosses to Python).
        Caller bounds `ids` (MAX_DECISIONS_PER_DOMAIN cap upstream).
        ids are UUID strings; the explicit uuid[] cast keeps asyncpg's array
        binding unambiguous through sa.text.
        Returns plain row dicts; the resonance script maps them to ResonancePair.
        """
        if not ids:
            return []
        query = sa.text(
            """
            SELECT
                a.id AS a_id, b.id AS b_id,
                a.project_key AS a_project, b.project_key AS b_project,
                a.title AS a_title, b.title AS b_title,
                a.created_at::date AS a_created_at, b.created_at::date AS b_created_at,
                (1 - (a.embedding <=> b.embedding))::float AS cosine
            FROM decisions a
            JOIN decisions b ON a.id < b.id
            WHERE a.id = ANY(CAST(:ids AS uuid[])) AND b.id = ANY(CAST(:ids AS uuid[]))
              AND a.project_key <> b.project_key
              AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
              AND (1 - (a.embedding <=> b.embedding)) >= :threshold
            ORDER BY cosine DESC
            """
        )
        async with self.get_session() as sess:
            result = await sess.execute(query, {"ids": list(ids), "threshold": threshold})
            return [dict(r) for r in result.mappings().all()]
```

Note: `PgDecisionRepo` deliberately has NO injected constructor (`BasePgRepository.get_session` uses the global `get_session_factory()` from `brain_v42.db.engine`). Do NOT add an `__init__`; the resonance script constructs it as `PgDecisionRepo()` and shares the global engine.

- [ ] **Step 4: Run tests** — targeted then full gate. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/pg_decision.py tests/unit/repositories/test_pg_decision.py
git commit -m "feat(repo): pgvector cross-project resonance pair query (Spec C)"
```

---

### Task 5: CrossProjectBriefingService

**Files:**
- Create: `src/brain_v42/services/cross_project_service.py`
- Test: `tests/unit/services/test_cross_project_service.py`

- [ ] **Step 1: Write the failing tests** — create `tests/unit/services/test_cross_project_service.py`:

```python
"""Tests for CrossProjectBriefingService (Spec C MVP β)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.cross_project_service import (
    CrossEntry,
    CrossProjectBlock,
    CrossProjectBriefingService,
)


def _mk_service(graph: AsyncMock, rows_by_query: list[list[dict]] | None = None):
    """Service with a mocked session factory returning canned PG rows."""
    session = MagicMock()
    results = []
    for rows in rows_by_query or []:
        r = MagicMock()
        r.mappings.return_value.all.return_value = rows
        results.append(r)
    session.execute = AsyncMock(side_effect=results)
    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(return_value=session)
    sf.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = CrossProjectBriefingService(sf, graph, top_n=2, entries_max=5)
    return svc, session


@pytest.mark.asyncio
async def test_none_when_no_active_domains():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = []
    svc, _ = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None


@pytest.mark.asyncio
async def test_none_when_no_cross_project_entities():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    graph.fetch_cross_project_entity_ids.return_value = []
    svc, _ = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None


@pytest.mark.asyncio
async def test_entries_sorted_by_recency_and_capped():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml", "memory"]
    ids = [str(uuid4()) for _ in range(3)]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": ids[0], "labels": ["Decision"], "project_key": "red-shrik"},
        {"id": ids[1], "labels": ["Decision"], "project_key": "red-monitor"},
        {"id": ids[2], "labels": ["Learning"], "project_key": "red-shrik"},
    ]
    old, mid, new = (
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 15, tzinfo=UTC),
        datetime(2026, 5, 1, tzinfo=UTC),
    )
    decision_rows = [
        {"id": ids[0], "display": "old decision", "created_at": old},
        {"id": ids[1], "display": "new decision", "created_at": new},
    ]
    learning_rows = [{"id": ids[2], "display": "mid learning", "created_at": mid}]
    svc, _ = _mk_service(graph, rows_by_query=[decision_rows, learning_rows])
    block = await svc.fetch_block("brain-v42")
    assert isinstance(block, CrossProjectBlock)
    assert block.domains == ["ml", "memory"]
    assert [e.display for e in block.entries] == ["new decision", "mid learning", "old decision"]
    assert block.entries[0].project_key == "red-monitor"
    assert block.entries[0].entity_type == "Decision"


@pytest.mark.asyncio
async def test_unknown_labels_are_skipped():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": str(uuid4()), "labels": ["Feature"], "project_key": "watchk"},
    ]
    svc, session = _mk_service(graph)
    assert await svc.fetch_block("brain-v42") is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_display_truncated_at_60_chars():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    eid = str(uuid4())
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": eid, "labels": ["Decision"], "project_key": "red-shrik"},
    ]
    long_title = "x" * 80
    rows = [{"id": eid, "display": long_title, "created_at": datetime(2026, 5, 1, tzinfo=UTC)}]
    svc, _ = _mk_service(graph, rows_by_query=[rows])
    block = await svc.fetch_block("brain-v42")
    assert block.entries[0].display == "x" * 60 + "…"


@pytest.mark.asyncio
async def test_entries_max_cap_applied():
    graph = AsyncMock()
    graph.fetch_active_domains.return_value = ["ml"]
    ids = [str(uuid4()) for _ in range(7)]
    graph.fetch_cross_project_entity_ids.return_value = [
        {"id": i, "labels": ["Decision"], "project_key": "red-shrik"} for i in ids
    ]
    rows = [
        {"id": i, "display": f"d{n}", "created_at": datetime(2026, 5, 1, n + 1, tzinfo=UTC)}
        for n, i in enumerate(ids)
    ]
    svc, _ = _mk_service(graph, rows_by_query=[rows])
    block = await svc.fetch_block("brain-v42")
    assert len(block.entries) == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/services/test_cross_project_service.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — create `src/brain_v42/services/cross_project_service.py`:

```python
"""Cross-project briefing service — Spec C MVP β.

Combines Neo4j domain topology (which domains is this project active in,
which entities from OTHER projects share them) with PG display briefs.
Read-only. Neo4j faults degrade to "no section" via GraphService's
fault-tolerant _run_read; PG faults propagate to the caller, which is
expected to catch and omit the section (session_tools graceful-degrade).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
import structlog

logger = structlog.get_logger(__name__)

_DISPLAY_TRUNCATE = 60

# label → (table, display column). Keep in sync with db/tables.py short fields.
_BRIEF_SQL: dict[str, sa.TextClause] = {
    "Decision": sa.text(
        "SELECT id, title AS display, created_at FROM decisions WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Learning": sa.text(
        "SELECT id, topic AS display, created_at FROM learnings WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Snippet": sa.text(
        "SELECT id, title AS display, created_at FROM snippets WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Runbook": sa.text(
        "SELECT id, title AS display, created_at FROM runbooks WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "ADR": sa.text("SELECT id, title AS display, created_at FROM adrs WHERE id = ANY(CAST(:ids AS uuid[]))"),
}


@dataclass(frozen=True)
class CrossEntry:
    project_key: str
    entity_type: str
    display: str
    created_at: datetime | None


@dataclass(frozen=True)
class CrossProjectBlock:
    domains: list[str]
    entries: list[CrossEntry]


class CrossProjectBriefingService:
    # Convention: `self._sf` mirrors dream_run_service.py.
    def __init__(
        self,
        session_factory: Any,
        graph: Any,
        *,
        top_n: int = 2,
        entries_max: int = 5,
    ) -> None:
        self._sf = session_factory
        self._graph = graph
        self._top_n = top_n
        self._entries_max = entries_max

    async def fetch_block(self, project_key: str) -> CrossProjectBlock | None:
        """Top cross-project entries for the briefing, or None (section omitted)."""
        domains = await self._graph.fetch_active_domains(project_key, self._top_n)
        if not domains:
            return None
        candidates = await self._graph.fetch_cross_project_entity_ids(
            domains, exclude_project_key=project_key
        )
        # label -> {entity_id: source_project}; unknown labels (Feature, Plan...) skipped
        by_label: dict[str, dict[str, str]] = {}
        for row in candidates:
            label = next((lb for lb in row.get("labels", []) if lb in _BRIEF_SQL), None)
            if label is None:
                continue
            by_label.setdefault(label, {})[str(row["id"])] = row["project_key"]
        if not by_label:
            return None

        entries: list[CrossEntry] = []
        async with self._sf() as session:
            for label, id_to_project in by_label.items():
                result = await session.execute(
                    _BRIEF_SQL[label], {"ids": list(id_to_project.keys())}
                )
                for r in result.mappings().all():
                    display = r["display"] or ""
                    if len(display) > _DISPLAY_TRUNCATE:
                        display = display[:_DISPLAY_TRUNCATE] + "…"
                    entries.append(
                        CrossEntry(
                            project_key=id_to_project[str(r["id"])],
                            entity_type=label,
                            display=display,
                            created_at=r["created_at"],
                        )
                    )
        if not entries:
            return None
        entries.sort(key=lambda e: (e.created_at is not None, e.created_at), reverse=True)
        return CrossProjectBlock(domains=domains, entries=entries[: self._entries_max])
```

Note: the `CAST(:ids AS uuid[])` form (not bare `ANY(:ids)`) keeps the asyncpg array binding unambiguous — pass the ids as a plain `list[str]`.

- [ ] **Step 4: Run tests** — targeted then full gate. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/cross_project_service.py tests/unit/services/test_cross_project_service.py
git commit -m "feat(services): CrossProjectBriefingService — Neo4j domains + PG briefs (Spec C)"
```

---

### Task 6: Briefing section + wiring

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py`
- Modify: `src/brain_v42/mcp/server.py:382-394`
- Test: `tests/unit/mcp/test_session_tools.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/mcp/test_session_tools.py`. The file has NO pytest fixtures — it uses module-level helpers (`_no_activity_ks()` at ~line 30) and inline `register_session_tools(...)` + `await tool.fn(project_key="p")` per test (existing pattern at ~lines 337-346). Two CRITICAL plumbing rules: (a) `get_settings` is `@lru_cache(maxsize=1)` — do NOT monkeypatch env vars (no effect once cached); patch `brain_v42.mcp.tools.session_tools.get_settings` instead; (b) call `_format_session_briefing` positionally like the existing tests do.

```python
from brain_v42.services.cross_project_service import CrossEntry, CrossProjectBlock


def _block():
    return CrossProjectBlock(
        domains=["ml", "memory"],
        entries=[
            CrossEntry("red-shrik", "Decision", "embedding healthcheck pattern",
                       datetime(2026, 4, 28, tzinfo=UTC)),
            CrossEntry("red-monitor", "Learning", "go-pubsub close channel race",
                       datetime(2026, 4, 15, tzinfo=UTC)),
        ],
    )


class TestCrossProjectSection:
    def test_section_renders_domains_and_entries(self):
        out = _section_cross_project(_block())
        assert out.startswith("### Cross-project (ml, memory)")
        assert "- [red-shrik] Decision · 2026-04-28 · embedding healthcheck pattern" in out
        assert "- [red-monitor] Learning · 2026-04-15 · go-pubsub close channel race" in out

    def test_section_empty_when_block_none(self):
        assert _section_cross_project(None) == ""

    def test_section_empty_when_no_entries(self):
        assert _section_cross_project(CrossProjectBlock(domains=["ml"], entries=[])) == ""

    def test_briefing_backward_compat_without_cross_block(self):
        out = _format_session_briefing(None, [], [], _no_activity_ks(), None, [], [])
        assert "Cross-project" not in out

    def test_briefing_includes_cross_section_before_drill_in(self):
        out = _format_session_briefing(
            None, [], [], _no_activity_ks(), None, [], [], cross_block=_block()
        )
        assert "### Cross-project (ml, memory)" in out
        assert out.index("### Cross-project") > out.index("### Focus")
        assert out.index("### Cross-project") < out.index("→ More:")
```

Also add tool-level graceful tests. Build the mock `mcp` object + 5 `AsyncMock()` services EXACTLY like the existing `register_session_tools(mcp, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())` tests do (~line 306 and ~337-346 — copy their mcp/tool capture plumbing verbatim), adding `cross_project_svc=`:

```python
class TestCrossProjectInTool:
    async def test_cross_svc_failure_degrades_to_no_section(self):
        cross_svc = AsyncMock()
        cross_svc.fetch_block.side_effect = RuntimeError("neo4j boom")
        settings = MagicMock(brain_dream_cross_project_enabled=True, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            # <mcp/tool capture plumbing copied from existing tests>
            register_session_tools(
                mcp, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
                cross_project_svc=cross_svc,
            )
            result = await tool.fn(project_key="p")  # MUST NOT raise
        assert "Cross-project" not in result

    async def test_cross_svc_not_called_when_flag_off(self):
        cross_svc = AsyncMock()
        settings = MagicMock(brain_dream_cross_project_enabled=False, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            register_session_tools(
                mcp, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
                cross_project_svc=cross_svc,
            )
            await tool.fn(project_key="p")
        cross_svc.fetch_block.assert_not_called()

    async def test_cross_svc_used_when_flag_on(self):
        cross_svc = AsyncMock()
        cross_svc.fetch_block.return_value = _block()
        settings = MagicMock(brain_dream_cross_project_enabled=True, graph_enabled=False)
        with patch("brain_v42.mcp.tools.session_tools.get_settings", return_value=settings):
            register_session_tools(
                mcp, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
                cross_project_svc=cross_svc,
            )
            result = await tool.fn(project_key="p")
        assert "### Cross-project (ml, memory)" in result
```

(The `# <mcp/tool capture plumbing>` comment is the ONLY part to adapt — copy it from the file's existing tool tests. Keep every assertion as written. Note `get_settings` is patched where it's LOOKED UP (`session_tools`), not where it's defined — see learning ba1a5b8b.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_session_tools.py -q -k CrossProject`
Expected: FAIL — `ImportError: _section_cross_project`.

- [ ] **Step 3: Implement** — in `session_tools.py`:

After `_section_recap` (line ~131):

```python
def _section_cross_project(block: Any | None) -> str:
    if block is None or not block.entries:
        return ""
    lines = [f"### Cross-project ({', '.join(block.domains)})"]
    for e in block.entries[:_CAP]:
        day = e.created_at.date().isoformat() if e.created_at else "?"
        lines.append(f"- [{e.project_key}] {e.entity_type} · {day} · {e.display}")
    return "\n".join(lines)
```

In `_format_session_briefing`, add a kw-only param and the section:

```python
def _format_session_briefing(
    ctx: Any | None,
    decisions: list[Any],
    learnings: list[Any],
    killswitches: KillswitchState,
    last_failure: LastFailureRow | None,
    in_flight: list[Any],
    stale_pinned: list[Any],
    *,
    graph_enabled: bool = False,
    killswitch_unavailable: bool = False,
    cross_block: Any | None = None,
) -> str:
```

and in `sections`, insert between `_section_recap(...)` and `_section_drill_in_hint()`:

```python
        _section_recap(decisions, learnings),
        _section_cross_project(cross_block),
        _section_drill_in_hint(),
```

In `register_session_tools`, add the optional dependency (keyword, default `None` — all existing callers stay valid):

```python
def register_session_tools(
    mcp: Any,
    project_context_svc: Any,
    decision_svc: Any,
    learning_svc: Any,
    dream_run_svc: Any,
    feature_svc: Any,
    cross_project_svc: Any | None = None,
) -> None:
```

In `brain_session_start`, after the `graph_enabled` resolution block and before the return:

```python
        # Cross-project section (Spec C MVP β) — env-gated, fully optional.
        # Any failure degrades to "section omitted" per the graceful-degrade
        # contract; the killswitch keeps this a zero-overhead no-op when off.
        cross_block = None
        if cross_project_svc is not None:
            try:
                if get_settings().brain_dream_cross_project_enabled:
                    cross_block = await cross_project_svc.fetch_block(project_key)
            except Exception as exc:
                logger.warning("brain_session_start_cross_project_failed", error=str(exc))
```

and pass `cross_block=cross_block` to `_format_session_briefing`.

- [ ] **Step 4: Wire in server.py** — replace the `register_session_tools(...)` call block (`server.py:386-394`):

```python
    _session_factory = get_session_factory()
    _cross_project_svc = None
    if services["graph_service"] is not None:
        from brain_v42.services.cross_project_service import (  # noqa: PLC0415
            CrossProjectBriefingService,
        )

        _settings = get_settings()
        _cross_project_svc = CrossProjectBriefingService(
            _session_factory,
            services["graph_service"],
            top_n=_settings.brain_cross_project_briefing_domains_top_n,
            entries_max=_settings.brain_cross_project_briefing_entries_max,
        )
    register_session_tools(
        mcp,
        project_context_svc=services["project_context_svc"],
        decision_svc=services["decision_svc"],
        learning_svc=services["learning_svc"],
        dream_run_svc=DreamRunService(_session_factory),
        feature_svc=FeatureService(_session_factory),
        cross_project_svc=_cross_project_svc,
    )
```

(Match the surrounding function's import style — deferred imports with `# noqa: PLC0415` are the local convention. Check whether `get_settings` is already imported in that scope; if a `settings` object is already in scope, use it.)

- [ ] **Step 5: Run tests** — targeted, then the WHOLE unit suite + gate (this task touches the briefing used by many tests). Expected: all green, golden-snapshot/token-budget tests for session_start unaffected (section renders only when `cross_block` is truthy, and no existing test passes one).

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/mcp/tools/session_tools.py src/brain_v42/mcp/server.py tests/unit/mcp/test_session_tools.py
git commit -m "feat(briefing): cross-project section in brain_session_start (Spec C, env-gated)"
```

---

### Task 7: PROMOTE insulation filter

**Files:**
- Modify: `scripts/dream/promote_prepare.py` (`_CANDIDATE_SQL`, line ~30)
- Test: `tests/unit/test_promote_prepare.py`

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_promote_prepare.py` (import style: `from scripts.dream import promote_prepare` — already at the top of the file). NOTE: the rest of this file is DB-backed integration-style tests (skipped without a test DB); put this in its own class so the convention break is visible:

```python
class TestCandidateSqlInsulation:
    """Pure SQL-text check — runs without a DB, unlike the rest of this file."""

    def test_candidate_sql_excludes_cross_project_resonance_learnings(self):
        """Spec C insulation: resonance learnings must never enter the PROMOTE pool."""
        sql = str(promote_prepare._CANDIDATE_SQL)
        assert "'EXCLUDE_FROM_PROMOTE' != ALL(l.tags)" in sql
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_promote_prepare.py -q -k EXCLUDE or -k exclude_cross`
(use `-k exclud` to be safe) Expected: FAIL.

- [ ] **Step 3: Implement** — in `_CANDIDATE_SQL`, after the `dream:generated` clause and before `AND l.project_key = :pk`:

```sql
      -- Spec C insulation: learnings emitted by the cross-project resonance
      -- script tag themselves EXCLUDE_FROM_PROMOTE; promoting them would
      -- close a feedback loop (resonance → ADR → resonance).
      AND 'EXCLUDE_FROM_PROMOTE' != ALL(l.tags)
```

- [ ] **Step 4: Run tests** — `pytest tests/unit/test_promote_prepare.py -q` all green, then full gate.

- [ ] **Step 5: Commit**

```bash
git add scripts/dream/promote_prepare.py tests/unit/test_promote_prepare.py
git commit -m "feat(dream): exclude EXCLUDE_FROM_PROMOTE-tagged learnings from PROMOTE pool (Spec C insulation)"
```

---

### Task 8: ResonancePair + markdown report (script module, part 1)

**Files:**
- Create: `scripts/dream/cross_project_resonance.py` (dataclass + report only; `main` comes in Task 9)
- Create: `tests/unit/test_cross_project_resonance.py`

- [ ] **Step 1: Write the failing tests** — create `tests/unit/test_cross_project_resonance.py`:

```python
"""Tests for scripts/dream/cross_project_resonance.py (Spec C MVP β)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from scripts.dream.cross_project_resonance import (
    ResonancePair,
    build_report_path,
    render_markdown_report,
)

A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")


def _pair(**kw) -> ResonancePair:
    defaults = dict(
        a_id=A, b_id=B,
        a_project="brain-v42", b_project="red-shrik",
        a_title="Use Qodo-Embed-1.5B", b_title="Qodo-Embed for code embedding",
        a_created_at=date(2026, 4, 15), b_created_at=date(2026, 4, 22),
        cosine=0.91, domain="ml",
    )
    defaults.update(kw)
    return ResonancePair(**defaults)


class TestResonancePair:
    def test_dedup_key_stable_across_id_order(self):
        p1 = _pair(a_id=A, b_id=B)
        p2 = _pair(a_id=B, b_id=A)
        assert p1.dedup_key == p2.dedup_key

    def test_dedup_key_differs_by_domain(self):
        assert _pair(domain="ml").dedup_key != _pair(domain="memory").dedup_key

    def test_hint_drift_on_numeric_divergence(self):
        p = _pair(a_title="Cosine 0.92 for dedup", b_title="Cosine 0.85 for dedup")
        assert p.hint.startswith("drift candidate")
        assert "0.92" in p.hint and "0.85" in p.hint

    def test_hint_convergence_without_divergence(self):
        assert _pair().hint.startswith("convergence likely")

    def test_format_insight_includes_both_projects_and_hint(self):
        text = _pair().format_insight()
        assert "[brain-v42]" in text and "[red-shrik]" in text
        assert "cosine=0.910" in text
        assert "Hint:" in text


class TestReport:
    def test_report_groups_by_domain_with_counts(self):
        pairs = [_pair(), _pair(domain="memory", cosine=0.83)]
        md = render_markdown_report(pairs, threshold=0.80, run_id=42, report_date="2026-06-12")
        assert "# Cross-Project Resonance — 2026-06-12" in md
        assert "Pairs found: 2" in md
        assert "Run ID: 42" in md
        assert "## Domain: ml (1 pair" in md
        assert "## Domain: memory (1 pair" in md
        assert "cosine=0.91" in md

    def test_report_zero_pairs(self):
        md = render_markdown_report([], threshold=0.80, run_id=42, report_date="2026-06-12")
        assert "Pairs found: 0" in md
        assert "No cross-project resonance pairs above threshold this run." in md

    def test_build_report_path_uses_date(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = build_report_path("2026-06-12")
        assert p.name == "cross_project_resonance_2026-06-12.md"
        assert p.parent.name == "dream"
        assert p.parent.parent.name == "artifacts"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_cross_project_resonance.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — create `scripts/dream/cross_project_resonance.py` (part 1: imports, constants, dataclass, report; `main` placeholder absent for now):

```python
#!/usr/bin/env python3
"""Cross-project resonance detector — Dream v3 Spec C MVP β.

Surfaces pairs of decisions from DIFFERENT projects within the same
knowledge domain whose embeddings are highly similar (cosine >= threshold).
The algorithm does not judge convergence vs drift — a heuristic hint is
attached, the human interprets.

DRY_RUN by default: writes a markdown report only. WET mode (opt-in,
double-gated) additionally writes insulated learnings to the brain.

Usage:
    python -m scripts.dream.cross_project_resonance [--mode dry_run|wet]
        [--domains ml,memory] [--date YYYY-MM-DD]

Killswitch: BRAIN_DREAM_CROSS_PROJECT_ENABLED (default false → exit 0 no-op).
Threshold: thresholds.by_name("cross_project_resonance_min") — no env var.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

MIN_DECISIONS_PER_DOMAIN = 5
MAX_DECISIONS_PER_DOMAIN = 200  # hard cap to bound PG pair-compute cost
MAX_PAIRS_PER_NIGHT = 20
PHASE = "RESONANCE"  # dream_runs.phase is VARCHAR(10)


@dataclass(frozen=True)
class ResonancePair:
    a_id: UUID
    b_id: UUID
    a_project: str
    b_project: str
    a_title: str
    b_title: str
    a_created_at: date
    b_created_at: date
    cosine: float
    domain: str

    @property
    def hint(self) -> str:
        """Heuristic only, never authoritative."""
        nums_a = set(re.findall(r"\d+\.\d+", self.a_title))
        nums_b = set(re.findall(r"\d+\.\d+", self.b_title))
        if nums_a and nums_b and nums_a != nums_b:
            return f"drift candidate (numeric divergence: {sorted(nums_a)} vs {sorted(nums_b)})"
        return "convergence likely (no numeric divergence detected)"

    @property
    def dedup_key(self) -> str:
        """SHA256 of the canonical pair fingerprint — WET idempotency."""
        lo, hi = sorted([str(self.a_id), str(self.b_id)])
        return hashlib.sha256(f"{lo}|{hi}|{self.domain}".encode()).hexdigest()

    def format_insight(self) -> str:
        """Body for the WET-mode learning."""
        return (
            f"Cross-project resonance in domain '{self.domain}' (cosine={self.cosine:.3f}):\n"
            f"- [{self.a_project}] {self.a_title} ({self.a_created_at})\n"
            f"- [{self.b_project}] {self.b_title} ({self.b_created_at})\n"
            f"Hint: {self.hint}"
        )


def build_report_path(report_date: str) -> Path:
    """artifacts/dream/cross_project_resonance_<UTC-ISO-date>.md (repo-relative)."""
    return Path("artifacts") / "dream" / f"cross_project_resonance_{report_date}.md"


def render_markdown_report(
    pairs: list[ResonancePair], *, threshold: float, run_id: int, report_date: str
) -> str:
    domains_with_pairs: dict[str, list[ResonancePair]] = {}
    for p in pairs:
        domains_with_pairs.setdefault(p.domain, []).append(p)
    lines = [
        f"# Cross-Project Resonance — {report_date}",
        "",
        f"Threshold: {threshold:.2f} · Pairs found: {len(pairs)} · "
        f"Domains with pairs: {len(domains_with_pairs)} · Run ID: {run_id}",
        "",
    ]
    if not pairs:
        lines.append("No cross-project resonance pairs above threshold this run.")
        return "\n".join(lines) + "\n"
    for domain in sorted(domains_with_pairs):
        dpairs = domains_with_pairs[domain]
        plural = "pair" if len(dpairs) == 1 else "pairs"
        lines.append(f"## Domain: {domain} ({len(dpairs)} {plural})")
        lines.append("")
        for n, p in enumerate(dpairs, start=1):
            lines.append(f"### Pair {n} — cosine={p.cosine:.2f}")
            lines.append(f"- [{p.a_project}] Decision {str(p.a_id)[:8]}… · "
                         f"\"{p.a_title}\" · {p.a_created_at}")
            lines.append(f"- [{p.b_project}] Decision {str(p.b_id)[:8]}… · "
                         f"\"{p.b_title}\" · {p.b_created_at}")
            lines.append(f"- Hint: {p.hint}")
            lines.append("")
    return "\n".join(lines)
```

(`render_markdown_report` takes keyword args in tests — make `threshold`, `run_id`, `report_date` keyword-only as tested. `UTC`/`datetime`/`argparse`/`asyncio`/`sys` imports will be used by Task 9 — if ruff flags F401 at this commit, add them in Task 9 instead.)

- [ ] **Step 4: Run tests** — targeted then full gate (ruff will catch unused imports — trim to only what part 1 uses). Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/dream/cross_project_resonance.py tests/unit/test_cross_project_resonance.py
git commit -m "feat(dream): ResonancePair + resonance markdown report (Spec C, part 1)"
```

---

### Task 9: Resonance script main — dream_runs traceability, DRY/WET

**Files:**
- Modify: `scripts/dream/cross_project_resonance.py` (add DB plumbing + `main`)
- Modify: `tests/unit/test_cross_project_resonance.py` (add main-flow tests)

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_cross_project_resonance.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.dream import cross_project_resonance as cpr


def _settings(enabled: bool):
    s = MagicMock()
    s.brain_dream_cross_project_enabled = enabled
    s.postgres_url = "postgresql+asyncpg://u:p@h:5432/db"
    return s


class TestMainGates:
    @pytest.mark.asyncio
    async def test_disabled_env_exits_0_without_db(self):
        with patch.object(cpr, "_load_settings", return_value=_settings(False)):
            rc = await cpr.run(mode="dry_run", domains=None, date_str=None)
        assert rc == 0

    @pytest.mark.asyncio
    async def test_missing_threshold_exits_1(self):
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr.thresholds, "by_name", return_value=None),
        ):
            rc = await cpr.run(mode="dry_run", domains=None, date_str=None)
        assert rc == 1


class TestMainFlow:
    def _wire(self, graph_ids, pair_rows):
        """Patch all I/O seams; return dict of mocks."""
        m = {
            "graph": AsyncMock(),
            "repo": AsyncMock(),
            "start": AsyncMock(return_value=42),       # _insert_run -> run_id
            "finish": AsyncMock(),                      # _finish_run
            "exists": AsyncMock(return_value=False),    # _learning_exists
            "write_learning": AsyncMock(),              # _write_learning
            "write_report": MagicMock(),                 # _write_report_file
        }
        m["graph"].fetch_decision_ids_in_domain.side_effect = graph_ids
        m["repo"].fetch_cross_project_resonance_pairs.return_value = pair_rows
        m["deps"] = (MagicMock(), m["graph"], m["repo"])  # (session_factory, graph, repo)
        return m

    def _row(self, cosine=0.9):
        return {
            "a_id": A, "b_id": B,
            "a_project": "brain-v42", "b_project": "red-shrik",
            "a_title": "t1", "b_title": "t2",
            "a_created_at": date(2026, 4, 1), "b_created_at": date(2026, 4, 2),
            "cosine": cosine,
        }

    @pytest.mark.asyncio
    async def test_dry_run_writes_report_no_learnings(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", m["write_report"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="dry_run", domains=None, date_str="2026-06-12")
        assert rc == 0
        m["write_report"].assert_called_once()
        m["write_learning"].assert_not_called()
        m["finish"].assert_awaited_once()
        assert m["finish"].call_args.kwargs.get("status", m["finish"].call_args[0][-1]) == "done"

    @pytest.mark.asyncio
    async def test_domain_below_min_is_skipped(self):
        m = self._wire(graph_ids=[[str(A)]] * 9, pair_rows=[])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
        ):
            await cpr.run(mode="dry_run", domains=None, date_str="2026-06-12")
        m["repo"].fetch_cross_project_resonance_pairs.assert_not_called()

    @pytest.mark.asyncio
    async def test_wet_writes_learnings_with_dedup(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        m["exists"].return_value = False
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_learning_exists", m["exists"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        assert rc == 0
        m["write_learning"].assert_awaited_once()
        pair_arg = m["write_learning"].call_args[0][1]  # (session_factory_or_run_id, pair, ...)
        assert isinstance(pair_arg, cpr.ResonancePair)

    @pytest.mark.asyncio
    async def test_wet_skips_existing_dedup_key(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=[self._row()])
        m["exists"].return_value = True
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_learning_exists", m["exists"]),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        m["write_learning"].assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_marks_run_fail_and_reraises(self):
        m = self._wire(graph_ids=RuntimeError("neo4j down"), pair_rows=[])
        m["graph"].fetch_decision_ids_in_domain.side_effect = RuntimeError("neo4j down")
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            pytest.raises(RuntimeError),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        status_arg = m["finish"].call_args.kwargs.get("status") or m["finish"].call_args[0][-1]
        assert status_arg == "fail"

    @pytest.mark.asyncio
    async def test_pairs_capped_at_max_per_night(self):
        ids = [str(UUID(int=i)) for i in range(6)]
        rows = [self._row(cosine=0.80 + i / 1000) for i in range(30)]
        m = self._wire(graph_ids=[ids] + [[]] * 8, pair_rows=rows)
        report_mock = MagicMock()
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", report_mock),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        pairs_arg = report_mock.call_args[0][1]
        assert len(pairs_arg) == cpr.MAX_PAIRS_PER_NIGHT
        assert pairs_arg[0].cosine >= pairs_arg[-1].cosine

    @pytest.mark.asyncio
    async def test_domain_ids_capped_at_max_decisions(self):
        ids = [str(UUID(int=i)) for i in range(250)]  # > MAX_DECISIONS_PER_DOMAIN
        m = self._wire(graph_ids=[ids], pair_rows=[])
        with (
            patch.object(cpr, "_load_settings", return_value=_settings(True)),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
        ):
            await cpr.run(mode="dry_run", domains=["ml"], date_str="2026-06-12")
        sent_ids = m["repo"].fetch_cross_project_resonance_pairs.call_args.kwargs["ids"]
        assert len(sent_ids) == cpr.MAX_DECISIONS_PER_DOMAIN

    @pytest.mark.asyncio
    async def test_wet_blocked_when_inner_recheck_disabled(self):
        """Spec safeguard 3c: env re-read just before WET writes must block."""
        ids = [str(UUID(int=i)) for i in range(6)]
        m = self._wire(graph_ids=[ids], pair_rows=[self._row()])
        with (
            patch.object(
                cpr, "_load_settings", side_effect=[_settings(True), _settings(False)]
            ),
            patch.object(cpr, "_build_deps", return_value=m["deps"]),
            patch.object(cpr, "_insert_run", m["start"]),
            patch.object(cpr, "_finish_run", m["finish"]),
            patch.object(cpr, "_write_report_file", MagicMock()),
            patch.object(cpr, "_write_learning", m["write_learning"]),
        ):
            rc = await cpr.run(mode="wet", domains=["ml"], date_str="2026-06-12")
        assert rc == 1
        m["write_learning"].assert_not_called()


class TestReportFile:
    def test_report_file_overwritten_on_rerun(self, tmp_path):
        p = tmp_path / "r.md"
        cpr._write_report_file(p, [], threshold=0.8, run_id=1, report_date="2026-06-12")
        cpr._write_report_file(p, [], threshold=0.8, run_id=2, report_date="2026-06-12")
        assert "Run ID: 2" in p.read_text()
        assert "Run ID: 1" not in p.read_text()
```

(The seam names `_load_settings`, `_build_deps`, `_insert_run`, `_finish_run`, `_learning_exists`, `_write_learning`, `_write_report_file` are the contract — implement `run()` against exactly these so the tests patch cleanly. `_build_deps(settings)` returns the triple `(session_factory, graph, repo)` — `run()` must obtain ALL THREE from it so tests never touch a real engine. `_load_settings` is called twice in a WET run: once at entry, once right before WET writes (safeguard 3c).)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_cross_project_resonance.py -q`
Expected: new tests FAIL (`AttributeError: run`), Task 8 tests still PASS.

- [ ] **Step 3: Implement** — append to `scripts/dream/cross_project_resonance.py`:

```python
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42 import thresholds
from brain_v42.config import Settings
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.graph_service import ALLOWED_DOMAINS, GraphService

logger = structlog.get_logger(__name__)

_SF = async_sessionmaker[AsyncSession]


def _load_settings() -> Settings:
    return Settings()


def _build_deps(settings: Settings) -> tuple[_SF, GraphService, PgDecisionRepo]:
    """Construct PG session factory + Neo4j + decision repo. Single test seam.

    Uses the global engine singleton (brain_v42.db.engine.get_session_factory)
    so PgDecisionRepo() — which has NO injectable constructor, its get_session
    reads the same singleton — shares one engine with everything else here.
    One-shot script + single asyncio.run loop, so the factory/loop-binding
    gotcha (pytest-asyncio singleton learning) does not apply.
    """
    from neo4j import AsyncGraphDatabase  # noqa: PLC0415

    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    session_factory = get_session_factory()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_url or "bolt://localhost:7687",
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    graph = GraphService(driver, timeout=settings.neo4j_timeout)
    return session_factory, graph, PgDecisionRepo()
```

(`neo4j_timeout` exists in config.py:88, default 5.0. Do NOT add an `__init__` to `PgDecisionRepo`.)

```python
async def _insert_run(session_factory: _SF, *, run_date: date, dry_run: bool) -> int:
    """Insert the RESONANCE dream_runs row up-front; returns run id.

    status='fail' placeholder — flipped to 'done' by _finish_run. A crash
    mid-run therefore leaves an honest 'fail' row (status enum: done|timeout|fail).
    """
    stmt = sa.text(
        "INSERT INTO dream_runs (run_date, phase, status, phase_dry_run, error_message) "
        "VALUES (:rd, :phase, 'fail', :dry, 'incomplete') RETURNING id"
    )
    async with session_factory() as session:
        result = await session.execute(
            stmt, {"rd": run_date, "phase": PHASE, "dry": dry_run}
        )
        await session.commit()
        return int(result.scalar_one())


async def _finish_run(
    session_factory: _SF, run_id: int, *, status: str, duration_s: float, error: str | None = None
) -> None:
    stmt = sa.text(
        "UPDATE dream_runs SET status = :st, duration_s = :du, error_message = :err "
        "WHERE id = :id"
    )
    async with session_factory() as session:
        await session.execute(
            stmt, {"st": status, "du": duration_s, "err": error, "id": run_id}
        )
        await session.commit()


async def _learning_exists(session_factory: _SF, dedup_key: str) -> bool:
    stmt = sa.text(
        "SELECT 1 FROM learnings WHERE metadata->>'dedup_key' = :dk LIMIT 1"
    )
    async with session_factory() as session:
        result = await session.execute(stmt, {"dk": dedup_key})
        return result.scalar() is not None


async def _write_learning(session_factory: _SF, pair: ResonancePair, run_id: int) -> None:
    repo = PgLearningRepo(session_factory)
    await repo.create(
        LearningCreate(
            topic=f"cross_project_resonance/{pair.domain}",
            insight=pair.format_insight(),
            source="scripts/dream/cross_project_resonance.py",
            source_type="automated",
            confidence="low",
            project_key="brain-v42",
            tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
            metadata={"dedup_key": pair.dedup_key, "dream_run_id": run_id},
        )
    )


def _write_report_file(path: Path, pairs: list[ResonancePair], *, threshold: float,
                       run_id: int, report_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_markdown_report(pairs, threshold=threshold, run_id=run_id,
                               report_date=report_date),
        encoding="utf-8",
    )


async def run(*, mode: str, domains: list[str] | None, date_str: str | None) -> int:
    settings = _load_settings()
    if not settings.brain_dream_cross_project_enabled:
        logger.info("cross_project_resonance_disabled")
        return 0

    spec = thresholds.by_name("cross_project_resonance_min")
    if spec is None:
        logger.error("threshold_missing", name="cross_project_resonance_min")
        return 1
    threshold = spec.value

    report_date = date_str or datetime.now(tz=UTC).date().isoformat()
    run_date = date.fromisoformat(report_date)
    target_domains = domains or sorted(ALLOWED_DOMAINS)

    session_factory, graph, repo = _build_deps(settings)
    run_id = await _insert_run(session_factory, run_date=run_date, dry_run=(mode != "wet"))
    started = datetime.now(tz=UTC)

    try:
        all_pairs: list[ResonancePair] = []
        for domain in target_domains:
            ids = await graph.fetch_decision_ids_in_domain(domain)
            if len(ids) < MIN_DECISIONS_PER_DOMAIN:
                continue
            rows = await repo.fetch_cross_project_resonance_pairs(
                ids=ids[:MAX_DECISIONS_PER_DOMAIN],  # UUID strings, cast in SQL
                threshold=threshold,
            )
            all_pairs.extend(ResonancePair(domain=domain, **row) for row in rows)

        all_pairs.sort(key=lambda p: p.cosine, reverse=True)
        all_pairs = all_pairs[:MAX_PAIRS_PER_NIGHT]

        _write_report_file(
            build_report_path(report_date), all_pairs,
            threshold=threshold, run_id=run_id, report_date=report_date,
        )

        if mode == "wet":
            # Safeguard 3c (spec): fresh env re-read right before writes —
            # a stale `settings` object would make this check dead code.
            if not _load_settings().brain_dream_cross_project_enabled:
                await _finish_run(
                    session_factory, run_id, status="fail",
                    duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
                    error="WET blocked: env disabled",
                )
                return 1
            written = 0
            for pair in all_pairs:
                if await _learning_exists(session_factory, pair.dedup_key):
                    continue
                await _write_learning(session_factory, pair, run_id)
                written += 1
            logger.info("resonance_wet_written", count=written)

        await _finish_run(
            session_factory, run_id, status="done",
            duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
        )
        logger.info("resonance_done", pairs=len(all_pairs), mode=mode, run_id=run_id)
        return 0
    except Exception as exc:
        await _finish_run(
            session_factory, run_id, status="fail",
            duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
            error=str(exc)[:500],
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dry_run", "wet"], default="dry_run")
    parser.add_argument("--domains", default=None,
                        help="Comma-separated domain subset (default: all 9)")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args(argv)
    domains = args.domains.split(",") if args.domains else None
    return asyncio.run(run(mode=args.mode, domains=domains, date_str=args.date))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests** — `pytest tests/unit/test_cross_project_resonance.py -q` then the FULL gate (ruff on `scripts/` is not CI-gated but keep it clean anyway; `mypy src/` doesn't cover scripts).

- [ ] **Step 5: Smoke-test the gates manually** (no DB write — killswitch off):

```bash
.venv/bin/python -m scripts.dream.cross_project_resonance --mode dry_run; echo "exit=$?"
```
Expected: log line `cross_project_resonance_disabled`, `exit=0`.

- [ ] **Step 6: Commit**

```bash
git add scripts/dream/cross_project_resonance.py tests/unit/test_cross_project_resonance.py
git commit -m "feat(dream): resonance script main — DRY/WET, dream_runs traceability (Spec C)"
```

---

### Task 10: Docs + full verification

**Files:**
- Modify: `CLAUDE.md` (Configuration section)
- Modify: `docs/superpowers/specs/2026-05-01-spec-c-mvp-cross-project-design.md` (status header only)

- [ ] **Step 1: Document env vars** — in `CLAUDE.md` Configuration block, after the Code Mode line:

```bash
# Cross-project (Dream v3 Spec C MVP β — disabled by default)
BRAIN_DREAM_CROSS_PROJECT_ENABLED=false
BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N=2
BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX=5
```

- [ ] **Step 2: Update spec status** — change the spec header `**Status** : Design (brainstorm complete, awaiting plan)` to `**Status** : Implemented 2026-06-12 (plan: docs/plans/2026-06-12-spec-c-cross-project-resonance.md) — killswitch closed, rollout pending`.

- [ ] **Step 3: Full verification**

```bash
.venv/bin/python -m pytest tests/unit -q          # all green (1877 baseline + ~35 new)
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/ruff format --check src/ tests/ scripts/
.venv/bin/mypy src/
```
Expected: 0 failures, 0 lint errors, mypy clean.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-05-01-spec-c-mvp-cross-project-design.md
git commit -m "docs: document Spec C env vars + mark spec implemented (rollout pending)"
```

---

## Out of scope (per spec Non-Goals — do NOT implement)

- No `brain_search` modification, no new MCP tool, no cron wiring, no integration tests, no `brain_log_decision` inline warning, no DB/Neo4j migration.
- WET mode stays unreachable in prod until the user flips `BRAIN_DREAM_CROSS_PROJECT_ENABLED` (rollout J+1..J+10 is a human decision).

## Rollout reminder (post-merge, manual)

```
J+0   : merged, killswitch closed (no-op)
J+1   : ENABLED=true locally, verify briefing on 2-3 sessions
J+2-5 : nightly manual DRY_RUN, review artifacts/dream/*.md
J+6+  : consider WET (PROMOTE filter already shipped)
```
