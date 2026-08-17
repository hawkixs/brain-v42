# Spec C MVP β — Cross-Project Briefing & Resonance Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two cross-project surfaces — `brain_session_start` enriched with cross-project entries from active domains, and a DRY_RUN-able resonance detection script — both behind a closed killswitch (`BRAIN_DREAM_CROSS_PROJECT_ENABLED=false`), on a feature branch, with PROMOTE insulation against feedback loop.

**Architecture:** Additive only, zero schema migration. Briefing path adds optional `cross_entries` param to `_format_session_briefing`, fed by 2 Cypher queries + 1-2 PG batch fetches when killswitch is open. Resonance script computes cross-project pairs in PG (pgvector `<=>`), bounded to 200 decisions per domain, with WET writes idempotent via `metadata.dedup_key`. PROMOTE phase gets a one-line `tags` filter to exclude resonance learnings.

**Tech Stack:** Python 3.12, FastMCP, SQLAlchemy 2.0 async, asyncpg, pgvector (PG-side cosine), Neo4j 5 (Domain registry), pytest+TDD.

**Spec reference:** `docs/superpowers/specs/2026-05-01-spec-c-mvp-cross-project-design.md` (v2 post-multi-judge)

---

## Pre-flight

**Verified by exploration during spec v2:**
- `decisions.embedding` is `Vector(1536)` nullable — script filters `WHERE embedding IS NOT NULL`
- `learnings.tags ARRAY(Text)` and `learnings.metadata JSONB` exist — used for `EXCLUDE_FROM_PROMOTE` tag and `dedup_key`/`source_kind` storage (no migration)
- `src/brain_v42/thresholds.py` has `by_name(name) -> ThresholdSpec | None`
- `ALLOWED_DOMAINS` constant in `src/brain_v42/services/graph_service.py` (lines 20-30)
- `dream_runs` table exists, used by promote_validate
- pgvector `<=>` natively supported via `pg_decision.py` and `pg_base.py`

**Baseline tests:** `pytest tests/unit -v` must report `1837 passed` before starting (post-Option 4 cosine-threshold-registry merge `d1f93d2`).

---

## Task 0: Setup feature branch

**Files:** none (git only).

- [ ] **Step 1: Verify clean working tree on main**

Run: `git status --short`
Expected: only untracked files unrelated to this work (e.g. `AGENTS.md`, brainstorm WIP). No staged/modified files in `src/`.

- [ ] **Step 2: Verify baseline test count**

Run: `pytest tests/unit -q 2>&1 | tail -3`
Expected: `1837 passed` (or current count post-merge of last MR; record actual number for non-regression checks throughout).

- [ ] **Step 3: Create branch**

Run:
```bash
git checkout -b feat/dream-cross-project-resonance-and-briefing
```

- [ ] **Step 4: Verify branch**

Run: `git branch --show-current`
Expected: `feat/dream-cross-project-resonance-and-briefing`

---

## Task 1: Threshold registry entry

**Files:**
- Modify: `src/brain_v42/thresholds.py` (append entry to `REGISTRY`)
- Test: `tests/unit/test_thresholds.py` (extend if exists, else create)

- [ ] **Step 1: Locate REGISTRY in thresholds.py**

Run: `grep -n "REGISTRY" src/brain_v42/thresholds.py | head -5`
Note the line where `REGISTRY: tuple[ThresholdSpec, ...] = (` opens and closes.

- [ ] **Step 2: Write failing test**

In `tests/unit/test_thresholds.py` (create file if absent), add:

```python
from brain_v42.thresholds import by_name


def test_cross_project_resonance_threshold_present():
    spec = by_name("cross_project_resonance_min")
    assert spec is not None
    assert spec.value == 0.80
    assert spec.calibrated is False
    assert spec.domain == "dream"


def test_threshold_lookup_via_by_name_returns_value():
    spec = by_name("cross_project_resonance_min")
    assert spec is not None
    assert isinstance(spec.value, float)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_thresholds.py::test_cross_project_resonance_threshold_present -v`
Expected: FAIL — `assert spec is not None` fails (spec returns None).

- [ ] **Step 4: Add entry to REGISTRY**

Edit `src/brain_v42/thresholds.py`, inside `REGISTRY = (...)`, append (style-match the existing entries):

```python
ThresholdSpec(
    name="cross_project_resonance_min",
    value=0.80,
    domain="dream",
    rationale="Min cosine to surface decision pair as cross-project resonance candidate (Spec C MVP β). Recalibrate after 5+ nights of DRY_RUN data.",
    calibrated=False,
),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_thresholds.py -v`
Expected: both new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/thresholds.py tests/unit/test_thresholds.py
git commit -m "feat(thresholds): add cross_project_resonance_min entry (0.80, uncalibrated)"
```

---

## Task 2: Env config additions

**Files:**
- Modify: `src/brain_v42/config.py` (add 3 fields to Settings)
- Test: `tests/unit/test_config.py` (extend or create)

- [ ] **Step 1: Read current Settings class**

Run: `grep -n "class Settings" src/brain_v42/config.py`
Read the surrounding ~30 lines to learn the pattern (likely Pydantic Settings with env-prefix or env-name overrides).

- [ ] **Step 2: Write failing tests**

In `tests/unit/test_config.py` add:

```python
import os
import pytest

from brain_v42.config import Settings


def test_cross_project_enabled_defaults_to_false(monkeypatch):
    monkeypatch.delenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", raising=False)
    s = Settings()
    assert s.dream_cross_project_enabled is False


def test_cross_project_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "true")
    s = Settings()
    assert s.dream_cross_project_enabled is True


def test_briefing_top_n_defaults_to_2(monkeypatch):
    monkeypatch.delenv("BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N", raising=False)
    s = Settings()
    assert s.cross_project_briefing_top_n == 2


def test_briefing_entries_max_defaults_to_5(monkeypatch):
    monkeypatch.delenv("BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX", raising=False)
    s = Settings()
    assert s.cross_project_briefing_entries_max == 5


def test_briefing_top_n_reads_env_override(monkeypatch):
    monkeypatch.setenv("BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N", "3")
    s = Settings()
    assert s.cross_project_briefing_top_n == 3
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_config.py -v -k cross_project`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'dream_cross_project_enabled'` (or similar).

- [ ] **Step 4: Add fields to Settings**

In `src/brain_v42/config.py`, inside the `Settings` class (matching the existing field-declaration pattern, typically `Field(default=..., env=...)` or pydantic-settings `model_config`):

```python
dream_cross_project_enabled: bool = Field(default=False, env="BRAIN_DREAM_CROSS_PROJECT_ENABLED")
cross_project_briefing_top_n: int = Field(default=2, env="BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N")
cross_project_briefing_entries_max: int = Field(default=5, env="BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX")
```

(If config uses `pydantic_settings` with `env_prefix`, adapt: drop the `env=` argument and rely on the prefix convention.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_config.py -v -k cross_project`
Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/config.py tests/unit/test_config.py
git commit -m "feat(config): add cross-project killswitch + briefing top_n/entries_max env vars"
```

---

## Task 3: GraphService.fetch_active_domains

**Files:**
- Modify: `src/brain_v42/services/graph_service.py` (add method)
- Test: `tests/unit/services/test_graph_service.py` (extend)

- [ ] **Step 1: Write failing test**

In `tests/unit/services/test_graph_service.py`, add:

```python
import pytest

from brain_v42.services.graph_service import GraphService


@pytest.mark.asyncio
async def test_fetch_active_domains_returns_top_n_by_count(monkeypatch):
    captured = {}

    async def fake_run_read(query, params):
        captured["query"] = query
        captured["params"] = params
        return [
            {"domain": "ml"},
            {"domain": "memory"},
        ]

    svc = GraphService(driver=None)
    monkeypatch.setattr(svc, "_run_read", fake_run_read)

    result = await svc.fetch_active_domains(project_key="brain-v42", top_n=2)

    assert result == ["ml", "memory"]
    assert captured["params"]["current"] == "brain-v42"
    assert captured["params"]["top_n"] == 2
    # Verify the Cypher counts entities per domain
    assert "BELONGS_TO_DOMAIN" in captured["query"]
    assert "count(e)" in captured["query"]
    assert "ORDER BY n DESC" in captured["query"]


@pytest.mark.asyncio
async def test_fetch_active_domains_returns_empty_when_no_entities(monkeypatch):
    svc = GraphService(driver=None)

    async def fake_run_read(_q, _p):
        return []

    monkeypatch.setattr(svc, "_run_read", fake_run_read)

    result = await svc.fetch_active_domains(project_key="empty-project", top_n=2)
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_active_domains`
Expected: FAIL — `AttributeError: 'GraphService' object has no attribute 'fetch_active_domains'`.

- [ ] **Step 3: Implement method**

In `src/brain_v42/services/graph_service.py`, add (in the GraphService class body, near other `fetch_*` / Cypher methods):

```python
async def fetch_active_domains(self, project_key: str, top_n: int) -> list[str]:
    """Return the top-N domain names in which `project_key` has the most
    classified entities. Ordered by count desc.

    Used by brain_session_start cross-project briefing.
    """
    query = (
        "MATCH (e)-[:BELONGS_TO]->(:Project {project_key: $current}) "
        "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
        "WITH d.name AS domain, count(e) AS n "
        "ORDER BY n DESC LIMIT $top_n "
        "RETURN domain"
    )
    rows = await self._run_read(query, {"current": project_key, "top_n": top_n})
    return [r["domain"] for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_active_domains`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/graph_service.py tests/unit/services/test_graph_service.py
git commit -m "feat(graph): GraphService.fetch_active_domains for briefing top-N"
```

---

## Task 4: GraphService.fetch_cross_project_entity_ids

**Files:**
- Modify: `src/brain_v42/services/graph_service.py`
- Test: `tests/unit/services/test_graph_service.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/services/test_graph_service.py`:

```python
@pytest.mark.asyncio
async def test_fetch_cross_project_entity_ids_excludes_current_project(monkeypatch):
    captured = {}

    async def fake_run_read(query, params):
        captured["query"] = query
        captured["params"] = params
        return [
            {"id": "uuid-1", "types": ["Decision"], "project": "red-shrik", "created_at": "2026-04-28"},
            {"id": "uuid-2", "types": ["Learning"], "project": "red-monitor", "created_at": "2026-04-15"},
        ]

    svc = GraphService(driver=None)
    monkeypatch.setattr(svc, "_run_read", fake_run_read)

    result = await svc.fetch_cross_project_entity_ids(
        domains=["ml", "memory"],
        exclude_project_key="brain-v42",
        limit=5,
    )

    assert len(result) == 2
    assert result[0]["id"] == "uuid-1"
    assert result[0]["project"] == "red-shrik"
    assert "Decision" in result[0]["types"]
    assert captured["params"]["domains"] == ["ml", "memory"]
    assert captured["params"]["current"] == "brain-v42"
    assert captured["params"]["entries_max"] == 5
    assert "p.project_key <> $current" in captured["query"]
    assert "ORDER BY e.created_at DESC" in captured["query"]


@pytest.mark.asyncio
async def test_fetch_cross_project_entity_ids_returns_empty_when_no_match(monkeypatch):
    svc = GraphService(driver=None)

    async def fake_run_read(_q, _p):
        return []

    monkeypatch.setattr(svc, "_run_read", fake_run_read)

    result = await svc.fetch_cross_project_entity_ids(
        domains=["ml"], exclude_project_key="brain-v42", limit=5
    )
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_cross_project_entity_ids`
Expected: FAIL — method not found.

- [ ] **Step 3: Implement method**

In `src/brain_v42/services/graph_service.py`:

```python
async def fetch_cross_project_entity_ids(
    self, *, domains: list[str], exclude_project_key: str, limit: int
) -> list[dict]:
    """Return entity ids/types/project/created_at for entities living in any
    of `domains` and NOT in `exclude_project_key`. Ordered recency desc.

    Returns ids only; caller hydrates display fields via PG repos.
    """
    query = (
        "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
        "WHERE d.name IN $domains "
        "MATCH (e)-[:BELONGS_TO]->(p:Project) "
        "WHERE p.project_key <> $current "
        "RETURN e.id AS id, labels(e) AS types, p.project_key AS project, "
        "       e.created_at AS created_at "
        "ORDER BY e.created_at DESC LIMIT $entries_max"
    )
    rows = await self._run_read(
        query,
        {"domains": domains, "current": exclude_project_key, "entries_max": limit},
    )
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_cross_project_entity_ids`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/graph_service.py tests/unit/services/test_graph_service.py
git commit -m "feat(graph): GraphService.fetch_cross_project_entity_ids for briefing hydration"
```

---

## Task 5: PG repo `fetch_brief_by_ids` (Decision + Learning + Snippet + Runbook + ADR)

**Files:**
- Modify: `src/brain_v42/repositories/pg_decision.py`, `pg_learning.py`, `pg_snippet.py`, `pg_runbook.py`, `pg_adr.py`
- Test: `tests/unit/repositories/test_fetch_brief_by_ids.py` (create)

A "brief" is a `dict` with `{"id": UUID, "type": str, "title": str, "project_key": str, "created_at": date}` for the briefing display. Each repo returns its own type label (`"Decision"`, `"Learning"`, etc.) and pulls the right display field per the Spec mapping table (Decision/ADR→`title`, Learning→`topic`, Snippet→`intent`, Runbook→`name`).

- [ ] **Step 1: Write failing tests**

Create `tests/unit/repositories/test_fetch_brief_by_ids.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_decision import DecisionRepo
from brain_v42.repositories.pg_learning import LearningRepo
from brain_v42.repositories.pg_snippet import SnippetRepo
from brain_v42.repositories.pg_runbook import RunbookRepo
from brain_v42.repositories.pg_adr import AdrRepo


def _factory(rows):
    """Returns an async session_factory whose session().execute() yields rows."""
    class FakeResult:
        def mappings(self):
            return self
        def all(self):
            return rows
    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def execute(self, *_a, **_k): return FakeResult()
    def factory(): return FakeSession()
    return factory


@pytest.mark.asyncio
async def test_decision_fetch_brief_by_ids_returns_title_and_type():
    id1 = uuid4()
    rows = [{"id": id1, "title": "Use Qodo-Embed", "project_key": "brain-v42",
             "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc)}]
    repo = DecisionRepo(_factory(rows))
    result = await repo.fetch_brief_by_ids([id1])
    assert len(result) == 1
    b = result[0]
    assert b["id"] == id1
    assert b["type"] == "Decision"
    assert b["title"] == "Use Qodo-Embed"
    assert b["project_key"] == "brain-v42"


@pytest.mark.asyncio
async def test_learning_fetch_brief_uses_topic_as_title():
    id1 = uuid4()
    rows = [{"id": id1, "topic": "embedding healthcheck", "project_key": "red-shrik",
             "created_at": datetime(2026, 4, 22, tzinfo=timezone.utc)}]
    repo = LearningRepo(_factory(rows))
    result = await repo.fetch_brief_by_ids([id1])
    assert result[0]["type"] == "Learning"
    assert result[0]["title"] == "embedding healthcheck"


@pytest.mark.asyncio
async def test_snippet_fetch_brief_uses_intent_as_title():
    id1 = uuid4()
    rows = [{"id": id1, "intent": "vector cosine threshold", "project_key": "brain-v42",
             "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc)}]
    repo = SnippetRepo(_factory(rows))
    result = await repo.fetch_brief_by_ids([id1])
    assert result[0]["type"] == "Snippet"
    assert result[0]["title"] == "vector cosine threshold"


@pytest.mark.asyncio
async def test_runbook_fetch_brief_uses_name_as_title():
    id1 = uuid4()
    rows = [{"id": id1, "name": "deploy-brain-v42", "project_key": "brain-v42",
             "created_at": datetime(2026, 4, 5, tzinfo=timezone.utc)}]
    repo = RunbookRepo(_factory(rows))
    result = await repo.fetch_brief_by_ids([id1])
    assert result[0]["type"] == "Runbook"
    assert result[0]["title"] == "deploy-brain-v42"


@pytest.mark.asyncio
async def test_adr_fetch_brief_uses_title():
    id1 = uuid4()
    rows = [{"id": id1, "title": "Adopt FastMCP", "project_key": "brain-v42",
             "created_at": datetime(2026, 3, 1, tzinfo=timezone.utc)}]
    repo = AdrRepo(_factory(rows))
    result = await repo.fetch_brief_by_ids([id1])
    assert result[0]["type"] == "ADR"
    assert result[0]["title"] == "Adopt FastMCP"


@pytest.mark.asyncio
async def test_fetch_brief_returns_empty_on_empty_input():
    repo = DecisionRepo(_factory([]))
    result = await repo.fetch_brief_by_ids([])
    assert result == []
```

(Adapt repo constructor signatures to whatever the project actually uses — likely `repo = DecisionRepo(session_factory=...)` or `repo = DecisionRepo(...)`. Read one existing repo test to mirror the pattern.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/repositories/test_fetch_brief_by_ids.py -v`
Expected: FAIL — `AttributeError: 'DecisionRepo' object has no attribute 'fetch_brief_by_ids'`.

- [ ] **Step 3: Implement `fetch_brief_by_ids` in each repo**

In `src/brain_v42/repositories/pg_decision.py`, add:

```python
async def fetch_brief_by_ids(self, ids: list[UUID]) -> list[dict]:
    """Return brief records (id/title/project_key/created_at) for given ids.

    Used by brain_session_start cross-project briefing — does NOT load
    embeddings or bodies.
    """
    if not ids:
        return []
    stmt = sa.select(
        decisions.c.id, decisions.c.title,
        decisions.c.project_key, decisions.c.created_at,
    ).where(decisions.c.id.in_(ids))
    async with self._session_factory() as session:
        result = await session.execute(stmt)
        rows = result.mappings().all()
    return [
        {"id": r["id"], "type": "Decision", "title": r["title"],
         "project_key": r["project_key"], "created_at": r["created_at"]}
        for r in rows
    ]
```

Repeat in `pg_learning.py` (use `learnings.c.topic` as title, type `"Learning"`), `pg_snippet.py` (use `snippets.c.intent`, type `"Snippet"`), `pg_runbook.py` (use `runbooks.c.name`, type `"Runbook"`), `pg_adr.py` (use `adrs.c.title`, type `"ADR"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/repositories/test_fetch_brief_by_ids.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/ tests/unit/repositories/test_fetch_brief_by_ids.py
git commit -m "feat(repos): fetch_brief_by_ids on Decision/Learning/Snippet/Runbook/ADR"
```

---

## Task 6: `_fetch_cross_project_entries` helper

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (add helper)
- Test: `tests/unit/mcp/tools/test_session_tools_cross_project.py` (create)

Helper orchestrates GraphService calls + per-type PG hydration + truncation + sort.

**Return type:** `tuple[list[dict], list[str]]` — `(briefs, active_domains)` so the formatter can render `**Cross-project (ml, memory):**` with the actual domain list.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/mcp/tools/test_session_tools_cross_project.py`:

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.session_tools import _fetch_cross_project_entries


@pytest.mark.asyncio
async def test_fetch_cross_project_entries_returns_empty_when_no_active_domains():
    graph_service = AsyncMock()
    graph_service.fetch_active_domains.return_value = []

    briefs, domains = await _fetch_cross_project_entries(
        graph_service=graph_service,
        repos={},
        project_key="brain-v42",
        top_n=2,
        entries_max=5,
    )
    assert briefs == []
    assert domains == []
    graph_service.fetch_cross_project_entity_ids.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_cross_project_entries_returns_empty_when_no_cross_entities():
    graph_service = AsyncMock()
    graph_service.fetch_active_domains.return_value = ["ml"]
    graph_service.fetch_cross_project_entity_ids.return_value = []

    briefs, domains = await _fetch_cross_project_entries(
        graph_service=graph_service,
        repos={"Decision": AsyncMock(), "Learning": AsyncMock()},
        project_key="brain-v42",
        top_n=2,
        entries_max=5,
    )
    assert briefs == []
    # Domains are still returned (briefer can decide whether to render header)
    assert domains == ["ml"]


@pytest.mark.asyncio
async def test_fetch_cross_project_entries_groups_ids_per_type_and_hydrates():
    id_d = uuid4()
    id_l = uuid4()
    graph_service = AsyncMock()
    graph_service.fetch_active_domains.return_value = ["ml", "memory"]
    graph_service.fetch_cross_project_entity_ids.return_value = [
        {"id": str(id_d), "types": ["Decision"], "project": "red-shrik",
         "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)},
        {"id": str(id_l), "types": ["Learning"], "project": "red-monitor",
         "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc)},
    ]

    decision_repo = AsyncMock()
    decision_repo.fetch_brief_by_ids.return_value = [
        {"id": id_d, "type": "Decision", "title": "Embedding healthcheck",
         "project_key": "red-shrik",
         "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)}
    ]
    learning_repo = AsyncMock()
    learning_repo.fetch_brief_by_ids.return_value = [
        {"id": id_l, "type": "Learning", "title": "go-pubsub close channel race",
         "project_key": "red-monitor",
         "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc)}
    ]

    briefs, domains = await _fetch_cross_project_entries(
        graph_service=graph_service,
        repos={"Decision": decision_repo, "Learning": learning_repo},
        project_key="brain-v42",
        top_n=2,
        entries_max=5,
    )

    assert len(briefs) == 2
    # Recency-desc order
    assert briefs[0]["title"] == "Embedding healthcheck"  # 2026-04-28 first
    assert briefs[1]["title"] == "go-pubsub close channel race"
    # Domains list propagated for the formatter
    assert domains == ["ml", "memory"]
    decision_repo.fetch_brief_by_ids.assert_called_once_with([id_d])
    learning_repo.fetch_brief_by_ids.assert_called_once_with([id_l])


@pytest.mark.asyncio
async def test_fetch_cross_project_entries_truncates_title_at_60_chars():
    long_title = "x" * 100
    id_d = uuid4()
    graph_service = AsyncMock()
    graph_service.fetch_active_domains.return_value = ["ml"]
    graph_service.fetch_cross_project_entity_ids.return_value = [
        {"id": str(id_d), "types": ["Decision"], "project": "red-shrik",
         "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)}
    ]
    decision_repo = AsyncMock()
    decision_repo.fetch_brief_by_ids.return_value = [
        {"id": id_d, "type": "Decision", "title": long_title,
         "project_key": "red-shrik",
         "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)}
    ]
    briefs, _ = await _fetch_cross_project_entries(
        graph_service=graph_service,
        repos={"Decision": decision_repo},
        project_key="brain-v42",
        top_n=1,
        entries_max=5,
    )
    assert briefs[0]["title"].endswith("…")
    assert len(briefs[0]["title"]) == 61  # 60 chars + ellipsis


@pytest.mark.asyncio
async def test_fetch_cross_project_entries_returns_empty_on_neo4j_failure(caplog):
    graph_service = AsyncMock()
    graph_service.fetch_active_domains.side_effect = RuntimeError("Neo4j down")

    briefs, domains = await _fetch_cross_project_entries(
        graph_service=graph_service,
        repos={},
        project_key="brain-v42",
        top_n=2,
        entries_max=5,
    )
    assert briefs == []
    assert domains == []
    assert any("Neo4j" in r.message or "cross-project" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py -v`
Expected: FAIL — `ImportError: cannot import name '_fetch_cross_project_entries'`.

- [ ] **Step 3: Implement helper**

In `src/brain_v42/mcp/tools/session_tools.py`, add (above `_format_session_briefing`):

```python
async def _fetch_cross_project_entries(
    *,
    graph_service: Any,
    repos: dict[str, Any],
    project_key: str,
    top_n: int,
    entries_max: int,
) -> tuple[list[dict], list[str]]:
    """Fetch top-N cross-project briefing entries plus the active-domains list.

    Returns (briefs, active_domains) where:
      - briefs is a list of {id, type, title, project_key, created_at} dicts,
        sorted by created_at desc, with title truncated to 60 chars + '…'.
      - active_domains is the list of domains used (caller renders them in
        the section header).

    Returns ([], []) gracefully if Neo4j fails entirely.
    Returns ([], domains) if domains were found but no cross-entities matched.
    """
    try:
        domains = await graph_service.fetch_active_domains(project_key, top_n=top_n)
        if not domains:
            return [], []
        ids_with_meta = await graph_service.fetch_cross_project_entity_ids(
            domains=domains, exclude_project_key=project_key, limit=entries_max
        )
        if not ids_with_meta:
            return [], domains
    except Exception as e:  # noqa: BLE001 — graceful degradation on any Neo4j fail
        logger.warning("cross-project briefing: Neo4j fetch failed: %s", e)
        return [], []

    # Group ids per entity type
    grouped: dict[str, list[Any]] = {}
    for row in ids_with_meta:
        for label in row["types"]:
            if label in repos:
                grouped.setdefault(label, []).append(row["id"])
                break

    # Hydrate per type (1 PG round-trip per type)
    briefs: list[dict] = []
    from uuid import UUID
    for entity_type, ids in grouped.items():
        repo = repos[entity_type]
        uuids = [UUID(s) if isinstance(s, str) else s for s in ids]
        briefs.extend(await repo.fetch_brief_by_ids(uuids))

    # Truncate titles
    for b in briefs:
        if len(b["title"]) > 60:
            b["title"] = b["title"][:60] + "…"

    # Sort recency desc + cap (cap is upstream Cypher LIMIT but defense-in-depth)
    briefs.sort(key=lambda b: b["created_at"], reverse=True)
    return briefs[:entries_max], domains
```

Also ensure `from typing import Any` and `logger = logging.getLogger(__name__)` are present at the top of the module (they already are).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/session_tools.py tests/unit/mcp/tools/test_session_tools_cross_project.py
git commit -m "feat(session): _fetch_cross_project_entries helper (graceful Neo4j fallback)"
```

---

## Task 7: `_format_session_briefing` extension

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py`
- Test: `tests/unit/mcp/tools/test_session_tools_cross_project.py` (extend)

Add optional `cross_entries` param. When non-empty, append a `**Cross-project (...):**` block after the Recent learnings block.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/mcp/tools/test_session_tools_cross_project.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from brain_v42.mcp.tools.session_tools import _format_session_briefing


def _ctx(key="brain-v42", focus="Spec C MVP"):
    return SimpleNamespace(project_key=key, current_focus=focus, description=None)


def test_format_briefing_omits_cross_section_when_none():
    out = _format_session_briefing(_ctx(), [], [], cross_entries=None)
    assert "Cross-project" not in out


def test_format_briefing_omits_cross_section_when_empty_list():
    out = _format_session_briefing(_ctx(), [], [], cross_entries=[])
    assert "Cross-project" not in out


def test_format_briefing_renders_cross_section_with_domains_in_header():
    cross = [
        {"id": "x", "type": "Decision", "title": "embedding healthcheck",
         "project_key": "red-shrik",
         "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)},
        {"id": "y", "type": "Learning", "title": "go-pubsub race",
         "project_key": "red-monitor",
         "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc)},
    ]
    out = _format_session_briefing(_ctx(), [], [],
                                   cross_entries=cross,
                                   cross_domains=["ml", "memory"])
    assert "**Cross-project (ml, memory):**" in out
    assert "[red-shrik] Decision · 2026-04-28 · embedding healthcheck" in out
    assert "[red-monitor] Learning · 2026-04-15 · go-pubsub race" in out


def test_format_briefing_renders_cross_section_without_domains_falls_back():
    cross = [
        {"id": "x", "type": "Decision", "title": "foo",
         "project_key": "p", "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc)}
    ]
    out = _format_session_briefing(_ctx(), [], [], cross_entries=cross)
    assert "**Cross-project:**" in out
    assert "(ml" not in out  # no domain header without explicit list


def test_format_briefing_backward_compat_when_param_omitted():
    """Calling with the original 3-arg signature must still work."""
    out = _format_session_briefing(_ctx(), [], [])
    assert "Cross-project" not in out
    assert "Session Briefing" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py -v -k briefing`
Expected: FAIL on the cross_entries tests (TypeError or rendering missing).

- [ ] **Step 3: Modify `_format_session_briefing`**

In `src/brain_v42/mcp/tools/session_tools.py`, change signature and append rendering. Adds two optional kwargs (`cross_entries` and `cross_domains`); when both are non-empty, renders the spec-faithful `**Cross-project (ml, memory):**` header with the actual domain list from the helper.

```python
def _format_session_briefing(
    ctx: Any | None,
    decisions: list[Any],
    learnings: list[Any],
    cross_entries: list[dict] | None = None,
    cross_domains: list[str] | None = None,
) -> str:
    """... (existing docstring) ...

    Args (added):
        cross_entries: Optional list of cross-project briefs (id/type/title/
            project_key/created_at). None or empty → section omitted.
        cross_domains: Optional list of domain names that drove the selection.
            Rendered in the section header. Falls back to `**Cross-project:**`
            if missing.
    """
    lines: list[str] = []
    # ... existing rendering for ctx / decisions / learnings ...

    if cross_entries:
        if cross_domains:
            lines.append(f"**Cross-project ({', '.join(cross_domains)}):**")
        else:
            lines.append("**Cross-project:**")
        for e in cross_entries:
            date_str = e["created_at"].strftime("%Y-%m-%d")
            lines.append(
                f"- [{e['project_key']}] {e['type']} · {date_str} · {e['title']}"
            )

    return "\n".join(lines)
```

Update the failing test `test_format_briefing_renders_cross_section_with_entries` above to also pass `cross_domains=["ml", "memory"]` and assert `"**Cross-project (ml, memory):**"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py -v`
Expected: all tests PASS (including the briefing-rendering ones).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/session_tools.py tests/unit/mcp/tools/test_session_tools_cross_project.py
git commit -m "feat(session): _format_session_briefing renders Cross-project section"
```

---

## Task 8: Wire `brain_session_start` to call enrichment + killswitch

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (`brain_session_start` + `register_session_tools` signature)
- Test: `tests/unit/mcp/tools/test_session_tools_cross_project.py` (extend)

`register_session_tools` needs new args: `graph_service`, `decision_repo`, `learning_repo`, `snippet_repo`, `runbook_repo`, `adr_repo`, `settings`. Inside, `brain_session_start` reads `settings.dream_cross_project_enabled` to decide whether to call the helper.

- [ ] **Step 1: Write failing test**

Append to test file:

```python
@pytest.mark.asyncio
async def test_brain_session_start_skips_cross_when_killswitch_off(monkeypatch):
    monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "false")
    # ... wire up register_session_tools with mocks ...
    # The helper _fetch_cross_project_entries must NOT be called.
    # Assert that the returned briefing does not contain "Cross-project".
    pass  # see implementation below for full mock setup
```

(Full mock setup is verbose; see implementation step. The test uses `register_session_tools` to register `brain_session_start` against a fake `mcp` shim and invokes it directly.)

Concrete test:

```python
@pytest.mark.asyncio
async def test_brain_session_start_skips_cross_when_killswitch_off():
    """Killswitch off → cross helper never called, briefing has no cross section."""
    from brain_v42.config import Settings
    from brain_v42.mcp.tools.session_tools import register_session_tools

    settings = Settings(dream_cross_project_enabled=False)
    helper_calls = {"called": False}

    # Capture register-time tool function via fake MCP shim
    captured = {}
    class FakeMcp:
        def tool(self, **_kwargs):
            def wrap(fn):
                captured["fn"] = fn
                return fn
            return wrap

    async def fake_helper(**_kw):
        helper_calls["called"] = True
        return ([], [])

    pcs = AsyncMock()
    pcs.get_by_key.return_value = SimpleNamespace(
        project_key="brain-v42", current_focus=None, description=None
    )
    ds = AsyncMock(); ds.list_all.return_value = []
    ls = AsyncMock(); ls.list_all.return_value = []

    register_session_tools(
        FakeMcp(),
        project_context_svc=pcs,
        decision_svc=ds,
        learning_svc=ls,
        graph_service=AsyncMock(),
        repos={"Decision": AsyncMock(), "Learning": AsyncMock()},
        settings=settings,
        _cross_helper=fake_helper,  # injection seam for tests
    )

    out = await captured["fn"]("brain-v42")
    assert helper_calls["called"] is False
    assert "Cross-project" not in out


@pytest.mark.asyncio
async def test_brain_session_start_calls_cross_when_killswitch_on():
    from brain_v42.config import Settings
    from brain_v42.mcp.tools.session_tools import register_session_tools

    settings = Settings(dream_cross_project_enabled=True)
    helper_calls = {"called": False}

    captured = {}
    class FakeMcp:
        def tool(self, **_k):
            def w(fn):
                captured["fn"] = fn
                return fn
            return w

    async def fake_helper(**_kw):
        helper_calls["called"] = True
        return (
            [{"id": "x", "type": "Decision", "title": "foo",
              "project_key": "red-shrik",
              "created_at": datetime(2026, 4, 28, tzinfo=timezone.utc)}],
            ["ml", "memory"],
        )

    pcs = AsyncMock()
    pcs.get_by_key.return_value = SimpleNamespace(
        project_key="brain-v42", current_focus=None, description=None
    )
    ds = AsyncMock(); ds.list_all.return_value = []
    ls = AsyncMock(); ls.list_all.return_value = []

    register_session_tools(
        FakeMcp(),
        project_context_svc=pcs,
        decision_svc=ds,
        learning_svc=ls,
        graph_service=AsyncMock(),
        repos={"Decision": AsyncMock()},
        settings=settings,
        _cross_helper=fake_helper,
    )

    out = await captured["fn"]("brain-v42")
    assert helper_calls["called"] is True
    assert "**Cross-project (ml, memory):**" in out
    assert "[red-shrik] Decision · 2026-04-28 · foo" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py::test_brain_session_start_calls_cross_when_killswitch_on -v`
Expected: FAIL — `register_session_tools()` got an unexpected keyword argument 'graph_service' / 'repos' / 'settings' / '_cross_helper'.

- [ ] **Step 3: Modify `register_session_tools`**

In `src/brain_v42/mcp/tools/session_tools.py`, replace signature and `brain_session_start` body:

```python
def register_session_tools(
    mcp: Any,
    project_context_svc: Any,
    decision_svc: Any,
    learning_svc: Any,
    *,
    graph_service: Any | None = None,
    repos: dict[str, Any] | None = None,
    settings: Any | None = None,
    _cross_helper: Any | None = None,  # injection seam (default = real helper)
) -> None:
    """Register session management tools on the MCP server."""

    cross_helper = _cross_helper or _fetch_cross_project_entries

    @mcp.tool(version="1.0")
    async def brain_session_start(project_key: str) -> str:
        """... (existing docstring, augment with cross-project note) ..."""
        ctx = await project_context_svc.get_by_key(project_key)
        recent_decisions = await decision_svc.list_all(project_key=project_key, limit=5)
        recent_learnings = await learning_svc.list_all(project_key=project_key, limit=5)

        cross_entries: list[dict] = []
        cross_domains: list[str] = []
        if settings is not None and getattr(settings, "dream_cross_project_enabled", False):
            if graph_service is not None and repos:
                cross_entries, cross_domains = await cross_helper(
                    graph_service=graph_service,
                    repos=repos,
                    project_key=project_key,
                    top_n=settings.cross_project_briefing_top_n,
                    entries_max=settings.cross_project_briefing_entries_max,
                )

        return _format_session_briefing(
            ctx, recent_decisions, recent_learnings,
            cross_entries=cross_entries or None,
            cross_domains=cross_domains or None,
        )
```

Update `_format_session_briefing` to accept `cross_domains: list[str] | None = None` and render `**Cross-project ({', '.join(cross_domains)}):**` when both `cross_entries` and `cross_domains` are non-empty (else fall back to `**Cross-project:**`).

Update `_fetch_cross_project_entries` to return `tuple[list[dict], list[str]]` (briefs + the active_domains list it queried). Adjust Task 6 tests' `assert result == [...]` to `assert result[0] == [...]` and check `result[1] == ["ml", "memory"]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/mcp/tools/test_session_tools_cross_project.py -v`
Expected: all PASS.

- [ ] **Step 5: Wire injection at MCP server registration site**

Find where `register_session_tools` is called: `grep -rn "register_session_tools" src/brain_v42/mcp`. The caller (likely `src/brain_v42/mcp/server.py`) must pass the new kwargs. Adjust:

```python
register_session_tools(
    mcp,
    project_context_svc=...,
    decision_svc=...,
    learning_svc=...,
    graph_service=graph_service,        # already constructed elsewhere
    repos={
        "Decision": decision_repo,
        "Learning": learning_repo,
        "Snippet": snippet_repo,
        "Runbook": runbook_repo,
        "ADR": adr_repo,
    },
    settings=settings,
)
```

If `graph_service` / `settings` are not in scope at the call site, hoist them up (they exist somewhere in the server bootstrap — find via `grep`).

- [ ] **Step 6: Run full unit suite to verify no regression**

Run: `pytest tests/unit -q 2>&1 | tail -3`
Expected: baseline + new tests, all passing.

- [ ] **Step 7: Commit**

```bash
git add src/brain_v42/mcp/ tests/unit/mcp/tools/test_session_tools_cross_project.py
git commit -m "feat(session): brain_session_start surfaces cross-project section behind killswitch"
```

---

## Task 9: `ResonancePair` dataclass

**Files:**
- Create: `src/brain_v42/dream/resonance.py` (new module — keep `scripts/dream/cross_project_resonance.py` thin)
- Test: `tests/unit/dream/test_resonance_pair.py` (create)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/dream/test_resonance_pair.py`:

```python
import re
from datetime import date
from uuid import UUID, uuid4

from brain_v42.dream.resonance import ResonancePair


def _pair(a_title="Use Qodo-Embed-1.5B", b_title="Qodo-Embed for code embedding",
          cosine=0.91, domain="ml"):
    return ResonancePair(
        a_id=uuid4(), b_id=uuid4(),
        a_project="brain-v42", b_project="red-shrik",
        a_title=a_title, b_title=b_title,
        a_created_at=date(2026, 4, 15), b_created_at=date(2026, 4, 22),
        cosine=cosine, domain=domain,
    )


def test_resonance_pair_dedup_key_stable_across_id_order():
    a, b = uuid4(), uuid4()
    p1 = ResonancePair(a, b, "p1", "p2", "t1", "t2",
                       date(2026, 1, 1), date(2026, 1, 2), 0.9, "ml")
    p2 = ResonancePair(b, a, "p2", "p1", "t2", "t1",
                       date(2026, 1, 2), date(2026, 1, 1), 0.9, "ml")
    assert p1.dedup_key == p2.dedup_key


def test_resonance_pair_dedup_key_differs_across_domains():
    a, b = uuid4(), uuid4()
    p_ml = ResonancePair(a, b, "p1", "p2", "t1", "t2",
                         date(2026, 1, 1), date(2026, 1, 2), 0.9, "ml")
    p_memory = ResonancePair(a, b, "p1", "p2", "t1", "t2",
                             date(2026, 1, 1), date(2026, 1, 2), 0.9, "memory")
    assert p_ml.dedup_key != p_memory.dedup_key


def test_resonance_pair_hint_drift_when_numeric_divergence():
    p = _pair(a_title="Cosine 0.92 for dedup", b_title="Cosine 0.85 for dedup")
    assert "drift candidate" in p.hint
    assert "0.92" in p.hint and "0.85" in p.hint


def test_resonance_pair_hint_convergence_when_no_divergence():
    p = _pair()  # default titles, no numeric divergence
    assert "convergence likely" in p.hint


def test_resonance_pair_hint_convergence_when_same_numbers():
    p = _pair(a_title="Cosine 0.85 v1", b_title="Cosine 0.85 v2")
    assert "convergence likely" in p.hint


def test_resonance_pair_format_insight_contains_titles_and_hint():
    p = _pair(cosine=0.91, domain="ml")
    out = p.format_insight()
    assert "Use Qodo-Embed-1.5B" in out
    assert "Qodo-Embed for code embedding" in out
    assert "domain 'ml'" in out
    assert "0.910" in out  # cosine formatted
    assert "Hint:" in out


def test_resonance_pair_dedup_key_is_sha256_hex():
    p = _pair()
    assert len(p.dedup_key) == 64
    assert all(c in "0123456789abcdef" for c in p.dedup_key)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/dream/test_resonance_pair.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.dream.resonance'`.

- [ ] **Step 3: Implement ResonancePair**

Create `src/brain_v42/dream/__init__.py` (empty) if not exists.

Create `src/brain_v42/dream/resonance.py`:

```python
"""Cross-project resonance pair model and heuristics (Spec C MVP β)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from uuid import UUID

_NUM_PATTERN = re.compile(r"\d+\.\d+")


@dataclass(frozen=True)
class ResonancePair:
    """A cross-project decision pair above the resonance threshold.

    The algorithm surfaces pairs; it does not classify them as drift vs
    convergence. The `hint` property is a numeric-divergence heuristic — never
    authoritative.
    """

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
        nums_a = set(_NUM_PATTERN.findall(self.a_title))
        nums_b = set(_NUM_PATTERN.findall(self.b_title))
        if nums_a and nums_b and nums_a != nums_b:
            return f"drift candidate (numeric divergence: {sorted(nums_a)} vs {sorted(nums_b)})"
        return "convergence likely (no numeric divergence detected)"

    @property
    def dedup_key(self) -> str:
        lo, hi = sorted([str(self.a_id), str(self.b_id)])
        return hashlib.sha256(f"{lo}|{hi}|{self.domain}".encode()).hexdigest()

    def format_insight(self) -> str:
        return (
            f"Cross-project resonance in domain '{self.domain}' "
            f"(cosine={self.cosine:.3f}):\n"
            f"- [{self.a_project}] {self.a_title} ({self.a_created_at})\n"
            f"- [{self.b_project}] {self.b_title} ({self.b_created_at})\n"
            f"Hint: {self.hint}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/dream/test_resonance_pair.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/dream/ tests/unit/dream/test_resonance_pair.py
git commit -m "feat(dream): ResonancePair dataclass with hint + dedup_key + format_insight"
```

---

## Task 10: `GraphService.fetch_decision_ids_in_domain_across_projects`

**Files:**
- Modify: `src/brain_v42/services/graph_service.py`
- Test: `tests/unit/services/test_graph_service.py`

- [ ] **Step 1: Write failing test**

Append:

```python
@pytest.mark.asyncio
async def test_fetch_decision_ids_in_domain_returns_ids_only(monkeypatch):
    captured = {}
    async def fake_run_read(q, p):
        captured["query"] = q
        captured["params"] = p
        return [{"id": "uuid-1"}, {"id": "uuid-2"}, {"id": "uuid-3"}]

    svc = GraphService(driver=None)
    monkeypatch.setattr(svc, "_run_read", fake_run_read)

    ids = await svc.fetch_decision_ids_in_domain_across_projects("ml")

    assert ids == ["uuid-1", "uuid-2", "uuid-3"]
    assert captured["params"]["domain"] == "ml"
    assert "Decision" in captured["query"]
    assert "BELONGS_TO_DOMAIN" in captured["query"]
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_decision_ids_in_domain`
Expected: FAIL — method missing.

- [ ] **Step 3: Implement**

```python
async def fetch_decision_ids_in_domain_across_projects(self, domain: str) -> list[str]:
    """Return all Decision ids classified into `domain`, across all projects.

    Used by cross_project_resonance.py — caller hydrates+filters in PG.
    """
    query = (
        "MATCH (e:Decision)-[:BELONGS_TO_DOMAIN]->(d:Domain {name: $domain}) "
        "RETURN e.id AS id"
    )
    rows = await self._run_read(query, {"domain": domain})
    return [r["id"] for r in rows]
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/unit/services/test_graph_service.py -v -k fetch_decision_ids_in_domain`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/graph_service.py tests/unit/services/test_graph_service.py
git commit -m "feat(graph): fetch_decision_ids_in_domain_across_projects"
```

---

## Task 11: `pg_decision.fetch_cross_project_resonance_pairs` (PG-side cosine)

**Files:**
- Modify: `src/brain_v42/repositories/pg_decision.py`
- Test: `tests/integration/repositories/test_pg_decision_resonance.py` (create — needs real PG for `<=>`)

The PG-side cosine SQL cannot be meaningfully unit-tested with mocked sessions (you'd just be asserting the SQL string). Use an integration test against the real PG fixture instead. For a unit-only test, we verify the SQL contains the right operators and clauses.

- [ ] **Step 1: Write failing unit test (SQL shape)**

In `tests/unit/repositories/test_pg_decision_resonance_sql.py`:

```python
"""Unit-level shape check on the cross-project resonance SQL string.
Behavior is integration-tested in tests/integration/.
"""

import inspect

from brain_v42.repositories.pg_decision import DecisionRepo


def test_resonance_sql_filters_intra_project():
    src = inspect.getsource(DecisionRepo.fetch_cross_project_resonance_pairs)
    assert "a.project_key <> b.project_key" in src


def test_resonance_sql_uses_pgvector_cosine():
    src = inspect.getsource(DecisionRepo.fetch_cross_project_resonance_pairs)
    assert "a.embedding <=> b.embedding" in src


def test_resonance_sql_filters_null_embeddings():
    src = inspect.getsource(DecisionRepo.fetch_cross_project_resonance_pairs)
    assert "a.embedding IS NOT NULL" in src
    assert "b.embedding IS NOT NULL" in src


def test_resonance_sql_avoids_self_and_duplicate_pairs():
    src = inspect.getsource(DecisionRepo.fetch_cross_project_resonance_pairs)
    assert "a.id < b.id" in src


def test_resonance_sql_thresholds_via_param():
    src = inspect.getsource(DecisionRepo.fetch_cross_project_resonance_pairs)
    assert ":threshold" in src
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/unit/repositories/test_pg_decision_resonance_sql.py -v`
Expected: FAIL — method missing.

- [ ] **Step 3: Implement method**

In `src/brain_v42/repositories/pg_decision.py`:

```python
async def fetch_cross_project_resonance_pairs(
    self, *, ids: list[UUID], threshold: float, domain: str
) -> list["ResonancePair"]:
    """Return all cross-project pairs (a, b) within `ids` whose cosine
    similarity is >= threshold. Computed PG-side via pgvector <=>.

    Bounded by len(ids) (caller caps at MAX_DECISIONS_PER_DOMAIN).
    Excludes intra-project pairs and rows with NULL embedding.
    """
    from brain_v42.dream.resonance import ResonancePair

    if not ids:
        return []
    sql = sa.text("""
        SELECT
            a.id AS a_id, b.id AS b_id,
            a.project_key AS a_project, b.project_key AS b_project,
            a.title AS a_title, b.title AS b_title,
            a.created_at::date AS a_created_at, b.created_at::date AS b_created_at,
            (1 - (a.embedding <=> b.embedding))::float AS cosine
        FROM decisions a
        JOIN decisions b ON a.id < b.id
        WHERE a.id = ANY(:ids) AND b.id = ANY(:ids)
          AND a.project_key <> b.project_key
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND (1 - (a.embedding <=> b.embedding)) >= :threshold
        ORDER BY cosine DESC
    """)
    async with self._session_factory() as session:
        result = await session.execute(sql, {"ids": ids, "threshold": threshold})
        rows = result.mappings().all()
    return [
        ResonancePair(
            a_id=r["a_id"], b_id=r["b_id"],
            a_project=r["a_project"], b_project=r["b_project"],
            a_title=r["a_title"], b_title=r["b_title"],
            a_created_at=r["a_created_at"], b_created_at=r["b_created_at"],
            cosine=r["cosine"], domain=domain,
        )
        for r in rows
    ]
```

- [ ] **Step 4: Run unit tests, verify pass**

Run: `pytest tests/unit/repositories/test_pg_decision_resonance_sql.py -v`
Expected: all PASS.

- [ ] **Step 5: Optional integration test (skip if PG fixture unavailable)**

Create `tests/integration/repositories/test_pg_decision_resonance.py` if a PG fixture exists in `tests/integration/conftest.py`. Otherwise skip — out of MVP per spec.

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/repositories/pg_decision.py tests/unit/repositories/test_pg_decision_resonance_sql.py
git commit -m "feat(pg_decision): fetch_cross_project_resonance_pairs (pgvector <=> SQL)"
```

---

## Task 12: `pg_learning.exists_by_dedup_key` + create-with-metadata extension

**Files:**
- Modify: `src/brain_v42/repositories/pg_learning.py`
- Test: `tests/unit/repositories/test_pg_learning_dedup.py` (create)

The `learnings.metadata` JSONB column already exists. Add a method to check if a learning with `metadata->>'dedup_key' = X` exists, and ensure `create()` accepts a `metadata` kwarg that sets `dedup_key` and `source_kind`.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/repositories/test_pg_learning_dedup.py`:

```python
import inspect

from brain_v42.repositories.pg_learning import LearningRepo


def test_exists_by_dedup_key_filters_metadata_jsonb():
    src = inspect.getsource(LearningRepo.exists_by_dedup_key)
    # Filter must hit the JSONB metadata column with the dedup_key path
    assert "metadata" in src
    assert "dedup_key" in src
```

(Then add a behavioral test if a fake-session pattern is feasible. The existing `_factory(rows)` shim from Task 5 won't natively support `EXISTS` queries, so this stays at SQL-shape level for unit tests; integration covers behavior.)

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/unit/repositories/test_pg_learning_dedup.py -v`
Expected: FAIL — method missing.

- [ ] **Step 3: Implement**

In `src/brain_v42/repositories/pg_learning.py`:

```python
async def exists_by_dedup_key(self, dedup_key: str) -> bool:
    """Return True if any learning row has metadata->>'dedup_key' == dedup_key.

    Used by cross_project_resonance.py WET mode for idempotency.
    """
    sql = sa.text(
        "SELECT 1 FROM learnings "
        "WHERE metadata->>'dedup_key' = :dedup_key "
        "LIMIT 1"
    )
    async with self._session_factory() as session:
        result = await session.execute(sql, {"dedup_key": dedup_key})
        return result.scalar() is not None
```

Verify the existing `LearningRepo.create(...)` already accepts a `metadata: dict | None = None` kwarg. If not, add it (style-match decision_repo's `create` signature). The kwarg should default to `{}` and be JSON-serialized into `learnings.metadata`.

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/unit/repositories/test_pg_learning_dedup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/pg_learning.py tests/unit/repositories/test_pg_learning_dedup.py
git commit -m "feat(pg_learning): exists_by_dedup_key + metadata kwarg on create"
```

---

## Task 13: `dream_runs` helpers (start/end for `cross_project_resonance` kind)

**Files:**
- Find/extend: existing `dream_runs` repo (likely `src/brain_v42/repositories/pg_dream_runs.py` — verify with `find src/brain_v42 -name "*dream_run*"`)
- Test: `tests/unit/repositories/test_pg_dream_runs_resonance.py` (create)

If a generic `start_run(kind, mode) -> UUID` and `end_run(run_id, status, pair_count)` already exist (likely, given promote_validate uses them), this task may be a no-op. Verify first.

- [ ] **Step 1: Discover existing API**

Run: `grep -rn "dream_runs" src/brain_v42/repositories/ src/brain_v42/services/ | head -20`

If `start_run` / `end_run` exist with `kind` parameter that accepts arbitrary strings: skip implementation and only confirm with a test.
If not: add them following the existing promote pattern.

- [ ] **Step 2: Write failing test**

Create `tests/unit/repositories/test_pg_dream_runs_resonance.py`:

```python
import inspect

# Adapt import path to the actual module discovered in Step 1
from brain_v42.repositories import pg_dream_runs


def test_start_run_accepts_cross_project_resonance_kind():
    sig = inspect.signature(pg_dream_runs.start_run)
    assert "kind" in sig.parameters
    # No closed-list constraint on kind (free string) — verify no enum check
    src = inspect.getsource(pg_dream_runs.start_run)
    assert "cross_project_resonance" not in src or "Literal" not in src
```

(Adapt to actual repo API — if it's a class, instantiate it; if a function, call it.)

- [ ] **Step 3: Implement if missing**

If the existing API doesn't accept arbitrary `kind`, add a generic `start_run` and `end_run` in the dream_runs repo following the existing migration's column shape:

```python
async def start_run(self, *, kind: str, mode: str = "dry_run") -> UUID:
    sql = sa.text(
        "INSERT INTO dream_runs (kind, mode, started_at) "
        "VALUES (:kind, :mode, NOW()) RETURNING id"
    )
    async with self._session_factory() as session:
        result = await session.execute(sql, {"kind": kind, "mode": mode})
        await session.commit()
        return result.scalar_one()


async def end_run(self, run_id: UUID, *, status: str, pair_count: int = 0) -> None:
    sql = sa.text(
        "UPDATE dream_runs SET ended_at = NOW(), status = :status, "
        "                       pair_count = :pc WHERE id = :rid"
    )
    async with self._session_factory() as session:
        await session.execute(sql, {"status": status, "pc": pair_count, "rid": run_id})
        await session.commit()
```

(Adjust column names to match the actual `dream_runs` schema — read `alembic/versions/013_dream_runs.py` and `015_dream_runs_error_message.py` to learn the columns.)

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/unit/repositories/test_pg_dream_runs_resonance.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/pg_dream_runs.py tests/unit/repositories/test_pg_dream_runs_resonance.py
git commit -m "feat(dream_runs): start_run/end_run accept cross_project_resonance kind"
```

(If no changes were needed, skip the commit.)

---

## Task 14: Markdown report writer

**Files:**
- Create: `src/brain_v42/dream/resonance_report.py`
- Test: `tests/unit/dream/test_resonance_report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/dream/test_resonance_report.py`:

```python
from datetime import date
from pathlib import Path
from uuid import uuid4

from brain_v42.dream.resonance import ResonancePair
from brain_v42.dream.resonance_report import (
    build_report_path,
    render_report,
    write_report,
)


def _pair(domain="ml", cosine=0.91, a_title="A", b_title="B"):
    return ResonancePair(
        a_id=uuid4(), b_id=uuid4(),
        a_project="brain-v42", b_project="red-shrik",
        a_title=a_title, b_title=b_title,
        a_created_at=date(2026, 4, 15), b_created_at=date(2026, 4, 22),
        cosine=cosine, domain=domain,
    )


def test_build_report_path_uses_utc_iso_date():
    path = build_report_path(date(2026, 5, 1), root="artifacts/dream")
    assert path == Path("artifacts/dream/cross_project_resonance_2026-05-01.md")


def test_render_report_zero_pairs_renders_empty_marker():
    out = render_report([], threshold=0.80, run_id="abc-123",
                        report_date=date(2026, 5, 1), domains_scanned=9)
    assert "Pairs found: 0" in out
    assert "No cross-project resonance pairs above threshold this run." in out


def test_render_report_groups_by_domain_with_counts():
    p1 = _pair(domain="ml", cosine=0.91)
    p2 = _pair(domain="ml", cosine=0.83)
    p3 = _pair(domain="memory", cosine=0.85)
    out = render_report([p1, p2, p3], threshold=0.80, run_id="abc-123",
                        report_date=date(2026, 5, 1), domains_scanned=9)
    assert "## Domain: ml (2 pairs)" in out
    assert "## Domain: memory (1 pairs)" in out
    assert "Pairs found: 3" in out
    assert "Domains with pairs: 2" in out


def test_render_report_includes_threshold_run_id_and_date():
    out = render_report([], threshold=0.80, run_id="abc-123",
                        report_date=date(2026, 5, 1), domains_scanned=9)
    assert "2026-05-01" in out
    assert "Threshold: 0.80" in out
    assert "Run ID: abc-123" in out
    assert "Domains scanned: 9" in out


def test_render_report_includes_pair_hint():
    p = _pair(a_title="Cosine 0.85", b_title="Cosine 0.92")
    out = render_report([p], threshold=0.80, run_id="r", report_date=date(2026, 5, 1),
                        domains_scanned=9)
    assert "drift candidate" in out


def test_write_report_overwrites_existing_file(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("OLD CONTENT")
    write_report(path, "NEW CONTENT")
    assert path.read_text() == "NEW CONTENT"


def test_write_report_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "report.md"
    write_report(path, "content")
    assert path.read_text() == "content"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/dream/test_resonance_report.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/brain_v42/dream/resonance_report.py`:

```python
"""Markdown report writer for cross-project resonance script."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from brain_v42.dream.resonance import ResonancePair


def build_report_path(report_date: date, root: str = "artifacts/dream") -> Path:
    return Path(root) / f"cross_project_resonance_{report_date.isoformat()}.md"


def render_report(
    pairs: list[ResonancePair],
    *,
    threshold: float,
    run_id: str,
    report_date: date,
    domains_scanned: int,
) -> str:
    by_domain: dict[str, list[ResonancePair]] = defaultdict(list)
    for p in pairs:
        by_domain[p.domain].append(p)

    lines = [
        f"# Cross-Project Resonance — {report_date.isoformat()}",
        "",
        f"Threshold: {threshold:.2f} · Pairs found: {len(pairs)} · "
        f"Domains scanned: {domains_scanned} · Domains with pairs: {len(by_domain)} · "
        f"Run ID: {run_id}",
        "",
    ]

    if not pairs:
        lines.append("No cross-project resonance pairs above threshold this run.")
        return "\n".join(lines)

    for domain in sorted(by_domain):
        domain_pairs = by_domain[domain]
        lines.append(f"## Domain: {domain} ({len(domain_pairs)} pairs)")
        lines.append("")
        for i, p in enumerate(domain_pairs, start=1):
            lines.append(f"### Pair {i} — cosine={p.cosine:.2f}")
            lines.append(
                f"- [{p.a_project}] Decision {str(p.a_id)[:6]}... · "
                f"\"{p.a_title}\" · {p.a_created_at}"
            )
            lines.append(
                f"- [{p.b_project}] Decision {str(p.b_id)[:6]}... · "
                f"\"{p.b_title}\" · {p.b_created_at}"
            )
            lines.append(f"- Hint: {p.hint}")
            lines.append("")

    return "\n".join(lines)


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/dream/test_resonance_report.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/dream/resonance_report.py tests/unit/dream/test_resonance_report.py
git commit -m "feat(dream): markdown report writer for cross-project resonance"
```

---

## Task 15: Main script — CLI + control flow + killswitch

**Files:**
- Create: `scripts/dream/cross_project_resonance.py`
- Test: `tests/unit/dream/test_cross_project_resonance_script.py`

The script orchestrates: env check → threshold lookup → dream_run start → per-domain Cypher+SQL → markdown write → optional WET writes → dream_run end. Tests use mocks at the repo/service boundary.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/dream/test_cross_project_resonance_script.py`:

```python
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.fixture
def deps(tmp_path, monkeypatch):
    """Build a complete dependency bundle for the script's main()."""
    monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "true")
    graph_service = AsyncMock()
    decision_repo = AsyncMock()
    learning_repo = AsyncMock()
    learning_repo.exists_by_dedup_key.return_value = False
    dream_runs_repo = AsyncMock()
    run_id = uuid4()
    dream_runs_repo.start_run.return_value = run_id
    return {
        "graph_service": graph_service,
        "decision_repo": decision_repo,
        "learning_repo": learning_repo,
        "dream_runs_repo": dream_runs_repo,
        "run_id": run_id,
        "report_root": tmp_path,
    }


@pytest.mark.asyncio
async def test_dry_run_blocked_when_env_disabled(monkeypatch, deps):
    from scripts.dream import cross_project_resonance as script
    monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "false")
    rc = await script.run(mode="dry_run", domains=None, report_date=date(2026, 5, 1),
                          report_root=deps["report_root"], **{k: deps[k] for k in
                          ["graph_service", "decision_repo", "learning_repo",
                           "dream_runs_repo"]})
    assert rc == 0
    deps["dream_runs_repo"].start_run.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_writes_markdown_no_brain_learn(deps):
    from scripts.dream import cross_project_resonance as script
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = [
        uuid4() for _ in range(10)
    ]
    deps["decision_repo"].fetch_cross_project_resonance_pairs.return_value = []
    rc = await script.run(mode="dry_run", domains=["ml"], report_date=date(2026, 5, 1),
                          report_root=deps["report_root"],
                          graph_service=deps["graph_service"],
                          decision_repo=deps["decision_repo"],
                          learning_repo=deps["learning_repo"],
                          dream_runs_repo=deps["dream_runs_repo"])
    assert rc == 0
    deps["learning_repo"].create.assert_not_called()
    report_path = deps["report_root"] / "cross_project_resonance_2026-05-01.md"
    assert report_path.exists()


@pytest.mark.asyncio
async def test_pair_decisions_skips_domains_below_min(deps):
    from scripts.dream import cross_project_resonance as script
    # Return only 3 ids → below MIN_DECISIONS_PER_DOMAIN=5 → skip
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = [
        uuid4() for _ in range(3)
    ]
    rc = await script.run(mode="dry_run", domains=["ml"], report_date=date(2026, 5, 1),
                          report_root=deps["report_root"],
                          graph_service=deps["graph_service"],
                          decision_repo=deps["decision_repo"],
                          learning_repo=deps["learning_repo"],
                          dream_runs_repo=deps["dream_runs_repo"])
    assert rc == 0
    deps["decision_repo"].fetch_cross_project_resonance_pairs.assert_not_called()


@pytest.mark.asyncio
async def test_pair_decisions_caps_per_domain_at_max_decisions(deps):
    from scripts.dream import cross_project_resonance as script
    ids = [uuid4() for _ in range(500)]  # > MAX_DECISIONS_PER_DOMAIN=200
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = ids
    deps["decision_repo"].fetch_cross_project_resonance_pairs.return_value = []
    await script.run(mode="dry_run", domains=["ml"], report_date=date(2026, 5, 1),
                     report_root=deps["report_root"],
                     graph_service=deps["graph_service"],
                     decision_repo=deps["decision_repo"],
                     learning_repo=deps["learning_repo"],
                     dream_runs_repo=deps["dream_runs_repo"])
    call_kwargs = deps["decision_repo"].fetch_cross_project_resonance_pairs.call_args.kwargs
    assert len(call_kwargs["ids"]) == 200


@pytest.mark.asyncio
async def test_wet_mode_writes_brain_learn_per_pair_with_dedup_key(deps):
    from scripts.dream import cross_project_resonance as script
    from brain_v42.dream.resonance import ResonancePair
    pair = ResonancePair(
        a_id=uuid4(), b_id=uuid4(), a_project="brain-v42", b_project="red-shrik",
        a_title="t1", b_title="t2",
        a_created_at=date(2026, 4, 15), b_created_at=date(2026, 4, 22),
        cosine=0.91, domain="ml",
    )
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = [
        uuid4() for _ in range(10)
    ]
    deps["decision_repo"].fetch_cross_project_resonance_pairs.return_value = [pair]

    await script.run(mode="wet", domains=["ml"], report_date=date(2026, 5, 1),
                     report_root=deps["report_root"],
                     graph_service=deps["graph_service"],
                     decision_repo=deps["decision_repo"],
                     learning_repo=deps["learning_repo"],
                     dream_runs_repo=deps["dream_runs_repo"])
    assert deps["learning_repo"].create.call_count == 1
    call_kwargs = deps["learning_repo"].create.call_args.kwargs
    assert "EXCLUDE_FROM_PROMOTE" in call_kwargs["tags"]
    assert call_kwargs["metadata"]["dedup_key"] == pair.dedup_key
    assert call_kwargs["metadata"]["source_kind"] == "cross_project_resonance"


@pytest.mark.asyncio
async def test_wet_mode_idempotent_when_dedup_key_exists(deps):
    from scripts.dream import cross_project_resonance as script
    from brain_v42.dream.resonance import ResonancePair
    pair = ResonancePair(
        a_id=uuid4(), b_id=uuid4(), a_project="a", b_project="b",
        a_title="t1", b_title="t2",
        a_created_at=date(2026, 4, 15), b_created_at=date(2026, 4, 22),
        cosine=0.91, domain="ml",
    )
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = [
        uuid4() for _ in range(10)
    ]
    deps["decision_repo"].fetch_cross_project_resonance_pairs.return_value = [pair]
    deps["learning_repo"].exists_by_dedup_key.return_value = True

    await script.run(mode="wet", domains=["ml"], report_date=date(2026, 5, 1),
                     report_root=deps["report_root"],
                     graph_service=deps["graph_service"],
                     decision_repo=deps["decision_repo"],
                     learning_repo=deps["learning_repo"],
                     dream_runs_repo=deps["dream_runs_repo"])
    deps["learning_repo"].create.assert_not_called()


@pytest.mark.asyncio
async def test_wet_mode_blocked_when_env_disabled_inside_branch(deps, monkeypatch):
    from scripts.dream import cross_project_resonance as script
    # Env on at start of script (so we don't take the early exit), but flipped off
    # before WET branch via monkeypatch on the env getter
    monkeypatch.setattr(script, "env_enabled",
                        MagicMock(side_effect=[True, False]))  # outer True, inner False
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = [
        uuid4() for _ in range(10)
    ]
    deps["decision_repo"].fetch_cross_project_resonance_pairs.return_value = []
    rc = await script.run(mode="wet", domains=["ml"], report_date=date(2026, 5, 1),
                          report_root=deps["report_root"],
                          graph_service=deps["graph_service"],
                          decision_repo=deps["decision_repo"],
                          learning_repo=deps["learning_repo"],
                          dream_runs_repo=deps["dream_runs_repo"])
    assert rc == 1
    deps["learning_repo"].create.assert_not_called()


@pytest.mark.asyncio
async def test_dream_runs_lifecycle_completed_on_success(deps):
    from scripts.dream import cross_project_resonance as script
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.return_value = []
    rc = await script.run(mode="dry_run", domains=["ml"], report_date=date(2026, 5, 1),
                          report_root=deps["report_root"],
                          graph_service=deps["graph_service"],
                          decision_repo=deps["decision_repo"],
                          learning_repo=deps["learning_repo"],
                          dream_runs_repo=deps["dream_runs_repo"])
    deps["dream_runs_repo"].start_run.assert_called_once()
    deps["dream_runs_repo"].end_run.assert_called_once()
    end_kwargs = deps["dream_runs_repo"].end_run.call_args.kwargs
    assert end_kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_dream_runs_lifecycle_error_on_exception(deps):
    from scripts.dream import cross_project_resonance as script
    deps["graph_service"].fetch_decision_ids_in_domain_across_projects.side_effect = (
        RuntimeError("boom")
    )
    with pytest.raises(RuntimeError):
        await script.run(mode="dry_run", domains=["ml"], report_date=date(2026, 5, 1),
                         report_root=deps["report_root"],
                         graph_service=deps["graph_service"],
                         decision_repo=deps["decision_repo"],
                         learning_repo=deps["learning_repo"],
                         dream_runs_repo=deps["dream_runs_repo"])
    end_kwargs = deps["dream_runs_repo"].end_run.call_args.kwargs
    assert end_kwargs["status"] == "error"


def test_argparse_default_mode_is_dry_run():
    from scripts.dream import cross_project_resonance as script
    parser = script.build_arg_parser()
    args = parser.parse_args([])
    assert args.mode == "dry_run"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/dream/test_cross_project_resonance_script.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement script**

Create `scripts/dream/cross_project_resonance.py`:

```python
#!/usr/bin/env python3
"""Cross-project resonance detection script (Spec C MVP β).

Scans each closed knowledge domain for pairs of Decisions from different
projects whose cosine similarity is >= threshold. Emits a markdown report.
WET mode also writes brain_learn entries (idempotent via dedup_key).

Killswitch: BRAIN_DREAM_CROSS_PROJECT_ENABLED must be set to "true".
DRY_RUN by default; --mode wet requires explicit intent + env var ON.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from brain_v42 import thresholds
from brain_v42.dream.resonance import ResonancePair
from brain_v42.dream.resonance_report import build_report_path, render_report, write_report
from brain_v42.services.graph_service import ALLOWED_DOMAINS

logger = logging.getLogger(__name__)

MIN_DECISIONS_PER_DOMAIN = 5
MAX_DECISIONS_PER_DOMAIN = 200
MAX_PAIRS_PER_NIGHT = 20


def env_enabled() -> bool:
    return os.environ.get("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "").lower() == "true"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["dry_run", "wet"], default="dry_run")
    p.add_argument("--domains", default=None,
                   help="Comma-separated domain list; default = all 9")
    p.add_argument("--date", default=None,
                   help="UTC ISO date for report; default = today UTC")
    return p


async def run(
    *,
    mode: str,
    domains: list[str] | None,
    report_date: date,
    report_root: Path,
    graph_service: Any,
    decision_repo: Any,
    learning_repo: Any,
    dream_runs_repo: Any,
) -> int:
    if not env_enabled():
        logger.info("cross-project resonance disabled (env var off), exiting")
        return 0

    threshold_spec = thresholds.by_name("cross_project_resonance_min")
    if threshold_spec is None:
        logger.error("threshold registry missing 'cross_project_resonance_min'")
        return 1
    threshold = threshold_spec.value

    target_domains = sorted(domains or ALLOWED_DOMAINS)

    run_id = await dream_runs_repo.start_run(kind="cross_project_resonance", mode=mode)

    try:
        all_pairs: list[ResonancePair] = []
        for domain in target_domains:
            ids = await graph_service.fetch_decision_ids_in_domain_across_projects(domain)
            if len(ids) < MIN_DECISIONS_PER_DOMAIN:
                continue
            pairs = await decision_repo.fetch_cross_project_resonance_pairs(
                ids=ids[:MAX_DECISIONS_PER_DOMAIN],
                threshold=threshold,
                domain=domain,
            )
            all_pairs.extend(pairs)

        all_pairs.sort(key=lambda p: p.cosine, reverse=True)
        all_pairs = all_pairs[:MAX_PAIRS_PER_NIGHT]

        report_path = build_report_path(report_date, root=str(report_root))
        report_md = render_report(
            all_pairs, threshold=threshold, run_id=str(run_id),
            report_date=report_date, domains_scanned=len(target_domains),
        )
        write_report(report_path, report_md)
        logger.info("wrote report: %s (%d pairs)", report_path, len(all_pairs))

        if mode == "wet":
            if not env_enabled():
                logger.error("WET mode blocked: env disabled (defensive double-check)")
                await dream_runs_repo.end_run(run_id, status="blocked", pair_count=0)
                return 1
            written = 0
            for pair in all_pairs:
                if await learning_repo.exists_by_dedup_key(pair.dedup_key):
                    continue
                await learning_repo.create(
                    topic=f"cross_project_resonance/{pair.domain}",
                    insight=pair.format_insight(),
                    tags=["dream", "cross_project_resonance",
                          pair.domain, "EXCLUDE_FROM_PROMOTE"],
                    project_key="brain-v42",
                    metadata={
                        "dedup_key": pair.dedup_key,
                        "source_kind": "cross_project_resonance",
                        "dream_run_id": str(run_id),
                    },
                )
                written += 1
            await dream_runs_repo.end_run(run_id, status="completed", pair_count=written)
        else:
            await dream_runs_repo.end_run(
                run_id, status="completed", pair_count=len(all_pairs)
            )
    except Exception:
        await dream_runs_repo.end_run(run_id, status="error", pair_count=0)
        raise

    return 0


def main() -> int:
    """CLI entry point. Wires the real production dependencies."""
    parser = build_arg_parser()
    args = parser.parse_args()

    domains = args.domains.split(",") if args.domains else None
    report_date = (
        date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    )

    # Lazy imports so test fixtures can mock without full app boot
    from brain_v42.config import Settings
    from brain_v42.repositories.pg_decision import DecisionRepo
    from brain_v42.repositories.pg_learning import LearningRepo
    from brain_v42.repositories.pg_dream_runs import DreamRunsRepo  # adapt if path differs
    from brain_v42.services.graph_service import GraphService
    # Build engine + sessions per the existing script pattern (cf. promote_prepare.py)
    # ... bootstrap omitted for brevity; copy from promote_prepare.py main()

    raise NotImplementedError(
        "main() bootstrap: copy session_factory + Neo4j driver setup from "
        "scripts/dream/promote_prepare.py main(), then call:\n"
        "  asyncio.run(run(mode=args.mode, domains=domains, report_date=report_date,\n"
        "                  report_root=Path('artifacts/dream'),\n"
        "                  graph_service=..., decision_repo=..., learning_repo=...,\n"
        "                  dream_runs_repo=...))"
    )


if __name__ == "__main__":
    sys.exit(main())
```

Then complete the `main()` bootstrap by mirroring `scripts/dream/promote_prepare.py` (engine creation, session factory, GraphService driver). Keep the `NotImplementedError` block in until you've copied the bootstrap. The `run()` function being independently testable is the point — `main()` is just plumbing.

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/dream/test_cross_project_resonance_script.py -v`
Expected: all 10 tests PASS (the bootstrap-`main()` is not under test).

- [ ] **Step 5: Commit**

```bash
git add scripts/dream/cross_project_resonance.py tests/unit/dream/test_cross_project_resonance_script.py
git commit -m "feat(dream): cross_project_resonance.py — DRY_RUN/WET script with killswitch"
```

---

## Task 16: PROMOTE insulation — exclude resonance learnings

**Files:**
- Modify: `scripts/dream/promote_prepare.py` (add tag exclusion to `_CANDIDATE_SQL`)
- Test: `tests/unit/test_promote_prepare.py` (extend)

- [ ] **Step 1: Write failing test**

In `tests/unit/test_promote_prepare.py`, add:

```python
import inspect

from scripts.dream import promote_prepare


def test_candidate_sql_excludes_resonance_learnings():
    """Cross-project resonance learnings (tagged EXCLUDE_FROM_PROMOTE) must not
    re-enter the PROMOTE candidate pool — feedback loop insulation per Spec C
    MVP β."""
    sql = str(promote_prepare._CANDIDATE_SQL)
    assert "EXCLUDE_FROM_PROMOTE" in sql
    # Should be in a NOT IN / != ALL clause on tags
    assert "tags" in sql
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/unit/test_promote_prepare.py -v -k resonance`
Expected: FAIL — `EXCLUDE_FROM_PROMOTE` not in SQL.

- [ ] **Step 3: Modify `_CANDIDATE_SQL`**

In `scripts/dream/promote_prepare.py`, add a new clause to `_CANDIDATE_SQL`:

```python
_CANDIDATE_SQL = sa.text(
    """
    SELECT l.id, l.topic, l.insight AS content, l.tags, l.metadata,
           l.confidence, l.access_count, l.created_at
    FROM learnings l
    WHERE (NOW() - l.created_at) >= INTERVAL '7 days'
      AND l.access_count >= 3
      AND NOT (l.confidence = 'low' AND l.access_count < 5)
      AND ('dream:generated' != ALL(l.tags)
           OR l.validated_at IS NOT NULL
           OR l.confidence != 'low')
      AND 'EXCLUDE_FROM_PROMOTE' != ALL(l.tags)         -- Spec C feedback-loop insulation
      AND l.project_key = :pk
      AND NOT EXISTS (
          SELECT 1 FROM dream_promotions p
          WHERE p.source_learning_id = l.id
            AND (
                (p.target_type = 'adr' AND p.target_adr_id IS NOT NULL)
                OR (p.target_type = 'runbook' AND p.target_runbook_id IS NOT NULL)
                OR p.target_type = 'skipped_dedup'
            )
      )
    ORDER BY l.access_count DESC, l.created_at DESC
    LIMIT :lim
    """
)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_promote_prepare.py -v`
Expected: new test PASSES, all existing promote_prepare tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/dream/promote_prepare.py tests/unit/test_promote_prepare.py
git commit -m "fix(promote): exclude EXCLUDE_FROM_PROMOTE-tagged learnings from candidate pool"
```

---

## Task 17: Final integration check + push + MR

**Files:** none (verification + git ops only).

- [ ] **Step 1: Run full unit suite**

Run: `pytest tests/unit -q 2>&1 | tail -5`
Expected: `1837 + N passed` where N = new test count (~30). Zero failures, zero errors.

- [ ] **Step 2: Run ruff + mypy if configured**

Run: `ruff check src/ tests/ scripts/`
Expected: no errors.

Run: `mypy src/` (if mypy is part of CI per CLAUDE.md)
Expected: no new errors introduced.

- [ ] **Step 3: Verify killswitch is closed in test config**

Run: `grep -rn "BRAIN_DREAM_CROSS_PROJECT_ENABLED" tests/`
Expected: only in the specific tests that explicitly set it via `monkeypatch.setenv(...)`. No `conftest.py` global override.

- [ ] **Step 4: Spot-check the spec coverage**

Re-read the spec sections and verify every Goal is covered by a task above:
- Goal 1 (briefing enrichment) — Tasks 3, 4, 5, 6, 7, 8 ✓
- Goal 2 (resonance script) — Tasks 9, 10, 11, 12, 13, 14, 15 ✓
- Goal 3 (killswitch) — Tasks 2, 8, 15 ✓
- Goal 4 (no MCP tool, no schema migration) — verified by absence ✓
- Goal 5 (1837 baseline + new tests TDD) — Step 1 above ✓
- PROMOTE insulation (spec safety §7) — Task 16 ✓

- [ ] **Step 5: Push branch**

```bash
git push -u origin feat/dream-cross-project-resonance-and-briefing
```

- [ ] **Step 6: Create MR**

```bash
glab mr create \
  --title "feat(dream): Spec C MVP β — cross-project briefing + resonance detection" \
  --description "$(cat <<'EOF'
## Summary

Spec C MVP β implementation per `docs/superpowers/specs/2026-05-01-spec-c-mvp-cross-project-design.md` (v2 post-multi-judge).

- Enrich `brain_session_start` with cross-project briefing section (top-N entries from active domains in other projects)
- New script `scripts/dream/cross_project_resonance.py` for DRY_RUN-able pair detection across projects
- Killswitch `BRAIN_DREAM_CROSS_PROJECT_ENABLED=false` (closed by default)
- PROMOTE insulation: EXCLUDE_FROM_PROMOTE tag filter prevents WET writes from re-entering pipeline
- New threshold registry entry `cross_project_resonance_min=0.80` (uncalibrated)
- Zero schema migration, zero new MCP tool, additive-only changes

## Test Plan

- [x] All existing unit tests still pass (1837 baseline)
- [x] ~30 new unit tests cover briefing path, resonance script (DRY_RUN/WET), idempotency, killswitch double-guards, PROMOTE filter
- [x] Killswitch closed in default test config
- [ ] Manual verification J+1: `export BRAIN_DREAM_CROSS_PROJECT_ENABLED=true` and observe briefing section in 2-3 sessions
- [ ] Manual verification J+2-5: run `python -m scripts.dream.cross_project_resonance --mode dry_run` and review markdown reports

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

(Use `gh pr create` instead if the project uses GitHub. The `glab` command is for GitLab — verify project remote first via `git remote -v`.)

- [ ] **Step 7: Wait for pipeline GREEN, then merge**

Monitor via `glab ci status` or `glab mr view --web`. Merge once green per existing project conventions (see recent merges of MR !61, !62 for the pattern).

---

## Post-merge follow-ups (out of MVP, not part of this plan)

These are tracked in the spec's "Follow-ups" section. Do not include in this MR:

1. Recalibrate `cross_project_resonance_min` after 5+ DRY_RUN nights.
2. Add cron nightly trigger for the script (separate MR).
3. Open WET mode after manual review confirms 0 false-positive aberrations.
4. Extend resonance to Learnings/ADRs (Decisions only in MVP).
5. Cross-project surfacing in `brain_search` (Spec C iteration α/δ).
6. Latency optimization with cached `fetch_active_domains` if briefing p99 > 500ms observed.
