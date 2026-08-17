# brain_session_start Enrichment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans-parallel` to dispatch tasks in batches via TeamCreate.

**Goal:** Reshape `brain_session_start` from a focus + recap dump into an action-forward briefing (killswitches → last-failure → in-flight → stale-pinned → focus → blockers → recap), backed by `dream_runs.phase_dry_run` per-phase soak progress and a 30d feature staleness threshold.

**Test command:** `uv run pytest tests/ -v`

**Tech Stack:** Python 3.12, FastMCP 3.1, SQLAlchemy 2.0 async (core), asyncpg, Alembic, Pydantic 2, structlog, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-14-brain-session-start-enrichment-design.md`

---

## Batch 1: Foundations (parallel)

Two independent foundations: the schema column (T1.1) and the feature query service (T1.2). T1.1 lives in alembic + `db/tables.py`. T1.2 lives in a new `services/feature_service.py` + new test file. Disjoint files, disjoint test surfaces, disjoint DB tables (T1.1 mutates `dream_runs`, T1.2 reads `features`).

### Task 1.1: Migration 022 — add `dream_runs.phase_dry_run`

**Files:**
- Create: `alembic/versions/022_dream_runs_phase_dry_run.py`
- Modify: `src/brain_v42/db/tables.py` (insert column in `dream_runs` table definition, around line 640 before `error_message`)

- [ ] **Step 1: Write the failing test for `db.tables` column presence**

Append to `tests/unit/test_alembic_env.py` (or create `tests/unit/test_dream_runs_phase_dry_run_column.py` if alembic_env tests look unrelated):

```python
def test_dream_runs_has_phase_dry_run_column():
    from brain_v42.db.tables import dream_runs

    assert "phase_dry_run" in dream_runs.c
    col = dream_runs.c.phase_dry_run
    assert str(col.type) == "BOOLEAN"
    assert col.nullable is False
    assert col.server_default is not None
```

- [ ] **Step 2: Run the test, expect FAIL**

```bash
uv run pytest tests/unit/test_dream_runs_phase_dry_run_column.py -v
```

Expect: `AssertionError` (column not present).

- [ ] **Step 3: Add column to `db/tables.py`**

In `src/brain_v42/db/tables.py`, locate the `dream_runs` Table definition (~line 624). Insert the new column **after** `Column("error_message", sa.Text, nullable=True)`:

```python
Column(
    "phase_dry_run",
    sa.Boolean,
    nullable=False,
    server_default=sa.text("false"),
),
```

- [ ] **Step 4: Re-run test, expect PASS**

```bash
uv run pytest tests/unit/test_dream_runs_phase_dry_run_column.py -v
```

- [ ] **Step 5: Create the alembic migration file**

Create `alembic/versions/022_dream_runs_phase_dry_run.py`:

```python
"""Add phase_dry_run BOOLEAN to dream_runs.

Supports per-phase DRY/WET classification needed by the session-start
killswitch briefing. Each dream phase now records whether it ran dry
or wet, decoupled from the global DRY_RUN flag (a REORG sub-flag
already exists; this generalises the model).

Default false: legacy rows are backfilled as WET, which matches the
historical reality where REORG was disabled and PROMOTE ran WET.

Revision ID: 022
Revises: 021
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dream_runs",
        sa.Column(
            "phase_dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dream_runs", "phase_dry_run")
```

- [ ] **Step 6: Run the migration round-trip test (if present in repo)**

```bash
uv run pytest tests/unit/test_alembic_env.py -v
```

Expect: PASS (the env test should auto-discover 022 once the file is named correctly).

- [ ] **Step 7: Smoke the upgrade against a disposable DB**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expect: each command exits 0. `\d dream_runs` in psql should show the column after the final `upgrade head`.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/022_dream_runs_phase_dry_run.py \
        src/brain_v42/db/tables.py \
        tests/unit/test_dream_runs_phase_dry_run_column.py
git commit -m "$(cat <<'EOF'
feat(schema): add dream_runs.phase_dry_run BOOLEAN (migration 022)

Per-phase DRY/WET classification, decoupled from the global DRY_RUN
flag. Enables the session-start killswitch briefing to report
"PROMOTE wet · REORG dry · N clean DRY nights" with phase-level
granularity. Default false backfills legacy rows as WET, matching
historical behaviour.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2: `FeatureService.in_flight` + `stale_pinned` (new service)

**Files:**
- Create: `src/brain_v42/services/feature_service.py`
- Create: `tests/unit/services/test_feature_service.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_feature_service.py`:

```python
"""Tests for FeatureService (in_flight + stale_pinned queries)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.db.tables import METADATA, features
from brain_v42.services.feature_service import FeatureService


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_feature(
    factory, *, project_key: str, name: str, status: str,
    pinned: bool = False, updated_at: datetime | None = None,
):
    now = datetime.now(tz=UTC)
    async with factory() as session:
        await session.execute(
            sa.insert(features).values(
                id=uuid4(),
                project_key=project_key,
                name=name,
                description="",
                status=status,
                status_updated_at=now,
                pinned=pinned,
                created_at=now,
                updated_at=updated_at or now,
            )
        )
        await session.commit()


class TestInFlight:
    @pytest.mark.asyncio
    async def test_returns_in_progress_features(self, session_factory):
        await _insert_feature(session_factory, project_key="p", name="A", status="in_progress")
        await _insert_feature(session_factory, project_key="p", name="B", status="planned")
        await _insert_feature(session_factory, project_key="p", name="C", status="done")

        svc = FeatureService(session_factory)
        result = await svc.in_flight(project_key="p")

        names = [f.name for f in result]
        assert "A" in names
        assert "B" in names
        assert "C" not in names

    @pytest.mark.asyncio
    async def test_filters_by_project_key(self, session_factory):
        await _insert_feature(session_factory, project_key="p1", name="X", status="in_progress")
        await _insert_feature(session_factory, project_key="p2", name="Y", status="in_progress")

        svc = FeatureService(session_factory)
        result = await svc.in_flight(project_key="p1")

        assert [f.name for f in result] == ["X"]

    @pytest.mark.asyncio
    async def test_orders_by_updated_at_desc(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=5)
        new = datetime.now(tz=UTC)
        await _insert_feature(session_factory, project_key="p", name="old", status="planned", updated_at=old)
        await _insert_feature(session_factory, project_key="p", name="new", status="planned", updated_at=new)

        svc = FeatureService(session_factory)
        result = await svc.in_flight(project_key="p")
        assert [f.name for f in result] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_enforces_limit(self, session_factory):
        for i in range(7):
            await _insert_feature(session_factory, project_key="p", name=f"F{i}", status="planned")

        svc = FeatureService(session_factory)
        result = await svc.in_flight(project_key="p", limit=5)
        assert len(result) == 5


class TestStalePinned:
    @pytest.mark.asyncio
    async def test_returns_pinned_older_than_threshold(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        recent = datetime.now(tz=UTC) - timedelta(days=5)
        await _insert_feature(session_factory, project_key="p", name="stale", status="in_progress",
                              pinned=True, updated_at=old)
        await _insert_feature(session_factory, project_key="p", name="fresh", status="in_progress",
                              pinned=True, updated_at=recent)

        svc = FeatureService(session_factory)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert [f.name for f in result] == ["stale"]

    @pytest.mark.asyncio
    async def test_excludes_unpinned(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        await _insert_feature(session_factory, project_key="p", name="unpinned", status="in_progress",
                              pinned=False, updated_at=old)

        svc = FeatureService(session_factory)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert result == []

    @pytest.mark.asyncio
    async def test_orders_oldest_first(self, session_factory):
        d50 = datetime.now(tz=UTC) - timedelta(days=50)
        d40 = datetime.now(tz=UTC) - timedelta(days=40)
        await _insert_feature(session_factory, project_key="p", name="d40", status="planned",
                              pinned=True, updated_at=d40)
        await _insert_feature(session_factory, project_key="p", name="d50", status="planned",
                              pinned=True, updated_at=d50)

        svc = FeatureService(session_factory)
        result = await svc.stale_pinned(project_key="p", stale_days=30)
        assert [f.name for f in result] == ["d50", "d40"]

    @pytest.mark.asyncio
    async def test_filters_by_project_key(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=45)
        await _insert_feature(session_factory, project_key="p1", name="X", status="planned",
                              pinned=True, updated_at=old)
        await _insert_feature(session_factory, project_key="p2", name="Y", status="planned",
                              pinned=True, updated_at=old)

        svc = FeatureService(session_factory)
        result = await svc.stale_pinned(project_key="p1", stale_days=30)
        assert [f.name for f in result] == ["X"]
```

- [ ] **Step 2: Run the test, expect FAIL**

```bash
uv run pytest tests/unit/services/test_feature_service.py -v
```

Expect: `ModuleNotFoundError: brain_v42.services.feature_service`.

- [ ] **Step 3: Implement the service**

Create `src/brain_v42/services/feature_service.py`:

```python
"""FeatureService — read-only queries for session briefing.

Two thin async queries used by brain_session_start:
- in_flight: features with status in {in_progress, planned}
- stale_pinned: pinned features whose updated_at is older than N days

Both filter by project_key and respect a small LIMIT to keep briefing
cheap. No write paths here — feature mutations live in dedicated
project_context / dream paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.db.tables import features
from brain_v42.models.feature import Feature

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FeatureService:
    """Briefing-side read queries for features."""

    # Convention: `self._sf` mirrors roadmap_service.py.
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def in_flight(self, project_key: str, limit: int = 5) -> list[Feature]:
        """Return features that are actively being worked, newest activity first."""
        stmt = (
            sa.select(features)
            .where(features.c.project_key == project_key)
            .where(features.c.status.in_(("in_progress", "planned")))
            .order_by(features.c.updated_at.desc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Feature.model_validate(dict(r)) for r in rows]

    async def stale_pinned(
        self,
        project_key: str,
        stale_days: int = 30,
        limit: int = 5,
    ) -> list[Feature]:
        """Return pinned features whose updated_at is older than stale_days."""
        cutoff = datetime.now(tz=UTC) - timedelta(days=stale_days)
        stmt = (
            sa.select(features)
            .where(features.c.project_key == project_key)
            .where(features.c.pinned.is_(True))
            .where(features.c.updated_at < cutoff)
            .order_by(features.c.updated_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Feature.model_validate(dict(r)) for r in rows]
```

- [ ] **Step 4: Re-run tests, expect PASS**

```bash
uv run pytest tests/unit/services/test_feature_service.py -v
```

Expect: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/feature_service.py \
        tests/unit/services/test_feature_service.py
git commit -m "$(cat <<'EOF'
feat(services): add FeatureService.in_flight + stale_pinned

Read-only briefing helpers used by the new session_start enrichment.
in_flight returns active work (status in_progress|planned) sorted by
recent activity; stale_pinned returns pinned features untouched for
N days (default 30). Both project-scoped, both LIMITed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 2: Dream pipeline integration (parallel)

T2.1 wires `phase_dry_run` from `dream.sh` through `dream_parser.py` into the new column. T2.2 builds the read-side `DreamRunService` that derives killswitch state and last-failure. Both depend on Batch 1 (the column exists). They touch disjoint files (`metrics/dream_parser.py` + `scripts/dream.sh` vs `services/dream_run_service.py`) and disjoint test files (`tests/unit/metrics/test_dream_parser_phase_dry_run.py` vs `tests/unit/services/test_dream_run_service.py`).

### Task 2.1: dream_parser — accept and persist `--phase-dry-run`

**Files:**
- Modify: `src/brain_v42/metrics/dream_parser.py` (CLI arg + INSERT shape)
- Modify: `scripts/dream.sh` (pass `--phase-dry-run "$effective_dry_run"` to dream_parser)
- Create: `tests/unit/metrics/test_dream_parser_phase_dry_run.py`

- [ ] **Step 1: Write the failing CLI-arg test**

Create `tests/unit/metrics/test_dream_parser_phase_dry_run.py`:

```python
"""Verify dream_parser accepts --phase-dry-run and forwards it to the INSERT."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.dream_parser import _build_arg_parser, _insert_dream_run


def test_phase_dry_run_arg_parses_true():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--phase", "reorg", "--model", "sonnet", "--date", "2026-05-14",
         "--status", "done", "--duration", "10", "--phase-dry-run", "true", "log.txt"]
    )
    assert args.phase_dry_run is True


def test_phase_dry_run_arg_parses_false():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--phase", "promote", "--model", "sonnet", "--date", "2026-05-14",
         "--status", "done", "--duration", "10", "--phase-dry-run", "false", "log.txt"]
    )
    assert args.phase_dry_run is False


def test_phase_dry_run_defaults_to_false_when_omitted():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--phase", "scan", "--model", "sonnet", "--date", "2026-05-14",
         "--status", "done", "--duration", "10", "log.txt"]
    )
    assert args.phase_dry_run is False


@pytest.mark.asyncio
async def test_insert_dream_run_passes_phase_dry_run_to_asyncpg():
    """The persistence helper forwards phase_dry_run as the new $14 bind param.

    The production code uses raw asyncpg (asyncpg.connect(dsn)) with raw SQL —
    we mock asyncpg.connect to capture the INSERT call and assert the column
    list and VALUES tuple include phase_dry_run.
    """
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch("brain_v42.metrics.dream_parser.asyncpg.connect",
               new=AsyncMock(return_value=mock_conn)):
        await _insert_dream_run(
            dsn="postgresql://fake",
            phase="reorg",
            model="sonnet",
            run_date="2026-05-14",
            status="done",
            duration_s=12.0,
            telemetry=None,
            error_message=None,
            phase_dry_run=True,
        )

    assert mock_conn.execute.await_count == 1
    sql, *bind_args = mock_conn.execute.await_args.args
    # Column list must include phase_dry_run; VALUES tuple must reach $14
    assert "phase_dry_run" in sql
    assert "$14" in sql
    # The phase_dry_run value should be the last positional bind argument
    assert bind_args[-1] is True
```

- [ ] **Step 2: Run the test, expect FAIL**

```bash
uv run pytest tests/unit/metrics/test_dream_parser_phase_dry_run.py -v
```

Expect: failures because `_build_arg_parser` may not exist and `_insert_dream_run` does not yet accept `phase_dry_run`.

- [ ] **Step 3: Refactor `dream_parser.py` to expose `_build_arg_parser` and accept phase_dry_run**

Inspect `src/brain_v42/metrics/dream_parser.py`. **The production code uses raw asyncpg (`asyncpg.connect(dsn)`) with raw SQL — do NOT rewrite it to SQLAlchemy.** Keep the existing asyncpg path; just add the new column to the column list, the VALUES placeholder list, and the bind tuple.

If the argparse code is inline in `main()`, extract it into a `_build_arg_parser() -> argparse.ArgumentParser` helper that `main()` calls. Add the new argument:

```python
def _str_to_bool(v: str) -> bool:
    return v.lower() in ("true", "1", "yes")

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(...)
    # existing args ...
    parser.add_argument(
        "--phase-dry-run",
        type=_str_to_bool,
        default=False,
        help="Whether this phase ran in dry-run mode (default: false).",
    )
    parser.add_argument("log_file")
    return parser
```

Locate the existing `_insert_dream_run` function. Add the new parameter `phase_dry_run: bool = False` and weave it into the existing raw SQL INSERT — column list AND VALUES tuple AND the bind list, as the new `$14`:

```python
async def _insert_dream_run(
    *,
    dsn: str,
    phase: str,
    model: str,
    run_date: str,
    status: str,
    duration_s: float,
    telemetry: PhaseTelemetry | None,
    error_message: str | None,
    phase_dry_run: bool = False,
) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        # Add `phase_dry_run` to the trailing position of the column list and
        # bump the VALUES placeholder list to $14. Keep the existing 13 columns
        # in place; do NOT reorder existing bind args.
        await conn.execute(
            """
            INSERT INTO dream_runs (
              run_date, phase, model, status, duration_s,
              input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
              cost_usd, api_calls, tool_calls, error_message, phase_dry_run
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """,
            run_date, phase, model, status, duration_s,
            telemetry.input_tokens if telemetry else None,
            telemetry.output_tokens if telemetry else None,
            telemetry.cache_read_tokens if telemetry else None,
            telemetry.cache_creation_tokens if telemetry else None,
            telemetry.cost_usd if telemetry else None,
            telemetry.api_calls if telemetry else None,
            telemetry.tool_calls if telemetry else None,
            error_message,
            phase_dry_run,
        )
    finally:
        await conn.close()
```

Adjust the snippet above to match the actual current column ordering in `_insert_dream_run` — only the trailing `phase_dry_run` ($14) is new. Update `main()` (or equivalent) to pass `args.phase_dry_run` through.

- [ ] **Step 4: Re-run test, expect PASS**

```bash
uv run pytest tests/unit/metrics/test_dream_parser_phase_dry_run.py -v
```

- [ ] **Step 5: Wire `dream.sh` to forward `effective_dry_run` to the parser**

In `scripts/dream.sh`, locate where `parser_args` is built (around line 203):

```bash
local parser_args=(--phase "$name" --model "$model" --date "$TIMESTAMP"
                   ...)
```

Append:

```bash
parser_args+=(--phase-dry-run "$effective_dry_run")
```

(`effective_dry_run` already exists in the function — line 99 — and is correctly computed as the REORG sub-flag override when applicable.)

- [ ] **Step 6: Smoke `dream.sh` parses without error**

```bash
bash -n scripts/dream.sh && echo "syntax OK"
```

Expect: `syntax OK`.

- [ ] **Step 7: Run the metrics test suite to ensure no regression**

```bash
uv run pytest tests/unit/metrics -v
```

- [ ] **Step 8: Commit**

```bash
git add src/brain_v42/metrics/dream_parser.py \
        scripts/dream.sh \
        tests/unit/metrics/test_dream_parser_phase_dry_run.py
git commit -m "$(cat <<'EOF'
feat(dream): persist per-phase dry_run flag in dream_runs

dream_parser accepts --phase-dry-run and forwards it into the
dream_runs INSERT. dream.sh feeds it the already-computed
effective_dry_run, so REORG sub-flag soaks are recorded with phase
granularity while PROMOTE keeps its global DRY_RUN. Unblocks the
session_start killswitch briefing's DRY/WET classification.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.2: `DreamRunService` — killswitch_state + last_failure

**Files:**
- Create: `src/brain_v42/services/dream_run_service.py`
- Create: `tests/unit/services/test_dream_run_service.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_dream_run_service.py`:

```python
"""Tests for DreamRunService — killswitch_state + last_failure."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.db.tables import METADATA, dream_runs
from brain_v42.services.dream_run_service import DreamRunService


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_run(factory, *, run_date, phase, status="done",
                      phase_dry_run=False, created_at=None):
    cat = created_at or datetime.now(tz=UTC)
    async with factory() as session:
        await session.execute(
            sa.insert(dream_runs).values(
                run_date=run_date,
                phase=phase,
                model="sonnet",
                status=status,
                duration_s=10.0,
                phase_dry_run=phase_dry_run,
                created_at=cat,
            )
        )
        await session.commit()


class TestKillswitchState:
    @pytest.mark.asyncio
    async def test_promote_wet_reorg_dry(self, session_factory):
        today = date.today()
        await _insert_run(session_factory, run_date=today, phase="promote", phase_dry_run=False)
        await _insert_run(session_factory, run_date=today, phase="reorg", phase_dry_run=True)

        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()

        assert state.promote_enabled is True
        assert state.promote_dry is False
        assert state.reorg_enabled is True
        assert state.reorg_dry is True
        assert state.last_run_date == today

    @pytest.mark.asyncio
    async def test_no_activity_in_7d(self, session_factory):
        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()
        assert state.last_run_date is None
        assert state.promote_enabled is False
        assert state.reorg_enabled is False

    @pytest.mark.asyncio
    async def test_clean_dry_nights_counter_increments(self, session_factory):
        for i in range(3):
            d = date.today() - timedelta(days=i)
            await _insert_run(session_factory, run_date=d, phase="reorg",
                              status="done", phase_dry_run=True)

        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()
        assert state.reorg_clean_dry_nights == 3

    @pytest.mark.asyncio
    async def test_failure_resets_counter(self, session_factory):
        # dream.sh emits done|timeout|fail (NOT 'failed'); seed with 'fail'.
        await _insert_run(session_factory, run_date=date.today() - timedelta(days=3),
                          phase="reorg", status="fail", phase_dry_run=True)
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(session_factory, run_date=d, phase="reorg",
                              status="done", phase_dry_run=True)

        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()
        assert state.reorg_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_wet_run_resets_counter(self, session_factory):
        await _insert_run(session_factory, run_date=date.today() - timedelta(days=3),
                          phase="reorg", status="done", phase_dry_run=False)
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(session_factory, run_date=d, phase="reorg",
                              status="done", phase_dry_run=True)

        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()
        assert state.reorg_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_promote_ran_reorg_did_not(self, session_factory):
        """One phase rows exist for last_run_date; the other doesn't.

        Guards against KeyError when phases.get('reorg') returns None.
        """
        today = date.today()
        await _insert_run(session_factory, run_date=today, phase="promote",
                          phase_dry_run=False)

        svc = DreamRunService(session_factory)
        state = await svc.killswitch_state()
        assert state.promote_enabled is True
        assert state.reorg_enabled is False
        assert state.reorg_dry is False
        assert state.reorg_clean_dry_nights == 0


class TestLastFailure:
    @pytest.mark.asyncio
    async def test_returns_most_recent_failure(self, session_factory):
        old_failure = datetime.now(tz=UTC) - timedelta(days=2)
        new_failure = datetime.now(tz=UTC) - timedelta(hours=4)
        await _insert_run(session_factory, run_date=date.today() - timedelta(days=2),
                          phase="promote", status="fail", created_at=old_failure)
        await _insert_run(session_factory, run_date=date.today(),
                          phase="reorg", status="fail", created_at=new_failure)

        svc = DreamRunService(session_factory)
        result = await svc.last_failure()
        assert result is not None
        assert result.phase == "reorg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_failures(self, session_factory):
        await _insert_run(session_factory, run_date=date.today(),
                          phase="promote", status="done")
        svc = DreamRunService(session_factory)
        assert await svc.last_failure() is None

    @pytest.mark.asyncio
    async def test_filters_outside_window(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=14)
        await _insert_run(session_factory, run_date=date.today() - timedelta(days=14),
                          phase="reorg", status="fail", created_at=old)
        svc = DreamRunService(session_factory)
        result = await svc.last_failure(within_days=7)
        assert result is None
```

- [ ] **Step 2: Run the test, expect FAIL**

```bash
uv run pytest tests/unit/services/test_dream_run_service.py -v
```

Expect: `ModuleNotFoundError: brain_v42.services.dream_run_service`.

- [ ] **Step 3: Implement the service**

Create `src/brain_v42/services/dream_run_service.py`:

```python
"""DreamRunService — read-only briefing helpers over dream_runs.

Two queries that power the session-start killswitch and last-failure
sections:
- killswitch_state(): derives PROMOTE/REORG enabled+DRY/WET +
  consecutive-clean-DRY counter from the latest run_date and the
  history that follows the most recent reset event (failure or WET run).
- last_failure(within_days): the most recent failed phase row within
  a sliding window (default 7d), or None.

No mutations. No graph. Pure SQL via session_factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.db.tables import dream_runs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class KillswitchState:
    last_run_date: date | None
    promote_enabled: bool
    promote_dry: bool
    reorg_enabled: bool
    reorg_dry: bool
    promote_clean_dry_nights: int
    reorg_clean_dry_nights: int


@dataclass(frozen=True)
class LastFailureRow:
    phase: str
    run_date: date
    error_message: str | None


class DreamRunService:
    # Convention: `self._sf` mirrors roadmap_service.py.
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def killswitch_state(self, within_days: int = 7) -> KillswitchState:
        cutoff = date.today() - timedelta(days=within_days)
        async with self._sf() as session:
            last_date_row = (
                await session.execute(
                    sa.select(sa.func.max(dream_runs.c.run_date))
                    .where(dream_runs.c.run_date >= cutoff)
                )
            ).scalar()
            if last_date_row is None:
                return KillswitchState(
                    last_run_date=None,
                    promote_enabled=False, promote_dry=False,
                    reorg_enabled=False, reorg_dry=False,
                    promote_clean_dry_nights=0, reorg_clean_dry_nights=0,
                )
            last_date = last_date_row

            rows = (
                await session.execute(
                    sa.select(dream_runs.c.phase, dream_runs.c.phase_dry_run)
                    .where(dream_runs.c.run_date == last_date)
                )
            ).mappings().all()
            phases = {r["phase"]: r for r in rows}
            promote_enabled = "promote" in phases
            reorg_enabled = "reorg" in phases
            promote_dry = bool(phases["promote"]["phase_dry_run"]) if promote_enabled else False
            reorg_dry = bool(phases["reorg"]["phase_dry_run"]) if reorg_enabled else False

            promote_streak = await self._clean_dry_streak(session, "promote")
            reorg_streak = await self._clean_dry_streak(session, "reorg")

        return KillswitchState(
            last_run_date=last_date,
            promote_enabled=promote_enabled, promote_dry=promote_dry,
            reorg_enabled=reorg_enabled, reorg_dry=reorg_dry,
            promote_clean_dry_nights=promote_streak,
            reorg_clean_dry_nights=reorg_streak,
        )

    async def _clean_dry_streak(self, session: AsyncSession, phase: str) -> int:
        # dream.sh emits done|timeout|fail; anything != 'done' = failure.
        reset_date = (
            await session.execute(
                sa.select(sa.func.max(dream_runs.c.run_date))
                .where(dream_runs.c.phase == phase)
                .where(
                    sa.or_(
                        dream_runs.c.status != "done",
                        dream_runs.c.phase_dry_run.is_(False),
                    )
                )
            )
        ).scalar()
        stmt = (
            sa.select(sa.func.count())
            .select_from(dream_runs)
            .where(dream_runs.c.phase == phase)
            .where(dream_runs.c.status == "done")
            .where(dream_runs.c.phase_dry_run.is_(True))
        )
        if reset_date is not None:
            stmt = stmt.where(dream_runs.c.run_date > reset_date)
        return int((await session.execute(stmt)).scalar() or 0)

    async def last_failure(self, within_days: int = 7) -> LastFailureRow | None:
        # dream.sh emits done|timeout|fail; anything != 'done' = failure.
        cutoff = datetime.now(tz=UTC) - timedelta(days=within_days)
        stmt = (
            sa.select(dream_runs)
            .where(dream_runs.c.status != "done")
            .where(dream_runs.c.created_at >= cutoff)
            .order_by(dream_runs.c.created_at.desc())
            .limit(1)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).mappings().first()
        if row is None:
            return None
        return LastFailureRow(
            phase=row["phase"],
            run_date=row["run_date"],
            error_message=row["error_message"],
        )
```

- [ ] **Step 4: Re-run tests, expect PASS**

```bash
uv run pytest tests/unit/services/test_dream_run_service.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/dream_run_service.py \
        tests/unit/services/test_dream_run_service.py
git commit -m "$(cat <<'EOF'
feat(services): add DreamRunService.killswitch_state + last_failure

Powers the session-start briefing's Killswitches and Last-failure
sections. killswitch_state derives PROMOTE/REORG enabled+DRY/WET +
consecutive-clean-DRY counters from dream_runs; counter resets on the
most recent failure or WET run. last_failure returns the most recent
failed phase row inside a sliding window (default 7d).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

--- checkpoint ---

## Batch 3: Briefing composer + tool wiring (sequential)

Refactors `_format_session_briefing` into a composer of small section helpers, extends `register_session_tools` to inject the two new services, and updates the MCP server factory to construct them. Single task because all changes live in `session_tools.py` (and its caller in the server factory) — touching the same file makes a single coherent commit.

### Task 3.1: Composer refactor + section helpers + service wiring

**Files:**
- Modify: `src/brain_v42/mcp/tools/session_tools.py` (composer + helpers + register signature)
- Modify: `tests/unit/mcp/test_session_tools.py` (extend with helper tests + tool registration update)
- Modify: `src/brain_v42/mcp/server.py` (or whichever factory wires `register_session_tools`; verify with `grep -rn register_session_tools src/`)

- [ ] **Step 1: Write the failing tests for section helpers**

Replace the contents of `tests/unit/mcp/test_session_tools.py` with the expanded suite. (Keep the two existing classes but add the new ones.) Append these test classes to the end of the file:

```python
# ---------------------------------------------------------------------------
# Section helper unit tests
# ---------------------------------------------------------------------------

from datetime import date
from brain_v42.mcp.tools.session_tools import (
    _section_killswitches, _section_last_failure, _section_in_flight,
    _section_stale_pinned, _section_focus, _section_blockers, _section_recap,
    _section_drill_in_hint,
)
from brain_v42.services.dream_run_service import KillswitchState, LastFailureRow


class TestSectionKillswitches:
    def test_renders_full_state(self):
        state = KillswitchState(
            last_run_date=date(2026, 5, 14),
            promote_enabled=True, promote_dry=False,
            reorg_enabled=True, reorg_dry=True,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=3,
        )
        out = _section_killswitches(state)
        assert "### Killswitches" in out
        assert "2026-05-14" in out
        assert "PROMOTE" in out and "wet" in out.lower()
        assert "REORG" in out and "dry" in out.lower()
        assert "3" in out

    def test_no_activity_renders_anchor(self):
        state = KillswitchState(
            last_run_date=None,
            promote_enabled=False, promote_dry=False,
            reorg_enabled=False, reorg_dry=False,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=0,
        )
        out = _section_killswitches(state)
        assert "no dream pipeline activity" in out.lower()

    def test_graph_row_reads_env_default_true(self, monkeypatch):
        monkeypatch.delenv("GRAPH_ENABLED", raising=False)
        state = KillswitchState(
            last_run_date=date(2026, 5, 14),
            promote_enabled=True, promote_dry=False,
            reorg_enabled=True, reorg_dry=True,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=3,
        )
        out = _section_killswitches(state)
        assert "GRAPH" in out
        assert "enabled" in out.split("GRAPH")[1].splitlines()[0]

    def test_graph_row_reads_env_false(self, monkeypatch):
        monkeypatch.setenv("GRAPH_ENABLED", "false")
        state = KillswitchState(
            last_run_date=date(2026, 5, 14),
            promote_enabled=True, promote_dry=False,
            reorg_enabled=True, reorg_dry=True,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=3,
        )
        out = _section_killswitches(state)
        assert "GRAPH" in out
        assert "disabled" in out.split("GRAPH")[1].splitlines()[0]


class TestSectionLastFailure:
    def test_renders_with_failure(self):
        failure = LastFailureRow(
            phase="reorg",
            run_date=date(2026, 5, 13),
            error_message="Boom\nstack...",
        )
        out = _section_last_failure(failure)
        assert "### Last failure" in out
        assert "reorg" in out
        assert "Boom" in out
        assert "stack" not in out  # only first line

    def test_omits_when_none(self):
        assert _section_last_failure(None) == ""


class TestSectionInFlight:
    def test_renders_list(self):
        from datetime import datetime, UTC
        f = type("F", (), {})()
        f.name = "feature-x"
        f.status = "in_progress"
        f.updated_at = datetime.now(tz=UTC)
        out = _section_in_flight([f])
        assert "### In-flight" in out
        assert "feature-x" in out
        assert "in_progress" in out

    def test_omits_when_empty(self):
        assert _section_in_flight([]) == ""

    def test_cap_5(self):
        from datetime import datetime, UTC
        items = [type("F", (), {"name": f"f{i}", "status": "planned",
                                "updated_at": datetime.now(tz=UTC)})() for i in range(7)]
        out = _section_in_flight(items)
        assert out.count("- ") == 5


class TestSectionStalePinned:
    def test_renders_list(self):
        from datetime import datetime, UTC, timedelta
        f = type("F", (), {})()
        f.name = "stale-x"
        f.updated_at = datetime.now(tz=UTC) - timedelta(days=40)
        out = _section_stale_pinned([f])
        assert "### Stale-pinned" in out
        assert "stale-x" in out

    def test_omits_when_empty(self):
        assert _section_stale_pinned([]) == ""

    def test_cap_5(self):
        from datetime import datetime, UTC, timedelta
        items = [type("F", (), {"name": f"f{i}",
                                "updated_at": datetime.now(tz=UTC) - timedelta(days=40)})()
                 for i in range(7)]
        out = _section_stale_pinned(items)
        assert out.count("- ") == 5


class TestSectionFocus:
    def test_renders_focus(self):
        ctx = type("C", (), {"current_focus": "ship the briefing"})()
        out = _section_focus(ctx)
        assert "### Focus" in out
        assert "ship the briefing" in out

    def test_anchor_when_no_focus(self):
        ctx = type("C", (), {"current_focus": None})()
        out = _section_focus(ctx)
        assert "### Focus" in out


class TestSectionBlockers:
    def test_renders_list(self):
        out = _section_blockers(["calibration pending", "GPU vmem drift"])
        assert "### Blockers" in out
        assert "calibration pending" in out
        assert "GPU vmem drift" in out

    def test_omits_when_empty(self):
        assert _section_blockers([]) == ""

    def test_cap_5(self):
        out = _section_blockers([f"b{i}" for i in range(7)])
        assert out.count("- ") == 5


class TestSectionRecap:
    def test_renders_three_of_each(self):
        decisions = [type("D", (), {"title": f"d{i}"})() for i in range(5)]
        learnings = [type("L", (), {"topic": f"t{i}",
                                    "insight": f"insight {i}" * 5})() for i in range(5)]
        out = _section_recap(decisions, learnings)
        assert "### Recap" in out
        # 3 + 3 entries with `d:` / `l:` prefixes
        assert out.count("\n- d:") == 3
        assert out.count("\n- l:") == 3

    def test_empty_returns_empty(self):
        assert _section_recap([], []) == ""


class TestSectionDrillInHint:
    def test_renders_hint(self):
        out = _section_drill_in_hint()
        assert "brain_search" in out or "brain_get" in out
```

- [ ] **Step 2: Write the failing test for the tool-level orchestration**

In the same file, replace `TestBrainSessionStartTool.test_calls_all_services` with one that asserts the new services are called:

```python
class TestBrainSessionStartTool:
    @pytest.mark.asyncio
    async def test_tool_registered(self):
        mcp = FastMCP("test")
        register_session_tools(mcp, AsyncMock(), AsyncMock(), AsyncMock(),
                               AsyncMock(), AsyncMock())
        tool = await mcp.get_tool("brain_session_start")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_calls_all_services(self):
        ctx = MagicMock(project_key="p", current_focus="f",
                       description="d", blockers=[])
        mock_ctx_svc = MagicMock(); mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock(); mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock(); mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(return_value=KillswitchState(
            last_run_date=None,
            promote_enabled=False, promote_dry=False,
            reorg_enabled=False, reorg_dry=False,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=0,
        ))
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        mock_feature_svc.in_flight = AsyncMock(return_value=[])
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])

        mcp = FastMCP("test")
        register_session_tools(mcp, mock_ctx_svc, mock_decision_svc,
                               mock_learning_svc, mock_dream_svc, mock_feature_svc)
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")

        mock_ctx_svc.get_by_key.assert_called_once_with("p")
        mock_dream_svc.killswitch_state.assert_called_once()
        mock_dream_svc.last_failure.assert_called_once()
        mock_feature_svc.in_flight.assert_called_once()
        mock_feature_svc.stale_pinned.assert_called_once()
        assert "### Killswitches" in result
        assert "### Focus" in result

    @pytest.mark.asyncio
    async def test_partial_failure_returns_degraded_briefing(self):
        """If any service call raises, gather(return_exceptions=True) absorbs
        the error, structlog warns, and the briefing still renders the rest."""
        ctx = MagicMock(project_key="p", current_focus="ship",
                        description="d", blockers=[])
        mock_ctx_svc = MagicMock(); mock_ctx_svc.get_by_key = AsyncMock(return_value=ctx)
        mock_decision_svc = MagicMock(); mock_decision_svc.list_all = AsyncMock(return_value=[])
        mock_learning_svc = MagicMock(); mock_learning_svc.list_all = AsyncMock(return_value=[])
        mock_dream_svc = MagicMock()
        mock_dream_svc.killswitch_state = AsyncMock(return_value=KillswitchState(
            last_run_date=None,
            promote_enabled=False, promote_dry=False,
            reorg_enabled=False, reorg_dry=False,
            promote_clean_dry_nights=0, reorg_clean_dry_nights=0,
        ))
        mock_dream_svc.last_failure = AsyncMock(return_value=None)
        mock_feature_svc = MagicMock()
        # Force in_flight to blow up — must NOT bubble up.
        mock_feature_svc.in_flight = AsyncMock(side_effect=RuntimeError("db down"))
        mock_feature_svc.stale_pinned = AsyncMock(return_value=[])

        mcp = FastMCP("test")
        register_session_tools(mcp, mock_ctx_svc, mock_decision_svc,
                               mock_learning_svc, mock_dream_svc, mock_feature_svc)
        tool = await mcp.get_tool("brain_session_start")
        result = await tool.fn(project_key="p")  # MUST NOT raise

        assert "### Killswitches" in result
        assert "### Focus" in result
        assert "ship" in result
```

- [ ] **Step 3: Run tests, expect FAIL**

```bash
uv run pytest tests/unit/mcp/test_session_tools.py -v
```

Expect: `ImportError` for the section helpers and `KillswitchState`, and the tool registration test fails on signature.

- [ ] **Step 4: Rewrite `session_tools.py` with composer + helpers**

Replace the contents of `src/brain_v42/mcp/tools/session_tools.py`:

```python
"""Session management tools for brain-v42 MCP — action-forward briefing."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from typing import Any

import structlog

from brain_v42.services.dream_run_service import (
    DreamRunService,
    KillswitchState,
    LastFailureRow,
)

logger = structlog.get_logger(__name__)

_CAP = 5


def _ds_relative(d: datetime | None) -> str:
    if d is None:
        return ""
    now = datetime.now(tz=UTC)
    days = (now - d).days
    if days <= 0:
        return "today"
    return f"{days}d ago"


def _section_header(ctx: Any | None) -> str:
    if ctx is None:
        return "## Session Briefing (no project context found)"
    return f"## {ctx.project_key} — Session Briefing"


def _section_killswitches(state: KillswitchState) -> str:
    if state.last_run_date is None:
        return "### Killswitches (no dream pipeline activity in 7d)"
    lines = [f"### Killswitches (as of {state.last_run_date.isoformat()})"]

    def _row(label: str, enabled: bool, dry: bool, streak: int) -> str:
        if not enabled:
            return f"- {label}: disabled"
        mode = "dry" if dry else "wet"
        streak_str = f" · {streak} clean DRY nights" if dry else ""
        return f"- {label}: enabled ({mode}{streak_str})"

    lines.append(_row("PROMOTE", state.promote_enabled, state.promote_dry,
                      state.promote_clean_dry_nights))
    lines.append(_row("REORG  ", state.reorg_enabled, state.reorg_dry,
                      state.reorg_clean_dry_nights))
    graph_enabled = os.getenv("GRAPH_ENABLED", "true").lower() == "true"
    lines.append(f"- GRAPH:   {'enabled' if graph_enabled else 'disabled'}")
    return "\n".join(lines)


def _section_last_failure(failure: LastFailureRow | None) -> str:
    if failure is None:
        return ""
    err = (failure.error_message or "(no message)").splitlines()[0]
    rd_str = failure.run_date.isoformat() if isinstance(failure.run_date, date) else str(failure.run_date)
    return (
        f"### Last failure\n"
        f"{failure.phase} on {rd_str} — {err}\n"
        f"→ drill in: brain_get(decision, …) or journalctl -u brain-v42-dream"
    )


def _section_in_flight(items: list[Any]) -> str:
    if not items:
        return ""
    lines = [f"### In-flight ({min(len(items), _CAP)})"]
    for f in items[:_CAP]:
        rel = _ds_relative(f.updated_at)
        lines.append(f"- {f.name} [{f.status}] — updated {rel}")
    return "\n".join(lines)


def _section_stale_pinned(items: list[Any]) -> str:
    if not items:
        return ""
    lines = [f"### Stale-pinned ({min(len(items), _CAP)})"]
    for f in items[:_CAP]:
        rel = _ds_relative(f.updated_at)
        # Spec §3.1 wording: "last access" (these are pinned features that
        # haven't been touched).
        lines.append(f"- {f.name} — pinned, last access {rel}")
    return "\n".join(lines)


def _section_focus(ctx: Any | None) -> str:
    if ctx is None or not ctx.current_focus:
        return "### Focus\n(no focus set)"
    return f"### Focus\n{ctx.current_focus}"


def _section_blockers(blockers: list[str] | None) -> str:
    if not blockers:
        return ""
    capped = blockers[:_CAP]
    lines = [f"### Blockers ({len(capped)})"]
    for b in capped:
        lines.append(f"- {b}")
    return "\n".join(lines)


def _section_recap(decisions: list[Any], learnings: list[Any]) -> str:
    if not decisions and not learnings:
        return ""
    lines = ["### Recap"]
    for d in decisions[:3]:
        lines.append(f"- d: {d.title}")
    for lr in learnings[:3]:
        snip = (lr.insight[:60] + "…") if len(lr.insight) > 60 else lr.insight
        lines.append(f"- l: {lr.topic}: {snip}")
    return "\n".join(lines)


def _section_drill_in_hint() -> str:
    return "→ More: brain_search · brain_get_roadmap · brain_list types=…"


def _format_session_briefing(
    ctx: Any | None,
    decisions: list[Any],
    learnings: list[Any],
    killswitches: KillswitchState,
    last_failure: LastFailureRow | None,
    in_flight: list[Any],
    stale_pinned: list[Any],
) -> str:
    blockers = list(getattr(ctx, "blockers", []) or []) if ctx else []
    sections = [
        _section_header(ctx),
        _section_killswitches(killswitches),
        _section_last_failure(last_failure),
        _section_in_flight(in_flight),
        _section_stale_pinned(stale_pinned),
        _section_focus(ctx),
        _section_blockers(blockers),
        _section_recap(decisions, learnings),
        _section_drill_in_hint(),
    ]
    return "\n\n".join(s for s in sections if s)


def register_session_tools(
    mcp: Any,
    project_context_svc: Any,
    decision_svc: Any,
    learning_svc: Any,
    dream_run_svc: Any,
    feature_svc: Any,
) -> None:
    """Register session management tools on the MCP server."""

    @mcp.tool(version="2.0")
    async def brain_session_start(project_key: str) -> str:
        """Action-forward project briefing in ~500-800 tokens.

        Returns: killswitches → last failure → in-flight features →
        stale pinned → focus → blockers → recent decisions/learnings.

        Graceful degrade (spec §9): if any service call raises, the
        offending section is dropped and a structlog warning is emitted —
        the briefing still renders the rest.
        """
        results = await asyncio.gather(
            project_context_svc.get_by_key(project_key),
            decision_svc.list_all(project_key=project_key, limit=3),
            learning_svc.list_all(project_key=project_key, limit=3),
            dream_run_svc.killswitch_state(),
            dream_run_svc.last_failure(within_days=7),
            feature_svc.in_flight(project_key=project_key, limit=5),
            feature_svc.stale_pinned(project_key=project_key, stale_days=30, limit=5),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("brain_session_start_partial_failure", error=str(r))
        ctx, decisions, learnings, killswitches, last_failure, in_flight, stale_pinned = [
            None if isinstance(r, Exception) else r for r in results
        ]
        # Killswitches must always render — fall back to no-activity anchor.
        if killswitches is None:
            killswitches = KillswitchState(
                last_run_date=None,
                promote_enabled=False, promote_dry=False,
                reorg_enabled=False, reorg_dry=False,
                promote_clean_dry_nights=0, reorg_clean_dry_nights=0,
            )
        # Coerce list defaults so the composer never sees None for collections.
        decisions = decisions or []
        learnings = learnings or []
        in_flight = in_flight or []
        stale_pinned = stale_pinned or []

        return _format_session_briefing(
            ctx, decisions, learnings,
            killswitches, last_failure, in_flight, stale_pinned,
        )
```

- [ ] **Step 5: Update the server factory to inject the two new services**

Run:

```bash
grep -rn "register_session_tools" src/ tests/
```

For each non-test call site, instantiate `DreamRunService(session_factory)` and `FeatureService(session_factory)` (both in the existing async startup block where other services are built) and pass them as the new last two positional args.

> **Pool sizing note:** The engine pool is `pool_size=5` with default `max_overflow=10` (db/engine.py:40). The 7 concurrent `asyncio.gather` calls above fit inside `pool_size + max_overflow = 15` — no contention expected for v1. If the gather count grows in a follow-up (e.g. cross-domain links from spec C), revisit the pool sizing then.

- [ ] **Step 6: Run all session tests, expect PASS**

```bash
uv run pytest tests/unit/mcp/test_session_tools.py -v
```

If older tests reference the old `_format_session_briefing` 3-arg signature, update them to pass empty `KillswitchState(no-activity)`, `last_failure=None`, `in_flight=[]`, `stale_pinned=[]`.

- [ ] **Step 7: Run the full unit suite, expect no regression**

```bash
uv run pytest tests/unit -v
```

- [ ] **Step 8: Commit**

```bash
git add src/brain_v42/mcp/tools/session_tools.py \
        tests/unit/mcp/test_session_tools.py \
        src/brain_v42/mcp/server.py
git commit -m "$(cat <<'EOF'
feat(session_start): action-forward briefing composer + new services

Rewrites _format_session_briefing as a composer of 8 small section
helpers (header, killswitches, last_failure, in_flight, stale_pinned,
focus, blockers, recap, drill_in_hint). brain_session_start now
parallel-gathers 7 service calls and renders the new layout:

  killswitches → last failure → in-flight → stale-pinned
  → focus → blockers → recap → drill-in hint

Blockers comes from ProjectContext.blockers (already loaded). Empty
sections are omitted except killswitches and focus (always-show
anchors). Tool version bumped to 2.0.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

> Spec §6.5 cap-aware drill-in hoist deferred per spec §10 out-of-scope (token-budget truncation engine).

--- checkpoint ---

## Batch 4: Integration golden snapshot + token budget (sequential)

End-to-end test that depends on every prior batch: real PG (or in-memory full stack), every section populated, briefing string compared against a golden file. Plus the token-budget backstop. Sequential by nature — single integration test file, single commit.

### Task 4.1: Integration golden snapshot + token budget regression

**Files:**
- Create: `tests/integration/test_session_start_briefing.py`
- Create: `tests/fixtures/briefing_full.md`
- Create: `tests/fixtures/briefing_empty.md`

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_session_start_briefing.py`:

```python
"""End-to-end golden snapshot for brain_session_start."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.db.tables import (
    METADATA, dream_runs, features, project_contexts, decisions, learnings,
)
from brain_v42.mcp.tools.session_tools import _format_session_briefing
from brain_v42.services.dream_run_service import DreamRunService
from brain_v42.services.feature_service import FeatureService

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _normalize(s: str) -> str:
    """Strip relative dates and last_run_date to make snapshots stable."""
    s = re.sub(r"\d+d ago", "Nd ago", s)
    s = re.sub(r"as of \d{4}-\d{2}-\d{2}", "as of YYYY-MM-DD", s)
    s = re.sub(r"on \d{4}-\d{2}-\d{2}", "on YYYY-MM-DD", s)
    return s


async def _seed_full(session_factory):
    """Shared full-seed helper — every section populated.

    Seeds 5 decisions + 5 learnings so the briefing exercises the 3+3 recap
    cap from spec §3.2 (only the most recent three of each should render).
    """
    async with session_factory() as session:
        ctx_id = uuid4()
        await session.execute(sa.insert(project_contexts).values(
            id=ctx_id, project_key="brain-v42", name="brain-v42",
            description="Second Cerveau", current_focus="ship the briefing",
            blockers=["calibration pending", "vmem drift"],
            created_at=datetime.now(tz=UTC), updated_at=datetime.now(tz=UTC),
        ))
        today = date.today()
        await session.execute(sa.insert(dream_runs).values(
            run_date=today, phase="promote", model="sonnet", status="done",
            duration_s=10.0, phase_dry_run=False, created_at=datetime.now(tz=UTC),
        ))
        await session.execute(sa.insert(dream_runs).values(
            run_date=today, phase="reorg", model="sonnet", status="done",
            duration_s=10.0, phase_dry_run=True, created_at=datetime.now(tz=UTC),
        ))
        await session.execute(sa.insert(features).values(
            id=uuid4(), project_key="brain-v42", name="enrich session start",
            description="", status="in_progress",
            status_updated_at=datetime.now(tz=UTC), pinned=False,
            created_at=datetime.now(tz=UTC), updated_at=datetime.now(tz=UTC),
        ))
        await session.execute(sa.insert(features).values(
            id=uuid4(), project_key="brain-v42", name="dream v3 spec b",
            description="", status="planned",
            status_updated_at=datetime.now(tz=UTC), pinned=True,
            created_at=datetime.now(tz=UTC) - timedelta(days=60),
            updated_at=datetime.now(tz=UTC) - timedelta(days=60),
        ))
        # 5 decisions + 5 learnings so the 3+3 recap cap (§3.2) is exercised.
        for i in range(5):
            await session.execute(sa.insert(decisions).values(
                id=uuid4(), project_key="brain-v42",
                title=f"decision {i}", rationale=f"because {i}",
                created_at=datetime.now(tz=UTC) - timedelta(hours=i),
                updated_at=datetime.now(tz=UTC) - timedelta(hours=i),
            ))
            await session.execute(sa.insert(learnings).values(
                id=uuid4(), project_key="brain-v42",
                topic=f"topic {i}", insight=f"insight {i} payload",
                created_at=datetime.now(tz=UTC) - timedelta(hours=i),
                updated_at=datetime.now(tz=UTC) - timedelta(hours=i),
            ))
        await session.commit()


async def _compose_full(session_factory):
    """Run the full briefing pipeline against the seeded DB."""
    from types import SimpleNamespace

    dream_svc = DreamRunService(session_factory)
    feature_svc = FeatureService(session_factory)
    async with session_factory() as session:
        ctx_row = (await session.execute(
            sa.select(project_contexts).where(project_contexts.c.project_key == "brain-v42")
        )).mappings().one()
        decision_rows = (await session.execute(
            sa.select(decisions).where(decisions.c.project_key == "brain-v42")
            .order_by(decisions.c.created_at.desc()).limit(5)
        )).mappings().all()
        learning_rows = (await session.execute(
            sa.select(learnings).where(learnings.c.project_key == "brain-v42")
            .order_by(learnings.c.created_at.desc()).limit(5)
        )).mappings().all()
    # Use SimpleNamespace (less brittle than dynamic type() construction) and
    # coerce blockers to a list to avoid None propagation.
    ctx_data = dict(ctx_row)
    ctx_data["blockers"] = ctx_data.get("blockers") or []
    ctx = SimpleNamespace(**ctx_data)
    decisions_list = [SimpleNamespace(**dict(r)) for r in decision_rows]
    learnings_list = [SimpleNamespace(**dict(r)) for r in learning_rows]
    killswitches = await dream_svc.killswitch_state()
    last_failure = await dream_svc.last_failure()
    in_flight = await feature_svc.in_flight(project_key="brain-v42")
    stale_pinned = await feature_svc.stale_pinned(project_key="brain-v42")
    return _format_session_briefing(
        ctx, decisions_list, learnings_list,
        killswitches, last_failure, in_flight, stale_pinned,
    )


@pytest.mark.asyncio
async def test_full_seed_matches_golden(session_factory):
    """Briefing with every section populated matches briefing_full.md."""
    await _seed_full(session_factory)
    out = await _compose_full(session_factory)
    golden = (FIXTURES / "briefing_full.md").read_text()
    assert _normalize(out).strip() == _normalize(golden).strip()


@pytest.mark.asyncio
async def test_empty_seed_matches_golden(session_factory):
    """Empty DB produces the no-context degraded briefing."""
    dream_svc = DreamRunService(session_factory)
    feature_svc = FeatureService(session_factory)
    killswitches = await dream_svc.killswitch_state()
    last_failure = await dream_svc.last_failure()
    in_flight = await feature_svc.in_flight(project_key="missing")
    stale_pinned = await feature_svc.stale_pinned(project_key="missing")

    out = _format_session_briefing(
        None, [], [], killswitches, last_failure, in_flight, stale_pinned,
    )
    golden = (FIXTURES / "briefing_empty.md").read_text()
    assert _normalize(out).strip() == _normalize(golden).strip()


@pytest.mark.asyncio
async def test_token_budget(session_factory):
    """Full briefing stays under 4000 chars (~800 tokens).

    NOTE: 4000 chars is a coarse proxy for "≲800 tokens" — Anthropic
    tokenisers average ~4-5 chars per token for English text, so 4000 chars
    sits comfortably below the 800-token guidance from spec §3. We assert
    chars (not tokens) to avoid pulling a tokenizer into the test path.
    Runs against the FULL-seed briefing (every section populated) so the
    test is meaningful — an empty briefing would trivially pass.
    """
    await _seed_full(session_factory)
    out = await _compose_full(session_factory)
    assert len(out) < 4000
```

- [ ] **Step 2: Run the test, expect FAIL**

```bash
uv run pytest tests/integration/test_session_start_briefing.py -v
```

Expect: file-not-found for fixtures.

- [ ] **Step 3: Generate the golden fixtures by capturing real output**

Temporarily replace each `assert` with `(FIXTURES / "briefing_full.md").write_text(_normalize(out))` and run once to capture the actual output. Inspect both `.md` files manually — confirm sections, ordering, anchor renders, and that no IDs / absolute dates leaked through the normaliser. Then restore the asserts.

- [ ] **Step 4: Re-run tests, expect PASS**

```bash
uv run pytest tests/integration/test_session_start_briefing.py -v
```

- [ ] **Step 5: Manual smoke against live PG**

```bash
uv run python -c "
import asyncio
from brain_v42.mcp.tools.session_tools import _format_session_briefing
# … minimal harness loading real services from .env …
"
```

Eyeball the output. Confirm killswitches reflect last night's REORG DRY soak, blockers section renders if `ctx.blockers` is populated, and the drill-in hint is on the last line.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_session_start_briefing.py \
        tests/fixtures/briefing_full.md \
        tests/fixtures/briefing_empty.md
git commit -m "$(cat <<'EOF'
test(session_start): golden snapshot + token budget regression

Two snapshot tests pin the action-forward briefing's shape end-to-end:
one full-seed case (every section populated) and one empty-DB case
(degraded fallback). A token-budget test asserts the briefing stays
under 4000 chars. Dates and relative timestamps are normalised before
comparison.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Safety checklist verification

| Batch | Layer | Tasks | File disjoint? | Test file disjoint? | Schema disjoint? | Verdict |
|-------|-------|-------|----------------|---------------------|------------------|---------|
| 1 | (parallel) | T1.1 (alembic+db/tables), T1.2 (services/feature_service+tests) | Yes | Yes | T1.1 mutates dream_runs schema, T1.2 reads features (no overlap) | SAFE PARALLEL |
| 2 | (parallel) | T2.1 (metrics/dream_parser + dream.sh), T2.2 (services/dream_run_service) | Yes | Yes | T2.1 writes dream_runs, T2.2 reads dream_runs — both downstream of T1.1; no concurrent schema change | SAFE PARALLEL |
| 3 | (sequential) | T3.1 (session_tools + server wiring) | n/a | n/a | n/a | trivial |
| 4 | (sequential) | T4.1 (integration golden) | n/a | n/a | n/a | trivial |

All `(parallel)` batches verified against the four checklist items in `writing-plans-parallel` step 4.

## Plan summary

| Surface | Net change |
|---------|------------|
| Schema | +1 column (migration 022) |
| Bash | +1 expression for phase_dry_run in dream.sh |
| Python (src) | +2 services (~200 LOC), +9 section helpers + composer (~120 LOC), CLI arg + insert path in dream_parser |
| Python (tests) | +4 new test files, +1 extended (~700 LOC) |
| Fixtures | +2 golden Markdown files |
| Commits | 6 (one per task) |

Roll-forward safe at every checkpoint. Each task ends with its own commit; on red, the prior checkpoint state is the rollback target.
