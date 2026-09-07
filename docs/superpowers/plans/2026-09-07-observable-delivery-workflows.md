# Observable Delivery Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Ship the approved GitHub-backed delivery workflow to the production Brain, including independent observation, guarded completion and a recorded live canary.

**Architecture:** PostgreSQL owns versioned contracts, bindings, immutable observations/receipts and leases. A pure domain evaluator feeds both transactional guards and read APIs. A separate GitHub observer publishes evidence; the external orchestrator owns every execution-agent action.

**Tech Stack:** Python 3.12, Pydantic 2, SQLAlchemy Core/asyncpg, PostgreSQL 16, Alembic, httpx, FastMCP, systemd, GitHub REST.

## Global Constraints

- Approved specification: `docs/superpowers/specs/2026-09-07-observable-delivery-workflows-design.md`, committed as `dfaed4a9cef4d1a672dbdfe10f56fbda4c2a5c0c`, approved by the user on 2026-09-07.
- **Brain never launches, stops, restarts, schedules, or selects execution agents.**
- The initial production adapter is GitHub; polling is required, webhooks are deferred.
- No LLM, embeddings, graph or agent session is required for evidence observation/evaluation.
- Existing tickets without a contract keep their lifecycle. Existing terminal states remain terminal.
- All new completion paths use `completion_eligible_now` inside the same PG transaction as the status CAS. A disabled observer must never disable these guards.
- Historical receipts and collection confirmations are immutable; a snapshot may be deduplicated, its confirmation timestamp may not be rewritten.
- Dependencies pin upstream ticket, contract revision and attempt, and require a matching receipt rather than a stage label.
- Worker credentials are within the current administrative trust model. Project/agent labels are provenance, not cryptographic role/model/sandbox proof.
- Python code and project documentation remain English; human briefing copy remains French.
- No new top-level import cycle. Domain evaluation lives under `models/`, so repositories never import services. This refines the spec's indicative module map without changing behavior.
- No production database in tests. All database tests receive a dedicated `BRAIN_V42_TEST_DB_URL` pointing to this task's disposable test database.
- Record an actual failing behavioral test before code for every task; keep RED/GREEN command output and commit identities in the task report.
- Run GitNexus upstream impact before editing an existing symbol and `detect_changes` before commits. Existing index evidence is `DEGRADED_MCP_EVIDENCE`; confirm affected callers in native source, never treat an empty stale graph as proof of safety.
- One implementation writer at a time. Workers do not merge, push, create worktrees, modify production, mutate Brain sessions or touch unrelated changes.
- Coordinator owns full verification, integration, normal push and the separately authorized production deployment. User approved E2E delivery; no routine permission checkpoints remain.

## Baseline and execution record

Feature worktree: `/tmp/brain-observable-workflows-01a07afd`.
Feature branch: `codex/observable-workflows`, based on `main` at `4990a920`.
Initial spec commit: `dfaed4a9`.
The configured upstream is `origin/main`, repository `hawkixs/brain-v42`, numeric ID `1337360966`.
Other `/tmp/lot-*` worktrees are outside this task.

The isolated `.venv` uses Python 3.12.12 and `uv sync --python 3.12 --locked --extra dev --offline`.
Baseline command:

```bash
env -u BRAIN_V42_TEST_DB_URL -u PYTEST_ADDOPTS PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -p no:cacheprovider -o addopts= -q tests/unit/models/test_ticket_models.py tests/unit/services/test_ticket_service.py tests/unit/services/test_brain_session_service_v4.py
```

Baseline result: **75 passed**, before implementation.
The per-plan `.superpowers/sdd/2026-09-07-observable-delivery-workflows/progress.md` ledger records task branches, commits, RED/GREEN evidence and reviews. Read it before resuming after context compaction.

### Required test database launcher

The coordinator provisions this task's separate Docker container `brain-delivery-test-01a07afd`, using the CI-pinned pgvector image, database `brain_test_delivery` and a dynamically assigned localhost-only port. It has no production mounts or connection. The private generated DSN is kept mode 0600 in this plan's ignored scratch directory, never in a report or shell command.

Every database test/migration in Tasks 3–10 MUST run through the following launcher, from the task's actual worktree:

```bash
TASK_TEST_RUNNER=/tmp/brain-observable-workflows-01a07afd/.superpowers/sdd/2026-09-07-observable-delivery-workflows/with_test_db.py
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/alembic upgrade head
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/pytest PATH_TO_TARGETED_TESTS -q
```

The launcher fails before executing its child if its private configuration is missing or does not select this container's localhost test database. It supplies both `BRAIN_V42_TEST_DB_URL` and `POSTGRES_URL`, removes inherited `PYTEST_ADDOPTS`, and disables the graph. The coordinator waits for the dedicated container's `healthy` state before migration. Each final database test run writes JUnit; `scripts/check_coverage_saw_the_db_tests.py` rejects a run that skipped DB tests. New delivery integration tests must have zero skips. A missing launcher/DSN or skipped delivery test is a failed gate, never a green result.

## Interfaces and storage decisions

Use small modules with the following responsibilities. Tasks may split an oversized new module into a same-package helper, keeping public interfaces and ownership explicit in the report.

| Module | Responsibility |
| --- | --- |
| `models/delivery.py` | Validated contracts, evidence, receipt/view schemas and typed error codes |
| `models/delivery_hashes.py` | Canonical payload, request and delivery digests |
| `models/delivery_evaluator.py` | Pure predicates, observed stage, health and eligible work |
| `delivery_config.py` | Dedicated settings, repository registry and private credential references |
| `db/delivery_tables.py` | Table-definition function accepting shared `METADATA`; no import back to `db.tables` |
| `repositories/pg_delivery.py` | Contracts, bindings, graph locks, read views and mutation idempotency |
| `repositories/pg_delivery_evidence.py` | Snapshot/confirmation persistence and immutable milestone receipts |
| `repositories/pg_delivery_claims.py` | Lease acquisition/renew/release and fencing |
| `repositories/delivery_ticket_guard.py` | Guard and workflow disposition changes in the ticket transaction |
| `services/delivery_service.py` | Project-role checks, context resolution and public use cases |
| `delivery_observer/github.py`, `auth.py` | Bounded GitHub collection and dedicated machine identity |
| `delivery_observer/runtime.py`, `__main__.py` | Ownership, fair scheduling, CLI and shutdown |
| `mcp/tools/delivery_tools.py`, `delivery_formatters.py` | Structured MCP surface and bounded French rendering |
| `deploy/systemd/brain-v42-delivery-observer.service.tmpl` | Separate observer service |

Shared production interfaces introduced below:

```python
def canonical_digest(payload: dict[str, object], *, domain: str) -> str:
    import hashlib
    import json
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(f"brain-delivery-{domain}:v1\n".encode() + encoded).hexdigest()
```

Validate the recursive value domain before hashing: no floats, invalid surrogates, non-string keys, or coercion of booleans to integers. The function above defines encoding, not a substitute for validation.

Define `DeliveryError(code, message)` with stable codes and sanitized messages. Domain models include `ContractInput`, `ContractRevision`, `ArtifactBinding`, `PullRequestEvidence`, `ObservationConfirmation`, `MilestoneReceipt`, `EvaluationInput`, `DeliveryAssessment`, `DeliveryView`, `DeliveryPage`, and `ClaimResult`. The detailed fields and relationships are specified in Tasks 1–2; later tasks import these definitions rather than redefine them.

Public `DeliveryService` methods use full UUIDs for mutations and return these typed models. Every mutation takes `actor_project`; requester/executor checks follow the spec. The authenticated caller label is stored separately from that declared project. No method accepts caller-authored observed success.

The database tables are:

1. `delivery_workflows`: ticket FK/PK, current revision, attempt, row version, disposition, claim owner/kind/digest/expiry/epoch, due/health timestamps.
2. `delivery_contract_revisions`: composite ticket/revision PK, normalized JSONB, digest, author, amendment reason, creation time.
3. `delivery_dependencies`: revision-scoped parent/upstream FKs and pinned upstream revision/attempt/milestone.
4. `delivery_artifact_bindings`: UUID PK, workflow/revision/attempt, deliverable key, registered repository ID/name, PR number, active flag, `row_version` bigint initially 1, due and latest-confirmation pointers. Unique active binding per deliverable/revision/attempt. Increment row_version when superseded or when a confirmation pointer changes; publication CAS compares the version captured before collection as well as the active workflow generation. Due-only scheduling changes do not alter evidence identity or row_version. Replacements always create a fresh binding ID/version 1, even when returning to an earlier PR.
5. `delivery_snapshots`: UUID PK, binding FK, semantic digest and normalized evidence JSONB; unique binding/digest.
6. `delivery_confirmations`: UUID PK, binding FK, snapshot FK when successful, collection start/end, outcome/error code. Append-only.
7. `delivery_receipts`: UUID PK, workflow/revision/attempt, milestone, delivery digest, frozen proof/context/dependency payload, issuer/basis/time. Unique milestone for the complete delivery identity.
8. `delivery_events`: UUID PK, operation, actor project, target ticket, idempotency key/digest and safe result, event payload/time. Idempotency uniqueness is scoped by operation, actor and key.

Use FK restrictions for proof history. New tables need no destructive backfill. The migration is numbered from the actual head when Task 3 starts. Downgrade refuses populated delivery tables; production rollback retains the guarded application.

### Repository context before the first PR

The evidence pair also serves required repository-document context. This refines
the binding-only table descriptions above: keep eight tables, and give snapshots
and confirmations an explicit `subject_kind` (`artifact_binding` or
`repository_context`). Enforce mutually exclusive subject columns with CHECKs and
foreign keys. Artifact subjects use their binding ID; context subjects use ticket,
contract revision, attempt and a digest of the sorted required repository pins.
Snapshot deduplication is scoped to that complete subject identity. Error
confirmations carry their subject even when they have no snapshot.

The workflow owns context due/health fields, its latest successful context
confirmation pointer and a context publication version, initially 1. Moving only
the due time changes no evidence version. Publishing an attempt or confirmation
increments the context version and workflow row version. Publication compares the
captured context version and exact subject generation, so a late collection cannot
replace a newer result. Amend/reopen supersedes the current pointer and queues the
new context generation.

Task 3 creates this shape and hydrates unresolved required repository pins as
missing, even when registry and pin syntax are valid. Task 4 publishes context
snapshots/confirmations using the same fenced observer connection and freezes their
IDs and times in receipts. Task 6 adds a bounded internal repository-context
collector that verifies the registered repository ID, exact commit and exact path.
Task 7 schedules due workflow-context subjects alongside PR bindings, including
workflows with no PR. Task 8 exposes the resulting context blockers through the
existing views; no new public operation is needed.

Reusing the existing freshness limit, hydration derives available/missing/error
predicates from current confirmations and PG time; it never stores a permanent
fresh boolean. Required context blocks new work until resolved. A freshly resolved
context can enable implementation while the workflow remains awaiting_artifact.
Brain entity refs still use authoritative PG content comparison; optional URLs
remain reference-only. The tests cover no-binding discovery, missing/stale/error
context, identical reconfirmation and stale-generation publication.

## Task 1: Validated contracts, identities and canonical digests

**Files:**
- Create `src/brain_v42/models/delivery.py`
- Create `src/brain_v42/models/delivery_hashes.py`
- Create `tests/delivery_helpers.py`
- Create `tests/unit/models/test_delivery_contracts.py`

**Interfaces:**
- Produces `ContractInput` and nested context/dependency/deliverable/check/review models, `ContractRevision`, `ArtifactBinding`, and `DeliveryError`.
- Produces `canonical_digest(payload, *, domain)` and `contract_digest(contract)`.
- Model aliases and serialization use the JSON names in spec §5; errors have stable codes.

- [ ] **Step 1: RED — validate actual acceptance boundaries and digest identity.**

Create a literal fixture `contract_payload()` in `tests/delivery_helpers.py` with the spec's example objective/constraints, one `implementation` deliverable for `hawkixs/brain-v42`, one `check_run` named `test-unit` from `github-actions`, no dependencies/context, priority 20 and explicit acceptance. Use copies in each test.

```python
def test_contract_digest_preserves_the_published_unicode_vector():
    from brain_v42.models.delivery_hashes import canonical_digest
    assert canonical_digest({"z": None, "a": ["é", 1, True]}, domain="contract") == (
        "3a9f5e4221a8f912fdb0af7ba4087dbd02fbcb3c82713f6676cb69bcbf3d87af"
    )

def test_empty_check_policy_needs_an_explicit_reason():
    from pydantic import ValidationError
    from brain_v42.models.delivery import ContractInput
    from tests.delivery_helpers import contract_payload
    import pytest
    payload = contract_payload()
    payload["deliverables"][0]["required_checks"] = []
    with pytest.raises(ValidationError, match="no_checks_reason"):
        ContractInput.model_validate(payload)
```

Also cover extra keys, duplicate deliverable keys/selectors, zero approvals versus missing policy, numeric coercion, Unicode surrogates, floats nested in input, invalid SHA/path/repository identities, duplicate/self dependency data, and list-order-sensitive hashes. Run `.venv/bin/pytest -q tests/unit/models/test_delivery_contracts.py` and retain RED output.

- [ ] **Step 2: GREEN — implement the schema and digest domain.**

Use `ConfigDict(extra="forbid")`, immutable stored models, bounded strings/lists, strict integer fields and explicit normalization. Bounds: contract payload 256 KiB, 1–20 deliverables, 0–32 dependencies/context references, objective 1–8000 characters, priority 0–10000 (lower is earlier), check name ≤200, key ≤64, positive repository/PR/provider IDs. SHA values are full 40- or 64-character lowercase hex; reject absolute/traversing repository paths. Brain context references identify supported entity type and UUID; repository references pin registered repository, SHA and path; URL references are reference-only and cannot satisfy a required proof. API input cannot supply current observations or receipt IDs as success.

Normalize repository IDs from the operator registry in the service before creating `ContractRevision`. The content digest excludes server metadata as specified in §5.1.1. Request/delivery digest domains differ, and delivery identity includes attempt and binding IDs.

- [ ] **Step 3: Verify, self-review and commit.**

Run the targeted tests, `.venv/bin/ruff check` and `.venv/bin/ruff format --check` on owned paths, and `.venv/bin/mypy src/brain_v42/models/delivery.py src/brain_v42/models/delivery_hashes.py`. Commit `feat: define observable delivery contracts` after `detect_changes` and record RED/GREEN proof.

## Task 2: Pure evidence assessment and receipt semantics

**Files:**
- Extend `src/brain_v42/models/delivery.py`
- Create `src/brain_v42/models/delivery_evaluator.py`
- Extend `tests/delivery_helpers.py`
- Create `tests/unit/models/test_delivery_evaluator.py`

**Interfaces:**
- Consumes Task 1 contracts and binding identities.
- Produces `evaluate_delivery(inputs: EvaluationInput, *, now: datetime) -> DeliveryAssessment`.
- `EvaluationInput` contains contract revision/attempt, coordination status/role, active bindings and per-binding latest successful confirmation/evidence plus last attempt health, context predicates, dependency receipts/current generations, current integration/fulfillment receipts, and feature availability.
- `DeliveryAssessment` exposes every field from spec §7.1, assessment identity/version, timestamps, per-deliverable findings, stable blocker codes and eligible work. It does no I/O and never changes ticket status.
- `assessment_id` uses canonical encoding with domain `assessment` (`brain-delivery-assessment:v1\n`). Its payload contains contract revision/digest, attempt, workflow version, coordination status/disposition, deliverable-key-sorted active binding IDs/versions and head/base identities, latest success/error confirmation IDs, current context digests/statuses, dependency generations/receipt IDs, own receipt IDs, claim epoch/owner/expiry, evaluated health/blocker codes/eligible-work kinds, and relevant freshness configuration. Exclude `assessed_at` and continuously changing age/lag numbers. Repeated reads with the same facts/predicates have the same ID; crossing freshness or lease expiry changes predicates and therefore the ID. Claims rehydrate and evaluate with PG time under the mutation locks before comparing the submitted ID; an ID never replaces this reevaluation.

- [ ] **Step 1: RED — build a literal state matrix.**

Extend the test helper with a fully populated PR evidence fixture, including repository/PR/provider IDs, head/base SHA, PR state, mergeability, draft, check attempts, reviews, integration revision and collection timestamps. Expected states are literals, not calculated by production helpers.

```python
def test_a_bare_merge_does_not_satisfy_technical_delivery():
    from brain_v42.models.delivery_evaluator import evaluate_delivery
    from tests.delivery_helpers import delivery_inputs, FIXED_NOW
    inputs = delivery_inputs(merged=True, required_check="failure")
    result = evaluate_delivery(inputs, now=FIXED_NOW)
    assert result.delivery_stage == "integrated"
    assert result.completion_eligible_now is False
    assert result.contract_fulfilled is False
    assert "check_failed" in {item.code for item in result.blockers}
```

Cover absent binding, draft, head/base mismatch, missing/pending/skipped/neutral/failed check, newer rerun, approval dismissal/change request, reviewer identity/self approval, incomplete observation, stale clock, changed context, dependency generation mismatch and every eligible-work role. Run `.venv/bin/pytest -q tests/unit/models/test_delivery_evaluator.py` and retain RED.

- [ ] **Step 2: GREEN — evaluate orthogonal facts and actions.**

Only explicit applicable success counts. Check attempts select newest applicable provider attempt, including pending reruns. Reviews use latest effective decision per allowed reviewer on the evaluated head. Synthetic merge evidence requires a proven head/base association. Pre-merge unknown mergeability blocks integration recommendations; merged observations do not require pre-merge mergeability to become known again.

`integrated` stage may coexist with failed requirements. Receipt eligibility requires complete fresh technical/context/dependency evidence. `contract_fulfilled` comes from a matching historical fulfillment receipt and survives observer outage; `completion_eligible_now` independently requires fresh current predicates. Dependency fulfillment uses current upstream generation and receipt identity. Terminal/cancelled/wontfix work produces no execution eligibility. A legal reopen increments attempt in storage, so the evaluator rejects old-attempt receipts.

Compute observed health at `now`, never trust a cached fresh boolean. Do not add a separate LLM classifier or a generic workflow DSL.

- [ ] **Step 3: Verify and commit.**

Run both domain test files, lint/format/mypy on owned files, and `scripts/check_module_layering.py --package src/brain_v42`. Commit `feat: evaluate delivery evidence deterministically` with RED/GREEN record.

## Task 3: Additive schema, versioned contracts, context and dependencies

**Files:**
- Create `src/brain_v42/db/delivery_tables.py`
- Extend `src/brain_v42/db/tables.py` with the new table registration
- Create the next `alembic/versions/*_delivery_workflows.py` revision
- Create `src/brain_v42/repositories/pg_delivery.py`
- Create `src/brain_v42/services/delivery_service.py`
- Create `src/brain_v42/delivery_config.py`
- Create `tests/integration/db/test_delivery_contracts.py`
- Create `tests/unit/test_delivery_config.py`
- Extend integration cleanup/schema-family inventories only where the new tables require it

**Interfaces:**
- Produces `PgDeliveryRepo(session_factory)`, shared transactional `lock_workflows(session, ticket_ids, *, graph_write=False)`, `set_contract`, `bind_pr`, `load_inputs`, `get_view`, and `list_views`.
- Produces `DeliveryService(repo, *, settings)` exposing `set_contract`, `bind_pr`, `get`, `list`, and `refresh`.
- Introduces `DeliverySettings` using `BRAIN_DELIVERY_*`; `ENABLED=false`, repository registry mapping project keys to numeric repository IDs/current names, polling/freshness/budget defaults from the spec. Service credential values are `SecretStr`/private path fields with safe representations.
- The dedicated observer environment file is `~/.config/brain-v42/delivery-observer.env`, mode 0600. The fine-grained credential key is `BRAIN_DELIVERY_GITHUB_TOKEN`; App configuration uses explicit app/installation IDs and a private key path. Never add these keys to `mcp-token.env` or borrow the interactive connector credential.
- All repository operations accept an optional caller-owned `AsyncSession`; they must not commit a transaction they did not start.

- [ ] **Step 1: RED — test real PG invariants.**

Use the existing dedicated `session_factory` integration fixture and generated project/ticket IDs; add a test-only factory in this test package that creates its own valid projects/request ticket via existing repos. Tests clean only their own rows in reverse FK order.

```python
async def test_stale_contract_revision_cannot_replace_another_revision(delivery_case):
    import pytest
    from brain_v42.models.delivery import DeliveryError
    case = delivery_case
    first = await case.service.set_contract(**case.create_request(expected_revision=0))
    with pytest.raises(DeliveryError, match="revision_conflict"):
        await case.service.set_contract(**case.amend_request(expected_revision=0))
    current = await case.service.get(case.ticket_id, actor_project=case.requester)
    assert current.contract.contract_revision == first.contract_revision
```

Also test retry-identical versus key reuse, immutable history, unknown project/repo, requester/executor/self roles, no contract on FYI/terminal ticket, PR replacement identities, graph cycle and simultaneous opposite-edge insertion, scoped stable pagination, and actual context content drift. Run targeted tests against this task's disposable PG and retain RED.

- [ ] **Step 2: GREEN — implement relational identity and context resolution.**

Create all eight tables described above so later tasks extend behavior without repeatedly changing schema. Use JSONB for normalized payloads and relational FKs/unique constraints for identities. Register tables with a function in `db/delivery_tables.py` that receives `METADATA`; avoid circular imports. Pin historical migration DDL locally rather than importing mutable application metadata.

Acquire the graph advisory lock before sorted ticket/workflow row locks; graph edits take it exclusively, assessments use it shared. Choose fixed, documented distinct int64 namespaces for graph coordination and observer ownership. Contract mutations CAS revision and row version, persist an audit event and return the same result for identical retries.

Resolve Brain context from existing PG knowledge tables without embeddings. Freeze content fields, type/ID and a digest; omit access counters and changing timestamps from the content digest. Read-only views may compare without locks. Guarded mutations acquire `FOR SHARE` on referenced Brain entity rows before computing their current digests, because existing knowledge writers do not acquire the delivery graph lock. Acquire these source-row locks after the sorted ticket/workflow locks, ordered by fixed table identity and entity UUID, and retain them through the decision transaction. A missing required row is unavailable and prevents success. Use a fixed supported-type/table mapping. Test a concurrent native knowledge update against claim/completion: the update and decision must serialize, with no success based on an already changed digest. Repository refs preserve exact repo/commit/path as pinned references; expose their provenance, never describe a URL as an observed proof. Required references that cannot be resolved must block acquisition rather than be dropped.

Binding validates against operator registry and queues provider validation. Until the observer verifies actual PR base repository/branch, it is a proposed binding with no successful evidence. Changing a binding makes a new UUID and supersedes previous current proof eligibility. Repository access is always built from the configured origin and validated registry.

Feature flags govern new contract/binding operations and claims, not enforcement for already contracted tickets. Reads remain useful in disabled mode.

- [ ] **Step 3: Verify migration and commit.**

Exercise a fresh migration chain, schema metadata agreement, FK/unique constraints, non-destructive upgrade and refused populated downgrade on disposable databases. Run targeted integration/config tests plus domain tests, mypy/lint/layering. Commit `feat: persist versioned delivery workflows`.

## Task 4: Persist observations and immutable milestone receipts

**Files:**
- Create `src/brain_v42/repositories/pg_delivery_evidence.py`
- Extend `src/brain_v42/repositories/pg_delivery.py`
- Extend `src/brain_v42/services/delivery_service.py`
- Create `tests/integration/db/test_delivery_evidence.py`

**Interfaces:**
- Produces `publish_observation(session, binding_id, expected_binding_version, evidence, collection_started_at, collection_finished_at)` and `record_observation_error(session, binding_id, code, attempted_at)`.
- Produces transactional `issue_integration_receipt`, `accept` and input hydration for Task 2's evaluator.
- Observer passes a session bound to its owned connection; publish functions neither acquire a replacement pool connection nor commit outside the owning transaction.

- [ ] **Step 1: RED — preserve historical proof identity.**

```python
async def test_identical_reobservation_does_not_retimestamp_an_old_receipt(evidence_case):
    case = evidence_case
    first = await case.observe_success_and_accept()
    await case.observe_identical_later()
    historical = await case.read_receipt(first.id)
    assert historical.model_dump() == first.model_dump()
    assert await case.confirmation_count() == 2
    assert await case.snapshot_count() == 1
```

The test-only case performs real repository calls and SQL reads, not a mocked service. Add stale-binding publication, mixed/incomplete snapshot, API outage after acceptance, merge with missing check, dependency receipt requirements, explicit/automatic acceptance, obsolete digest rejection and competing acceptance/amendment tests. Run `.venv/bin/pytest -q tests/integration/db/test_delivery_evidence.py` and retain RED.

- [ ] **Step 2: GREEN — commit facts before projecting success.**

Deduplicate semantic snapshots, append a confirmation for each successful collection, and update latest pointers only for the active binding generation. Failures preserve the last successful content while updating attempt health. Perform input hydration/evaluation under shared graph + sorted workflow locks and issue receipts only when the pure evaluator permits the milestone. Receipts freeze exact confirmation IDs/times, contexts and upstream receipt IDs. Automatic acceptance produces a distinct fulfillment receipt with automatic basis; explicit acceptance is requester-only with nonempty rationale and exact expected revision/attempt/digest.

Support collection of already merged PRs: preserve pre-merge tested head/base association, integration SHA and evidence times. A missing CI record remains a blocker. No evidence write API is exposed to agents.

- [ ] **Step 3: Verify and commit.**

Run domain + contract + evidence tests with real PG, lint/mypy/layering; commit `feat: retain auditable delivery evidence and receipts`.

## Task 5: Fenced claims and canonical ticket completion guards

**Files:**
- Create `src/brain_v42/repositories/pg_delivery_claims.py`
- Create `src/brain_v42/repositories/delivery_ticket_guard.py`
- Extend `src/brain_v42/services/delivery_service.py`
- Modify `src/brain_v42/repositories/pg_ticket.py:PgTicketRepo.apply_transition`
- Modify `src/brain_v42/services/ticket_service.py:TicketService.transition`
- Create `tests/integration/db/test_delivery_claims.py`
- Create `tests/integration/db/test_delivery_ticket_guards.py`
- Adapt directly affected existing ticket repository/service tests

**Interfaces:**
- `DeliveryService.claim(ticket_id, actor_project, owner_key, work_kind, expected_workflow_version, expected_assessment_id, ttl_seconds=900) -> ClaimResult`.
- `renew_claim` and `release_claim` require ticket ID, matching actor/owner, claim token and epoch. Store only token digest, compare in constant time, never log it.
- `PgTicketRepo.apply_transition` accepts `action` and `actor_project`; for an existing delivery workflow, missing action/actor fails closed. Legacy uncontracted callers retain compatibility.
- `guard_delivery_transition(session, ticket_id, action, actor_project, expected_status, new_status)` runs under the existing transaction and returns any workflow disposition/generation mutations to apply before the ticket CAS.

- [ ] **Step 1: RED — prove concurrency and bypass resistance.**

```python
async def test_only_one_orchestrator_acquires_the_same_work(claim_case):
    import asyncio
    results = await asyncio.gather(
        claim_case.acquire("orchestrator-a"),
        claim_case.acquire("orchestrator-b"),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1

async def test_direct_repository_completion_cannot_bypass_the_contract(guard_case):
    import pytest
    from brain_v42.models.delivery import DeliveryError
    with pytest.raises(DeliveryError, match="delivery_requirements_unsatisfied"):
        await guard_case.complete_via_pg_ticket_repo()
    assert await guard_case.ticket_status() == "open"
```

Cover expiry/reacquisition/new epoch, stale renew/release, unavailable context, wrong role, token redaction, paused feature, self resolve, cross resolve/confirm, cancel/wontfix disposition, legal reopen attempt reset, terminal rejection and legacy tickets. Use controlled clocks/PG timestamp fixtures instead of sleeps. Race completion against amendment, binding replacement and reopen using independent real sessions/barriers. Retain RED evidence.

Also call the repository with inconsistent `(action, new_status)` pairs, including `start -> closed` and a `cancel` paired with a successful-delivery status. Assert refusal, unchanged status and no success receipt/disposition. Test that unchanged facts keep assessment identity stable while confirmation replacement, context drift, dependency generation, freshness expiry and lease expiry invalidate a submitted assessment.

- [ ] **Step 2: GREEN — put guard and mutation in one unit of work.**

Acquire graph/workflow locks before guard evaluation, re-read actual dependency generations and use PG time. A request carrying an old displayed version conflicts. Observer outage may leave a historical fulfilled receipt visible but prevents a new completion based on stale confirmation.

The service must pass the actual requested action to repository mutation so `cancel`, self `wontfix` and successful self `resolve` cannot be conflated by their common terminal status. Cancellation never becomes successful delivery. Reopen atomically increments attempt and claim epoch and clears active claim/current proof eligibility; it never rewrites receipt history. Closed tickets keep the existing terminal rule.

For a contracted ticket, the repository re-reads its actual kind/status/projects under lock and derives the only legal resulting status and actor role using the existing canonical `TRANSITIONS`/`SELF_TRANSITIONS` table. It refuses a passed `new_status` or `expected_status` inconsistent with that result, missing actor/action, illegal action or wrong project. The delivery guard operates on this validated transition, never on a caller-supplied action label paired with an arbitrary status. Keep compatibility for legacy uncontracted direct callers.

All API compositions call the same repository guard. Do not implement a guard only in a tool, gateway route or service precheck. The paused feature retains that guard.

- [ ] **Step 3: Verify and commit.**

Run claims/guard PG tests and existing ticket atomicity, models/service, MCP ticket and gateway transition tests. Inspect real call sites after GitNexus impact. Commit `feat: fence delivery claims and ticket completion`.

## Task 6: GitHub collection, machine authentication and rate handling

**Files:**
- Create `src/brain_v42/delivery_observer/__init__.py`
- Create `src/brain_v42/delivery_observer/auth.py`
- Create `src/brain_v42/delivery_observer/github.py`
- Extend `src/brain_v42/delivery_config.py`
- Modify `pyproject.toml` and `uv.lock` to declare directly used JWT crypto dependency
- Create `tests/unit/delivery_observer/test_auth.py`
- Create `tests/unit/delivery_observer/test_github.py`

**Interfaces:**
- `GitHubClient(http: httpx.AsyncClient, settings: DeliverySettings, auth: GitHubAuthProvider)`.
- `collect(binding: ArtifactBinding, contract: ContractRevision) -> PullRequestEvidence` with collection interval and complete proof metadata; failures carry sanitized typed provider codes.
- `GitHubAuthProvider.authorization_headers()` supports dedicated fine-grained token or App installation tokens with expiry-aware refresh. Authentication can be injected through a fake token provider in tests.

- [ ] **Step 1: RED — test through controlled HTTP responses.**

Use `httpx.MockTransport` at the network boundary with realistic full GitHub payloads. Record requested URL/headers privately in fixtures; assertions must demonstrate correct evidence association and refusal behavior, never print a bearer.

```python
async def test_head_change_during_collection_rejects_the_snapshot(github_case):
    import pytest
    from brain_v42.models.delivery import DeliveryError
    github_case.serve_head_change_between_pr_reads()
    with pytest.raises(DeliveryError, match="provider_revision_changed"):
        await github_case.client.collect(github_case.binding, github_case.contract)
```

Cover wrong repository/base, forks, merge/squash/rebase payloads, synthetic merge SHA provenance, paginated checks/statuses/reviews, newer pending rerun, dismissed reviews, partial/truncated lists, 401/403/404/429/5xx, timeout, malformed/oversized JSON and redirect to a different origin. Auth tests use a generated test RSA key and validate claims/expiry with fixed time; never access live secrets. Retain RED.

- [ ] **Step 2: GREEN — implement bounded source collection.**

Use a fixed operator-configured API origin (HTTPS in production), explicit Accept/version headers and manually validated same-origin pagination/redirects. API requests use registered repo identities; validate returned numeric repo IDs. Read PR before and after evidence collection. Fetch applicable full pages (`per_page=100`, limit 20); stop with incomplete evidence if more pages exist. Fetch check runs with all relevant attempts rather than selecting an old completed success. Read reviews and commit statuses when the policy requires them.

For a check whose `head_sha` is a synthetic merge revision `S`, verify it via `GET /repos/{owner}/{repo}/git/commits/{S}`: the response SHA must equal `S` and its two ordered parent SHAs must equal the evaluated base `B` and PR head `H`. The opening and closing PR reads must agree on repository ID, PR number, `head.sha=H`, `base.sha=B`, state, and (while open) `merge_commit_sha=S`. Fetch checks for both `H` and this verified `S` when needed; a positive check records `(repository_id, pr_number, H, B, S, check_run_id, app_id, app_slug, check_suite_id)` plus the parent proof. A known synthetic SHA from the binding's earlier complete observation can be rechecked after merge, preserving the evaluated pair; do not infer its parents from the now-advanced base branch. If no such proof can be recovered, expose `revision_unverifiable` instead of treating unrelated current-base checks as success. This uses the documented [Git commit object](https://docs.github.com/en/rest/git/commits#get-a-commit-object) and [check runs by reference](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference) APIs; it is our explicit evidence policy, not a GitHub atomicity guarantee.

Provide full positive HTTP fixtures for direct-head and synthetic checks, and negative fixtures changing each of repository/PR/head/base/synthetic SHA/ordered parents/provider identity. Record the live canary's actual check/run revision association; if GitHub exposes its PR checks at `H`, validate that direct association and exercise the synthetic positive fixture separately. Record actual commit/revision identities so merges do not masquerade as deployments.

Enforce 10-second total request timeout, at most two concurrent requests, and shared 40 requests/minute budget. Respect `Retry-After`/rate reset with bounded exponential backoff; expose retry scheduling rather than sleeping for unbounded time inside one collection. App JWT uses RS256 and bounded issuer/iat/exp, installation tokens refresh before expiry. Declare `PyJWT[crypto]>=2.13,<3` directly; retain the existing locked 2.13.0 if compatible rather than upgrading unrelated dependencies.

Do not infer API capability solely from a token type or permission label. GitHub's generic PAT documentation lists a Checks API limitation while the read endpoint documents fine-grained and public access. The deployment preflight must actually read the required endpoints with the configured identity. For the public `hawkixs/brain-v42` token setup, request Contents/Pull requests/Commit statuses read and verify check visibility directly. Never silently drop checks or fall back to an unrelated identity if access is denied; private repositories may require the supported dedicated GitHub App path.

- [ ] **Step 3: Verify and commit.**

Run observer HTTP/auth and config tests, lint/mypy/layering, and `uv lock --check`. Commit `feat: collect GitHub delivery evidence with machine credentials`.

## Task 7: Independent observer process, fairness and ownership

**Files:**
- Create `src/brain_v42/delivery_observer/runtime.py`
- Create `src/brain_v42/delivery_observer/__main__.py`
- Extend evidence repository scheduling helpers
- Create `tests/unit/delivery_observer/test_runtime.py`
- Create `tests/integration/test_delivery_observer.py`

**Interfaces:**
- `DeliveryObserverRuntime.run(stop_event: asyncio.Event) -> int` and `run_once(project_key: str | None = None) -> ObservationRunResult`.
- CLI `python -m brain_v42.delivery_observer --once [--project-key brain-v42]`; without `--once`, supervise polling until SIGTERM/SIGINT.
- `ObservationRunResult` reports collected/failed/deferred counts, last success/lag and exit status; never credentials or raw private payloads.

- [ ] **Step 1: RED — restart, fencing and fair scheduling.**

```python
async def test_observer_never_publishes_after_ownership_connection_is_lost(observer_case):
    case = observer_case
    await case.start_collecting()
    await case.terminate_owner_backend()
    await case.finish_http_response()
    assert await case.confirmation_count() == 0
    assert await case.wait_exit() != 0
```

Use real PG for ownership/publication and an in-process HTTP fixture. Cover duplicate process conflict, cancellation cleanup, restart/replay, refresh queued while offline, no embedding/graph availability, and error preserving prior success. A controlled scheduler test simulates 20 changed PRs with at most five requests each, ≤1-second responses and the rate budget; all become visible within the specified 300-second baseline envelope with no starvation. Retain RED.

- [ ] **Step 2: GREEN — compose only required resources.**

Build dedicated engine/client/token provider, acquire the observer advisory lock on a dedicated **non-autocommit** connection, and finish the short lock-acquisition transaction while retaining its session advisory lock. Do not reuse `automation.OwnershipLease`: its AUTOCOMMIT connection/watcher contract is incompatible with transactional evidence writes. Serialize all uses of the owned connection (publication, ownership/heartbeat queries, release) through one mutex/publisher queue; two HTTP collections may overlap, two AsyncSessions on that connection may not. Bind each publication session to that connection and use an explicit transaction. Before publication, verify the original backend identity and owned lock; connection invalidation or backend replacement permanently loses ownership and stops the runtime. No transparent pool reconnect may publish using stale ownership. Finish/roll back transactions before network waits; acquire graph/workflow locks only while publishing/evaluating.

Add tests for two concurrent HTTP completions publishing serially without connection-operation conflicts, and for an injected failure after the snapshot insert rolling back snapshot/confirmation/pointer/receipt changes together. These are required in addition to the backend-termination ownership test.

Schedule from persisted due work in a fair stable order; start a new process with no memory of prior agents. Settings default polling 60 seconds and freshness 600 seconds. Terminal receipts remain historical; unfinished workflows and due refreshes are reconciled. No model client, semantic ingestor or Dream runtime is imported. SIGTERM stops new collection, cancels bounded pending work and closes every resource.

- [ ] **Step 3: Verify and commit.**

Run runtime/observer integration tests, all delivery tests, lint/mypy/layering, and execute CLI `--help`. Commit `feat: run the independent delivery observer`.

## Task 8: MCP operations, briefing and gateway compatibility

**Files:**
- Create `src/brain_v42/mcp/tools/delivery_tools.py`
- Create `src/brain_v42/mcp/tools/delivery_formatters.py`
- Modify `src/brain_v42/mcp/server.py` composition/registration
- Modify `src/brain_v42/mcp/tools/ticket_tools.py` reads/rendering
- Modify `src/brain_v42/mcp/tools/session_tools.py` optional delivery briefing section
- Modify `src/brain_v42/mcp/business_errors.py` for typed delivery errors
- Modify `src/brain_v42/codex_gateway/ticket_routes.py` error mapping where required
- Extend gateway composition only if required to retain canonical guard behavior
- Create `tests/unit/mcp/test_delivery_tools.py`
- Create `tests/integration/test_delivery_mcp.py`
- Extend existing briefing, discovery, capability and gateway contract tests
- Update `docs/MCP_TOOLS.md`

**Interfaces:**
- Register the nine operations in spec §8, with typed structured results and stable error codes.
- `register_delivery_tools(mcp, delivery_svc)`; reads return `DeliveryView`/`DeliveryPage`, writes return versioned mutation results.
- `format_delivery_briefing(page: DeliveryPage) -> str` is bounded, French, and never invokes GitHub.

- [ ] **Step 1: RED — exercise the actual tool path.**

Use FastMCP's local client against actual registration and PG-backed service for E2E. Unit tests may isolate a service boundary only for malformed tool argument/error formatting behavior.

```python
async def test_tool_completion_refuses_a_contract_without_observed_proof(mcp_delivery_case):
    case = mcp_delivery_case
    await case.create_contract_through_mcp()
    result = await case.call_legacy_resolve()
    assert result.isError or "delivery_requirements_unsatisfied" in case.text(result)
    assert await case.ticket_status() == "open"
```

Test registration under actual discovery gateways, structured schema stability, Dream-scoped mutation refusal on direct and gateway calls, wrong project/full UUID requirements, stale CAS, claim tokens redacted from read/list, legacy transitions across MCP/gateway/service, and the same evidence appearing in both delivery read and briefing. A broken GitHub fixture must not stall the briefing. Cap five delivery rows and show omitted count plus `brain_delivery_list` for the remainder.

- [ ] **Step 2: GREEN — wire shared services without inflating session schemas.**

Build `DeliveryService` using the existing PG session factory. Append the delivery section to the briefing text; keep lifecycle tool output schema budgets intact. The new section must report unavailable observation honestly, preserve the existing ticket groups and date the free-form focus as declared context.

Enforce requester/executor roles inside services, not only discovery. Dream allowlists continue to exclude every new mutation. Delivery tools use existing authorization provenance and sanitized error surfaces. Gateway HTTP maps version/claim/guard conflicts to 409 and authorization/validation failures through its established handling. The guard stays active even if public delivery tools are disabled.

- [ ] **Step 3: Verify and commit.**

Run delivery MCP E2E plus existing ticket/session/discovery/gateway/capability tests, full mypy/lint/layering and documentation consistency checks. Commit `feat: expose observed deliveries to external orchestrators`.

## Task 9: Deployment artifacts and release verification harness

**Files:**
- Create `deploy/systemd/brain-v42-delivery-observer.service.tmpl`
- Create `deploy/systemd/install-delivery-observer.sh`
- Create `scripts/check_delivery_deployment.py`
- Create `scripts/verify_delivery_canary.py`
- Create `docs/runbooks/2026-09-07-observable-delivery-workflows.md`
- Create `tests/unit/deploy/test_delivery_observer_unit.py`
- Create `tests/unit/test_delivery_canary.py`
- Update README feature/configuration documentation and relevant packaging tests

**Interfaces:**
- Installer has `--check-only` and `--render-dir ABSOLUTE_NEW_DIRECTORY`; renders only this observer's unit, never starts/stops another service. Production publication/activation remains coordinator-owned.
- `check_delivery_deployment.py` verifies schema capability, configured minimum guarded revision, required service settings/private file permissions and GitHub access. Output is sanitized JSON, with nonzero status on a required failure.
- `verify_delivery_canary.py` consumes explicit ticket/contract/PR identities and the authenticated Brain endpoint configuration; it verifies evidence through public APIs and emits a timestamped JSON receipt. It never silently creates a session or executes agent work.

- [ ] **Step 1: RED — test executable deploy behavior.**

Run installer against a temporary directory and controlled systemd verifier, asserting outputs, permissions and absence of changes outside the target directory. Test missing credential/schema, wrong active revision, unsafe redirect/path, and guarded paused mode via MCP/gateway/direct service. Canary tests use controlled Brain/GitHub responses to reject stale head, missing proof, unrelated PR and dormant observer.

```python
def test_deployment_preflight_refuses_a_pre_guard_rollback(deployment_case):
    case = deployment_case
    result = case.run_check(current_revision="old", minimum_revision="guarded")
    assert result.returncode != 0
    assert "incompatible_application" in result.stdout
    assert case.live_files_unchanged()
```

- [ ] **Step 2: GREEN — make the rollout concrete.**

Observer unit uses the production interpreter/package path and dedicated private `EnvironmentFile`, safe systemd restrictions, restart-on-failure, bounded stop timeout and no inbound listener. Private credential file contains only the needed delivery keys; preflight verifies owner, regular-file/no-symlink status and mode 0600 without printing values. A GitHub token is not installed by copying an interactive agent credential.

Runbook enumerates all measured writers, backup validation, additive migration, dormant application/observer deployment, opt-in activation, live canary and guarded rollback. Include exact commands matching the implemented CLI. The canary records running revision, migration, observer success, old/new PR head, CI evidence, integration and acceptance. Do not claim deployment proof from code merge or a green test alone.

- [ ] **Step 3: Verify and commit.**

Run deployment/canary tests, build a wheel with `uv build`, inspect that migration/CLI modules are included and run the wheel packaging tests. Commit `feat: prepare verified delivery workflow deployment`.

## Task 10: Coordinator integration, production activation and evidence

**Files / responsibility:** coordinator owns branch integration and deployment. Only bounded repairs go back to an implementer with new RED evidence. Production evidence is recorded under `ops/recovery/receipts/2026-09-07-delivery-workflows.md` with no secret values.

**Consumes:** Tasks 1–9, exact final branch SHA, independent reviews, machine credential availability and live operational preflight.
**Produces:** normally pushed integrated commit, deployed matching application/observer/schema, successful real canary, a receipt clearly identifying rollback as prepared/test-proven or actually exercised, and a Brain delivery status update that does not close a session.

- [ ] **Step 1: Verify the complete branch personally.**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/
.venv/bin/python scripts/check_module_layering.py --package src/brain_v42
.venv/bin/python scripts/check_container_image_pins.py
TASK_TEST_RUNNER=/tmp/brain-observable-workflows-01a07afd/.superpowers/sdd/2026-09-07-observable-delivery-workflows/with_test_db.py
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/alembic upgrade head
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/pytest tests/unit/ -q --junitxml=/tmp/brain-delivery-unit.xml
.venv/bin/python scripts/check_coverage_saw_the_db_tests.py /tmp/brain-delivery-unit.xml
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/pytest tests/integration/ -q --junitxml=/tmp/brain-delivery-integration.xml
.venv/bin/python scripts/check_coverage_saw_the_db_tests.py /tmp/brain-delivery-integration.xml
.venv/bin/python "$TASK_TEST_RUNNER" .venv/bin/pytest tests/unit/ --cov=brain_v42 --cov-report=term-missing --cov-fail-under=60 -q --junitxml=/tmp/brain-delivery-coverage.xml
.venv/bin/python scripts/check_coverage_saw_the_db_tests.py /tmp/brain-delivery-coverage.xml
uv build
```

Use this task's disposable PG for every database command; enable no production service in tests. Record unrelated baseline failures separately, and resolve every introduced failure. Run the existing security checks used by CI, then obtain an independent whole-branch review of the exact verified commit range. Stop any remaining **implementation subagent** before Git integration so its worktree is stable. Production services continue running at this stage.

- [ ] **Step 2: Integrate against current upstream and publish normally.**

Fetch origin, integrate any advanced main into the isolated feature without rewriting published history and rerun required gates on the resulting graph. Publish a normal feature push and PR so the actual proposed change passes the PR CI rail before production activation; a direct main-only push is insufficient. Merge the reviewed PR through the repository's allowed non-force path and record exact remote refs. Production MCP and metrics were measured loading the canonical checkout's editable package; therefore do not update that checkout while its processes run. Build and verify the immutable release from the integrated commit in an isolated checkout. Update a clean local main only inside the deployment window below, preserving unrelated changes; no stash or forced checkout.

- [ ] **Step 3: Prepare and execute the measured deployment runbook.**

Identify every running ticket writer and coordinate a short MCP/gateway upgrade window while preserving Brain session records. Order the deployment: verify backup and preflight, pause watchdog/restart triggers as required, quiesce measured ticket-writing processes, apply the additive migration, upgrade those processes, then activate the observer and canary. Production writer quiescence happens only in this window, after Git integration/CI. Validate private service credentials without exposing them; if absent, request that one external prerequisite while continuing every independent implementation/check. Do not label missing access a completed deployment.

Deploy the verified immutable package, preflight and observer unit; upgrade all writers before activating contracted workflows. Pin the runtime path to the release identity so later worktree edits cannot mutate serving code. Reuse existing private environment-file references without disclosing values, and keep a guarded compatible release for rollback. Update canonical main only while affected editable-package processes are quiesced and the tree is clean. Do not use the general installer uninstall path. Record application/schema/observer identities before creating the canary contract. Apply the permanent no-agent-launch boundary in the running service.

- [ ] **Step 4: Exercise the complete production path.**

Create one explicitly labelled documentation-only canary PR and a self-ticket with extraction disabled. Define its contract before observing success. Observe missing evidence, push a second harmless documentation commit, wait for observed SHA/CI update, and verify the legacy completion guard before merge. Merge only after the normal checks/review; observe integration and record explicit acceptance. Confirm the shared Brain briefing reports the same receipt and the observer continues without an agent updating the ticket. Keep the canary history.

- [ ] **Step 5: Persist the receipt and final state.**

Commit the nonsecret production receipt and push it normally with appropriate checks. Mark rollback `prepared and test-proven` unless a real production guarded pause was executed and recorded; never claim an unexecuted rollback. Update the Brain feature/durable architectural decision using compare-and-swap where applicable, preserving concurrent focus changes. Report what is actually deployed, its SHAs, verification and any remaining limitation. Do not close the Brain session without the user's explicit lifecycle command.

## Spec coverage and completion rule

| Approved requirement | Tasks |
| --- | --- |
| Contract schema, canonical digest, amendments, provenance | 1, 3 |
| Exact GitHub binding, checks/reviews, revision invalidation | 2, 3, 6 |
| Context snapshots and pinned receipt dependencies | 3, 4 |
| Deterministic assessment and historical receipts | 2, 4 |
| Concurrent orchestration claims and lifecycle guards | 5 |
| Independent polling, ownership, budget and failure recovery | 6, 7 |
| Structured MCP, briefing and existing-client compatibility | 8 |
| Migration/package/unit/rollback/readiness checks | 3, 9 |
| Real production E2E canary and final receipt | 10 |

The task is not complete at plan, implementation, merge, dormant installation or green CI. Completion requires the deployed canary and receipt from Task 10. If an external credential or permission remains unavailable, retain all verified artifacts and report the precise missing prerequisite without claiming production success.
