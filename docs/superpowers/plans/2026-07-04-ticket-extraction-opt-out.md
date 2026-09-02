# Ticket Extraction Opt-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a ticket be created with `extraction='skipped'` so high-volume operational/job tickets (e.g. `red-lab-factory`) never enter the nightly knowledge-extraction scan.

**Architecture:** Additive, **no migration** — the `tickets.extraction_status` column + CHECK already allow `'skipped'` (migration 028). Four thin changes: (1) `TicketCreate` carries an optional `extraction_status`; (2) the repo persists it at INSERT; (3) the MCP tool exposes an `extraction` param; (4) the terminal-transition side-effect must **not** overwrite a pre-set `'skipped'` back to `'pending'`. The extract job already filters `WHERE extraction_status='pending'`, so a `'skipped'` ticket is excluded for free.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLAlchemy 2.0 async, FastMCP, pytest (asyncio_mode=auto).

## Global Constraints

- **TDD strict** — RED (failing test) before GREEN (minimal impl). Never edit a test to make code pass. (CLAUDE.md)
- **No migration** — `extraction_status VARCHAR(10)` + CHECK `IN ('pending','proposed','skipped','done')` already exist (`alembic/versions/028_tickets.py:30,40-43`). Do **not** add a migration.
- Coverage ≥ 60% (CI gate). Type hints everywhere. Async for I/O.
- Green before commit: `python -m pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy src/` (CI runs `ruff format --check` — `ruff check` alone is not enough).
- Conventional Commits (`feat(...)`, `docs(...)`). Commit after every green task.
- Contract already resolved to red-lab (ticket `#0de7f408`): the create-time param is spelled **`extraction='skipped'`** — keep that spelling.

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/brain_v42/models/ticket.py` | Pydantic models | Add `extraction_status` field to `TicketCreate` |
| `src/brain_v42/repositories/pg_ticket.py` | PG CRUD | Persist `extraction_status` in `create()` INSERT |
| `src/brain_v42/services/ticket_service.py` | State machine + side effects | Guard terminal transition against overwriting a pre-set `'skipped'` |
| `src/brain_v42/mcp/tools/ticket_tools.py` | MCP surface | Add `extraction` param to `brain_ticket_create` |
| `docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md` | Spec §6 | Document the opt-out |

Tests touched: `tests/unit/models/test_ticket_models.py`, `tests/unit/services/test_ticket_service.py`, `tests/unit/mcp/test_ticket_tools.py`, `tests/integration/db/test_tickets_roundtrip.py`.

**Task order:** 0 → 1 → 2 → 3 → 4 → 5 (Tasks 3 & 4 consume the field added in Task 1; Task 2 is independent but grouped here).

---

## Task 0: Worktree environment (collision-safe)

**Why:** This worktree lives under `.claude/worktrees/`. The repo-root `.venv` is a **shared editable install pointing at the *main* tree's `src`** — the roadmap session uses it. Running `pip install -e` into it (or otherwise) from here would repoint `brain_v42` and disrupt that session. So build a **dedicated venv** for this worktree.

- [ ] **Step 1: Create an isolated venv + install**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42/.claude/worktrees/ticket-extract-opt-out
python3.12 -m venv .venv-wt
source .venv-wt/bin/activate
pip install -e ".[dev]"
```

Note: `.claude/worktrees/` is not tracked by the parent repo, so `.venv-wt` cannot pollute git status. No `.gitignore` change needed.

- [ ] **Step 2: Verify a clean baseline (pre-change, must be green)**

Run:
```bash
python -m pytest tests/unit/models/test_ticket_models.py tests/unit/services/test_ticket_service.py tests/unit/mcp/test_ticket_tools.py -q
```
Expected: all pass, 0 failures. This confirms the dedicated venv imports **this worktree's** `brain_v42`.

- [ ] **Step 3: No commit** (env only, nothing tracked changed).

---

## Task 1: `TicketCreate` carries an optional `extraction_status`

**Files:**
- Modify: `src/brain_v42/models/ticket.py:143-144` (the `TicketCreate` class)
- Test: `tests/unit/models/test_ticket_models.py`

**Interfaces:**
- Produces: `TicketCreate(..., extraction_status: ExtractionStatus | None = None)` — defaults to `None`. `ExtractionStatus` is already defined in the same module.

- [ ] **Step 1: Write the failing tests** — append to `class TestTicketCreate` in `tests/unit/models/test_ticket_models.py` (`ExtractionStatus` is already imported there):

```python
    def test_extraction_status_defaults_none(self):
        t = TicketCreate(
            kind=TicketKind.FYI,
            title="t",
            body="b",
            from_project="red-lab-factory",
            to_project="brain-v42",
        )
        assert t.extraction_status is None

    def test_accepts_skipped_opt_out(self):
        t = TicketCreate(
            kind=TicketKind.FYI,
            title="job factory terminé",
            body="b",
            from_project="red-lab-factory",
            to_project="brain-v42",
            extraction_status=ExtractionStatus.SKIPPED,
        )
        assert t.extraction_status is ExtractionStatus.SKIPPED
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/models/test_ticket_models.py -q -k "extraction_status_defaults_none or accepts_skipped_opt_out"`
Expected: FAIL — `AttributeError: 'TicketCreate' object has no attribute 'extraction_status'` (Pydantic drops the unknown kwarg).

- [ ] **Step 3: Write minimal implementation** — in `src/brain_v42/models/ticket.py`, replace the empty `TicketCreate` body:

```python
class TicketCreate(TicketBase):
    extraction_status: ExtractionStatus | None = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/models/test_ticket_models.py -q`
Expected: PASS (whole file, no regression).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/models/ticket.py tests/unit/models/test_ticket_models.py
git commit -m "feat(tickets): TicketCreate porte extraction_status optionnel (opt-out)"
```

---

## Task 2: Terminal transition preserves a pre-set `'skipped'`

**Files:**
- Modify: `src/brain_v42/services/ticket_service.py:135-137`
- Test: `tests/unit/services/test_ticket_service.py`

**Interfaces:**
- Consumes: `Ticket.extraction_status`, `ExtractionStatus.SKIPPED` (both already imported in the service).
- **The bug this prevents:** without the guard, a ticket created `'skipped'` gets flipped to `'pending'` the moment it reaches a terminal state (close/ack) — silently defeating the opt-out.

- [ ] **Step 1: Write the failing tests** — append to `class TestTransition` in `tests/unit/services/test_ticket_service.py` (`ExtractionStatus` and the `_ticket`/`_svc` helpers already exist; `_ticket(**kw)` forwards `extraction_status` to the `Ticket`):

```python
    async def test_terminal_transition_preserves_preset_skipped(self):
        # Opt-out: a ticket created 'skipped' must NOT flip back to 'pending' on closing.
        svc, repo, _ = _svc(
            ticket=_ticket(
                status=TicketStatus.RESOLVED, extraction_status=ExtractionStatus.SKIPPED
            )
        )
        updated = await svc.transition(uuid4(), FROM, "confirm")
        assert updated.status is TicketStatus.CLOSED
        assert (
            repo.apply_transition.await_args.kwargs["extraction_status"]
            is ExtractionStatus.SKIPPED
        )

    async def test_ack_preserves_preset_skipped(self):
        svc, repo, _ = _svc(
            ticket=_ticket(kind=TicketKind.FYI, extraction_status=ExtractionStatus.SKIPPED)
        )
        await svc.transition(uuid4(), TO, "ack")
        assert (
            repo.apply_transition.await_args.kwargs["extraction_status"]
            is ExtractionStatus.SKIPPED
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/services/test_ticket_service.py -q -k "preserves_preset_skipped"`
Expected: FAIL — the two asserts get `ExtractionStatus.PENDING` (current code sets PENDING unconditionally).

- [ ] **Step 3: Write minimal implementation** — in `ticket_service.py`, change the terminal branch (currently lines 135-137):

```python
        if new_status in TERMINAL_STATUSES:
            closed_at = now
            # Respect an opt-out set at creation: do not overwrite 'skipped'.
            if ticket.extraction_status is not ExtractionStatus.SKIPPED:
                extraction = ExtractionStatus.PENDING
```

(`extraction` is already initialised to `ticket.extraction_status` at line 129, so a skipped ticket keeps its value.)

- [ ] **Step 4: Run to verify it passes (and no regression)**

Run: `python -m pytest tests/unit/services/test_ticket_service.py -q`
Expected: PASS — including the existing `test_confirm_by_requester_closes_and_marks_extraction` and `test_ack_fyi_marks_extraction_pending` (default `None` → still PENDING).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/services/ticket_service.py tests/unit/services/test_ticket_service.py
git commit -m "feat(tickets): la fermeture préserve un extraction_status 'skipped' pré-posé"
```

---

## Task 3: Repo persists `extraction_status` at create (integration)

**Files:**
- Modify: `src/brain_v42/repositories/pg_ticket.py:33-39` (the `values` dict in `create()`)
- Test: `tests/integration/db/test_tickets_roundtrip.py`

**Interfaces:**
- Consumes: `TicketCreate.extraction_status` (Task 1).
- DB-gated: requires `BRAIN_V42_TEST_DB_URL` (skips otherwise), drives `alembic upgrade head`.

- [ ] **Step 1: Write the failing test** — append to `tests/integration/db/test_tickets_roundtrip.py` (imports `ExtractionStatus`, `TicketCreate`, `TicketKind`, `PgTicketRepo`, `require_test_db_url`, `_run_alembic_upgrade` already present):

```python
async def test_create_skipped_persists_and_excluded_from_pending_scan():
    """Un ticket créé 'skipped' persiste et n'entre jamais dans le scan extract."""
    db_url = require_test_db_url()
    _run_alembic_upgrade(db_url)
    engine = create_async_engine(db_url)
    try:
        sf = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgTicketRepo(sf)

        created = await repo.create(
            TicketCreate(
                kind=TicketKind.FYI,
                title="job factory #123 terminé",
                body="bruit opérationnel volumineux",
                from_project="red-shrik",
                to_project="red-data",
                extraction_status=ExtractionStatus.SKIPPED,
            )
        )
        assert created.extraction_status is ExtractionStatus.SKIPPED

        refreshed = await repo.get_by_id(created.id)
        assert refreshed is not None
        assert refreshed.extraction_status is ExtractionStatus.SKIPPED

        # End-to-end opt-out: the extract job's fetch never sees it.
        from scripts.ticket_extract import fetch_pending_threads

        pending = await fetch_pending_threads(sf, limit=100)
        assert all(t.id != created.id for t in pending)
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run to verify it fails**

Run: `BRAIN_V42_TEST_DB_URL=<test-db-url> python -m pytest tests/integration/db/test_tickets_roundtrip.py -q -k "skipped_persists"`
Expected: FAIL — `create()` does not INSERT `extraction_status`, so the column is NULL → `created.extraction_status is None`, assert fails.
(If `BRAIN_V42_TEST_DB_URL` is unset the test is skipped — that is not a pass; set it.)

- [ ] **Step 3: Write minimal implementation** — in `pg_ticket.py`, extend the `values` dict in `create()`:

```python
        values = {
            "kind": data.kind.value,
            "title": data.title,
            "body": data.body,
            "from_project": data.from_project,
            "to_project": data.to_project,
            "extraction_status": (
                data.extraction_status.value if data.extraction_status else None
            ),
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `BRAIN_V42_TEST_DB_URL=<test-db-url> python -m pytest tests/integration/db/test_tickets_roundtrip.py -q`
Expected: PASS (both existing roundtrips + the new one).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/repositories/pg_ticket.py tests/integration/db/test_tickets_roundtrip.py
git commit -m "feat(tickets): persiste extraction_status à la création (repo)"
```

---

## Task 4: `brain_ticket_create` exposes `extraction='skipped'`

**Files:**
- Modify: `src/brain_v42/mcp/tools/ticket_tools.py:19-26` (imports) and `:99-139` (`brain_ticket_create`)
- Test: `tests/unit/mcp/test_ticket_tools.py`

**Interfaces:**
- Consumes: `TicketCreate.extraction_status` (Task 1), `ExtractionStatus.SKIPPED`.
- Produces: tool param `extraction: str | None = None`; only `'skipped'` is accepted, anything else returns a `format_error` string.

- [ ] **Step 1: Write the failing tests** — first add `ExtractionStatus` to the model import block at the top of `tests/unit/mcp/test_ticket_tools.py`:

```python
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketGroups,
    TicketKind,
    TicketMessage,
    TicketStatus,
)
```

Then append to `class TestCreate`:

```python
    async def test_create_with_extraction_skipped(self):
        svc = MagicMock()
        svc.create = AsyncMock(return_value=_ticket())
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="fyi",
            title="job done",
            body="b",
            extraction="skipped",
        )
        assert result.startswith("ok ")
        data = svc.create.await_args.args[0]
        assert data.extraction_status is ExtractionStatus.SKIPPED

    async def test_create_invalid_extraction_rejected(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="fyi",
            title="t",
            body="b",
            extraction="garbage",
        )
        assert result.startswith("✗")
        assert "skipped" in result
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/mcp/test_ticket_tools.py -q -k "extraction"`
Expected: FAIL — `TypeError: brain_ticket_create() got an unexpected keyword argument 'extraction'`.

- [ ] **Step 3: Write minimal implementation** — in `ticket_tools.py`:

Add `ExtractionStatus` to the `from brain_v42.models.ticket import (...)` block. Then update the tool (bump `version` to `"1.1"` since the surface changed):

```python
    @mcp.tool(version="1.1")
    async def brain_ticket_create(
        from_project: str,
        to_project: str,
        kind: str,
        title: str,
        body: str,
        extraction: str | None = None,
    ) -> str:
        """Open a cross-project ticket addressed to another project.

        kind='request': ask the target project to do something — full loop
        (they resolve, you confirm). kind='fyi': heads-up needing only an
        ack (e.g. contract change). The target sees it at its next
        brain_session_start. Both project keys must already exist.

        extraction='skipped' opts this ticket out of the nightly
        knowledge-extraction job — use it for high-volume operational/job
        tickets (e.g. a factory daemon) that are noise, not durable knowledge.
        """
        if kind not in _VALID_KINDS:
            return format_error(f"Invalid kind '{kind}'. Valid: {list(_VALID_KINDS)}")
        if extraction is not None and extraction != ExtractionStatus.SKIPPED.value:
            return format_error(
                f"Invalid extraction '{extraction}'. Only 'skipped' (opt-out) or omit."
            )
        try:
            data = TicketCreate(
                kind=TicketKind(kind),
                title=title,
                body=body,
                from_project=from_project,
                to_project=to_project,
                extraction_status=(ExtractionStatus.SKIPPED if extraction else None),
            )
            ticket = await ticket_svc.create(data)
        except (TicketError, ValidationError) as exc:
            return format_error(str(exc))
        logger.info(
            "mcp.brain_ticket_create",
            ticket_id=str(ticket.id),
            kind=kind,
            to_project=ticket.to_project,
        )
        return format_confirmation(
            "Ticket created",
            title,
            id=str(ticket.id),
            kind=kind,
            to=ticket.to_project,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/mcp/test_ticket_tools.py -q`
Expected: PASS (whole file — existing create tests omit `extraction`, default `None`, unchanged behaviour).

- [ ] **Step 5: Commit**

```bash
git add src/brain_v42/mcp/tools/ticket_tools.py tests/unit/mcp/test_ticket_tools.py
git commit -m "feat(tickets): brain_ticket_create accepte extraction='skipped' (opt-out job tickets)"
```

---

## Task 5: Document the opt-out (spec §6)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md` (§6 — extraction)

- [ ] **Step 1: Add a short subsection** under §6 describing:
  - `brain_ticket_create(extraction='skipped')` sets `extraction_status='skipped'` at creation.
  - The terminal-transition side-effect preserves it (does **not** flip to `'pending'`).
  - The extract job filters `WHERE extraction_status='pending'`, so skipped tickets are excluded end-to-end.
  - Rationale: high-volume operational/job tickets (e.g. `red-lab-factory` daemon) are noise, not durable knowledge — keep them out of the nightly LLM scan.
  - No migration: column + CHECK already allowed `'skipped'` since migration 028.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md
git commit -m "docs(tickets): documente l'opt-out extraction='skipped' (§6)"
```

---

## Final Gate (run before declaring done)

- [ ] Full unit suite: `python -m pytest tests/unit -q` → green
- [ ] Integration (DB available): `BRAIN_V42_TEST_DB_URL=<url> python -m pytest tests/integration/db/test_tickets_roundtrip.py -q` → green
- [ ] `ruff check src/ tests/` → clean
- [ ] `ruff format --check src/ tests/` → clean (CI runs this exact check)
- [ ] `mypy src/` → clean
- [ ] Reply/reopen ticket `#0de7f408` to red-lab: opt-out shipped, `extraction='skipped'` live.

## Self-Review Notes

- **Design coverage (ticket Q3):** (a) create-time `extraction='skipped'` → Tasks 1+4; (b) terminal transition doesn't overwrite skipped → Task 2; persistence → Task 3; extract exclusion → existing `WHERE extraction_status='pending'`, asserted in Task 3; no migration → confirmed against `028_tickets.py`.
- **Naming consistency:** field `extraction_status` (model/repo/service/tests); tool param `extraction` (string, only `'skipped'`) → maps to `ExtractionStatus.SKIPPED` (`.value == "skipped"`). Intentional and consistent.
- **No new env var, no migration, no graph/embedding impact** (tickets are the coordination family — out of search/decay/graph).
