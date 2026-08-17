# CONNECT Canonical Orphans and Honest Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Dream CONNECT classify only active canonical PostgreSQL entities and fail the phase whenever its final report contains errors or violates the two-line contract.

**Architecture:** Add one read-only orphan query to `PgGraphLedgerRepo` and expose it through `DurableGraphService`, preserving the current Neo4j method when the durable facade is disabled. Add a CONNECT-specific, fail-closed report validator after the final phase attempt so malformed or non-zero-error reports mark the latest CONNECT `dream_runs` row partial and contribute to the nightly failure count.

**Tech Stack:** Python 3.12, SQLAlchemy async Core, PostgreSQL, pytest/pytest-asyncio, Bash, Ruff, mypy.

## Global Constraints

- PostgreSQL `brain_entities` and `entity_relations` are the authority when the durable graph facade is enabled.
- Eligible types are exactly `decision`, `learning`, `snippet`, `runbook`, and `adr`.
- Eligible entities have `lifecycle = 'active'`, a non-null `source_uuid`, no active `RELATED_TO` edge in either direction, and no active outgoing `BELONGS_TO_DOMAIN` edge.
- Apply optional `project_key`, deterministic `created_at, id` ordering, and `limit` inside the PostgreSQL query.
- Preserve the existing Neo4j `GraphService.find_orphans_for_classification()` path when `DurableGraphService` is disabled.
- Keep the MCP tool response contract unchanged: rows shaped as `{"id": <uuid>, "labels": [<Neo4j label>]}` before metadata hydration.
- Do not modify `DurableGraphService.link_entity_to_domain`; GitNexus reports CRITICAL upstream impact for that write contract.
- CONNECT succeeds only when the report is exactly two non-empty lines in `STEP_A` then `STEP_B` order, every count is non-negative, freshness is in `0.00..1.00`, and both error counts are zero.
- A validator failure marks the latest `dream_runs` row with `phase='connect'` and the supplied run date as `partial`, stores at most 1,000 error characters, and exits non-zero.
- No schema migration, Neo4j rebuild, MCP response change, dependency change, merge, or push is part of this plan.

---

## File Map

- `src/brain_v42/repositories/pg_graph_ledger.py` — canonical orphan selection.
- `src/brain_v42/services/durable_graph_service.py` — facade routing and entity-type-to-label mapping.
- `scripts/dream/connect_validate.py` — pure report parsing plus failure persistence CLI.
- `scripts/dream.sh` — invoke the validator after CONNECT's final successful process exit.
- `tests/unit/repositories/test_pg_graph_ledger.py` — fast SQL-shape and mapping contract.
- `tests/integration/db/test_graph_classification_orphans.py` — PostgreSQL behavior across lifecycle, relations, ordering, limit, and project scope.
- `tests/unit/services/test_durable_graph_service.py` — enabled canonical path and disabled legacy fallback.
- `tests/unit/test_connect_validate.py` — report contract and `dream_runs` partial persistence.
- `tests/unit/test_dream_sh_connect_validator.py` — static shell wiring and syntax contract.

### Task 1: Canonical PostgreSQL orphan selection

**Files:**

- Modify: `src/brain_v42/repositories/pg_graph_ledger.py:183-189`
- Modify: `tests/unit/repositories/test_pg_graph_ledger.py`
- Create: `tests/integration/db/test_graph_classification_orphans.py`

**Interfaces:**

- Consumes: `brain_entities`, `entity_relations`, and the injected `async_sessionmaker[AsyncSession]`.
- Produces: `PgGraphLedgerRepo.list_active_classification_orphans(*, limit: int = 20, project_key: str | None = None) -> list[dict[str, Any]]`.
- Each result contains `source_uuid: UUID` and `entity_type: str`.

- [ ] **Step 1: Add a failing unit contract for query shape and result mapping**

Append this test to `tests/unit/repositories/test_pg_graph_ledger.py`:

```python
@pytest.mark.asyncio
async def test_list_active_classification_orphans_uses_canonical_filters() -> None:
    from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

    source_uuid = uuid4()
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {"source_uuid": source_uuid, "entity_type": "learning"}
    ]
    factory, session = _session_factory_with_results(result)
    repo = PgGraphLedgerRepo(factory)

    rows = await repo.list_active_classification_orphans(
        limit=7,
        project_key="brain-v42",
    )

    assert rows == [{"source_uuid": source_uuid, "entity_type": "learning"}]
    statement, params = session.execute.await_args.args
    sql = " ".join(str(statement).lower().split())
    assert "candidate.lifecycle = 'active'" in sql
    assert "candidate.source_uuid is not null" in sql
    assert "relation.relation_type = 'related_to'" in sql
    assert "relation.source_entity_id = candidate.id" in sql
    assert "relation.target_entity_id = candidate.id" in sql
    assert "domain_relation.relation_type = 'belongs_to_domain'" in sql
    assert "domain_relation.source_entity_id = candidate.id" in sql
    assert "order by candidate.created_at, candidate.id" in sql
    assert "limit :limit" in sql
    assert params == {"limit": 7, "project_key": "brain-v42"}
    session.commit.assert_not_awaited()
```

- [ ] **Step 2: Run the unit test and confirm RED**

Run:

```bash
uv run pytest tests/unit/repositories/test_pg_graph_ledger.py::test_list_active_classification_orphans_uses_canonical_filters -v
```

Expected: FAIL with `AttributeError: 'PgGraphLedgerRepo' object has no attribute 'list_active_classification_orphans'`.

- [ ] **Step 3: Implement the minimal canonical read**

Add this method immediately after `PgGraphLedgerRepo.__init__`:

```python
async def list_active_classification_orphans(
    self,
    *,
    limit: int = 20,
    project_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return active knowledge entities without graph or domain relations."""
    statement = sa.text(
        """
        SELECT candidate.source_uuid, candidate.entity_type
        FROM brain_entities AS candidate
        WHERE candidate.lifecycle = 'active'
          AND candidate.entity_type IN (
              'decision', 'learning', 'snippet', 'runbook', 'adr'
          )
          AND candidate.source_uuid IS NOT NULL
          AND (:project_key IS NULL OR candidate.project_key = :project_key)
          AND NOT EXISTS (
              SELECT 1
              FROM entity_relations AS relation
              WHERE relation.lifecycle = 'active'
                AND relation.relation_type = 'RELATED_TO'
                AND (
                    relation.source_entity_id = candidate.id
                    OR relation.target_entity_id = candidate.id
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM entity_relations AS domain_relation
              WHERE domain_relation.lifecycle = 'active'
                AND domain_relation.relation_type = 'BELONGS_TO_DOMAIN'
                AND domain_relation.source_entity_id = candidate.id
          )
        ORDER BY candidate.created_at, candidate.id
        LIMIT :limit
        """
    )
    async with self._session_factory() as session:
        result = await session.execute(
            statement,
            {"limit": max(1, limit), "project_key": project_key},
        )
    return [dict(row) for row in result.mappings().all()]
```

- [ ] **Step 4: Run the unit test and confirm GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Add the PostgreSQL behavior test**

Create `tests/integration/db/test_graph_classification_orphans.py` with one isolated scenario. Seed two `integ-...` projects and direct canonical rows with controlled `created_at` values:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_entities, entity_relations, projects
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

pytestmark = pytest.mark.integration


async def _entity(
    session: AsyncSession,
    *,
    project_key: str | None,
    entity_type: str = "learning",
    lifecycle: str = "active",
    created_at: datetime,
) -> tuple[UUID, UUID | None]:
    entity_id = uuid4()
    source_uuid = None if entity_type == "domain" else uuid4()
    await session.execute(
        sa.insert(brain_entities).values(
            id=entity_id,
            entity_type=entity_type,
            entity_key=str(source_uuid or f"domain-{entity_id}"),
            source_uuid=source_uuid,
            project_key=project_key,
            scope_kind="project" if project_key else "global",
            lifecycle=lifecycle,
            created_at=created_at,
        )
    )
    return entity_id, source_uuid


async def test_lists_only_active_canonical_orphans_before_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:10]
    project_key = f"integ-orphans-{suffix}"
    other_project = f"integ-orphans-other-{suffix}"
    now = datetime.now(UTC)

    async with session_factory.begin() as session:
        await session.execute(
            sa.insert(projects),
            [{"project_key": project_key}, {"project_key": other_project}],
        )
        _, archived_source = await _entity(
            session,
            project_key=project_key,
            lifecycle="archived",
            created_at=now - timedelta(minutes=10),
        )
        _, expected_source = await _entity(
            session,
            project_key=project_key,
            created_at=now,
        )
        _, other_source = await _entity(
            session,
            project_key=other_project,
            created_at=now - timedelta(minutes=9),
        )
        related_source_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=8)
        )
        related_target_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=7)
        )
        await session.execute(
            sa.insert(entity_relations).values(
                source_entity_id=related_source_id,
                target_entity_id=related_target_id,
                relation_type="RELATED_TO",
                origin="integration",
                lifecycle="active",
            )
        )
        domain_member_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=6)
        )
        domain_id, _ = await _entity(
            session,
            project_key=project_key,
            entity_type="domain",
            created_at=now - timedelta(minutes=5),
        )
        await session.execute(
            sa.insert(entity_relations).values(
                source_entity_id=domain_member_id,
                target_entity_id=domain_id,
                relation_type="BELONGS_TO_DOMAIN",
                origin="integration",
                lifecycle="active",
            )
        )

    repo = PgGraphLedgerRepo(session_factory)
    limited = await repo.list_active_classification_orphans(
        limit=1,
        project_key=project_key,
    )
    scoped = await repo.list_active_classification_orphans(
        limit=20,
        project_key=project_key,
    )
    other_scoped = await repo.list_active_classification_orphans(
        limit=20,
        project_key=other_project,
    )

    expected = [{"source_uuid": expected_source, "entity_type": "learning"}]
    assert limited == expected
    assert scoped == expected
    assert archived_source not in {row["source_uuid"] for row in scoped}
    assert other_scoped == [{"source_uuid": other_source, "entity_type": "learning"}]
```

This scenario proves archived rows are filtered before `LIMIT`, both endpoints of an active `RELATED_TO` relation are excluded, outgoing domain membership is excluded, and project scoping holds.

- [ ] **Step 6: Run repository verification**

Run:

```bash
uv run pytest tests/unit/repositories/test_pg_graph_ledger.py -v
uv run pytest tests/integration/db/test_graph_classification_orphans.py -v
```

Expected: unit suite PASS; integration test PASS when `BRAIN_V42_TEST_DB_URL` is available, otherwise a guarded SKIP that explicitly names the missing dedicated test database.

- [ ] **Step 7: Commit the repository slice**

```bash
git add src/brain_v42/repositories/pg_graph_ledger.py \
  tests/unit/repositories/test_pg_graph_ledger.py \
  tests/integration/db/test_graph_classification_orphans.py
git commit -m "🐛 fix: list canonical CONNECT orphans"
```

### Task 2: Route durable orphan reads through the ledger

**Files:**

- Modify: `src/brain_v42/services/durable_graph_service.py:62-81`
- Modify: `tests/unit/services/test_durable_graph_service.py`

**Interfaces:**

- Consumes: Task 1's `list_active_classification_orphans(limit=..., project_key=...)` result.
- Produces: `DurableGraphService.find_orphans_for_classification(limit: int = 20, *, project_key: str | None = None) -> list[dict[str, Any]]`.
- Entity label mapping is exact: `decision -> Decision`, `learning -> Learning`, `snippet -> Snippet`, `runbook -> Runbook`, `adr -> ADR`.

- [ ] **Step 1: Add failing enabled and disabled path tests**

Add `AsyncMock` to the imports in `tests/unit/services/test_durable_graph_service.py`, then append:

```python
@pytest.mark.asyncio
async def test_enabled_facade_reads_classification_orphans_from_ledger() -> None:
    trace: list[tuple[Any, ...]] = []
    first, second = uuid4(), uuid4()
    ledger = _FakeLedger(trace)
    ledger.list_active_classification_orphans = AsyncMock(
        return_value=[
            {"source_uuid": first, "entity_type": "learning"},
            {"source_uuid": second, "entity_type": "adr"},
        ]
    )
    graph = _FakeGraph(trace)
    graph.find_orphans_for_classification = AsyncMock()
    service = DurableGraphService(graph, ledger)

    rows = await service.find_orphans_for_classification(
        limit=9,
        project_key="brain-v42",
    )

    assert rows == [
        {"id": str(first), "labels": ["Learning"]},
        {"id": str(second), "labels": ["ADR"]},
    ]
    ledger.list_active_classification_orphans.assert_awaited_once_with(
        limit=9,
        project_key="brain-v42",
    )
    graph.find_orphans_for_classification.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_facade_keeps_legacy_orphan_reader() -> None:
    trace: list[tuple[Any, ...]] = []
    legacy = [{"id": str(uuid4()), "labels": ["Decision"]}]
    ledger = _FakeLedger(trace)
    ledger.list_active_classification_orphans = AsyncMock()
    graph = _FakeGraph(trace)
    graph.find_orphans_for_classification = AsyncMock(return_value=legacy)
    service = DurableGraphService(graph, ledger, enabled=False)

    rows = await service.find_orphans_for_classification(
        limit=4,
        project_key="brain-v42",
    )

    assert rows == legacy
    graph.find_orphans_for_classification.assert_awaited_once_with(
        limit=4,
        project_key="brain-v42",
    )
    ledger.list_active_classification_orphans.assert_not_awaited()
```

- [ ] **Step 2: Run both tests and confirm RED**

Run:

```bash
uv run pytest \
  tests/unit/services/test_durable_graph_service.py::test_enabled_facade_reads_classification_orphans_from_ledger \
  tests/unit/services/test_durable_graph_service.py::test_disabled_facade_keeps_legacy_orphan_reader \
  -v
```

Expected: enabled test FAIL because `__getattr__` delegates to the mocked Neo4j reader instead of the ledger.

- [ ] **Step 3: Implement the facade method**

Add this module constant below the imports:

```python
_CLASSIFICATION_LABELS = {
    "decision": "Decision",
    "learning": "Learning",
    "snippet": "Snippet",
    "runbook": "Runbook",
    "adr": "ADR",
}
```

Add this method after `__getattr__`:

```python
async def find_orphans_for_classification(
    self,
    limit: int = 20,
    *,
    project_key: str | None = None,
) -> list[dict[str, Any]]:
    if not self._enabled:
        return cast(
            list[dict[str, Any]],
            await self._graph.find_orphans_for_classification(
                limit=limit,
                project_key=project_key,
            ),
        )

    rows = await self._ledger.list_active_classification_orphans(
        limit=limit,
        project_key=project_key,
    )
    return [
        {
            "id": str(row["source_uuid"]),
            "labels": [_CLASSIFICATION_LABELS[str(row["entity_type"])]],
        }
        for row in rows
    ]
```

- [ ] **Step 4: Run service and MCP regression tests**

Run:

```bash
uv run pytest \
  tests/unit/services/test_durable_graph_service.py \
  tests/unit/test_brain_list_orphans_for_classification.py \
  tests/unit/mcp/tools/test_project_scoped_graph_tools.py \
  -v
```

Expected: PASS. The MCP tests prove hydration and JSON output remain unchanged.

- [ ] **Step 5: Commit the facade slice**

```bash
git add src/brain_v42/services/durable_graph_service.py \
  tests/unit/services/test_durable_graph_service.py
git commit -m "🐛 fix: route CONNECT reads through ledger"
```

### Task 3: Validate CONNECT's exact report and persist partial failures

**Files:**

- Create: `scripts/dream/connect_validate.py`
- Create: `tests/unit/test_connect_validate.py`

**Interfaces:**

- CLI consumes `--report-log PATH --run-date YYYY-MM-DD`.
- `parse_report(raw: str) -> ConnectReport` accepts exactly two non-empty lines in the documented field order.
- `_mark_latest_connect_partial(session_factory, run_date, error_message) -> bool` updates the highest `dream_runs.id` for that date and `phase='connect'`.
- CLI returns `0` only for a zero-error valid report; contract or error-count failures return `1` after the partial update attempt.

- [ ] **Step 1: Write parser and error-bucket tests first**

Create `tests/unit/test_connect_validate.py` with this pure test surface:

```python
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import dream_runs
from scripts.dream.connect_validate import (
    ValidationFailure,
    _mark_latest_connect_partial,
    parse_report,
)
from tests.conftest import require_test_db_url

VALID_REPORT = (
    "STEP_A: entities_processed=10 created=2 matched=8 skipped=0 errors=0 freshness=0.20\n"
    "STEP_B: orphans_listed=4 created=3 matched=1 invalid=0 errors=0\n"
)


def test_parse_report_accepts_exact_zero_error_contract() -> None:
    report = parse_report(VALID_REPORT)
    assert report.step_a["entities_processed"] == 10
    assert report.step_a["freshness"] == 0.20
    assert report.step_b["orphans_listed"] == 4


@pytest.mark.parametrize(
    "raw",
    [
        VALID_REPORT.replace("errors=0 freshness", "errors=1 freshness", 1),
        VALID_REPORT.rsplit("errors=0", 1)[0] + "errors=2\n",
    ],
)
def test_parse_report_rejects_any_non_zero_error_bucket(raw: str) -> None:
    with pytest.raises(ValidationFailure, match="reported errors"):
        parse_report(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (VALID_REPORT.splitlines()[0], "exactly two"),
        (VALID_REPORT + VALID_REPORT.splitlines()[1] + "\n", "exactly two"),
        (VALID_REPORT.replace("STEP_A", "STEP_X"), "malformed STEP_A"),
        (VALID_REPORT.replace("created=2", "created=-2"), "non-negative"),
        (VALID_REPORT.replace("freshness=0.20", "freshness=1.20"), "freshness"),
    ],
)
def test_parse_report_fails_closed(raw: str, message: str) -> None:
    with pytest.raises(ValidationFailure, match=message):
        parse_report(raw)


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncEngine:  # type: ignore[misc]
    database = create_async_engine(require_test_db_url(), poolclass=NullPool)
    try:
        async with database.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL test database is not reachable: {exc}")
    yield database  # type: ignore[misc]
    await database.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_marks_only_latest_connect_row_partial_with_bounded_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_date = dt.date(2099, 12, 30)
    async with session_factory.begin() as session:
        first_id = (
            await session.execute(
                dream_runs.insert()
                .values(run_date=run_date, phase="connect", status="done")
                .returning(dream_runs.c.id)
            )
        ).scalar_one()
        second_id = (
            await session.execute(
                dream_runs.insert()
                .values(run_date=run_date, phase="connect", status="done")
                .returning(dream_runs.c.id)
            )
        ).scalar_one()

    assert await _mark_latest_connect_partial(session_factory, run_date, "x" * 1_200)

    async with session_factory.begin() as session:
        rows = (
            await session.execute(
                sa.select(
                    dream_runs.c.id,
                    dream_runs.c.status,
                    dream_runs.c.error_message,
                )
                .where(dream_runs.c.id.in_([first_id, second_id]))
                .order_by(dream_runs.c.id)
            )
        ).mappings().all()
        await session.execute(
            dream_runs.delete().where(dream_runs.c.id.in_([first_id, second_id]))
        )

    assert rows[0]["status"] == "done"
    assert rows[0]["error_message"] is None
    assert rows[1]["status"] == "partial"
    assert rows[1]["error_message"] == "x" * 1_000


def test_main_reports_missing_dream_run_on_invalid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.dream import connect_validate

    report_log = tmp_path / "connect.log"
    report_log.write_text(
        VALID_REPORT.replace("errors=0 freshness", "errors=6 freshness"),
        encoding="utf-8",
    )
    marker = AsyncMock(return_value=False)
    monkeypatch.setattr(connect_validate, "_mark_latest_connect_partial", marker)
    monkeypatch.setattr(connect_validate, "Settings", lambda: MagicMock(postgres_url="unused"))
    monkeypatch.setattr(connect_validate, "_build_factory", lambda _url: MagicMock())

    assert connect_validate.main(
        ["--report-log", str(report_log), "--run-date", "2026-07-25"]
    ) == 1
    assert "no CONNECT dream_runs row" in capsys.readouterr().err
```

- [ ] **Step 2: Run the new test file and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_connect_validate.py -v
```

Expected: collection ERROR with `ModuleNotFoundError: No module named 'scripts.dream.connect_validate'`.

- [ ] **Step 3: Implement the validator module**

Create `scripts/dream/connect_validate.py` with:

```python
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.config import Settings
from brain_v42.db.tables import dream_runs

_MAX_ERROR_LENGTH = 1_000
_STEP_A_RE = re.compile(
    r"STEP_A: entities_processed=(?P<entities_processed>-?\d+) "
    r"created=(?P<created>-?\d+) matched=(?P<matched>-?\d+) "
    r"skipped=(?P<skipped>-?\d+) errors=(?P<errors>-?\d+) "
    r"freshness=(?P<freshness>-?\d+\.\d{2})"
)
_STEP_B_RE = re.compile(
    r"STEP_B: orphans_listed=(?P<orphans_listed>-?\d+) "
    r"created=(?P<created>-?\d+) matched=(?P<matched>-?\d+) "
    r"invalid=(?P<invalid>-?\d+) errors=(?P<errors>-?\d+)"
)


class ValidationFailure(Exception):
    """Violation of the CONNECT final-report contract."""


@dataclass(frozen=True, slots=True)
class ConnectReport:
    step_a: dict[str, int | float]
    step_b: dict[str, int]


def parse_report(raw: str) -> ConnectReport:
    lines = raw.splitlines()
    if len(lines) != 2:
        raise ValidationFailure(f"expected exactly two report lines, got {len(lines)}")
    step_a_match = _STEP_A_RE.fullmatch(lines[0])
    if step_a_match is None:
        raise ValidationFailure("malformed STEP_A report line")
    step_b_match = _STEP_B_RE.fullmatch(lines[1])
    if step_b_match is None:
        raise ValidationFailure("malformed STEP_B report line")

    step_a: dict[str, int | float] = {
        key: float(value) if key == "freshness" else int(value)
        for key, value in step_a_match.groupdict().items()
    }
    step_b = {key: int(value) for key, value in step_b_match.groupdict().items()}
    for name, value in (*step_a.items(), *step_b.items()):
        if name != "freshness" and value < 0:
            raise ValidationFailure(f"{name} must be non-negative")
    freshness = float(step_a["freshness"])
    if not 0.0 <= freshness <= 1.0:
        raise ValidationFailure(f"freshness must be within 0.00..1.00, got {freshness:.2f}")
    if step_a["errors"] or step_b["errors"]:
        raise ValidationFailure(
            "CONNECT reported errors: "
            f"STEP_A.errors={step_a['errors']} STEP_B.errors={step_b['errors']}"
        )
    return ConnectReport(step_a=step_a, step_b=step_b)
```

Add the remaining functions below `parse_report`. The persistence body selects the latest matching ID before updating:

```python
async def _mark_latest_connect_partial(
    session_factory: async_sessionmaker[AsyncSession],
    run_date: dt.date,
    error_message: str,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            run_id = (
                await session.execute(
                    sa.select(dream_runs.c.id)
                    .where(dream_runs.c.phase == "connect")
                    .where(dream_runs.c.run_date == run_date)
                    .order_by(dream_runs.c.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if run_id is None:
                return False
            await session.execute(
                sa.update(dream_runs)
                .where(dream_runs.c.id == run_id)
                .values(status="partial", error_message=error_message[:_MAX_ERROR_LENGTH])
            )
    return True
```

```python
def _build_factory(postgres_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-log", required=True)
    parser.add_argument("--run-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args(argv)

    raw = Path(args.report_log).read_text(encoding="utf-8")
    session_factory = _build_factory(Settings().postgres_url)
    try:
        report = parse_report(raw)
    except ValidationFailure as exc:
        marked = asyncio.run(
            _mark_latest_connect_partial(session_factory, args.run_date, str(exc))
        )
        detail = str(exc)
        if not marked:
            detail += "; no CONNECT dream_runs row for run date"
        print(f"CONNECT VALIDATION FAILED: {detail}", file=sys.stderr)
        return 1

    print(
        "CONNECT VALIDATE: OK — "
        f"STEP_A.errors={report.step_a['errors']} "
        f"STEP_B.errors={report.step_b['errors']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run validator tests and confirm GREEN**

Run:

```bash
uv run pytest tests/unit/test_connect_validate.py -v
```

Expected: pure contract tests PASS; DB persistence tests PASS with the dedicated test database or report a guarded SKIP.

- [ ] **Step 5: Commit the validator slice**

```bash
git add scripts/dream/connect_validate.py tests/unit/test_connect_validate.py
git commit -m "🐛 fix: validate CONNECT phase reports"
```

### Task 4: Wire validator outcome into Dream and run regressions

**Files:**

- Modify: `scripts/dream.sh:511-513`
- Create: `tests/unit/test_dream_sh_connect_validator.py`

**Interfaces:**

- Consumes: Task 3 CLI and the final `logs/dream/<date>_connect.log` written by `run_phase`.
- Produces: `phase_rc=1` on validator failure, which appends `connect` to `FAILED_PHASES` and makes the final Dream process exit non-zero.

- [ ] **Step 1: Add a failing static orchestration contract**

Create `tests/unit/test_dream_sh_connect_validator.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _connect_validator_block() -> str:
    content = DREAM_SH.read_text(encoding="utf-8")
    start = content.index("# --- CONNECT: post-phase validator")
    end = content.index("# --- PROMOTE: post-phase validator")
    return content[start:end]


def test_connect_validator_runs_after_retry_and_propagates_failure() -> None:
    block = _connect_validator_block()
    assert '"$name" == "connect" && "$phase_rc" == "0"' in block
    assert "scripts.dream.connect_validate" in block
    assert '--report-log "$LOG_DIR/${TIMESTAMP}_${name}.log"' in block
    assert '--run-date "$TIMESTAMP"' in block
    assert "validator_rc=$?" in block
    assert "phase_rc=1" in block


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)
```

- [ ] **Step 2: Run the shell contract and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_dream_sh_connect_validator.py -v
```

Expected: FAIL because the CONNECT validator block does not exist.

- [ ] **Step 3: Add the post-retry CONNECT validator block**

Insert this block after `set -e` at the end of the retry section and before the PROMOTE validator:

```bash
  # --- CONNECT: post-phase validator ------------------------------------
  # A zero agent exit is insufficient: the exact report must also contain
  # zero tool-level errors. Validation runs after the final retry so only the
  # retained report determines the phase outcome.
  if [[ "$name" == "connect" && "$phase_rc" == "0" ]]; then
    set +e
    uv run python -m scripts.dream.connect_validate \
      --report-log "$LOG_DIR/${TIMESTAMP}_${name}.log" \
      --run-date "$TIMESTAMP" \
      >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
    validator_rc=$?
    set -e
    if (( validator_rc != 0 )); then
      log "FAIL connect — validator rejected CONNECT report; see validation detail"
      phase_rc=1
    fi
  fi
```

- [ ] **Step 4: Run focused and existing Dream contracts**

Run:

```bash
uv run pytest \
  tests/unit/test_connect_validate.py \
  tests/unit/test_dream_sh_connect_validator.py \
  tests/unit/test_dream_sh_agent_provider.py \
  tests/unit/test_dream_sh_phase_timeouts.py \
  -v
bash tests/integration/test_dream_sh_fail_propagation.sh
bash -n scripts/dream.sh
```

Expected: all commands exit `0`. The fail-propagation harness remains green because its mocked `uv` validator exits `0`; the new focused contract pins the real CONNECT invocation and `phase_rc` translation.

- [ ] **Step 5: Run all relevant Python regression and quality gates**

Run:

```bash
uv run pytest \
  tests/unit/repositories/test_pg_graph_ledger.py \
  tests/unit/services/test_durable_graph_service.py \
  tests/unit/test_brain_list_orphans_for_classification.py \
  tests/unit/mcp/tools/test_project_scoped_graph_tools.py \
  tests/unit/test_connect_validate.py \
  tests/unit/test_dream_sh_connect_validator.py \
  -v
uv run pytest tests/integration/db/test_graph_classification_orphans.py -v
uv run ruff check \
  src/brain_v42/repositories/pg_graph_ledger.py \
  src/brain_v42/services/durable_graph_service.py \
  scripts/dream/connect_validate.py \
  tests/unit/repositories/test_pg_graph_ledger.py \
  tests/unit/services/test_durable_graph_service.py \
  tests/unit/test_connect_validate.py \
  tests/unit/test_dream_sh_connect_validator.py \
  tests/integration/db/test_graph_classification_orphans.py
uv run mypy \
  src/brain_v42/repositories/pg_graph_ledger.py \
  src/brain_v42/services/durable_graph_service.py \
  scripts/dream/connect_validate.py
git diff --check
```

Expected: every available gate exits `0`; the dedicated PostgreSQL test may only skip for the explicit safe-test-DB guard.

- [ ] **Step 6: Run the mandatory GitNexus pre-commit scope check**

Run `gitnexus_detect_changes(scope="all")`. Confirm the changed symbols are limited to the new ledger reader, durable read facade, validator, tests, and shell wiring. Stop and investigate if `link_entity_to_domain` or unrelated execution flows appear as changed symbols.

- [ ] **Step 7: Commit the wiring and plan completion**

```bash
git add scripts/dream.sh tests/unit/test_dream_sh_connect_validator.py \
  docs/superpowers/plans/2026-07-25-connect-canonical-orphans.md
git commit -m "🐛 fix: fail Dream on CONNECT report errors"
```

- [ ] **Step 8: Verify final main state without pushing**

Run:

```bash
git status --short --branch
git log --oneline --decorate -5
```

Expected: clean `main`, ahead of `origin/main` by the local CONNECT commits, with no push performed.
