# Draining ghost sessions — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec**: `docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md` (commit `2eb98583`, approved by the operator)
**Brain ticket**: `2bd14b24-ccfe-4372-adf2-245b00304402`

**Goal:** Give the server — and only the server — the right to abandon an open session with no sign of life for 7 days, via a Dream `sweep` phase that starts in DRY.

**Architecture:** The `brain_sessions` SQL stays entirely inside `PgBrainSessionRepo`, which gains an `abandon_stale` method with no identity guard (the server is not a client). A `brain_v42.maintenance.session_sweep` CLI carries the policy (threshold, DRY/WET, report, `dream_runs` row), modeled on `reap_stale_mcp` for shape and on `roadmap_curate` for the Dream integration. `scripts/dream.sh` calls it the way it calls `extract` and `roadmap`. No migration: `dream_runs.phase` is a `varchar(10)` with no enum constraint, and `sweep` only writes existing columns.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, asyncpg, Pydantic 2, pytest / pytest-asyncio, bash.

## Global Constraints

Values copied **verbatim** from the spec. Every task honors them implicitly.

- Predicate: `status = 'open' AND last_heartbeat_at < now() - interval '7 days'`.
- Terminal state: `abandoned`, **never** `ended` (D2).
- `abandonment_reason`: the exact constant `auto_stale_7d`, forever distinct from a manual abandon.
- Scope: **all projects**, no filter.
- Trace: one `dream_runs` row, `phase='sweep'`, `model` NULL, `phase_dry_run` reflecting the mode.
- Killswitches: `BRAIN_DREAM_SWEEP_ENABLED`, `BRAIN_DREAM_SWEEP_DRY_RUN`.
- Shipped defaults: **closed** (`ENABLED=false`) and **DRY** (`DRY_RUN=true`).
- In DRY: log exactly what **would have** been abandoned, write **nothing** to `brain_sessions`.
- The automatic abandon produces neither `summary` nor `next_focus`, and **never** touches the project focus.
- No `unabandon`: out of scope until DRY has produced zero false positives.
- The 7-day threshold lives in **one single** Python constant (`AUTO_STALE_AFTER`). No other file — shell included — copies it (learning `8dc7e042`: a duplicated constant is a time bomb).

### State measured on 2026-08-07 (re-measured, not copied from the spec)

```
21 open · 17 stale à 7j · 4 vivantes
fantôme le plus récent : 10,6 j   ·   vivante la plus ancienne : 0,4 j   →   fossé de 10,2 j
schéma production : 041
```

The gap confirms D3. **It must be re-measured before the WET flip**, not re-read here.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/brain_v42/models/brain_session.py` | `AUTO_STALE_AFTER` / `AUTO_STALE_ABANDONMENT_REASON` constants, sweep result models | 1 |
| `src/brain_v42/repositories/pg_brain_session.py` | `abandon_stale` — the sole SQL writer for `brain_sessions` | 1 |
| `src/brain_v42/maintenance/session_sweep.py` | CLI: policy, report, `dream_runs` row | 2 |
| `scripts/dream.sh` | `sweep` phase + killswitches | 3 |
| `src/brain_v42/dream_killswitches.py` | reads the systemd drop-in | 4 |
| `src/brain_v42/services/dream_run_service.py` | `KillswitchState` | 4 |
| `src/brain_v42/mcp/tools/session_tools.py` | briefing's SWEEP line | 4 |
| `src/brain_v42/metrics/collector_dream.py` | expected phase when the killswitch is open | 4 |
| `CLAUDE.md`, `README.md`, `docs/MCP_TOOLS.md` | doctrinal amendment | 5 |

---

### Task 1: the persistence-side sweep

**Files:**
- Modify: `src/brain_v42/models/brain_session.py` (constants after `SESSION_STALE_AFTER:14`, models after `BrainSessionAbandonResult:278`)
- Modify: `src/brain_v42/repositories/pg_brain_session.py` (new method after `abandon`, which ends on line 487)
- Test: `tests/unit/repositories/test_pg_brain_session_sweep.py` (create)
- Test: `tests/integration/db/test_brain_sessions_sweep.py` (create)

**Interfaces:**
- Consumes: `PgBrainSessionRepo(BasePgRepository)`, its `self.transaction()`, the `brain_sessions` table.
- Produces: `AUTO_STALE_AFTER: timedelta`, `AUTO_STALE_ABANDONMENT_REASON: str`, `BrainSessionSweepCandidate`, `BrainSessionSweepResult`, and
  `PgBrainSessionRepo.abandon_stale(*, older_than: timedelta = AUTO_STALE_AFTER, reason: str = AUTO_STALE_ABANDONMENT_REASON, dry_run: bool = True, now: datetime | None = None) -> BrainSessionSweepResult`.

**Design note not to lose:** `abandon_stale` does **not** take an `expected_client_key`. This is not an oversight: the identity guard protects one client from targeting another, and here no client is asking for anything. Passing the row's own `client_key` back to itself would simulate a check that checks nothing. The doctrinal amendment in Task 5 is what authorizes this path; the two should be read together.

**Second note:** in WET, **a single** statement. No `SELECT` followed by `UPDATE`: under READ COMMITTED, PostgreSQL re-evaluates the `WHERE` under the row lock, so a `heartbeat` that commits during the sweep removes its own row from the update instead of losing the race. This is the direct answer to the false-death of 2026-08-06 (session `9b6f7e18` abandoned while alive).

- [ ] **Step 1: write the failing unit tests**

Create `tests/unit/repositories/test_pg_brain_session_sweep.py`. The mock harness of the neighboring module (`tests/unit/repositories/test_pg_brain_session.py`) is reused via import: it performs no I/O.

```python
"""Contrat unitaire du balayage serveur des sessions sans signe de vie.

Le harnais compile les statements SQLAlchemy sans PostgreSQL : il prouve la
FORME du prédicat et le fait que le DRY n'émet aucun UPDATE. La frontière
réelle du prédicat (N-1 / N+1 jour) ne se prouve que contre une vraie base :
elle vit dans tests/integration/db/test_brain_sessions_sweep.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _is_update,
    _make_session,
    _params,
    _result,
    _sql,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _stale_row(*, project_key: str = "auto-discord", days: float = 24.1) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "project_key": project_key,
        "client_key": "codex-factory-28aeb338",
        "last_heartbeat_at": NOW - timedelta(days=days),
    }


def _router(rows: list[dict[str, Any]]):
    def route(statement: Any):
        return _result(rows=rows)

    return route


@pytest.mark.asyncio
async def test_dry_run_selects_and_never_updates() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert [candidate.project_key for candidate in result.candidates] == ["auto-discord"]
    assert result.dry_run is True
    assert result.abandoned_count == 0
    assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(statements) == 1
    assert _sql(statements[0]).startswith("select")


@pytest.mark.asyncio
async def test_wet_run_updates_in_a_single_statement() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert result.dry_run is False
    assert result.abandoned_count == 1
    updates = [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(updates) == 1
    assert len(statements) == 1, "un seul statement : pas de fenêtre SELECT-puis-UPDATE"
    sql = _sql(updates[0])
    assert "returning" in sql
    assert "status" in sql and "abandonment_reason" in sql and "ended_at" in sql
    assert "summary" not in sql
    assert "next_focus" not in sql
    assert "project_contexts" not in sql


@pytest.mark.asyncio
async def test_cutoff_is_now_minus_threshold_and_strict() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_AFTER
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert AUTO_STALE_AFTER == timedelta(days=7)
    assert result.cutoff == NOW - timedelta(days=7)
    sql = _sql(statements[0])
    assert "status =" in sql
    assert "last_heartbeat_at <" in sql
    assert "last_heartbeat_at <=" not in sql


@pytest.mark.asyncio
async def test_default_reason_is_the_auto_constant() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_ABANDONMENT_REASON
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([_stale_row()]))

    await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert AUTO_STALE_ABANDONMENT_REASON == "auto_stale_7d"
    assert "auto_stale_7d" in _params(statements[0]).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_blank_reason_is_refused(bad: str) -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(reason=bad, dry_run=False, now=NOW)


@pytest.mark.asyncio
async def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(
            older_than=timedelta(0), dry_run=False, now=NOW
        )
```

- [ ] **Step 2: verify the failure for the right reason**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/repositories/test_pg_brain_session_sweep.py -v
```
Expected: `ImportError` on `AUTO_STALE_AFTER` / `AttributeError: 'PgBrainSessionRepo' object has no attribute 'abandon_stale'`. If the failure comes from `_is_update` or `_make_session`, the harness import is wrong — fix the import, not the test.

- [ ] **Step 3: add the constants and the models**

In `src/brain_v42/models/brain_session.py`, right after `SESSION_STALE_AFTER` (line 14):

```python
SESSION_STALE_AFTER = timedelta(hours=24)
# Two distinct thresholds, deliberately placed side by side so they are never
# confused: SESSION_STALE_AFTER (24h) is a DERIVED flag shown to the client,
# it changes no status; AUTO_STALE_AFTER (7d) is the threshold at which the
# SERVER abandons. The gap measured on 2026-08-07 between the most recent
# ghost (10.6d) and the oldest live session (0.4d) calibrates the second one.
AUTO_STALE_AFTER = timedelta(days=7)
AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"
```

At the end of the file, after `BrainSessionListResult`:

```python
class BrainSessionSweepCandidate(BaseModel):
    """Une session ouverte retenue par le balayage, en DRY comme en WET."""

    id: UUID
    project_key: str
    client_key: str
    last_heartbeat_at: datetime


class BrainSessionSweepResult(BaseModel):
    """Résultat d'un balayage serveur, tous projets confondus."""

    candidates: list[BrainSessionSweepCandidate]
    dry_run: bool
    cutoff: datetime
    # Always 0 in DRY. Redundant with len(candidates) — deliberately: a log
    # must render "17 auraient été abandonnées" as unreadable as
    # "17 ont été abandonnées".
    abandoned_count: int = Field(..., ge=0)
```

- [ ] **Step 4: implement `abandon_stale`**

In `src/brain_v42/repositories/pg_brain_session.py`, add to the imports from `brain_v42.models.brain_session`:

```python
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
```

Add to the standard imports: `from datetime import UTC, datetime, timedelta` (line 7 already exists, add `timedelta`).

Then, right after the `abandon` method (which ends on line 487):

```python
    async def abandon_stale(
        self,
        *,
        older_than: timedelta = AUTO_STALE_AFTER,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        """Abandonner toute session ouverte sans heartbeat depuis ``older_than``.

        Chemin SERVEUR uniquement : pas de garde ``expected_client_key``, parce
        qu'aucun client ne demande — c'est le serveur. L'amendement doctrinal du
        CLAUDE.md borne ce droit à ce seul chemin ; il n'ouvre rien pour l'agent
        ni pour le client, dont les sept commandes restent explicites.

        Ne touche ni ``project_contexts`` ni ``brain_session_artifacts`` : le
        focus et le ledger de capture d'une session abandonnée survivent, comme
        pour un abandon manuel.
        """
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")
        if older_than <= timedelta(0):
            raise BrainSessionInputError("older_than must be a positive interval")

        reference = now or datetime.now(UTC)
        cutoff = reference - older_than
        stale = sa.and_(
            brain_sessions.c.status == "open",
            brain_sessions.c.last_heartbeat_at < cutoff,
        )
        selection = (
            brain_sessions.c.id,
            brain_sessions.c.project_key,
            brain_sessions.c.client_key,
            brain_sessions.c.last_heartbeat_at,
        )

        if dry_run:
            statement: Any = sa.select(*selection).where(stale)
        else:
            # A SINGLE statement. No SELECT then UPDATE: under READ
            # COMMITTED, PostgreSQL re-evaluates `stale` under the row lock,
            # so a heartbeat that commits during the sweep removes its own row
            # from the update instead of losing the race. This is the answer
            # to the false-death of 2026-08-06 (a live session wrongly abandoned).
            statement = (
                brain_sessions.update()
                .where(stale)
                .values(
                    status="abandoned",
                    abandonment_reason=normalized_reason,
                    ended_at=reference,
                    updated_at=reference,
                )
                .returning(*selection)
            )

        async with self.transaction() as session:
            rows = (await session.execute(statement)).mappings().all()

        candidates = sorted(
            (BrainSessionSweepCandidate(**dict(row)) for row in rows),
            key=lambda candidate: candidate.last_heartbeat_at,
        )
        return BrainSessionSweepResult(
            candidates=candidates,
            dry_run=dry_run,
            cutoff=cutoff,
            abandoned_count=0 if dry_run else len(candidates),
        )
```

- [ ] **Step 5: verify the unit tests pass**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/repositories/test_pg_brain_session_sweep.py -v
```
Expected: 7 passed.

- [ ] **Step 6: write the failing integration tests**

Create `tests/integration/db/test_brain_sessions_sweep.py`. **The 365-day threshold is not decorative**: the sweep is global by design, and the integration database is shared with the other fixtures. Backdating the test rows and targeting 365 days guarantees that no session created by a neighboring test can fall inside the scope.

```python
"""Le balayage serveur contre une vraie base : frontière et invariants.

Seuil de 365 jours partout : le balayage est GLOBAL par conception, et la base
d'intégration est partagée. Antidater les lignes de la fixture et viser un an
rend structurellement impossible d'emporter la session d'un test voisin, qui
est forcément créée « maintenant ».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    decisions,
    project_contexts,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

THRESHOLD = timedelta(days=365)


@pytest_asyncio.fixture
async def sweep_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-sweep-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Session sweep integration",
                description="Isolated sweep fixture",
                current_focus="focus avant balayage",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            project_sessions = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(project_sessions)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(decisions.delete().where(decisions.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _insert_open_session(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    client_key: str,
    heartbeat: datetime,
):
    async with session_factory.begin() as session:
        row = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=project_key,
                    client_key=client_key,
                    started_focus="focus avant balayage",
                    started_focus_revision=1,
                    started_at=heartbeat,
                    last_heartbeat_at=heartbeat,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()
    return row


async def _read(session_factory: async_sessionmaker[AsyncSession], session_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def test_predicate_boundary_spares_n_minus_one_and_takes_n_plus_one(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    inside = await _insert_open_session(
        session_factory, sweep_project, "inside", now - THRESHOLD + timedelta(days=1)
    )
    outside = await _insert_open_session(
        session_factory, sweep_project, "outside", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert outside in swept
    assert inside not in swept
    assert (await _read(session_factory, inside))["status"] == "open"
    abandoned = await _read(session_factory, outside)
    assert abandoned["status"] == "abandoned"
    assert abandoned["abandonment_reason"] == "auto_stale_7d"
    assert abandoned["ended_at"] is not None
    assert abandoned["summary"] is None
    assert abandoned["next_focus"] is None
    assert abandoned["focus_outcome"] is None


async def test_dry_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )
    before = await _read(session_factory, ghost)

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=True, now=now
    )

    assert [candidate.id for candidate in result.candidates] == [ghost]
    assert result.abandoned_count == 0
    assert dict(await _read(session_factory, ghost)) == dict(before)


async def test_sweep_preserves_focus_revision_and_attributions(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost-with-capture", now - THRESHOLD - timedelta(days=2)
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision capturée avant le balayage",
                content="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=ghost,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )
    async with session_factory() as session:
        focus_before = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )

    await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    async with session_factory() as session:
        focus_after = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )
        attributions = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.knowledge_id).where(
                        brain_session_artifacts.c.session_id == ghost
                    )
                )
            )
            .scalars()
            .all()
        )

    assert dict(focus_after) == dict(focus_before)
    assert list(attributions) == [knowledge_id]
    swept = await _read(session_factory, ghost)
    assert swept["status"] == "abandoned"
    # The terminal snapshot stays empty: that's the CHECK constraint
    # brain_sessions_terminal_state_valid for 'abandoned'. The ledger, on
    # the other hand, lives in brain_session_artifacts and survives.
    assert list(swept["captured_knowledge_ids"]) == []


async def test_manual_abandonment_reason_is_never_overwritten(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    manual = await _insert_open_session(
        session_factory, sweep_project, "manual", now - THRESHOLD - timedelta(days=3)
    )
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == manual)
            .values(
                status="abandoned",
                abandonment_reason="abandon manuel de l'opérateur",
                ended_at=now,
            )
        )

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    assert manual not in {candidate.id for candidate in result.candidates}
    row = await _read(session_factory, manual)
    assert row["abandonment_reason"] == "abandon manuel de l'opérateur"
```

- [ ] **Step 7: verify the integration tests fail**

```bash
unset VIRTUAL_ENV
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/db/test_brain_sessions_sweep.py -v
```
Expected: 4 failing tests (`AttributeError` if Step 4 has not been done yet, otherwise assertion failures). If the tests **SKIP** despite the variable, the integration database is unreachable — the DB fixture lives in `tests/integration/conftest.py`, a DB test under `tests/unit/` skips silently.

- [ ] **Step 8: make integration pass**

No new code is expected if Step 4 is correct. Two plausible failures to handle:
- `decisions.insert()` refused for a missing required column → read `src/brain_v42/db/tables.py` and fill in the values, **without** touching production code.
- `CheckViolationError` on `brain_sessions_terminal_state_valid` → the `values()` clause of `abandon_stale` writes a column forbidden for `abandoned`; remove it.

```bash
unset VIRTUAL_ENV
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/db/test_brain_sessions_sweep.py -v
```
Expected: `4 passed` — a nonzero count, not "4 skipped".

- [ ] **Step 9: gates then commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit/repositories tests/unit/models -q
git add src/brain_v42/models/brain_session.py src/brain_v42/repositories/pg_brain_session.py \
        tests/unit/repositories/test_pg_brain_session_sweep.py \
        tests/integration/db/test_brain_sessions_sweep.py
git commit -m "feat(sessions): abandonner côté serveur les sessions sans signe de vie depuis 7 jours"
```

---

### Task 2: the phase CLI

**Files:**
- Create: `src/brain_v42/maintenance/session_sweep.py`
- Test: `tests/unit/maintenance/test_session_sweep.py` (create, along with `tests/unit/maintenance/__init__.py` if the package doesn't exist)

**Interfaces:**
- Consumes: `PgBrainSessionRepo.abandon_stale`, `AUTO_STALE_AFTER`, `BrainSessionSweepResult` (Task 1).
- Produces: `build_parser() -> argparse.ArgumentParser`, `render_report(result: BrainSessionSweepResult) -> str`, `record_dream_run(session_factory, status, dry, duration_s, error) -> None`, `main() -> int`. Entry point: `python -m brain_v42.maintenance.session_sweep`.

- [ ] **Step 1: write the failing tests**

Create `tests/unit/maintenance/__init__.py` (empty) then `tests/unit/maintenance/test_session_sweep.py`:

```python
"""Contrat du CLI de balayage : DRY par défaut, seuil non dupliqué, rapport lisible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    AUTO_STALE_AFTER,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _result(*, dry_run: bool, count: int = 2) -> BrainSessionSweepResult:
    candidates = [
        BrainSessionSweepCandidate(
            id=uuid4(),
            project_key=f"projet-{index}",
            client_key=f"codex-factory-{index}",
            last_heartbeat_at=NOW - timedelta(days=10 + index),
        )
        for index in range(count)
    ]
    return BrainSessionSweepResult(
        candidates=candidates,
        dry_run=dry_run,
        cutoff=NOW - AUTO_STALE_AFTER,
        abandoned_count=0 if dry_run else count,
    )


def test_dry_is_the_default_mode() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.wet is False


def test_threshold_default_comes_from_the_single_constant() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.older_than_days == AUTO_STALE_AFTER.days == 7


def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--older-than-days", "0"])


def test_dry_report_says_would_and_never_says_abandoned() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True))

    assert "DRY" in report
    assert "auraient été abandonnées" in report
    assert "ont été abandonnées" not in report
    assert "projet-0" in report and "projet-1" in report
    assert "2026-07-31" in report  # cutoff rendered, not just the count


def test_wet_report_states_what_was_written() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=False))

    assert "WET" in report
    assert "2 sessions ont été abandonnées" in report
    assert "auraient" not in report


def test_empty_sweep_is_reported_as_a_normal_night() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True, count=0))

    assert "aucune session à abandonner" in report
    assert len(report.splitlines()) == 1, "aucune ligne de candidat"


@pytest.mark.asyncio
async def test_record_dream_run_never_raises_when_the_database_is_down() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    def broken_factory():
        raise RuntimeError("base injoignable")

    await record_dream_run(
        broken_factory, "done", dry=True, duration_s=1.0, error=None
    )  # must not raise
```

- [ ] **Step 2: verify the failure**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/maintenance/test_session_sweep.py -v
```
Expected: `ModuleNotFoundError: No module named 'brain_v42.maintenance.session_sweep'`.

- [ ] **Step 3: write the CLI**

Create `src/brain_v42/maintenance/session_sweep.py`:

```python
"""Phase Dream `sweep` — tarir les sessions ouvertes sans signe de vie.

Spec : docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md

Déterministe et sans modèle : aucun appel LLM, aucun réseau. La row
``dream_runs`` porte donc ``model = NULL`` — forme déjà admise, observée sur
``extract`` et sur le run ``roadmap`` du 2026-08-05.

Livré DRY : ``--wet`` est le seul chemin qui écrit.

Usage:
    python -m brain_v42.maintenance.session_sweep           # dry (défaut)
    python -m brain_v42.maintenance.session_sweep --wet     # applique
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa

from brain_v42.models.brain_session import AUTO_STALE_AFTER, BrainSessionSweepResult

_MAX_ERROR_CHARS = 2000


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_sweep",
        description="Abandonner les sessions ouvertes sans heartbeat depuis N jours.",
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="applique les abandons (défaut : dry, aucune écriture)",
    )
    parser.add_argument(
        "--older-than-days",
        type=_positive_int,
        # Default READ from the constant, never copied: two copies of the
        # same threshold is the textbook mistake from learning 8dc7e042.
        default=AUTO_STALE_AFTER.days,
        help=f"seuil en jours (défaut : {AUTO_STALE_AFTER.days}, depuis AUTO_STALE_AFTER)",
    )
    return parser


def render_report(result: BrainSessionSweepResult) -> str:
    """Rapport texte du balayage, pour le log daté de la nuit."""
    mode = "DRY" if result.dry_run else "WET"
    cutoff = result.cutoff.isoformat(timespec="seconds")
    count = len(result.candidates)
    if count == 0:
        return f"sweep [{mode}] cutoff={cutoff} — aucune session à abandonner"

    verb = "auraient été abandonnées" if result.dry_run else "ont été abandonnées"
    lines = [f"sweep [{mode}] cutoff={cutoff} — {count} sessions {verb}"]
    lines.extend(
        f"  {candidate.project_key:<16} {candidate.client_key:<40} "
        f"{candidate.last_heartbeat_at.isoformat(timespec='seconds')}"
        for candidate in result.candidates
    )
    return "\n".join(lines)


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
) -> None:
    """INSERT dream_runs pour phase='sweep'. Best-effort — ne lève jamais.

    `model` reste NULL : la phase n'appelle aucun modèle.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "phase_dry_run, model) "
                        "VALUES (:run_date, 'sweep', :status, :duration_s, "
                        ":error_message, :phase_dry_run, NULL)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "phase_dry_run": dry,
                    },
                )
    except Exception as exc:  # noqa: BLE001 — the trace must never kill the phase
        print(f"! warning: could not record dream_run: {exc}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    session_factory = get_session_factory()
    dry = not args.wet
    started = time.monotonic()
    try:
        result = await PgBrainSessionRepo(session_factory).abandon_stale(
            older_than=timedelta(days=args.older_than_days),
            dry_run=dry,
        )
    except Exception as exc:  # noqa: BLE001 — translated into a dream_runs row + rc=1
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        await record_dream_run(
            session_factory, "fail", dry=dry, duration_s=time.monotonic() - started, error=detail
        )
        print(f"sweep: FAIL — {detail}", file=sys.stderr)
        return 1

    print(render_report(result), flush=True)
    await record_dream_run(
        session_factory, "done", dry=dry, duration_s=time.monotonic() - started, error=None
    )
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: verify the tests pass**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/maintenance/test_session_sweep.py -v
```
Expected: 7 passed. If `test_dry_report_says_would...` fails on `"2026-07-31"`, check the cutoff computation in the test fixture (`NOW - AUTO_STALE_AFTER` = 2026-07-31T06:00) — do not loosen the assertion: the cutoff MUST be in the report.

- [ ] **Step 5: smoke-test the CLI in DRY against production**

Read-only by construction (`--wet` absent). This is the first real proof of the mechanism.

```bash
unset VIRTUAL_ENV
uv run python -m brain_v42.maintenance.session_sweep
```
Expected: the list of sessions with no heartbeat for 7 days, ~17 lines on 2026-08-07, `abandoned_count` implicitly 0. **Check by eye that no session from today appears.** Also check that the trace row is written:

```bash
docker exec brain_v42_postgres psql -U brain -d brain -c \
  "select run_date, phase, status, phase_dry_run, model, duration_s from dream_runs where phase='sweep' order by id desc limit 3;"
```
Expected: one row `sweep | done | t | (null)`.

- [ ] **Step 6: gates then commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run python scripts/check_module_layering.py --package src/brain_v42
git add src/brain_v42/maintenance/session_sweep.py tests/unit/maintenance/
git commit -m "feat(dream): ajouter le CLI de balayage des sessions fantômes"
```

---

### Task 3: the phase in `dream.sh`

**Files:**
- Modify: `scripts/dream.sh` (killswitches after line 54; phase block after the ROADMAP block, which ends on line 681)
- Test: `tests/unit/test_dream_sh_sweep.py` (create)

**Interfaces:**
- Consumes: `python -m brain_v42.maintenance.session_sweep` and its `--wet` flag (Task 2).
- Produces: the shell variables `BRAIN_DREAM_SWEEP_ENABLED` and `BRAIN_DREAM_SWEEP_DRY_RUN`, and the dated log `${TIMESTAMP}_sweep.log`.

- [ ] **Step 1: write the failing tests**

Create `tests/unit/test_dream_sh_sweep.py`, modeled on `tests/unit/test_dream_sh_roadmap.py`:

```python
"""Épingle le câblage de la phase SWEEP dans dream.sh (grep, sans exécution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_sweep_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"' in content


def test_sweep_step_invokes_the_cli_module():
    content = _content()
    assert "brain_v42.maintenance.session_sweep" in content
    assert "SKIP sweep (killswitch" in content


def test_sweep_wet_flag_only_when_dry_run_false():
    content = _content()
    assert 'if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]' in content


def test_sweep_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 5m uv run python -m brain_v42.maintenance.session_sweep" in content
    assert "_sweep.log" in content


def test_sweep_step_does_not_duplicate_the_threshold():
    """Le seuil vit dans AUTO_STALE_AFTER. Une deuxième copie dans le shell
    serait la bombe à retardement du learning 8dc7e042 : deux constantes qui
    se contredisent en silence le jour où l'une bouge."""
    content = _content()
    sweep_block = content.split("--- SWEEP")[1]
    assert "--older-than-days" not in sweep_block
    assert "7" not in sweep_block.split("sweep_args=(")[1].split(")")[0]
```

- [ ] **Step 2: verify the failure**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_dream_sh_sweep.py -v
```
Expected: 5 failed (`assert ... in content`). The last one may raise `IndexError` — it's the same failure, the section doesn't exist.

- [ ] **Step 3: declare the killswitches**

In `scripts/dream.sh`, right after line 54 (`BRAIN_DREAM_ROADMAP_DRY_RUN=...`):

```bash
# SWEEP killswitch — draining ghost sessions (spec 2026-08-07).
# Shipped CLOSED and DRY. Deterministic phase, no model or network: the
# threshold lives in brain_v42.models.brain_session.AUTO_STALE_AFTER, never here.
BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"
BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"
```

- [ ] **Step 4: add the phase block**

In `scripts/dream.sh`, between the end of the ROADMAP block (the `fi` on line 681) and the `FAIL_TOTAL=$(( ... ))` line:

```bash
# --- SWEEP: draining ghost sessions -----------------------------------------
# Not an agent phase: direct Python CLI (extract/roadmap pattern). Inserts
# its own dream_runs row (phase='sweep', model NULL) for briefing
# visibility. The threshold is NOT passed as an argument: a single constant.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
if [[ "$BRAIN_DREAM_SWEEP_ENABLED" != "true" ]]; then
  log "SKIP sweep (killswitch BRAIN_DREAM_SWEEP_ENABLED=$BRAIN_DREAM_SWEEP_ENABLED)"
  SKIPPED_PHASES+=("sweep")
else
  sweep_args=()
  if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]; then
    sweep_args+=(--wet)
  fi
  log "sweep: session_sweep starting (dry_run=$BRAIN_DREAM_SWEEP_DRY_RUN)"
  set +e
  # 5m: an indexed query, no model call or network. A timeout
  # signals a struggling database, not a slow phase.
  timeout 5m uv run python -m brain_v42.maintenance.session_sweep "${sweep_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_sweep.log" 2>&1
  sweep_rc=$?
  set -e
  if (( sweep_rc == 0 )); then
    log "DONE sweep"
  else
    log "FAIL sweep (rc=$sweep_rc) — see ${TIMESTAMP}_sweep.log"
    FAILED_PHASES+=("sweep")
  fi
fi
```

**Careful with `set -u`:** `"${sweep_args[@]}"` on an empty array fails on bash < 4.4. Check `bash --version` ≥ 4.4 (the case on this host); otherwise write `${sweep_args[@]+"${sweep_args[@]}"}`.

- [ ] **Step 5: verify the tests pass and the script stays valid**

```bash
unset VIRTUAL_ENV
bash -n scripts/dream.sh
uv run pytest tests/unit/test_dream_sh_sweep.py tests/unit/test_dream_sh_roadmap.py tests/unit/test_dream_sh_extract.py -v
```
Expected: `bash -n` silent, all tests pass.

- [ ] **Step 6: commit**

```bash
git add scripts/dream.sh tests/unit/test_dream_sh_sweep.py
git commit -m "feat(dream): câbler la phase sweep derrière son killswitch, fermé et dry"
```

---

### Task 4: make the phase visible

Without this task, an operator cannot see from the briefing whether the sweep is armed, and the metrics do not expect the phase — a missing `sweep` would pass for a normal night.

**Files:**
- Modify: `src/brain_v42/dream_killswitches.py:12-20` (`_KS_KEYS`)
- Modify: `src/brain_v42/services/dream_run_service.py:38-52` (`KillswitchState`) and `:134-152`
- Modify: `src/brain_v42/mcp/tools/session_tools.py:106-128` (`_section_killswitches`)
- Modify: `src/brain_v42/metrics/collector_dream.py:45` (`expected_dream_phases`)
- Modify: `tests/fixtures/briefing_full.md:3-8` (golden)
- Test: `tests/unit/services/test_dream_run_service.py`, `tests/unit/mcp/test_session_tools.py`, `tests/unit/metrics/test_dream_metrics.py`

**Interfaces:**
- Consumes: the environment keys `BRAIN_DREAM_SWEEP_ENABLED` / `BRAIN_DREAM_SWEEP_DRY_RUN` (Task 3) and the `dream_runs` rows of phase `sweep` (Task 2).
- Produces: `KillswitchState.sweep_enabled: bool`, `.sweep_dry: bool`, `.sweep_clean_dry_nights: int`, and the briefing line `- SWEEP  : …`.

- [ ] **Step 1: write the failing tests**

In `tests/unit/services/test_dream_run_service.py`, add a method to the existing
`TestKillswitchState` class (it has the `session_factory` fixture — in-memory
SQLite — and the `_insert_run` helper):

```python
    @pytest.mark.asyncio
    async def test_sweep_enabled_dry_from_the_drop_in(self, session_factory, tmp_path):
        """SWEEP suit exactement le contrat des autres phases optionnelles."""
        today = date.today()
        await _insert_run(
            session_factory, run_date=today, phase="sweep", phase_dry_run=True, model=None
        )
        ks = tmp_path / "killswitches.conf"
        ks.write_text(
            "[Service]\n"
            "Environment=BRAIN_DREAM_SWEEP_ENABLED=true\n"
            "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=true\n"
        )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_enabled is True
        assert state.sweep_dry is True
```

In `tests/unit/mcp/test_session_tools.py`, add a method to the existing
`TestSectionKillswitches` class (it builds its `KillswitchState` directly):

```python
    def test_sweep_row_sits_between_roadmap_and_graph(self):
        state = KillswitchState(
            last_run_date=date(2026, 8, 7),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            sweep_enabled=True,
            sweep_dry=True,
            sweep_clean_dry_nights=3,
        )

        lines = _section_killswitches(state, graph_enabled=True).splitlines()

        assert "- SWEEP  : enabled (dry · 3 clean DRY nights)" in lines
        assert lines.index("- SWEEP  : enabled (dry · 3 clean DRY nights)") == (
            lines.index("- GRAPH:   enabled") - 1
        )
```

In `tests/unit/metrics/test_dream_metrics.py`, add:

```python
def test_expected_phases_include_sweep_when_the_killswitch_is_open(tmp_path) -> None:
    from brain_v42.metrics.collector_dream import expected_dream_phases

    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text("[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=true\n")

    assert "sweep" in expected_dream_phases(drop_in)
```

- [ ] **Step 2: verify the failure**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py \
              tests/unit/metrics/test_dream_metrics.py -v -k sweep
```
Expected: `AttributeError: 'KillswitchState' object has no attribute 'sweep_enabled'` and the absence of the SWEEP line.

- [ ] **Step 3: add the drop-in keys**

In `src/brain_v42/dream_killswitches.py`, in `_KS_KEYS`, after the ROADMAP entries:

```python
    "BRAIN_DREAM_SWEEP_ENABLED": "sweep",
    "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
```

- [ ] **Step 4: extend `KillswitchState`**

In `src/brain_v42/services/dream_run_service.py`, at the end of `KillswitchState`'s fields:

```python
    sweep_enabled: bool = False
    sweep_dry: bool = True
    sweep_clean_dry_nights: int = 0
```

Then in `killswitch_state`, after the `roadmap_*` block (line 136):

```python
            sweep_enabled = phase_enabled("sweep")
            sweep_dry = phase_dry("sweep", True)
            sweep_streak = await self._clean_dry_streak(session, "sweep")
```

and in the final `return KillswitchState(...)`:

```python
            sweep_enabled=sweep_enabled,
            sweep_dry=sweep_dry,
            sweep_clean_dry_nights=sweep_streak,
```

- [ ] **Step 5: add the briefing line**

In `src/brain_v42/mcp/tools/session_tools.py`, between the ROADMAP line and the GRAPH line:

```python
    lines.append(
        _row("SWEEP  ", state.sweep_enabled, state.sweep_dry, state.sweep_clean_dry_nights)
    )
```

Update the golden `tests/fixtures/briefing_full.md` — insert between `ROADMAP` and `GRAPH`:

```
- SWEEP  : disabled
```

- [ ] **Step 6: add the expected phase on the metrics side**

In `src/brain_v42/metrics/collector_dream.py`, line 45:

```python
    return {phase for phase in ("promote", "reorg", "extract", "roadmap", "sweep") if flags.get(phase)}
```

- [ ] **Step 7: verify everything passes**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py \
              tests/unit/metrics tests/unit/codex_gateway/test_killswitch_reader.py -q
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/test_session_start_briefing.py -q
```
Expected: unit tests green, then `3 passed` on integration — a nonzero count, not "3 skipped". The briefing golden test fails if Step 5 forgot the fixture — it is what proves the SWEEP line is actually rendered, and it proves nothing if it skips.

- [ ] **Step 8: gates then commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/dream_killswitches.py src/brain_v42/services/dream_run_service.py \
        src/brain_v42/mcp/tools/session_tools.py src/brain_v42/metrics/collector_dream.py \
        tests/fixtures/briefing_full.md tests/unit/
git commit -m "feat(briefing): exposer l'état du killswitch SWEEP au même titre que les autres phases"
```

---

### Task 5: the doctrinal amendment

The spec is explicit: without this amendment, the code contradicts a written prohibition. "Written in black and white, not worked around." Three documents carry the prohibition, and a fourth statement — the one in `docs/MCP_TOOLS.md` — becomes a trap without clarification, because `is_stale` (24h) and the sweep (7d) are two different thresholds.

**Files:**
- Modify: `CLAUDE.md:86-90` (the strict exception) and the `## Configuration` section
- Modify: `README.md:108-111`
- Modify: `docs/MCP_TOOLS.md:355`
- Test: `tests/unit/test_documentation_contract.py` (add a test function)

**Interfaces:**
- Consumes: the constant `auto_stale_7d` and the `BRAIN_DREAM_SWEEP_*` keys (Tasks 1 to 3).
- Produces: nothing programmatic — a documentation contract that fails if the amendment is removed or broadened.

- [ ] **Step 1: write the failing contract test**

In `tests/unit/test_documentation_contract.py`, add at the end:

```python
def test_server_side_sweep_amendment_is_narrow_and_stated() -> None:
    """L'exception du serveur doit être ÉCRITE, et bornée au serveur.

    Le CLAUDE.md interdisait catégoriquement toute fermeture automatique.
    La phase sweep contredirait cette phrase si elle n'était pas amendée
    explicitement : l'interdiction reste entière pour l'agent et le client,
    seul le serveur gagne le droit d'abandonner une session sans signe de vie.
    """
    claude_normalized = " ".join(CLAUDE.split())
    readme_normalized = " ".join(README.split())
    mcp_tools_normalized = " ".join(MCP_TOOLS.split())

    # The prohibition survives, explicitly borne by the agent and the client.
    for document in (claude_normalized, readme_normalized):
        assert "ne ferme une session côté agent ou client" in document

    # The exception is named, with its scope and its constant.
    for document in (claude_normalized, readme_normalized):
        assert "sans signe de vie depuis 7 jours" in document
        assert "auto_stale_7d" in document
        assert "ne touche jamais le focus du projet" in document

    # The two thresholds must never be confusable.
    assert "is_stale" in mcp_tools_normalized
    assert "24 hours old" in mcp_tools_normalized
    assert "seven-day server-side sweep" in mcp_tools_normalized

    # The exception does not extend to the client's commands.
    assert "restent des commandes explicites" in claude_normalized


def test_sweep_killswitches_are_documented_in_the_shared_configuration() -> None:
    claude_configuration = CLAUDE.split("## Configuration", maxsplit=1)[1]
    config_blocks = re.findall(r"```bash\n(.*?)```", claude_configuration, flags=re.DOTALL)
    shared_config = config_blocks[0]

    assert "BRAIN_DREAM_SWEEP_ENABLED=false" in shared_config
    assert "BRAIN_DREAM_SWEEP_DRY_RUN=true" in shared_config
    shared_key_list = _environment_assignment_keys(shared_config)
    assert len(shared_key_list) == len(set(shared_key_list))
```

- [ ] **Step 2: verify the failure**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_documentation_contract.py -v -k sweep
```
Expected: 2 failed on the `assert ... in document`.

- [ ] **Step 3: amend `CLAUDE.md`**

Replace the blockquote at lines 86-90 with:

```markdown
> **Exception stricte — cycle de session :** appeler `brain_session_start`,
> `brain_session_list`, `brain_session_resume`, `brain_session_capture`,
> `brain_session_heartbeat`, `brain_session_end` ou `brain_session_abandon` uniquement
> après la commande explicite correspondante de
> l'utilisateur. Aucun hook, auto-close, livraison de travail ou fin de réponse
> ne ferme une session côté agent ou client. Une feature livrée peut mettre à jour la
> roadmap, jamais fermer Brain.
>
> **Seule exception, côté serveur :** la phase Dream `sweep` abandonne une session ouverte
> sans signe de vie depuis 7 jours, avec `abandonment_reason='auto_stale_7d'`. Elle n'écrit
> ni summary ni `next_focus` et ne touche jamais le focus du projet. Aucun agent, aucun hook
> et aucun client ne gagne ce droit : `start`, `resume`, `end` et `abandon` restent des
> commandes explicites de l'utilisateur.
```

In the `## Configuration` section, add to the first `bash` block, after the ROADMAP keys:

```bash
# Sessions — balayage nocturne des fantômes (dream, serveur seul)
BRAIN_DREAM_SWEEP_ENABLED=false
BRAIN_DREAM_SWEEP_DRY_RUN=true
```

- [ ] **Step 4: amend `README.md`**

Replace lines 108-111 with:

```markdown
L'utilisateur contrôle toutes les frontières de session. Les sept commandes ci-dessous
ne s'exécutent qu'après sa demande explicite. Aucun hook, auto-close, livraison de travail
ou fin de réponse ne ferme une session côté agent ou client.

Une seule exception, côté serveur : la phase Dream `sweep` abandonne une session ouverte
sans signe de vie depuis 7 jours, avec `abandonment_reason='auto_stale_7d'`. Elle ne produit
ni summary ni `next_focus` et ne touche jamais le focus du projet.
```

- [ ] **Step 5: resolve the ambiguity in `docs/MCP_TOOLS.md`**

Replace line 355 with:

```markdown
An open session becomes `is_stale=true` when its last heartbeat is at least 24 hours old. `status="stale"` selects that subset of open sessions; this derived flag never changes the persisted `status` and never auto-closes a session. The regular `open` filter therefore includes both fresh and stale open sessions. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).
```

- [ ] **Step 6: verify the full documentation contract**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_documentation_contract.py -q
```
Expected: all green. This file may fail elsewhere for unrelated reasons (migration contract) — in that case, do not "fix" anything along the way: report it and stick to scope.

- [ ] **Step 7: commit**

```bash
git add CLAUDE.md README.md docs/MCP_TOOLS.md tests/unit/test_documentation_contract.py
git commit -m "docs(sessions): amender l'interdiction de fermeture automatique pour le seul serveur"
```

---

## Final verification, before any completion announcement

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python scripts/check_module_layering.py --package src/brain_v42
uv run pytest tests/unit -q
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration -q
bash -n scripts/dream.sh
```

Expected on integration: a nonzero `passed` count — measured `256 passed, 32 skipped` on
2026-08-07. A total that's entirely `skipped` is not green, it's an absence of execution.

`GRAPH_LEDGER_WRITE_ENABLED=false` is not optional on integration: without it the trunk's
`.env` leaks through and the tests come out as ERROR instead of failing, which masks real
regressions (learning `54fdfddc`).

`BRAIN_V42_TEST_DB_URL` is no more optional. `tests/integration/conftest.py` resolves its
database from this variable alone and skips the entire suite if it's absent, or if it targets
the prod database `brain` (guard `_resolve_integration_db_url`). Without `BRAIN_V42_TEST_DB_URL`,
`pytest tests/integration` skips the suite entirely and exits green: demand a nonzero `passed`
count, never "all green". Measurement from 2026-08-07: `288 skipped in 1.38s` without the
variable, `256 passed, 32 skipped in 82.97s` with it. The database is `brain_test` in the
`brain_v42_postgres` container (port 5433), never `brain`.

Then, before the final commit: `detect_changes()` to verify that the blast radius is the one
you expect. The GitNexus index is stale — refresh it **from the canonical root**, never
from a worktree.

---

## Deployment — operator actions, outside the code's scope

These steps are not tasks of this plan. They follow the spec's "Safety and
deployment" section and require the operator's hand.

1. **Arm in DRY.** Add to the drop-in `~/.config/systemd/user/brain-v42-dream.service.d/killswitches.conf`:
   ```
   # SWEEP: opened (dry) on <date> — soak before any WET flip (spec 2026-08-07).
   Environment=BRAIN_DREAM_SWEEP_ENABLED=true
   Environment=BRAIN_DREAM_SWEEP_DRY_RUN=true
   ```
   Then `systemctl --user daemon-reload`. The drop-in survives unit regeneration by
   `install.sh` — never put these lines in the template (incident 2026-06-30).
2. **Let it run for several nights.** Read `logs/dream/<date>_sweep.log` and verify that
   the phase only targets ghosts.
3. **Re-measure the gap before the WET flip.** Do not copy the numbers from the spec or
   from this plan:
   ```bash
   docker exec brain_v42_postgres psql -U brain -d brain -c \
     "select status, round(extract(epoch from now()-last_heartbeat_at)/86400.0,1) as age_days, project_key, client_key
        from brain_sessions where status='open' order by last_heartbeat_at desc;"
   ```
   The flip is only legitimate if a clear gap still separates the two populations.
4. **Flip WET**: `Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false`, `daemon-reload`.
5. **Verify after the first WET night** that the abandons do carry `auto_stale_7d` and
   that no live session was swept up.

The abandon is **irreversible** (`brain_session_resume` requires `status='open'`). Only three
mitigations: a generous threshold, prior DRY, and guaranteed preservation of
captures. No `unabandon` until DRY has produced zero false positives.

## Points explicitly left out

Carried over from the spec, each for its own reason:

- **Auto-heartbeat (`7ffe0e8a`)** — useless here (D3). D1's principle unblocks it
  conceptually: attribution by *(project, actor)* is enough.
- **Session identity (`2dfbb83d`)** — measured non-functional, made non-blocking by D3.
- **Semantic checkpoint (`d04dc588`)** — BLOCKED by its own audit.
- **Doctrine "subagents do not open sessions" (D1)** — touches nine projects, separate
  ecosystem ticket. Not technically applicable, and this is not an oversight.
- **Manual cleanup of the 17 ghosts** — DRY will list them, WET will handle them. Purging
  them by hand before then would make success unverifiable.
- **`unabandon`** — would solve a problem that has not been observed.

## Known limitations of this plan

- **A deliberate deviation from the spec, on a single point.** The spec classifies the
  predicate boundary (N−1 / N+1) as a *unit* test. The repository's unit harness compiles
  statements against mocks: it cannot evaluate a `WHERE`, so such a test would prove the
  shape of the SQL while implying it proves the boundary. The boundary is therefore tested
  at *integration* level (Task 1, Step 6), and the unit test keeps what it can actually
  prove: the computed cutoff and the strict inequality. The four behaviors required by the
  spec are covered, two moved to a different tier.
- The 7-day threshold remains calibrated on two measurements (2026-08-06 and 2026-08-07) of
  the same work regime. Longer-running projects would invalidate the margin.
- `last_heartbeat_at` remains declarative: a session that's alive but silent for more than
  7 days will be wrongly abandoned. This is the tradeoff accepted in D3, mitigated by the
  preservation of captures.
- Task 4 (briefing/metrics visibility) is not required word for word by the spec. It is
  included because without it, the phase's armed/DRY state is readable nowhere and the
  soak in deployment step 2 has to be steered by grep. It is isolated in its own task
  precisely so it stays guilty if the operator judges it out of scope.
