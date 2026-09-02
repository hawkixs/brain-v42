# Roadmap Step Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Revision 2 (post-review by 3 judges): stdout flush under redirection (HIGH),
> refresh of duplicates so the WET flip isn't inert (HIGH), dedup extended to
> `rejected` (MEDIUM x2), RED tests for the `_run` loop and the rotation
> wiring (MEDIUM x2), lines >100 chars fixed (HIGH CI), accents in commit
> messages, index [i/N] also on failed batches.

**Goal:** Harden the dream's roadmap step (proposer-only) following the 6 findings
from the verification of the first dry run (2026-07-04): run at 597s/600s of budget,
starvation from the alphabetical cap (3/26 projects served, 16 never scanned), 25%
no-op proposals, PG password in cleartext in the log, guaranteed inter-night
duplicates, streak counting rows instead of nights.

**Architecture:** The `scripts/roadmap_curate.py` CLI keeps its shape (batch per
project, one LLM call per project, proposer-only). We add three pure, testable
functions (`drop_noops`, `rotate_keys`, `batch_allowance`) and restructure the
propose loop of `_run` into **incremental persist**: each batch is
validated/filtered/persisted as soon as it comes back from the LLM, with a
progress line flushed per batch — a timeout now only loses the batch in
progress, and the log stays diagnosable. The inter-night dedup returns the ids
of `proposed` duplicates (refresh) so that the future nightly wet run also
applies what has accumulated in dry. Two one-off fixes outside the CLI:
`_clean_dry_streak` (DISTINCT run_date) and the engine-init log (masked
password). The shell budget goes from 10m to 20m.

**Tech Stack:** Python 3.12+ (local venv = uv py3.14), SQLAlchemy 2.0 async
(`sa.text` mode + Table core in scripts/), pytest + pytest-asyncio, structlog,
bash (dream.sh), ruff + mypy.

## Global Constraints

- **TDD mandatory**: each code step follows Red → Green (a test that fails BEFORE the implementation). NEVER modify an existing test to make code pass — except for an explicit spec change (Task 7: pinning the timeout, synth precedent >=15m).
- **Atomic commits, Conventional Commits**, messages in French WITH accents, as in the history (`dédup`, `hermétique`…).
- **Green before every commit**: `env -u VIRTUAL_ENV uv run pytest tests/unit -q` + `env -u VIRTUAL_ENV uv run ruff check src/ tests/ scripts/` + `env -u VIRTUAL_ENV uv run ruff format --check src/ tests/ scripts/` + `env -u VIRTUAL_ENV uv run mypy src/`. ruff line-length = 100 — run `ruff format` on touched files before committing.
- **`env -u VIRTUAL_ENV`** systematically in front of `uv run`: the shell can inherit a VIRTUAL_ENV from another project (incident 2026-07-04 — uv sync of the wrong venv). NEVER use `uv run --active`.
- **mypy does NOT cover `scripts/`** (project config): in script tests, follow the existing conventions of `tests/unit/test_roadmap_curate_apply.py` (`MagicMock(spec=AsyncSession)`, `@asynccontextmanager` factories).
- **Two `persist_proposals` twins** exist: `scripts/ticket_extract.py` AND `scripts/roadmap_curate.py` (copied skeleton). This plan touches ONLY the one in `roadmap_curate.py`. Do not edit `ticket_extract.py`.
- **Test pins not to break**: `tests/unit/test_dream_sh_roadmap.py` (updated only in Task 7), `tests/unit/test_dream_sh_phase_timeouts.py` (untouched — it only pins the claude PHASES array, not the roadmap step).
- Blast radius (GitNexus, verified): `persist_proposals` (roadmap) → `_run` + apply tests; `_clean_dry_streak` → `killswitch_state` → briefing (golden integration tests `tests/integration/test_session_start_briefing.py` — do not break them; they only run if the test DB is up).
- Do NOT commit `AGENTS.md` / `CLAUDE.md` if they appear modified (out of scope).

## File Structure

| File | Role in this project |
|---|---|
| `src/brain_v42/services/dream_run_service.py` | Task 1 — streak DISTINCT run_date (`_clean_dry_streak` method, lines ~118-137) |
| `tests/unit/services/test_dream_run_service.py` | Task 1 — new same-day streak test |
| `src/brain_v42/db/engine.py` | Task 2 — password masking, line 48 |
| `tests/unit/db/test_engine.py` | Task 2 — capture_logs test |
| `scripts/roadmap_curate.py` | Tasks 3-6 — `drop_noops`, `PersistResult` + dedup, `rotate_keys`, `batch_allowance`, incremental `_run` loop |
| `tests/unit/test_roadmap_curate.py` | Tasks 3, 5, 6 — pure function tests + rotation wiring + `_run` loop |
| `tests/unit/test_roadmap_curate_apply.py` | Task 4 — dedup persist tests ("apply/persist" test file) |
| `scripts/dream.sh` | Task 7 — `timeout 10m` → `timeout 20m` (line ~535) |
| `tests/unit/test_dream_sh_roadmap.py` | Task 7 — updated pin |

Execution order: 1 → 2 → 3 → 4 → 5 → 6 → 7. Tasks 1 and 2 are independent of
everything. Tasks 3-4 deliver bricks wired in minimally; task 6 restructures
the loop building on 3+4+5. Task 7 is pure shell.

---

### Task 1: Clean-dry streak — count distinct nights

Today the streak counts `done+dry` **rows**; a manual run on the same day as
the nightly one therefore counts as one more "night". Skewed WET flip
criterion. Fix: `COUNT(DISTINCT run_date)`.

**Files:**
- Modify: `src/brain_v42/services/dream_run_service.py:128-137` (method `DreamRunService._clean_dry_streak`)
- Test: `tests/unit/services/test_dream_run_service.py` (class `TestKillswitchState`)

**Interfaces:**
- Consumes: nothing (leaf task).
- Produces: `_clean_dry_streak` keeps its signature `(self, session, phase: str) -> int` — only the semantics of the COUNT change (distinct nights).

- [ ] **Step 1: Write the failing test**

In `tests/unit/services/test_dream_run_service.py`, add to the
`TestKillswitchState` class (after `test_roadmap_enabled_dry_with_streak`, line ~180),
reusing the module's `_insert_run` helper (kwargs: `run_date`, `phase`,
`status`, `phase_dry_run`):

```python
    @pytest.mark.asyncio
    async def test_streak_counts_distinct_nights_not_rows(self, session_factory):
        """Un re-run manuel le même jour ne doit PAS gonfler le streak (finding 2026-07-04)."""
        d = date.today()
        for _ in range(2):  # nightly + run manuel le même jour
            await _insert_run(
                session_factory, run_date=d, phase="roadmap", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state()
        assert state.roadmap_clean_dry_nights == 1
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/services/test_dream_run_service.py::TestKillswitchState::test_streak_counts_distinct_nights_not_rows -v`
Expected: FAIL — `assert 2 == 1` (the current COUNT counts both rows).

- [ ] **Step 3: Minimal implementation**

In `src/brain_v42/services/dream_run_service.py`, method `_clean_dry_streak`,
replace:

```python
        stmt = (
            sa.select(sa.func.count())
            .select_from(t)
            .where(t.c.phase == phase)
            .where(t.c.status == "done")
            .where(t.c.phase_dry_run.is_(True))
        )
```

with:

```python
        # DISTINCT run_date: a manual re-run on the same day is not one
        # more "night" (finding, roadmap verification, 2026-07-04).
        stmt = (
            sa.select(sa.func.count(sa.func.distinct(t.c.run_date)))
            .select_from(t)
            .where(t.c.phase == phase)
            .where(t.c.status == "done")
            .where(t.c.phase_dry_run.is_(True))
        )
```

- [ ] **Step 4: Verify green (new test + module non-regression)**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/services/test_dream_run_service.py -v`
Expected: PASS everywhere (existing tests insert 1 row/date → unchanged).

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff check src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
env -u VIRTUAL_ENV uv run ruff format --check src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
env -u VIRTUAL_ENV uv run mypy src/
git add src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
git commit -m "fix(dream): streak clean-dry compte les nuits distinctes, pas les rows"
```

---

### Task 2: Mask the PG password in the engine-init log

`engine.py:48` logs the raw `settings.postgres_url` → `postgresql+asyncpg://brain:brain@…`
ends up in `logs/dream/*_roadmap.log` (and everywhere else). Fix: log the URL
rendered by SQLAlchemy with `hide_password=True`.

**Files:**
- Modify: `src/brain_v42/db/engine.py:48`
- Test: `tests/unit/db/test_engine.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (log content change only).

- [ ] **Step 1: Write the failing test**

In `tests/unit/db/test_engine.py` (the autouse fixtures `reset_engine_singletons`
and `mock_settings` already apply), add at the end of the file:

```python
def test_engine_log_masks_password(mock_settings):
    """Le DSN loggé ne doit pas contenir le password (finding 2026-07-04)."""
    from structlog.testing import capture_logs

    mock_settings.postgres_url = "postgresql+asyncpg://brain:s3cret@localhost:5433/brain"
    from brain_v42.db.engine import get_engine

    with capture_logs() as logs:
        get_engine()

    created = [e for e in logs if e["event"] == "SQLAlchemy async engine created"]
    assert len(created) == 1
    assert "s3cret" not in created[0]["url"]
    assert "***" in created[0]["url"]
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/db/test_engine.py::test_engine_log_masks_password -v`
Expected: FAIL — `assert 's3cret' not in 'postgresql+asyncpg://brain:s3cret@…'`.

- [ ] **Step 3: Minimal implementation**

In `src/brain_v42/db/engine.py`, replace line 48:

```python
        logger.info("SQLAlchemy async engine created", url=settings.postgres_url)
```

with:

```python
        logger.info(
            "SQLAlchemy async engine created",
            url=_engine.url.render_as_string(hide_password=True),
        )
```

(`AsyncEngine.url` is a `sqlalchemy.URL`; `render_as_string(hide_password=True)`
renders `postgresql+asyncpg://brain:***@localhost:5433/brain`.)

- [ ] **Step 4: Verify green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/db/test_engine.py -v`
Expected: PASS everywhere.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff check src/brain_v42/db/engine.py tests/unit/db/test_engine.py
env -u VIRTUAL_ENV uv run ruff format --check src/brain_v42/db/engine.py tests/unit/db/test_engine.py
env -u VIRTUAL_ENV uv run mypy src/
git add src/brain_v42/db/engine.py tests/unit/db/test_engine.py
git commit -m "fix(db): masquer le password PG dans le log d'init engine"
```

---

### Task 3: `drop_noops` — discard proposals without effect

First real run: 8 `status` deployed→deployed proposals + 2 `rename` proposals
identical to the current name = 25% of the cap burned. `parse_and_validate`
validates the *shape*; we add an *effect* filter applied after validation, per
batch, with a log of the drop (never silent). We do NOT raise an error (an
error would trigger the LLM's corrective re-prompt — a waste for a no-op).

**Files:**
- Modify: `scripts/roadmap_curate.py` (new function after `parse_and_validate`, line ~221; wired into the `_run` aggregation loop, lines ~676-688)
- Test: `tests/unit/test_roadmap_curate.py` (new class `TestDropNoops`)

**Interfaces:**
- Consumes: `CurationDraft`, `ProjectBatch`, `FeatureCard` (existing module dataclasses).
- Produces: `drop_noops(drafts: list[CurationDraft], batch: ProjectBatch) -> tuple[list[CurationDraft], list[CurationDraft]]` — returns `(kept, dropped)`. Task 6 rewires this call into the incremental loop.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_roadmap_curate.py`, add the class (complete the top-level
import `from scripts.roadmap_curate import …` with `drop_noops` — and
`CurationDraft`, `FeatureCard`, `ProjectBatch`, `from uuid import UUID` if they
are missing):

```python
class TestDropNoops:
    def _batch_one(self, *, name="Feature A", status="research", pinned=False):
        fid = UUID("11111111-1111-1111-1111-111111111111")
        return fid, ProjectBatch(
            project_key="p",
            features=[FeatureCard(id=fid, name=name, status=status, pinned=pinned)],
        )

    def test_status_identical_is_dropped(self):
        fid, batch = self._batch_one(status="deployed")
        drafts = [
            CurationDraft(
                op="status", feature_id=fid, payload={"status": "deployed"}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_status_different_is_kept(self):
        fid, batch = self._batch_one(status="research")
        drafts = [
            CurationDraft(op="status", feature_id=fid, payload={"status": "done"}, rationale="r")
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_rename_identical_modulo_whitespace_is_dropped(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(
                op="rename", feature_id=fid, payload={"name": "  Feature A "}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_rename_different_is_kept(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(
                op="rename", feature_id=fid, payload={"name": "Feature B"}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_archive_and_merge_never_noop(self):
        fid, batch = self._batch_one()
        drafts = [
            CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r"),
            CurationDraft(op="merge", feature_id=fid, payload={"into": str(fid)}, rationale="r"),
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 2 and dropped == []
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestDropNoops -v`
Expected: FAIL — `ImportError: cannot import name 'drop_noops'`.

- [ ] **Step 3: Minimal implementation**

In `scripts/roadmap_curate.py`, right after the end of `parse_and_validate`
(after the `return drafts` line, ~221):

```python
def drop_noops(
    drafts: list[CurationDraft], batch: ProjectBatch
) -> tuple[list[CurationDraft], list[CurationDraft]]:
    """Écarte les proposals sans effet — status identique, rename identique.

    Premier run réel (2026-07-04) : 10/40 proposals étaient des no-ops qui
    brûlaient le cap. Filtre d'effet post-validation ; on ne raise pas (un
    raise déclencherait le re-prompt correctif LLM pour un simple no-op).
    """
    by_id = {f.id: f for f in batch.features}
    kept: list[CurationDraft] = []
    dropped: list[CurationDraft] = []
    for draft in drafts:
        feature = by_id[draft.feature_id]
        is_noop = (
            draft.op == "status" and draft.payload.get("status") == feature.status
        ) or (
            draft.op == "rename"
            and str(draft.payload.get("name", "")).strip() == feature.name.strip()
        )
        (dropped if is_noop else kept).append(draft)
    return kept, dropped
```

- [ ] **Step 4: Verify green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py -v`
Expected: PASS everywhere.

- [ ] **Step 5: Wire into `_run` (current aggregation loop)**

In `scripts/roadmap_curate.py`, loop `for outcome in outcomes:` (~line 679),
replace:

```python
        if not outcome.drafts:
            skipped += 1
        all_drafts.extend(outcome.drafts)
```

with:

```python
        kept, noops = drop_noops(outcome.drafts, outcome.batch)
        if noops:
            print(f"~ projet {outcome.batch.project_key}: {len(noops)} no-op droppées")
        if not kept:
            skipped += 1
        all_drafts.extend(kept)
```

- [ ] **Step 6: Re-verify full green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit -q`
Expected: PASS (no existing test pins the no-op behavior).

- [ ] **Step 7: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): drop des proposals no-op (status/rename sans effet)"
```

---

### Task 4: Inter-night dedup in `persist_proposals` (roadmap)

In dry mode, features don't move → every night re-inserts ~40 near-identical
proposals. Fix: before each INSERT, look for an identical row
(op + feature_id + payload, semantic JSONB equality) with status `proposed` OR
`rejected`:

- `proposed` duplicate → skip the INSERT but **return its id** ("refreshed") —
  without this, the future WET flip would be inert: wet only applies the run's
  ids, and dedup would prevent proposals accumulated in dry from showing up
  there;
- `rejected` duplicate → skip for good (a proposal rejected in review must not
  resurrect on every rotation cycle), counted for the log.

The return value becomes a `PersistResult` (dataclass, module style).

⚠️ **`scripts/roadmap_curate.py` ONLY** — do not touch the twin in
`scripts/ticket_extract.py`.

**Files:**
- Modify: `scripts/roadmap_curate.py:351-373` (function `persist_proposals` + new dataclass `PersistResult`) + call site in `_run` (~line 699) + wet block (~line 708)
- Test: `tests/unit/test_roadmap_curate_apply.py` (class `TestPersistProposals`)

**Interfaces:**
- Consumes: `CurationDraft`, table `roadmap_curation_proposals` (existing function-local import).
- Produces:
  ```python
  @dataclass
  class PersistResult:
      inserted: list[int]        # ids insérés ce run
      refreshed: list[int]       # ids des doublons 'proposed' re-proposés ce run
      rejected_skipped: int      # doublons 'rejected' écartés
  ```
  `persist_proposals(session_factory, drafts) -> PersistResult`. Task 6 builds on
  this return value; wet applies `inserted + refreshed`.

- [ ] **Step 1: Adapt the existing test + write dedup tests (RED)**

In `tests/unit/test_roadmap_curate_apply.py`: add `CurationDraft` and
`PersistResult` to the existing top-level import
(`from scripts.roadmap_curate import apply_proposals, persist_proposals`), then
in the `TestPersistProposals` class:

1. Adapt the existing test to the new return value (spec change accepted,
   same commit as the implementation):

```python
    @pytest.mark.asyncio
    async def test_empty_drafts_noop(self):
        factory = MagicMock()
        res = await persist_proposals(factory, [])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 0
        factory.assert_not_called()
```

2. Add (reusing the module's `_session_with` helpers and the
   `MagicMock(spec=AsyncSession)` style; the dedup SELECT results expose
   `.first()`):

```python
    @pytest.mark.asyncio
    async def test_duplicate_proposed_is_refreshed_not_reinserted(self):
        """Doublon 'proposed' → pas d'INSERT, id retourné en refreshed (flip WET non inerte)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(123, "proposed"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [123] and res.rejected_skipped == 0
        assert session.execute.await_count == 1  # SELECT seulement, pas d'INSERT

    @pytest.mark.asyncio
    async def test_duplicate_rejected_is_skipped_for_good(self):
        """Doublon 'rejected' → ni INSERT ni refresh — pas de résurrection en review."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(55, "rejected"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 1
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_new_draft_is_inserted(self):
        """Pas de doublon → SELECT (None) puis INSERT (returning id)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_none = MagicMock()
        dup_none.first = MagicMock(return_value=None)
        factory, session = _session_with([dup_none, _scalar_one(7)])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [7] and res.refreshed == [] and res.rejected_skipped == 0
        assert session.execute.await_count == 2  # SELECT + INSERT
```

(Check the exact shape of `_session_with` / `_scalar_one` at the top of the
file — `_scalar_one(7)` must provide `.scalar_one()`; if the helper differs,
build the mock inline in the same style.)

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest "tests/unit/test_roadmap_curate_apply.py::TestPersistProposals" -v`
Expected: FAIL — `ImportError: cannot import name 'PersistResult'`.

- [ ] **Step 3: Implementation**

In `scripts/roadmap_curate.py`:

1. Dataclass, placed with the others (`BatchOutcome`, ~line 118):

```python
@dataclass
class PersistResult:
    """Résultat de persist_proposals — voir dedup inter-nuits (2026-07-04)."""

    inserted: list[int] = field(default_factory=list)
    refreshed: list[int] = field(default_factory=list)
    rejected_skipped: int = 0
```

2. Replace `persist_proposals` entirely:

```python
async def persist_proposals(session_factory: Any, drafts: list[CurationDraft]) -> PersistResult:
    """INSERT proposals status='proposed', en dédupliquant contre l'existant.

    Dedup inter-nuits (finding 2026-07-04) : en dry les features ne bougent
    pas, chaque nuit re-proposerait les mêmes ops. Une row identique
    (op + feature_id + payload, égalité JSONB sémantique) suffit à skipper :
    'proposed' → refresh (l'id est retourné, le wet du run l'applique) ;
    'rejected' → skip définitif (pas de résurrection en review).
    """
    from brain_v42.db.tables import roadmap_curation_proposals  # noqa: PLC0415

    result = PersistResult()
    if not drafts:
        return result
    t = roadmap_curation_proposals
    async with session_factory() as session:
        async with session.begin():
            for draft in drafts:
                dup_stmt = (
                    sa.select(t.c.id, t.c.status)
                    .where(
                        t.c.op == draft.op,
                        t.c.feature_id == draft.feature_id,
                        t.c.payload == draft.payload,
                        t.c.status.in_(("proposed", "rejected")),
                    )
                    # asc: 'proposed' < 'rejected' — if both exist,
                    # the refresh wins over the permanent skip.
                    .order_by(t.c.status)
                    .limit(1)
                )
                row = (await session.execute(dup_stmt)).first()
                if row is not None:
                    dup_id, dup_status = row
                    if dup_status == "proposed":
                        result.refreshed.append(dup_id)
                    else:
                        result.rejected_skipped += 1
                    continue
                stmt = (
                    t.insert()
                    .values(
                        op=draft.op,
                        feature_id=draft.feature_id,
                        payload=draft.payload,
                        rationale=draft.rationale,
                        status="proposed",
                    )
                    .returning(t.c.id)
                )
                result.inserted.append((await session.execute(stmt)).scalar_one())
    return result
```

3. Adapt the call site in `_run` (~line 699) — replace:

```python
    proposal_ids = await persist_proposals(sf, all_drafts)
```

with:

```python
    res = await persist_proposals(sf, all_drafts)
    proposal_ids = res.inserted
    if res.refreshed:
        print(f"~ {len(res.refreshed)} doublons déjà proposés — refresh (dédup inter-nuits)")
    if res.rejected_skipped:
        print(f"~ {res.rejected_skipped} déjà rejetées — non ré-insérées")
```

4. Adapt the wet block (~line 708) — wet applies inserted + refreshed:

```python
    # --wet: apply the run (inserted + refreshed — without the refreshed, the
    # dedup would make the WET flip inert). Restricted to safe ops.
    if args.wet and (proposal_ids or res.refreshed):
        applied = await apply_proposals(
            sf, proposal_ids + res.refreshed, allowed_ops=WET_APPLYABLE_OPS
        )
        print(f"wet: {applied} appliqués (ops {WET_APPLYABLE_OPS})")
```

- [ ] **Step 4: Verify green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate_apply.py tests/unit/test_roadmap_curate.py -v`
Expected: PASS everywhere.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
git commit -m "feat(roadmap): dédup inter-nuits — refresh des proposed, skip des rejected"
```

---

### Task 5: Deterministic rotation of scanned projects

`_KEYS_SQL` does `ORDER BY project_key LIMIT 10`: the 10 alphabetically first
projects are scanned every night, the other 16 never. Fix: a deterministic
sliding window per day — fetch ALL keys, rotate by `limit` positions per day
(`toordinal()`), full cycle in ⌈n/limit⌉ nights.

**Files:**
- Modify: `scripts/roadmap_curate.py` (`_KEYS_SQL` ~line 226, `fetch_project_batches` ~line 265, new function `rotate_keys`)
- Test: `tests/unit/test_roadmap_curate.py` (classes `TestRotateKeys` + `TestFetchRotationWiring`)

**Interfaces:**
- Consumes: nothing.
- Produces: `rotate_keys(keys: list[str], limit: int, day_ordinal: int) -> list[str]`; `fetch_project_batches(session_factory, limit, day_ordinal: int | None = None)` — extended, backward-compatible signature (None → `date.today().toordinal()`).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_roadmap_curate.py` (complete the top-level import with
`rotate_keys` and `fetch_project_batches`; also need `AsyncMock`, `MagicMock`,
`asynccontextmanager`, `AsyncSession` — check what the file already imports):

```python
class TestRotateKeys:
    def test_window_advances_by_limit_each_day(self):
        keys = [f"p{i:02d}" for i in range(26)]
        day0 = rotate_keys(keys, 10, day_ordinal=0)
        day1 = rotate_keys(keys, 10, day_ordinal=1)
        day2 = rotate_keys(keys, 10, day_ordinal=2)
        assert day0 == keys[0:10]
        assert day1 == keys[10:20]
        assert day2 == keys[20:26] + keys[0:4]  # wrap

    def test_full_cycle_covers_every_project(self):
        keys = [f"p{i:02d}" for i in range(26)]
        seen: set[str] = set()
        for day in range(3):  # ceil(26/10) = 3 nuits
            seen.update(rotate_keys(keys, 10, day_ordinal=day))
        assert seen == set(keys)

    def test_fewer_projects_than_limit_returns_all(self):
        keys = ["a", "b", "c"]
        assert sorted(rotate_keys(keys, 10, day_ordinal=5)) == keys
        assert len(rotate_keys(keys, 10, day_ordinal=5)) == 3

    def test_empty_keys(self):
        assert rotate_keys([], 10, day_ordinal=3) == []

    def test_deterministic_same_day(self):
        keys = [f"p{i}" for i in range(26)]
        assert rotate_keys(keys, 10, 7) == rotate_keys(keys, 10, 7)


class TestFetchRotationWiring:
    @pytest.mark.asyncio
    async def test_fetch_queries_only_rotated_window(self):
        """fetch_project_batches n'interroge que les projets de la fenêtre rotée."""
        keys_result = MagicMock()
        keys_result.all = MagicMock(return_value=[("a",), ("b",), ("c",)])
        empty_features = MagicMock()
        empty_features.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[keys_result, empty_features, empty_features])

        @asynccontextmanager
        async def factory():
            yield session

        batches = await fetch_project_batches(factory, limit=2, day_ordinal=1)
        assert batches == []  # features vides → batchs skippés
        # offset = (1*2) % 3 = 2 → rotated window = ['c', 'a']
        feature_calls = session.execute.await_args_list[1:]
        assert [call.args[1]["pk"] for call in feature_calls] == ["c", "a"]
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestRotateKeys tests/unit/test_roadmap_curate.py::TestFetchRotationWiring -v`
Expected: FAIL — `ImportError: cannot import name 'rotate_keys'`.

- [ ] **Step 3: Implementation**

In `scripts/roadmap_curate.py`:

1. `_KEYS_SQL` loses its LIMIT (replace the entire constant):

```python
_KEYS_SQL = """
SELECT DISTINCT project_key FROM features
WHERE status NOT IN ('done', 'archived') AND merged_into IS NULL
ORDER BY project_key
"""
```

2. New pure function right above `fetch_project_batches`:

```python
def rotate_keys(keys: list[str], limit: int, day_ordinal: int) -> list[str]:
    """Fenêtre glissante déterministe sur la liste (triée) des projets.

    Avance de `limit` positions par jour → cycle complet en ⌈n/limit⌉
    nuits, à liste stable ; si elle change entre nuits la couverture
    reste bornée (l'offset avance quand même chaque jour). Sans
    rotation, ORDER BY + LIMIT scannait les 10 premiers projets
    alphabétiques chaque nuit et jamais les 16 autres (2026-07-04).
    """
    if not keys:
        return []
    offset = (day_ordinal * limit) % len(keys)
    rotated = keys[offset:] + keys[:offset]
    return rotated[:limit]
```

3. `fetch_project_batches` — new signature and key selection
(replace the signature and the `keys = …` line):

```python
async def fetch_project_batches(
    session_factory: Any, limit: int, day_ordinal: int | None = None
) -> list[ProjectBatch]:
    """Batchs par projet : features vivantes (cap 30) + digests (cap 10/feature).

    La fenêtre de projets tourne chaque jour (rotate_keys) pour que tous
    les projets soient couverts en ⌈n/limit⌉ nuits.
    """
    if day_ordinal is None:
        day_ordinal = date.today().toordinal()
    async with session_factory() as session:
        all_keys = [r[0] for r in (await session.execute(sa.text(_KEYS_SQL))).all()]
        keys = rotate_keys(all_keys, limit, day_ordinal)
```

(the rest of the body — the `for pk in keys:` loop — is unchanged; the
`{"lim": limit}` parameter of the old execute disappears along with the LIMIT).

- [ ] **Step 4: Verify green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py tests/unit/test_roadmap_curate_apply.py -v`
Expected: PASS everywhere.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): rotation déterministe des projets scannés par nuit"
```

---

### Task 6: Fair-share of the cap + incremental persist + per-batch progress

The core of the hardening. Today: all LLM calls happen first, global
alphabetical-order truncation `[:40]` (3/26 projects served), a single persist
at the end (a timeout loses EVERYTHING), zero progress log (a timeout leaves an
empty log). After: a single loop — each batch is curated, filtered (no-ops),
capped at its fair share of the remaining cap, persisted (dedup), logged with
timing **and `flush=True`** (stdout is block-buffered under dream.sh's `>>`
redirection — without flush, a timeout SIGTERM would lose every progress line,
and the finding would go unnoticed in exactly the case it targets). An
exhausted cap breaks the loop (no more useless LLM calls).

**Files:**
- Modify: `scripts/roadmap_curate.py` — new function `batch_allowance` + restructuring of the `_run` propose block (lines ~652-717)
- Test: `tests/unit/test_roadmap_curate.py` (classes `TestBatchAllowance` + `TestRunProposeLoop`)

**Interfaces:**
- Consumes: `drop_noops` (Task 3), `persist_proposals -> PersistResult` (Task 4), `fetch_project_batches(sf, limit, day_ordinal)` (Task 5), `curate_batch`, `record_dream_run`, `MAX_PROPOSALS_PER_NIGHT`.
- Produces: `batch_allowance(remaining_cap: int, remaining_batches: int) -> int`; per-batch log format `[i/N] <project>: …` flushed (the morning-check will read these lines — failed batches keep their index `! [i/N] … failed:`).

- [ ] **Step 1: Write the failing tests (pure function)**

In `tests/unit/test_roadmap_curate.py` (add `batch_allowance` to the import):

```python
class TestBatchAllowance:
    def test_even_split(self):
        assert batch_allowance(40, 10) == 4

    def test_ceil_redistributes(self):
        assert batch_allowance(38, 9) == 5  # ceil — les slots non consommés se redistribuent

    def test_last_batch_gets_all_remaining(self):
        assert batch_allowance(7, 1) == 7

    def test_exhausted_cap(self):
        assert batch_allowance(0, 5) == 0

    def test_no_remaining_batches(self):
        assert batch_allowance(10, 0) == 0
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestBatchAllowance -v`
Expected: FAIL — `ImportError: cannot import name 'batch_allowance'`.

- [ ] **Step 3: Implement `batch_allowance`**

In `scripts/roadmap_curate.py`, under `rotate_keys`:

```python
def batch_allowance(remaining_cap: int, remaining_batches: int) -> int:
    """Part équitable du cap restant pour le prochain batch (ceil).

    Le ceil redistribue les slots non consommés par les batches
    précédents. Sans fair-share, la troncature globale [:cap] en ordre
    de batch servait 3 projets sur 26 (finding 2026-07-04).
    """
    if remaining_batches <= 0 or remaining_cap <= 0:
        return 0
    return -(-remaining_cap // remaining_batches)
```

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestBatchAllowance -v` → PASS.

- [ ] **Step 4: Write the RED tests for the `_run` loop**

Still in `tests/unit/test_roadmap_curate.py` — the restructured loop is the
core of this project, it has its own tests (monkeypatched collaborators; the
`Settings`/`get_session_factory` imports of `_run` are function-local, so we
monkeypatch their origin modules). Add:

```python
class TestRunProposeLoop:
    """Flux propose de _run — collaborateurs monkeypatchés, aucun I/O réel."""

    def _args(self, limit=10, wet=False):
        return SimpleNamespace(limit=limit, wet=wet, apply_ids=None, model=None, base_url=None)

    def _feature(self, fid):
        return FeatureCard(id=fid, name="F", status="research", pinned=False)

    def _outcome(self, batch, fid):
        draft = CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r")
        return BatchOutcome(batch=batch, drafts=[draft])

    def _hermetic(self, monkeypatch):
        monkeypatch.setattr("brain_v42.config.Settings", MagicMock())
        monkeypatch.setattr(
            "brain_v42.db.engine.get_session_factory", MagicMock(return_value=MagicMock())
        )
        import scripts.roadmap_curate as rc

        monkeypatch.setattr(rc, "record_dream_run", AsyncMock())
        return rc

    @pytest.mark.asyncio
    async def test_persist_called_per_batch_and_progress_flushed(self, monkeypatch, capsys):
        """Persist incrémental : un persist PAR batch + ligne [i/N] par batch."""
        rc = self._hermetic(monkeypatch)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        monkeypatch.setattr(
            rc,
            "curate_batch",
            AsyncMock(side_effect=[self._outcome(b1, fid1), self._outcome(b2, fid2)]),
        )
        persist = AsyncMock(
            side_effect=[rc.PersistResult(inserted=[1]), rc.PersistResult(inserted=[2])]
        )
        monkeypatch.setattr(rc, "persist_proposals", persist)

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert persist.await_count == 2  # incrémental — l'ancien design persistait 1 fois
        out = capsys.readouterr().out
        assert "[1/2] p1:" in out and "[2/2] p2:" in out

    @pytest.mark.asyncio
    async def test_cap_exhausted_skips_remaining_llm_calls(self, monkeypatch, capsys):
        """Cap épuisé → break AVANT l'appel LLM suivant, message explicite."""
        rc = self._hermetic(monkeypatch)
        monkeypatch.setattr(rc, "MAX_PROPOSALS_PER_NIGHT", 1)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        curate = AsyncMock(return_value=self._outcome(b1, fid1))
        monkeypatch.setattr(rc, "curate_batch", curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=rc.PersistResult(inserted=[1]))
        )

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert curate.await_count == 1  # le batch 2 n'est jamais envoyé au LLM
        assert "épuisé" in capsys.readouterr().out
```

Complete the file's imports: `from types import SimpleNamespace`,
`from unittest.mock import AsyncMock, MagicMock`, `from uuid import UUID, uuid4`,
and `BatchOutcome` + `PersistResult` in the `from scripts.roadmap_curate
import …` import (following the existing style).

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestRunProposeLoop -v`
Expected: FAIL — the old `_run` persists only once (`await_count == 1`) and
emits no `[i/N]` line.

- [ ] **Step 5: Restructure the `_run` propose block**

In `scripts/roadmap_curate.py`, replace the ENTIRE block from
`# Propose mode (dry ou wet).` through `return 1 if any_failed else 0` (end of
`_run`) with:

```python
    # Propose mode (dry or wet) — incremental persist batch by batch:
    # a shell timeout only loses the batch in progress, and the
    # per-batch progress log (flush=True: stdout is block-buffered
    # under dream.sh's >> redirection) keeps the night diagnosable
    # (finding 2026-07-04: 597s/600s, single final persist, empty log).
    # NB: a SIGTERM mid-batch leaves the night without a dream_runs row
    # (record_dream_run runs at the end) — mitigated by the 20m budget.
    batches = await fetch_project_batches(sf, args.limit)
    if not batches:
        print("Aucune feature vivante — rien à curer.", flush=True)
        await record_dream_run(
            sf, "done", dry=not args.wet, duration_s=time.monotonic() - t0, error=None
        )
        return 0

    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    all_ids: list[int] = []
    refreshed_ids: list[int] = []
    remaining_cap = MAX_PROPOSALS_PER_NIGHT
    scanned = 0
    skipped = 0
    failed = 0
    total = len(batches)
    try:
        for i, batch in enumerate(batches, 1):
            if remaining_cap <= 0:
                print(
                    f"! cap {MAX_PROPOSALS_PER_NIGHT} proposals/nuit épuisé — "
                    f"{total - i + 1} projets non traités ce soir "
                    f"(le cycle de rotation les resservira)",
                    flush=True,
                )
                break
            t_batch = time.monotonic()
            outcome = await curate_batch(http_client, model, batch)
            scanned += 1
            if outcome.failed:
                failed += 1
                any_failed = True
                error_msg = outcome.error
                print(f"! [{i}/{total}] {batch.project_key} failed: {outcome.error}", flush=True)
                continue
            kept, noops = drop_noops(outcome.drafts, batch)
            allowance = batch_allowance(remaining_cap, total - i + 1)
            to_persist, cap_dropped = kept[:allowance], kept[allowance:]
            res = await persist_proposals(sf, to_persist)
            remaining_cap -= len(res.inserted)
            all_ids.extend(res.inserted)
            refreshed_ids.extend(res.refreshed)
            if not res.inserted and not res.refreshed:
                skipped += 1
            if cap_dropped:
                print(
                    f"! projet {batch.project_key}: {len(cap_dropped)} proposals "
                    f"au-delà de la part de cap ({allowance}) — droppées "
                    f"(pas de troncature silencieuse)",
                    flush=True,
                )
            print(
                f"[{i}/{total}] {batch.project_key}: "
                f"{len(outcome.drafts)} drafts, {len(noops)} no-op, "
                f"{len(res.refreshed)} dup, {res.rejected_skipped} rej-skip, "
                f"{len(cap_dropped)} cap-drop, {len(res.inserted)} persistées "
                f"({time.monotonic() - t_batch:.0f}s)",
                flush=True,
            )
    finally:
        await http_client.aclose()

    print(
        f"{scanned} projets scannés, {len(all_ids)} proposals, "
        f"{skipped} sans proposition, {failed} failed",
        flush=True,
    )
    if all_ids:
        print(f"proposal ids: {all_ids}", flush=True)
    if refreshed_ids:
        print(f"déjà proposées (refresh): {refreshed_ids}", flush=True)

    # --wet: apply the run (inserted + refreshed — without the refreshed, the
    # dedup would make the WET flip inert). Restricted to safe ops. NEVER
    # merge/rename.
    if args.wet and (all_ids or refreshed_ids):
        applied = await apply_proposals(sf, all_ids + refreshed_ids, allowed_ops=WET_APPLYABLE_OPS)
        print(f"wet: {applied} appliqués (ops {WET_APPLYABLE_OPS})", flush=True)

    duration = time.monotonic() - t0
    status = "fail" if any_failed else "done"
    await record_dream_run(
        sf, status=status, dry=not args.wet, duration_s=duration, error=error_msg
    )
    return 1 if any_failed else 0
```

Implementation notes:
- The Task 4 wet block (variables `res`/`proposal_ids`) disappears with this
  restructuring — that's expected (double-touch handled: each commit stays green).
- The final summary line keeps EXACTLY its shape (`N projets scannés, …`) —
  the morning-check and any greps expect it.
- Accepted trade-off: the cap share is applied BEFORE the dedup — a batch full
  of duplicates persists less than its share and the remainder is
  redistributed via `remaining_cap` (the ceil in `batch_allowance` handles it).
- `skipped` counts projects with neither a persisted NOR a refreshed proposal —
  the "no proposal" summary semantics are preserved.

- [ ] **Step 6: Verify full green**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit -q`
Expected: PASS — including `TestRunProposeLoop` (RED at Step 4, GREEN now).

- [ ] **Step 7: Blank smoke test of the CLI (no LLM, no DB)**

Run: `env -u VIRTUAL_ENV uv run python -m scripts.roadmap_curate --limit 0 2>&1 | tail -1`
Expected: `roadmap_curate: error: argument --limit: doit être >= 1 (reçu : 0)` —
the module imports and parses (no SyntaxError/NameError introduced).

- [ ] **Step 8: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): fair-share du cap + persist incrémental + progression par batch"
```

---

### Task 7: Roadmap shell budget 10m → 20m

First real run: 597s for 10 projects under `timeout 10m` — 3s of margin. Same
archetype as the synth timeouts (bump 10→15 on 2026-05-03). Incremental persist
(Task 6) makes the timeout non-catastrophic, but the budget still needs
margin: 20m = ~100% headroom over the observed value. Pinned spec change:
test first (RED), then dream.sh (GREEN).

**Files:**
- Modify: `tests/unit/test_dream_sh_roadmap.py` (function `test_roadmap_step_has_timeout_and_own_log`)
- Modify: `scripts/dream.sh` (~line 535)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (shell config).

- [ ] **Step 1: Update the pin (RED)**

In `tests/unit/test_dream_sh_roadmap.py`, replace:

```python
def test_roadmap_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 10m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
```

with:

```python
def test_roadmap_step_has_timeout_and_own_log():
    """Budget 20m : premier run réel à 597s/600s (2026-07-04) — même
    archétype que les timeouts synth (bump 10→15 du 2026-05-03)."""
    content = _content()
    assert "timeout 20m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
```

- [ ] **Step 2: Verify the failure**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_dream_sh_roadmap.py -v`
Expected: FAIL — `test_roadmap_step_has_timeout_and_own_log` (dream.sh still contains `timeout 10m`).

- [ ] **Step 3: Bump in dream.sh (GREEN)**

In `scripts/dream.sh` (~line 535), replace:

```bash
  timeout 10m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
```

with:

```bash
  # 20m: first real run (2026-07-04) at 597s/600s — zero margin under 10m.
  # Pinned by tests/unit/test_dream_sh_roadmap.py.
  timeout 20m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
```

- [ ] **Step 4: Verify green (complete dream.sh pins)**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_dream_sh_roadmap.py tests/unit/test_dream_sh_phase_timeouts.py -v`
Expected: PASS everywhere.

- [ ] **Step 5: Gates + commit**

```bash
bash -n scripts/dream.sh
env -u VIRTUAL_ENV uv run ruff check tests/unit/test_dream_sh_roadmap.py
env -u VIRTUAL_ENV uv run ruff format --check tests/unit/test_dream_sh_roadmap.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/dream.sh tests/unit/test_dream_sh_roadmap.py
git commit -m "fix(dream): budget roadmap 10m→20m — premier run réel à 597s/600s"
```

---

## Final gate (before merge)

```bash
env -u VIRTUAL_ENV uv run pytest tests/unit -q
env -u VIRTUAL_ENV uv run ruff check src/ tests/ scripts/
env -u VIRTUAL_ENV uv run ruff format --check src/ tests/ scripts/
env -u VIRTUAL_ENV uv run mypy src/
bash -n scripts/dream.sh
```

Then a final whole-branch review (`git diff main..HEAD`) on the most capable
model — per-task reviews don't see the interactions across steps
(SDD learning 2026-07-04).
