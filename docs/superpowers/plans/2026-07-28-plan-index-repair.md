# Canonical Multi-Project Plan Index Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent relative plan scans and deliver a read-only-first, CAS-protected repair command for the seven polluted project indexes.

**Architecture:** Harden `PlanIndexer` at its filesystem boundary, then add a maintenance service that separates manifest validation, control snapshots, PostgreSQL transactions, and CLI presentation. The repair copies and verifies canonical rows before it can remove exact polluted rows; production mutation remains an operator action outside this delivery.

**Tech Stack:** Python 3.12, pathlib, dataclasses, Pydantic v2, SQLAlchemy 2 async, PostgreSQL 16, pytest, structlog, JSON.

## Global Constraints

- Scope is ticket `44ee7643-fb06-4186-a364-cb175610b973`; Dream is excluded.
- Accept only canonical absolute, existing, readable, searchable directories.
- Preserve every project-context field except `plan_scan_paths`.
- Default to read-only inventory; expose no one-step destructive shortcut.
- Require snapshot, backup receipt, restore-tested, writers-off, and schema-head gates before mutation.
- Use `BRAIN_V42_TEST_DB_URL` exclusively for database-backed tests; never fall back to production.
- Mask plan contents, embeddings, environment values, DSNs, credentials, and raw exception messages.
- Do not deploy, restart, merge, push, transition tickets, or run production apply/finalize in this mission.
- Run GitNexus upstream impact before editing every existing symbol and `gitnexus_detect_changes()` before every commit.

---

## File map

- `src/brain_v42/services/plan_indexer.py`: canonical scan-path boundary and project-scoped unchanged checks.
- `src/brain_v42/maintenance/plan_index_repair.py`: public repair models, manifest/filesystem inventory, snapshot and proof validation, orchestration.
- `src/brain_v42/maintenance/plan_index_repair_store.py`: PostgreSQL read-only inventory and bounded serializable mutations.
- `scripts/repair_plan_index.py`: argument parsing, phase dispatch, masked JSON output, and exit codes.
- `ops/recovery/plan-index-repair-v1.json`: explicit seven-project production manifest.
- `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`: operator sequence, gates, reindex evidence, rollback, and rollout boundary.
- `tests/unit/test_plan_indexer.py`: indexer regression tests.
- `tests/unit/test_plan_index_repair.py`: pure manifest, filesystem, snapshot, proof, and orchestration tests.
- `tests/unit/test_plan_index_repair_store.py`: SQL shape, CAS, transaction, finalize, and rollback tests.
- `tests/unit/test_repair_plan_index_cli.py`: CLI contract and secret-safe failure tests.
- `tests/integration/test_plan_index_repair.py`: isolated PostgreSQL transaction and rollback proof.

---

### Task 1: Reject unsafe scan paths and scope unchanged checks

**Files:**
- Modify: `src/brain_v42/services/plan_indexer.py:88-563`
- Modify: `tests/unit/test_plan_indexer.py`

**Interfaces:**
- Produces: `PlanScanPathError(path: str, reason_code: str)`.
- Produces: `PlanIndexer._canonical_scan_path(scan_path: str) -> str`.
- Preserves: `PlanIndexer.index_path(scan_path: str, project_key: str) -> dict[str, int]`.
- Preserves: `PlanIndexer.index_project(project_key: str) -> dict[str, int] | None`.

- [ ] **Step 1: Run upstream impact analysis**

Run GitNexus impact for `index_path`, `index_project`, `_is_unchanged`, and `_find_plan_files`, each with `direction="upstream"`, `file_path="src/brain_v42/services/plan_indexer.py"`, and `repo="/home/hawixs/hawkixs_infra/git_repo/brain_v42"`. Stop and report before editing if any result is HIGH or CRITICAL.

- [ ] **Step 2: Write failing path-boundary tests**

Add tests that establish the exact contracts:

```python
def test_canonical_scan_path_rejects_relative_path(mock_deps) -> None:
    indexer = _build_indexer(mock_deps)
    with pytest.raises(PlanScanPathError) as exc:
        indexer._canonical_scan_path("docs/plans")
    assert exc.value.reason_code == "relative"


@pytest.mark.asyncio
async def test_index_project_counts_invalid_path_without_scanning(mock_deps) -> None:
    ctx_row = MagicMock(plan_scan_paths=["docs/plans"])
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    indexer = _build_indexer(mock_deps)

    result = await indexer.index_project("red-phone")

    assert result == {
        "indexed": 0,
        "skipped": 0,
        "linked": 0,
        "errors": 1,
        "chunks_created": 0,
    }
```

Add cases for a missing path, a regular file, an unreadable directory by patched `os.access`, canonical persistence in `IndexedPlanCreate.file_path` and `metadata["source_file"]`, and a content-safe `plan_indexer.invalid_scan_path` log.

- [ ] **Step 3: Write failing ownership tests**

Extend `_is_unchanged` tests so the exact-path SQL includes
`indexed_plans.c.project_key == project_key`. Add a case where a relative duplicate row with the
same hash returns `False`, and an absolute live duplicate from the same project returns `True`.

- [ ] **Step 4: Run RED tests**

Run:

```bash
uv run pytest tests/unit/test_plan_indexer.py -q
```

Expected: the new tests fail because `PlanScanPathError`, `_canonical_scan_path`, canonical persistence, and the project predicate do not exist.

- [ ] **Step 5: Implement the minimal path gate**

Add the closed error type and helper:

```python
class PlanScanPathError(ValueError):
    def __init__(self, path: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.path = path
        self.reason_code = reason_code


@staticmethod
def _canonical_scan_path(scan_path: str) -> str:
    candidate = Path(scan_path)
    if not candidate.is_absolute():
        raise PlanScanPathError(scan_path, "relative")
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise PlanScanPathError(scan_path, "missing") from exc
    if not canonical.is_dir():
        raise PlanScanPathError(scan_path, "not_directory")
    if not os.access(canonical, os.R_OK | os.X_OK):
        raise PlanScanPathError(scan_path, "unreadable")
    return str(canonical)
```

Canonicalize at the start of `index_path()`. Catch only `PlanScanPathError` inside
`index_project()`, increment `errors`, and log `project_key`, `file_path`, and `reason_code`.
Add the `project_key` predicate to the exact lookup. Require `Path(existing_path).is_absolute()`
before the duplicate shortcut checks `is_file()`.

- [ ] **Step 6: Run GREEN and regression tests**

Run:

```bash
uv run pytest tests/unit/test_plan_indexer.py tests/unit/mcp/tools/test_plan_tools.py -q
uv run ruff check src/brain_v42/services/plan_indexer.py tests/unit/test_plan_indexer.py
uv run ruff format --check src/brain_v42/services/plan_indexer.py tests/unit/test_plan_indexer.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 7: Detect changes and commit**

Run GitNexus change detection for the linked worktree, review the affected `brain_reindex_plans`
flows, then commit only the two task files:

```bash
git add src/brain_v42/services/plan_indexer.py tests/unit/test_plan_indexer.py
git commit -m "🐛 fix(plans): reject unsafe scan paths"
```

---

### Task 2: Validate the seven-project manifest and inventory local files

**Files:**
- Create: `src/brain_v42/maintenance/plan_index_repair.py`
- Create: `tests/unit/test_plan_index_repair.py`
- Create: `ops/recovery/plan-index-repair-v1.json`

**Interfaces:**
- Produces: `TARGET_PROJECT_KEYS: frozenset[str]`.
- Produces: `ProjectTarget`, `RepairManifest`, and `LocalPlanFile` frozen dataclasses.
- Produces: `load_manifest(path: Path, *, allowed_project_keys: frozenset[str] = TARGET_PROJECT_KEYS) -> RepairManifest`.
- Produces: `discover_local_files(manifest: RepairManifest) -> tuple[LocalPlanFile, ...]`.

- [ ] **Step 1: Write failing manifest tests**

Create tests for version `1`, the exact target set, duplicate keys, duplicate canonical paths,
relative roots, relative scan paths, missing paths, regular files, unreadable paths, and symlinks
escaping the declared project root. The default must require the production target set; an explicit
`allowed_project_keys` test set is permitted only for isolated tests. Use temporary directories and
JSON written by pytest.

```python
def test_load_manifest_requires_exact_ticket_projects(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, projects=[])
    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(manifest_path)
    assert exc.value.reason_code == "project_set_mismatch"
```

- [ ] **Step 2: Write failing file-inventory tests**

Create files ending in `-design.md`, `-plan.md`, and unrelated suffixes. Assert deterministic
canonical path ordering, SHA-256 hashes, byte sizes, and project ownership. Assert one file
reachable through two scan paths appears once.

- [ ] **Step 3: Run RED tests**

Run:

```bash
uv run pytest tests/unit/test_plan_index_repair.py -q
```

Expected: collection fails because the repair module and interfaces do not exist.

- [ ] **Step 4: Implement immutable models and validation**

Define the closed error and models:

```python
class RepairSafetyError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ProjectTarget:
    project_key: str
    project_root: Path
    scan_paths: tuple[Path, ...]


@dataclass(frozen=True)
class LocalPlanFile:
    project_key: str
    file_path: str
    content_hash: str
    size_bytes: int
```

Parse JSON with a closed `version` and `projects` schema. Resolve all paths with `strict=True`,
require `is_relative_to(canonical_root)`, and validate `R_OK | X_OK`. Discover only
`**/*-design.md` and `**/*-plan.md`; hash bytes without including contents in results.

- [ ] **Step 5: Add the production manifest**

Add all seven projects under the canonical root
`/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects`. Use the explicit current scan directories:

- `red-phone`: `docs/specs`, `docs/plans`
- `red-viewer`: `docs/specs`, `docs/plans`
- `red-quant`: `docs/specs`, `docs/plans`, `docs/runbooks`
- `red-shrik`: `docs/specs`, `docs/plans`
- `red-writer`: `docs/specs`, `docs/plans`
- `red-games`: `docs/specs`, `docs/plans`
- `red-gift`: `docs/specs`, `docs/plans`

- [ ] **Step 6: Run GREEN and quality checks**

Run:

```bash
uv run pytest tests/unit/test_plan_index_repair.py -q
uv run ruff check src/brain_v42/maintenance/plan_index_repair.py tests/unit/test_plan_index_repair.py
uv run ruff format --check src/brain_v42/maintenance/plan_index_repair.py tests/unit/test_plan_index_repair.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 7: Detect changes and commit**

Run GitNexus change detection, then commit the manifest unit:

```bash
git add src/brain_v42/maintenance/plan_index_repair.py tests/unit/test_plan_index_repair.py ops/recovery/plan-index-repair-v1.json
git commit -m "✨ feat(plans): inventory canonical project files"
```

---

### Task 3: Build complete read-only control snapshots

**Files:**
- Modify: `src/brain_v42/maintenance/plan_index_repair.py`
- Create: `src/brain_v42/maintenance/plan_index_repair_store.py`
- Modify: `tests/unit/test_plan_index_repair.py`
- Create: `tests/unit/test_plan_index_repair_store.py`

**Interfaces:**
- Produces: `ContextRecord`, `IndexedPlanRecord`, `FeatureLinkRecord`, `RepairSnapshot`.
- Produces: `async RepairStore.inventory(manifest, local_files) -> RepairSnapshot`.
- Produces: `RepairSnapshot.to_dict() -> dict[str, object]` for canonical private serialization.
- Produces: `write_private_json(path: Path, payload: Mapping[str, object]) -> str` returning SHA-256.
- Produces: `load_snapshot(path: Path, expected_sha256: str) -> RepairSnapshot`.

- [ ] **Step 1: Run impact analysis for table consumers**

Use GitNexus context and upstream impact for `project_contexts`, `indexed_plans`,
`indexed_plan_chunks`, and `feature_artifacts`. This task adds queries but changes no existing
table declaration. Report HIGH or CRITICAL results before editing.

- [ ] **Step 2: Write failing snapshot tests**

Test deterministic serialization, a signed timezone-aware `mutation_timestamp`, complete context
values, context CAS fingerprints, exact
polluted/missing/collision classification, database-identity hashing, and omission of plan content,
embedding values, and DSNs. Test atomic `0600` creation, refusal to overwrite, owner/mode checks,
and expected-digest mismatch.

```python
def test_write_private_json_creates_0600_file(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    digest = write_private_json(target, {"version": 1})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
```

- [ ] **Step 3: Write failing store tests**

Use an `AsyncMock` session factory. Assert inventory begins a transaction, executes
`SET TRANSACTION READ ONLY`, selects all columns from the seven locked-out-of-mutation context
rows, fetches targeted plans plus canonical-path owners, counts chunks, fetches plan artifact
links, reads `alembic_version`, and hashes database identity without returning it.

- [ ] **Step 4: Run RED tests**

Run:

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
```

Expected: failures name the missing snapshot models, private-file functions, and store queries.

- [ ] **Step 5: Implement snapshot serialization and classification**

Canonicalize UUIDs and datetimes to strings, mappings by sorted key, and sequences in stable order.
Define:

```python
def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
```

Use `os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)`, flush, `os.fsync`, and parent-directory
`fsync` for snapshots. Persist one timezone-aware UTC `mutation_timestamp`; later mutations must
use that exact signed value so expected post-update fingerprints are deterministic. A row is
polluted when its path is absent from that project's canonical local set. A collision exists when a
canonical path is owned by another project or has a different content hash.

- [ ] **Step 6: Implement read-only PostgreSQL inventory**

Inject `async_sessionmaker[AsyncSession]` into `RepairStore`. Inside `session.begin()`, execute
`SET TRANSACTION READ ONLY`, then use SQLAlchemy table objects and sorted results. Never select
`indexed_plans.content` or `embedding`, and never select chunk content or embeddings.

- [ ] **Step 7: Run GREEN and quality checks**

Run:

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
uv run ruff check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
uv run ruff format --check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git diff --check
```

- [ ] **Step 8: Detect changes and commit**

```bash
git add src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git commit -m "✨ feat(plans): snapshot repair inventory"
```

---

### Task 4: Gate and apply canonical context paths with CAS

**Files:**
- Modify: `src/brain_v42/maintenance/plan_index_repair.py`
- Modify: `src/brain_v42/maintenance/plan_index_repair_store.py`
- Modify: `tests/unit/test_plan_index_repair.py`
- Modify: `tests/unit/test_plan_index_repair_store.py`

**Interfaces:**
- Produces: `MutationProof` with snapshot and backup receipt digests plus attestations.
- Produces: `validate_mutation_proof(snapshot_path, snapshot_sha256, backup_receipt_path, backup_receipt_sha256, postgres_restore_tested, writers_off_confirmed) -> tuple[RepairSnapshot, MutationProof]`.
- Produces: `async RepairStore.apply_paths(snapshot: RepairSnapshot, proof: MutationProof) -> PhaseResult`.

- [ ] **Step 1: Run upstream impact analysis**

Run GitNexus upstream impact for `PgProjectContextRepo.get_or_create`,
`ProjectContextService.get_or_create`, and `project_contexts`. The new implementation must bypass
full upsert and update only `plan_scan_paths`.

- [ ] **Step 2: Write failing proof-gate tests**

Cover missing or changed snapshot, missing or changed backup receipt, non-regular proof files,
wrong owner, mode other than `0600`, absent `postgres_restore_tested`, and absent
`writers_off_confirmed`. Store tests must also block a wrong database-identity fingerprint and an
Alembic head other than revision `037` inside the same transaction as the mutation.

- [ ] **Step 3: Write failing apply-path tests**

Test original-fingerprint success, post-update idempotent replay, one-row drift blocking every
update, seven rows locked with `FOR UPDATE`, serializable isolation, partial-update SQL containing
only `plan_scan_paths` and `updated_at`, and transaction rollback when the seventh update fails.

```python
@pytest.mark.asyncio
async def test_apply_paths_conflict_updates_nothing(store, snapshot, session) -> None:
    _configure_locked_context_rows(session, _drift_one_context(snapshot.contexts))
    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot)
    assert exc.value.reason_code == "context_cas_conflict"
    update_calls = [
        call
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sqlalchemy.sql.dml.Update)
    ]
    assert update_calls == []
```

- [ ] **Step 4: Run RED tests**

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
```

Expected: new proof and apply tests fail.

- [ ] **Step 5: Implement proof validation and apply transaction**

Validate private files before opening a mutating session. In `apply_paths`, start a serializable
transaction, re-read and compare the database-identity fingerprint and Alembic head from the
signed snapshot, lock all target contexts, compare all fingerprints before issuing any update, accept
either the exact original fingerprint or the exact expected post-update fingerprint, and update
only original rows. Derive the expected post-update fingerprint by replacing only
`plan_scan_paths` and `updated_at`, with `updated_at` fixed to the signed
`snapshot.mutation_timestamp`. Return `already_applied` only when all seven rows already match
their exact expected post-update fingerprints.

- [ ] **Step 6: Run GREEN and quality checks**

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
uv run ruff check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
uv run ruff format --check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git diff --check
```

- [ ] **Step 7: Detect changes and commit**

```bash
git add src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git commit -m "✨ feat(plans): apply canonical paths with CAS"
```

---

### Task 5: Verify canonical corpus, finalize exact rows, and roll back safely

**Files:**
- Modify: `src/brain_v42/maintenance/plan_index_repair.py`
- Modify: `src/brain_v42/maintenance/plan_index_repair_store.py`
- Modify: `tests/unit/test_plan_index_repair.py`
- Modify: `tests/unit/test_plan_index_repair_store.py`

**Interfaces:**
- Produces: `ReindexEvidence`, `VerificationReport`.
- Produces: `async RepairStore.verify(snapshot, evidence) -> VerificationReport`.
- Produces: `async RepairStore.finalize(snapshot, proof, report) -> PhaseResult`.
- Produces: `async RepairStore.rollback_before_finalize(snapshot, proof) -> PhaseResult`.

- [ ] **Step 1: Write failing verification tests**

Define reindex evidence version `1`, bound to `snapshot_sha256`, with exactly seven project stats
objects containing `indexed`, `skipped`, `linked`, `errors`, and `chunks_created`. Reject a missing
project, extra project, non-zero `errors`, wrong digest, missing canonical path, wrong owner, wrong
hash, extra canonical row, changed polluted row, or changed local file.

- [ ] **Step 2: Write failing finalize tests**

Assert finalization repeats database identity, schema head, context, plan, report, and filesystem
CAS checks inside a serializable transaction after the private-file proof was validated. Assert it
deletes `feature_artifacts` with
`artifact_type == "plan"` for the exact snapshot plan IDs before deleting those exact
`indexed_plans` IDs. Assert chunks rely on the existing `ON DELETE CASCADE`. Assert zero or excess
row counts roll back.

- [ ] **Step 3: Write failing rollback tests**

Test restoration of the original `plan_scan_paths` and `updated_at` values after verifying that
every untouched context column still matches the complete snapshot fingerprint. Test deletion of
only canonical plan IDs absent from the original snapshot, link deletion before plan deletion,
idempotent replay, and refusal after a polluted snapshot row has disappeared. The post-finalize
path must return `backup_restore_required` with only the backup receipt digest.

- [ ] **Step 4: Run RED tests**

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
```

- [ ] **Step 5: Implement verification and bounded deletion**

Recompute local file hashes at verification time. Compare canonical DB rows by the tuple
`(project_key, file_path, content_hash)`. Bind the private verification report to the snapshot
digest and proof digest. In `finalize`, use snapshot UUIDs plus project, path, and content-hash
predicates so a reused UUID or changed row cannot match. Check SQL `rowcount` after both link and
plan deletes.

- [ ] **Step 6: Implement pre-finalize rollback**

Restore only `plan_scan_paths` and `updated_at`—the two fields changed by apply—when every current
context row equals its exact expected post-update fingerprint. This returns the complete row to its
snapshotted state without rewriting untouched columns. Delete only canonical rows not present in
the original snapshot and proven by the current local-file tuple. Preserve the control snapshot
and proof files on every outcome.

- [ ] **Step 7: Run GREEN and quality checks**

```bash
uv run pytest tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py -q
uv run ruff check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
uv run ruff format --check src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git diff --check
```

- [ ] **Step 8: Detect changes and commit**

```bash
git add src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py
git commit -m "✨ feat(plans): finalize verified index repairs"
```

---

### Task 6: Expose the safe CLI and operator runbook

**Files:**
- Create: `scripts/repair_plan_index.py`
- Create: `tests/unit/test_repair_plan_index_cli.py`
- Create: `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`

**Interfaces:**
- Produces: `parse_args(argv: list[str] | None) -> argparse.Namespace`.
- Produces: `async run_from_args(args: argparse.Namespace) -> int`.
- Produces: `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write failing parser tests**

Assert no subcommand selects `inventory`, inventory requires an explicit snapshot output path,
mutating phases require snapshot/proof paths and digests, attestations are rejected for inventory,
repair run IDs must be canonical full UUID strings accepted by `uuid.UUID`, and there is no
`--wet`, `--force`, `--skip-backup`, or combined apply/finalize option.

- [ ] **Step 2: Write failing dispatch and masking tests**

Patch the service and store. Assert each phase calls only its matching method, inventory opens no
mutating method, exit codes are `0` success, `2` safety/input, and `1` operational. Inject a secret
sentinel into an unexpected exception and assert stdout/stderr contain only the exception type and
repair run ID.

- [ ] **Step 3: Run RED CLI tests**

```bash
uv run pytest tests/unit/test_repair_plan_index_cli.py -q
```

- [ ] **Step 4: Implement the thin CLI**

Use local imports for runtime settings and session factory. `main()` calls
`asyncio.run(run_from_args(args))`. Serialize only dataclass result summaries with sorted JSON.
Call `dispose_engine()` in `finally`. The exception boundary inside `run_from_args()` is:

```python
except RepairSafetyError as exc:
    print(json.dumps({"status": "blocked", "reason_code": exc.reason_code}), file=sys.stderr)
    return 2
except Exception as exc:  # noqa: BLE001
    print(
        json.dumps({"status": "failed", "error_type": type(exc).__name__, "run_id": run_id}),
        file=sys.stderr,
    )
    return 1
```

- [ ] **Step 5: Write the operator runbook**

Document these separate gates: confirm migrations 036/037, stop writers, verify a full PostgreSQL
backup and tested restore, inventory, inspect snapshot counts, apply paths, deploy through
`install.sh`, restart last, run `brain_reindex_plans` one project at a time, capture version-1
reindex evidence, verify, finalize, and validate exact counts. Include pre-finalize rollback and
post-finalize full-restore commands as operator-owned examples. State that this mission executes
none of those production mutations.

- [ ] **Step 6: Run GREEN and documentation checks**

```bash
uv run pytest tests/unit/test_repair_plan_index_cli.py -q
uv run ruff check scripts/repair_plan_index.py tests/unit/test_repair_plan_index_cli.py
uv run ruff format --check scripts/repair_plan_index.py tests/unit/test_repair_plan_index_cli.py
git diff --check
```

- [ ] **Step 7: Detect changes and commit**

```bash
git add scripts/repair_plan_index.py tests/unit/test_repair_plan_index_cli.py docs/PLAN_INDEX_REPAIR_RUNBOOK.md
git commit -m "📝 docs(plans): add repair operator workflow"
```

---

### Task 7: Prove PostgreSQL transactions and finish the code delivery

**Files:**
- Create: `tests/integration/test_plan_index_repair.py`
- Modify only if evidence requires: files from Tasks 1-6

**Interfaces:**
- Consumes: all repair interfaces from Tasks 1-6.
- Produces: isolated PostgreSQL evidence for inventory, CAS, apply, finalize, and rollback.

- [ ] **Step 1: Write isolated PostgreSQL tests**

Use `session_factory` from `tests/integration/conftest.py` and unique `integ-plan-repair-*` project
keys mapped through a test-only manifest passed to
`load_manifest(..., allowed_project_keys=frozenset(test_keys))`. Seed
context rows, polluted plan rows, chunks, and plan artifact links. Prove read-only inventory leaves
row counts unchanged; CAS drift rolls back all contexts; apply updates only paths; finalize removes
exact links/plans/chunks; and a forced seventh-operation failure rolls back the whole transaction.

- [ ] **Step 2: Run the integration test safely**

Run:

```bash
uv run pytest tests/integration/test_plan_index_repair.py -q
```

Expected when `BRAIN_V42_TEST_DB_URL` is absent: a loud skip with no migration or connection
attempt. Expected when it points at an isolated database: all tests pass. Any production database
name must be rejected by the existing guard.

- [ ] **Step 3: Run all targeted suites**

```bash
uv run pytest tests/unit/test_plan_indexer.py tests/unit/mcp/tools/test_plan_tools.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py tests/unit/test_repair_plan_index_cli.py tests/integration/test_plan_index_repair.py -q
uv run ruff check src/brain_v42/services/plan_indexer.py src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py scripts/repair_plan_index.py tests/unit/test_plan_indexer.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py tests/unit/test_repair_plan_index_cli.py tests/integration/test_plan_index_repair.py
uv run ruff format --check src/brain_v42/services/plan_indexer.py src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py scripts/repair_plan_index.py tests/unit/test_plan_indexer.py tests/unit/test_plan_index_repair.py tests/unit/test_plan_index_repair_store.py tests/unit/test_repair_plan_index_cli.py tests/integration/test_plan_index_repair.py
uv run mypy src/brain_v42/services/plan_indexer.py src/brain_v42/maintenance/plan_index_repair.py src/brain_v42/maintenance/plan_index_repair_store.py scripts/repair_plan_index.py
git diff --check
```

- [ ] **Step 4: Run broader non-Dream regression tests**

```bash
uv run pytest tests/unit -q
```

Record all exit codes. Explain skips and any failure unrelated to this exact change; do not hide or
modify tests to obtain a green result.

- [ ] **Step 5: Perform final GitNexus and Git gates**

Run `gitnexus_detect_changes(scope="all", worktree=<mission-worktree>)`. Review every affected
process. Then require `git diff --check`, `git status --short`, and an exact diff review from the
mission base SHA.

- [ ] **Step 6: Commit the final test proof**

```bash
git add tests/integration/test_plan_index_repair.py
git commit -m "✅ test(plans): prove transactional index repair"
```

- [ ] **Step 7: Update only directly related Brain records**

Reply to ticket `44ee7643-fb06-4186-a364-cb175610b973` with the committed interval, test commands,
current read-only counts, production-operation exclusions, and runbook path. Keep the ticket open
because production apply is separately authorized. Keep the roadmap feature `building`; do not
transition any ticket.

- [ ] **Step 8: Produce the ReD proof packet**

Verify `HEAD`, exact `base_sha..head_sha` files, and final worktree snapshot equality. Publish
`WorkerProofPacketV1` to the mission ticket and wait for the independent reviewer mission required
by the ReD validation policy.
