# Observable delivery workflows

**Date:** 2026-09-07

**Status:** proposed specification; implementation and production activation are unverified

**Project:** `brain-v42`

**Source baseline:** `4990a920c2836b8d434c60ea17d6ed7e5ac0e51a`

**Delivery target:** a complete GitHub-backed workflow, available through Brain MCP and verified on the production service.

## 1. Purpose

Brain must expose the actual delivery state of work even when an agent forgets to
update a ticket, exits unexpectedly, or leaves an outdated project summary.
An external orchestrator defines the expected work in Brain, reads the resulting
state and context, and decides how to execute it.

**Brain never launches, stops, restarts, schedules, or selects execution agents.**
This is a permanent boundary. A work item becoming eligible only changes the data
available to an external orchestrator.

This feature supplies a versioned contract, an independent GitHub observer, a
deterministic evaluator, and a consumable view of evidence and missing work. It
does not require an LLM, embeddings, Neo4j, Dream, or an active agent session to
observe GitHub and calculate delivery state.

## 2. Ownership and sources of authority

| Concern | Authority |
| --- | --- |
| Objective, scope, priority, acceptance rules, permitted work | User and external project orchestrator, recorded in Brain |
| Ticket assignment and coordination lifecycle | External project orchestrator, through Brain's existing ticket rules |
| PR identity, commit revisions, CI results, GitHub reviews, merge facts | GitHub, read directly by the observer |
| Delivery assessment, evidence freshness, missing requirements | Brain's deterministic evaluator |
| Agent selection, execution, retries, stopping, human escalation | External orchestrator |
| Product acceptance or a justified contract amendment | Authorized decision maker, explicitly recorded |

The contract stored in Brain is canonical. Its JSON export is portable and
versioned; a repository copy may reference its revision and digest. This release
does not create two independently editable contract authorities in Git and Brain.

An agent's message, a ticket's `closed` status, an issue-closing keyword, and a
branch name are not evidence that the delivery contract was fulfilled.

## 3. Production scope

The first production release includes the entire following path:

1. Attach a versioned delivery contract to a `request` ticket.
2. Pin context and dependencies before execution.
3. Bind each expected code deliverable to an explicitly identified GitHub PR.
4. Observe PR revisions, checks, reviews and merge facts without agent activity.
5. Calculate an explainable delivery assessment and eligible next work.
6. Reserve eligible work atomically for an external orchestrator.
7. Display the assessment in ticket reads and session briefings.
8. Enforce proof requirements through existing ticket completion paths.
9. Record explicit acceptance when required by the contract.
10. Deploy, observe a real GitHub PR through the production service, and retain a
    production verification receipt.

GitHub polling is the required observation mechanism in this release. Webhooks
are a later latency optimization; the release must not require an inbound public
endpoint or webhook delivery to remain correct.

The release does not include automatic deployment observation, generic execution
of runbooks, arbitrary workflow scripts, automatic rewriting of knowledge, a new
dashboard, GitLab parity, or a new agent runtime. The API rejects unsupported
proof types instead of silently treating them as satisfied. An operational or
research ticket can continue using the existing coordination lifecycle until a
corresponding proof adapter is specified.

## 4. Existing integration points

These observations describe the source baseline, not a permanent deployment fact:

- `models/ticket.py` defines coordination states and requester/executor roles.
  Self-tickets can currently close directly through `resolve`.
- `services/ticket_service.py` applies lifecycle rules;
  `repositories/pg_ticket.py` makes status changes atomically.
- `mcp/tools/ticket_tools.py` exposes ticket operations. The Codex gateway also
  invokes `TicketService` through `codex_gateway/ticket_routes.py`.
- `mcp/tools/session_tools.py` renders tickets alongside handwritten project focus.
- `services/gitlab_ingestor.py` and `services/status_engine.py` provide semantic
  feature discovery and advancement. They are not the evidence authority for
  this feature: ingestion depends on embedding, and a merge can map to `deployed`.
- `automation/runtime.py` currently constructs embedding and deduplication
  resources. The new observer has a separate, small process composition so that
  these dependencies cannot stop observation.
- Production MCP is managed by `brain-mcp-http.service`. Deployment must account
  for its watchdog and every other running ticket writer, including the gateway.

GitNexus evidence was degraded during discovery: index freshness could not be
established against the current history. Native source reads establish the facts
above. Implementation must repeat upstream impact checks before symbol changes.

## 5. Contract

### 5.1 Identity and revision

One ticket has at most one delivery workflow. A workflow has immutable contract
revisions and one current revision. A contract can be attached only to an open or
in-progress `request` ticket; `fyi` tickets retain acknowledgement semantics.
It also has an `attempt` generation, initially 1. Reopening an unsuccessful or
previously delivered attempt increments this generation atomically; it does not
rewrite the contract or prior receipts.

Every contract revision contains:

- `schema_version`, initially `1`;
- `ticket_id`, `contract_revision` and a canonical content digest;
- objective, constraints, acceptance criteria and declared priority;
- contextual references and the snapshots described below;
- dependencies and their required milestones;
- one or more named code deliverables;
- acceptance mode: `automatic` or `explicit`;
- author provenance, creation time, and amendment reason for revisions after 1.

Contract creation and amendments require an expected current revision. Creation
uses `expected_revision=0`; a stale revision fails with `revision_conflict`.
An idempotency key binds to the request digest: an identical retry returns the
original result; reusing the key for different content fails.

Amendments preserve prior contracts, observations and acceptance records. They
immediately cause reevaluation. An old acceptance cannot satisfy a new revision.
Changing the ticket's prose does not silently amend its contract.

### 5.1.1 Digest format

The server computes and returns every digest; it never trusts a supplied digest
without comparing it with its own calculation. The normalized v1 contract payload
contains the objective, constraints, acceptance criteria, priority, pinned context,
dependencies, deliverables and acceptance mode. Repository names resolve to pinned
numeric IDs before normalization. It excludes author, timestamps, ticket ID,
contract revision, current observations, claims and the digest itself.

Normalization emits all schema fields, including explicit defaults and nulls,
rejects extra keys and floating-point numbers, and preserves list order and Unicode
code points without normalization. Invalid Unicode surrogate code points are
rejected. JSON uses UTF-8, lexicographically sorted object keys, no ASCII escaping,
no whitespace separators, and lowercase JSON booleans/null. The hex digest is
SHA-256 of `brain-delivery-contract:v1\n` followed by those JSON bytes. Implementations
must preserve this format for schema v1; changed canonicalization requires a new
schema version.

Canonicalization test vector (an algorithm fixture, not a valid whole contract):
`{"z":null,"a":["é",1,true]}` becomes `{"a":["é",1,true],"z":null}` and produces
`3a9f5e4221a8f912fdb0af7ba4087dbd02fbcb3c82713f6676cb69bcbf3d87af`.
Idempotency and delivery digests use the same encoding with distinct prefixes
`brain-delivery-request:v1\n` and `brain-delivery-result:v1\n`. A request digest covers
operation, actor project, target IDs, expected versions and the complete validated
payload, excluding the idempotency key. A delivery digest covers contract digest,
attempt and deliverable-key-sorted binding IDs, repository IDs, PR IDs, evaluated
head/base pairs and integration revisions. Each replacement creates a new binding
ID, including a later return to a formerly bound PR. The test suite also supplies
a complete contract vector.

### 5.2 Deliverables

Each deliverable has a stable key, an allowed repository, a target branch, an
explicit required-check policy and a review policy. One deliverable binds to one
active PR. A ticket requiring several PRs declares several deliverables; all are
required. Replacing an active PR binding is an explicit, audited operation.

Repository registration resolves and pins the GitHub numeric repository ID.
Names and URLs are display and discovery attributes, not the sole identity.
The bound PR must belong to the declared base repository and target branch.
A similarly named branch, commit message or PR cannot create a binding.

The required-check list is explicit. An empty list requires a nonempty
`no_checks_reason`; missing policy is invalid. Each check identifies its name,
provider kind (`check_run` or `commit_status`), and trusted provider identity
where the API exposes it. Duplicate ambiguous selectors are rejected.

Review policy declares the number of approvals and allowed reviewer identities.
Zero approvals is an explicit policy choice, never inferred from an empty API
response. A PR author's own review cannot satisfy an independent-review rule.

### 5.3 Context

The contract records relevant Brain entity references with their types, IDs,
content snapshots and content digests. Repository document references pin a
repository ID, commit SHA and path. An external URL is a reference only; its
presence is not a freshness or acceptance proof.

The orchestrator receives the pinned context used to define the work. For Brain
entities, reads also compare the current content digest with the pinned digest:
`unchanged`, `changed`, or `unavailable`. A required context reference that changed
or became unavailable blocks acquisition of new work until an explicit contract
amendment accepts the updated context. Existing execution is not stopped by Brain;
the discrepancy is exposed to its orchestrator.

The observer does not semantically judge whether arbitrary code changes invalidate
a runbook. General knowledge-source invalidation is outside this release.

### 5.4 Dependencies

A dependency names another ticket, a specific contract revision and attempt, and
a required milestone: `integrated` or `accepted`. The referenced workflow must
exist and be visible to the caller. Plain legacy ticket closure does not satisfy
a dependency. An `integrated` dependency requires an immutable integration receipt
issued after all technical, context and dependency requirements passed; the stage
`integrated` or a bare `merged=true` is insufficient. An `accepted` dependency
requires the fulfillment receipt described in section 7.2.

Self-dependencies and cycles are rejected. Concurrent dependency edits serialize
through a PostgreSQL graph-mutation lock so that two individually valid requests
cannot create a cycle together. An upstream amendment produces
`dependency_revision_changed`; an upstream reopen produces
`dependency_attempt_changed`. Adopting either requires a downstream amendment.
Cancelled or abandoned delivery never satisfies a successful dependency.
Already issued downstream receipts retain the upstream receipts they relied on
and remain historical facts. Dependency changes block new downstream acquisition
or completion; they do not rewrite deliveries previously accepted.

### 5.5 Example contract input

The following is illustrative input for the existing `brain-v42` project; ticket
and knowledge IDs are supplied by the caller when creating the actual contract.

```json
{
  "schema_version": 1,
  "objective": "Expose delivery progress from GitHub evidence",
  "constraints": ["Brain never launches execution agents"],
  "acceptance_criteria": [
    "A new commit requires evidence for that revision",
    "The session briefing shows missing proofs without an agent update"
  ],
  "priority": 20,
  "context_refs": [],
  "dependencies": [],
  "deliverables": [
    {
      "key": "implementation",
      "repository": "hawkixs/brain-v42",
      "target_branch": "main",
      "required_checks": [
        {"kind": "check_run", "name": "lint-ruff", "app_slug": "github-actions"},
        {"kind": "check_run", "name": "lint-mypy", "app_slug": "github-actions"},
        {"kind": "check_run", "name": "test-unit", "app_slug": "github-actions"},
        {"kind": "check_run", "name": "test-integration", "app_slug": "github-actions"}
      ],
      "review": {"required_approvals": 0, "allowed_reviewers": []}
    }
  ],
  "acceptance_mode": "explicit"
}
```

The example is not the complete release gate for this feature. Section 12 governs
the actual implementation's release, including coverage, review and live checks.

## 6. Observations and reconciliation

### 6.1 Independent observer

A dedicated `brain_v42.delivery_observer` entry point provides `--once` for
verification and a supervised polling mode for production. It performs outbound
HTTPS requests to configured GitHub API endpoints and writes observations to PG.
It exposes no command executor, agent launcher, merge operation or deployment API.

The polling process starts without embedding, graph, MCP-session or LLM services.
Reads through MCP use persisted observations; they never wait for GitHub network
requests. An authenticated refresh request marks work due for the observer and
returns immediately without asserting that synchronization succeeded.

There is one fenced observer owner for a deployment. A dedicated PostgreSQL
connection owns the advisory lock and the transactions that publish observations.
Loss of that connection prevents publication and stops that observer. A second
process cannot become an unfenced concurrent writer. Network I/O occurs outside
database transactions.

### 6.2 GitHub access

The client uses `httpx` and the documented REST API. It supports a GitHub App
installation for a long-lived service, with short-lived installation tokens, and
an explicitly configured fine-grained read token for an initial installation.
Credentials are held in a private service configuration, never in contracts,
database evidence, tool arguments, logs, repository files or response bodies.

The permissions are read-only and limited to registered repositories: metadata,
pull requests, checks, commit statuses and repository contents as needed for
revision identity. Brain does not borrow the interactive agent's GitHub connector
or run `gh auth token`. The connector available to this task is not proof that
the production service already has an appropriate credential.

The API origin is fixed by operator configuration. User-supplied URLs cannot
redirect credentials to another origin. Repository redirects are accepted only
within that origin and after repository identity validation. Private repositories
with inaccessible credentials remain unavailable; no success is inferred.

### 6.3 Complete evidence snapshots

An observation includes repository ID, PR number, PR state and draft flag, base
branch and SHA, head repository and SHA, merge result, required check results,
relevant review records, observation time and provider record IDs/URLs.

Collection reads the PR, fetches all required evidence pages, then re-reads the PR
identity/revisions. If head, base or lifecycle changed during collection, the
snapshot is rejected and retried within the bounded budget. Partial pagination,
missing permissions and malformed provider data yield an incomplete observation.

An assessment is an as-of statement about the revisions and provider records
observed during its collection interval. Polling does not atomically synchronize
GitHub and PostgreSQL: a provider change becomes visible on a later successful
observation. `fresh` describes observation age, not proof that nothing changed
since collection. The external orchestrator must revalidate and condition its
GitHub actions on the expected head/base and applicable repository rules.

Before integration, checks must map to the current PR head. If a check evaluates
a synthetic merge revision, its association with this PR's current head/base pair
must be established from provider metadata. An unrelated merge SHA is never
accepted. If that association cannot be established, the result is `unknown`.

For a check selector, the newest applicable attempt is authoritative: a rerun
that is pending or failed supersedes an older success. Only an explicit success
counts. Missing, skipped, neutral, cancelled, timed-out and unknown results block
unless a future schema explicitly introduces a different policy.

Approvals must reference the current head revision and an allowed reviewer. Use
the latest effective decision per reviewer; a dismissal or subsequent request for
changes invalidates the earlier approval. An effective request for changes from
a configured reviewer blocks readiness. Review comments alone are not approvals.

After merge, retain the last evaluated head SHA and the actual resulting merge,
squash or rebase revision reported by GitHub. Do not demand SHA equality between
the pre-merge head and the integration revision. A merge observed before valid
required evidence is collected is `integrated` with unsatisfied requirements,
not a successful contract.

### 6.4 Freshness and failure

Defaults: polling every 60 seconds, freshness limit 600 seconds, bounded request
budget of 40 requests per minute per configured credential, maximum 2 simultaneous
requests, and 10 seconds total timeout per request. Scheduling is fair across
active PRs; budget exhaustion cannot repeatedly starve the same ticket.

The client respects GitHub retry and rate-reset headers and backs off on errors.
Pagination is bounded to 20 pages of 100 records per evidence family; reaching a
bound with more results makes that family incomplete. Configuration exposes the
bounds and reports backlog/lag instead of certifying truncated evidence.

Each read returns `last_attempt_at`, `last_success_at`, `fresh_until` and
`observation_health`: `never_observed`, `fresh`, `stale`, `error`, or `disabled`.
Freshness is evaluated against the current server clock at read and mutation
time, so a dead observer cannot leave a cached `fresh=true` indefinitely.

An error retains the last known facts with their timestamps. It cannot create a
new success or permit a new completion based on unverified current state.
An already recorded historical merge or acceptance remains a dated historical
fact during an outage; an outage alone does not erase it or reopen a ticket.

Observations with identical semantic content reuse an immutable content-snapshot
identity. Each successful collection creates a separate immutable confirmation
with its own collection interval and snapshot ID. A mutable latest-confirmation
pointer supports fast reads; it never changes the timestamps of an older
confirmation. Receipts reference exact confirmations, so later polls cannot make
an old acceptance appear retrospectively fresher. Changed facts create a new
content snapshot. A restarted process reconstructs its schedule from PG and
reconciles unfinished workflows; agent memory and in-memory queues are not required.

## 7. Assessment and lifecycle

### 7.1 Orthogonal state

Avoid a single forward-only enum that conflates progress, health and acceptance.
The public assessment carries these separate dimensions:

| Field | Meaning |
| --- | --- |
| `coordination_status` | Existing ticket status, owned by the project orchestrator |
| `delivery_stage` | `awaiting_artifact`, `proposed`, `verified`, `integrated` |
| `observation_health` | Current ability to trust the provider view |
| `acceptance_state` | `not_required`, `pending`, `accepted`, `superseded` |
| `requirements_satisfied` | All live evidence, context and dependency predicates hold as of the current assessment |
| `completion_eligible_now` | A new completion is permitted by fresh evidence and the applicable receipt/acceptance rules |
| `contract_fulfilled` | A valid historical fulfillment receipt exists for the current revision, attempt and delivery digest |
| `blockers` | Stable reason codes with the exact missing or invalid evidence |
| `eligible_work` | Work the external orchestrator may choose to acquire |

Stages describe observed progress, not authorization. A draft PR, unavailable
mergeability, conflicts, stale evidence or unsatisfied requirements prevents an
integration-ready recommendation. A provider check passing never grants a right
to merge or deploy. Those permissions remain external.

The evaluator is a pure function of contract, bindings, observations, context,
dependencies, acceptance and current time. Replaying the same inputs produces
the same assessment. No public API accepts an arbitrary observed state, a CI
result, or `contract_fulfilled=true` from an agent.

### 7.2 Acceptance and revisions

The server emits an immutable integration receipt only after observing merge and
satisfying technical, context and dependency predicates with fresh, complete
evidence. It freezes the ticket, contract revision/digest, attempt, delivery digest,
head/base pairs, integration revisions, content snapshot IDs, exact confirmation
IDs and times, context digests and upstream receipt IDs used by the evaluation.
Receipt issuance is idempotent for this exact milestone and delivery identity.

For `automatic` acceptance, the same transaction emits a fulfillment receipt with
`acceptance_basis=automatic`. For `explicit` acceptance, a requester operation
emits the fulfillment receipt with `acceptance_basis=explicit` and its decision
record. Thus an `accepted` dependency has defined behavior for both modes.

An explicit acceptance identifies the exact contract revision, attempt and delivery digest
(repository/PR bindings, evaluated heads and integration revisions). It includes
the authenticated caller's provenance, timestamp and a nonempty rationale.
It is rejected unless the integration receipt matches and current observations,
technical requirements, context and dependencies permit a new completion.

A new contract, replacement PR, or changed evaluated revision supersedes the
current acceptance. Old acceptance records remain historical evidence. A caller
cannot approve an earlier assessment while a newer one is current.

Receipt content and timestamps never change. An outage or expired freshness may
make `completion_eligible_now=false` while `contract_fulfilled=true` remains true
for a delivery already accepted. This is intentional: historical delivery and
permission for a new mutation are different facts. No outage alone reopens a
ticket or invalidates a dependency supported by a matching historical receipt.

A contract amendment, binding replacement or reopen supersedes current receipt
eligibility without deleting history. Reopen increments `attempt`, clears the
current acceptance/receipt pointers, releases the old claim and increments its
fencing epoch. Reaccepting identical code requires a receipt for the new attempt.
Downstream contracts pinned to the old attempt are blocked as described in 5.4.

### 7.3 Existing completion commands

Guard existing completion paths, including MCP, the Codex gateway and direct
service calls, so that old clients cannot bypass the workflow contract.

- `start`, discussion and prose correction retain existing role semantics.
- For cross-project tickets, `resolve` requires integration and satisfied
  technical/context/dependency requirements. `confirm` additionally
  requires explicit acceptance when the contract calls for it.
- For a self-ticket, `resolve` closes directly only when the whole contract is
  fulfilled. `resolve_pending` may stop at technically delivered, awaiting acceptance.
- `cancel` and `wontfix` retain their existing role rules but record a distinct
  unsuccessful delivery disposition. They cannot satisfy downstream dependencies.
- Reopening follows the new-attempt rule in section 7.2. It never manufactures
  successful evidence or silently restores old acceptance.

The guard and status mutation share a PG transaction and lock the ticket/workflow
against concurrent contract, binding and acceptance changes. All participating
write paths use the same lock order: the graph-coordination advisory lock first
(shared for evaluation/milestone/claim decisions, exclusive for graph amendments),
then involved ticket/workflow rows in UUID order, then receipt/audit inserts.
Dependency revisions and attempts are rechecked under these locks. The guard
evaluates `completion_eligible_now` and freshness in that transaction; it does not
rely on a previously displayed assessment.

For tickets without a contract, the existing lifecycle remains unchanged. Initial
activation does not infer contracts or rewrite old tickets in bulk.
Existing terminal `closed`/`acked` statuses remain terminal: this feature adds no
new legal `reopen` transition. Additional work on a closed ticket requires a new
ticket. Contract/binding/acceptance mutations on terminal tickets are rejected;
historical reads remain available. For a nonterminal ticket whose coordination
state no longer permits implementation after an amendment, expose
`reopen_required` instead of silently changing its status or launching work.

## 8. Orchestrator interface and concurrency

MCP exposes typed, versioned structured results through the existing tool
discovery mechanism. Human-readable renderings are supplementary.

| Operation | Required behavior |
| --- | --- |
| `brain_delivery_contract_set` | Create/amend using `ticket_id`, `expected_revision`, contract, reason and idempotency key |
| `brain_delivery_bind_pr` | Bind/replace a named deliverable using expected contract and workflow versions; validate registered repository identity |
| `brain_delivery_get` | Contract, pinned context, current assessment, evidence links, freshness and history cursor |
| `brain_delivery_list` | Paginated project view filterable by eligible work, blockers and delivery stage |
| `brain_delivery_refresh` | Mark a workflow due; return queued status and the current observation timestamp |
| `brain_delivery_claim` | Acquire eligible work atomically for an external owner with expected assessment/version |
| `brain_delivery_claim_renew` | Renew only the matching unexpired claim token and epoch |
| `brain_delivery_claim_release` | Release only the matching claim; a stale owner cannot release a newer claim |
| `brain_delivery_accept` | Record acceptance of the exact current contract/delivery digest |

Full UUIDs are required for mutations. Reads may follow existing unambiguous
prefix behavior. Lists use stable ordering, bounded `limit` (default 20, maximum
100), and an explicit pagination cursor. A briefing's truncated list always
includes the omitted count and the operation for retrieving the remainder.

Claims last 15 minutes by default, configurable from 60 to 3600 seconds. PG time
is authoritative. Acquisition compares contract/assessment versions, current
eligibility and lease expiry atomically; only one owner succeeds. Reacquisition
after expiry increments a fencing epoch and returns a new secret-safe claim token.
Claim tokens are never logged and are stored as digests.

An expired claim makes work available; it does not stop the old agent. Brain can
fence its own claim mutations, while the external orchestrator must fence external
execution using the returned epoch. The API states this limitation explicitly.

Initial eligible work kinds are `implement`, `repair`, `review`, `integrate`, and
`accept`. `implement`/`repair`/`review` are addressed to the executor; `accept` to
the requester; integration follows declared external permissions. Findings that
require credential repair, context amendment or dependency resolution are exposed
as blockers, not as a guessed agent assignment. No callback launches execution.

## 9. Briefing and trust boundary

The session briefing renders a bounded delivery section from the same assessment
used by the API: ticket, contract revision, observed stage, freshness, primary
blocker and next eligible work. Example:

```text
Livraisons observées — GitHub vérifié il y a 42 s
Ticket … · contrat v3 · PR #142 @ abc1234 · proposé
Bloque : test-integration en échec sur abc1234
Travail disponible : réparation · contexte v3
```

This section is rendered separately from the historical handwritten focus. The
focus is dated and described as declared context; it cannot override observed
delivery facts. The feature does not parse and rewrite free-form focus text.

Authorization uses the existing authenticated project boundaries and the
requester/executor rules:

| Operation | Participant role |
| --- | --- |
| Create or amend a contract | Requester |
| Bind or replace a deliverable PR | Executor |
| Read a workflow or request refresh | Requester or executor |
| List workflows | Only workflows involving the requested project |
| Claim implementation, repair, review or integration work | Executor |
| Claim acceptance work; record acceptance | Requester |
| Renew/release a claim | Matching participant, claim token and epoch |
| Start/resolve/reopen/confirm/cancel/wontfix | Existing ticket action table, plus delivery guard |

For self-tickets, the one project holds both roles. A claimed actor project must
be a participant before applying the operation's role rule. Configured service
credentials identify the caller; project fields describe the delegated project
within the current administrative trust boundary. Claims coordinate ownership;
they are not proof of an agent's role, model, sandbox, or external process identity.
Dream-scoped credentials must not gain access to these mutations. All guards
apply inside tool execution as well as discovery/gateway dispatch. Repository
registration is operator configuration mapping canonical project keys to allowed
GitHub numeric repository IDs and their current names; it is not an agent tool.

The current shared administrative credential does not cryptographically identify
an individual human or distinguish an orchestrator from a worker holding that
same credential. `actor_project` and `X-Brain-Agent` are provenance, not that proof.
This release prevents client-authored evidence and records all contract changes;
it does not claim to prevent an administrator from deliberately changing policy.
Separately scoped worker credentials remain a future authorization extension.

## 10. Persistence and implementation boundaries

Use additive PostgreSQL migrations and existing SQLAlchemy/Pydantic conventions.
The persistence design must represent current workflows, immutable contract
revisions, artifact bindings, revision-pinned dependencies, normalized observation
content, immutable collection confirmations, integration/fulfillment receipts,
acceptance records and idempotent audit events. Claims can live on the
workflow row. JSONB is appropriate for versioned payloads; identities, unique
constraints, revisions and foreign keys remain relational.

Required constraints include one workflow per ticket, unique contract revision,
one active binding per deliverable/revision, unique provider snapshot identity,
and unique mutation idempotency key within its operation/actor scope.
Workflow updates use a monotonically increasing row version. An observation
collected for an old binding can be retained historically but cannot replace the
assessment of its successor.

Expected modules:

- `models/delivery.py`: contracts, evidence, assessment and API schemas;
- `services/delivery_evaluator.py`: pure state and predicate evaluation;
- `repositories/pg_delivery.py`: persistence, CAS, dependencies and claims;
- `services/delivery_service.py`: authorized use cases and context resolution;
- `delivery_observer/`: GitHub adapter, token provider, bounded scheduler and CLI;
- `mcp/tools/delivery_tools.py`: typed tool surface;
- narrow integrations in ticket service/repository, MCP composition, briefing,
  gateway composition and the existing capability/discovery tests;
- dedicated observer systemd template and a feature deployment runbook.

The implementation plan allocates the next migration revision from the actual
current Alembic head, accounting for concurrent work. It must not assume that a
number selected during this specification remains free.

## 11. Alternatives

**Chosen: contracts and evidence in PG, independent GitHub polling.** Fits the
existing Brain ownership model, survives missed agent updates, and avoids a new
inbound service dependency. It adds bounded polling latency and rate-budget work.

**GitHub issues/PRs as the complete workflow authority.** Reduces local workflow
storage, but disperses Brain context and cross-project contracts and makes the
orchestrator reconcile two different sources of intent. Not selected.

**A general workflow engine with agent execution.** Adds infrastructure and a
large execution abstraction while violating the permanent agent-launch boundary.
Not selected. The evaluator remains a small deterministic Brain component.

## 12. Verification and production delivery

### 12.1 Required automated evidence

Behavioral changes follow Red-Green-Refactor with recorded failing tests. Required
coverage includes:

1. Contract validation, immutable history, idempotent retries and revision conflicts.
2. Explicit PR identity, wrong repository/base rejection and binding replacement.
3. No PR, draft, check missing/failing/pending, approval missing/dismissed, ready,
   integrated with missing proof, and fulfilled acceptance.
4. New head before integration invalidates old success after the next observation;
   synthetic merge association, base change, squash merge, rebase merge and a
   rerun superseding an old green result are covered. An open green PR can never
   pass a delivery completion guard.
5. Pagination beyond one page, truncated evidence, reordered provider records,
   timeout, revoked credentials, rate limits, observer restart and ownership loss.
6. A dead observer becomes stale at read time without another background write;
   old receipt timestamps and fulfilled history remain unchanged, while a new
   completion is refused. Identical polls cannot rewrite receipt freshness.
7. Simultaneous contract amendments, cycle-creating dependency edits, claims,
   stale claim renewal/release and acceptance racing a new observation. A reopen
   starts a new attempt; a bare merge without technical proof cannot satisfy an
   integrated dependency, and an old attempt's receipt cannot satisfy a new one.
   Race completion against amendment, binding replacement and reopen: the losing
   operation must conflict, never issue a receipt for an obsolete identity or
   close the new attempt using the previous attempt's proof.
8. Every legacy completion entry point rejects unsatisfied contracted work;
   cancellation/wontfix never counts as successful delivery; legacy tickets retain
   their behavior.
9. Briefing and MCP structured results agree; context drift and omitted items are
   visible; failed GitHub access cannot stall a session briefing.
10. Dream-scoped mutation refusal, secret redaction, fixed-origin HTTP requests,
    and refusal of client-supplied observed success.

Integration tests use a disposable PostgreSQL database with real transactions and
migrations plus a controlled HTTP GitHub fixture. They do not use production PG
or change live GitHub repositories. The normal full lint, formatting, mypy,
layering, unit/integration, coverage and packaging gates remain required.

### 12.2 Release sequence

1. Implement and review bounded tasks in isolated worktrees; integrate against the
   latest `main` and rerun the complete required checks on the resulting revision.
2. Build the release artifact and verify that it contains the migration and
   observer entry point. Identify every live MCP/gateway writer before activation.
3. Prepare a concrete deployment runbook with the measured current schema,
   application revision, unit files, private credential path, and rollback target.
   Validate service GitHub access without exposing credentials.
4. Take and verify a pre-migration backup according to the existing runbook. Apply
   the additive migration, keeping the feature inactive for current clients.
5. Deploy the new application and observer, retaining current Dream and unrelated
   services. Upgrade every writer before enabling contracted ticket guards.
6. Start the observer and enable the feature for `brain-v42`. Existing tickets are
   unaffected until a contract is explicitly attached.
7. Execute the production canary below, then retain measured evidence and update
   the Brain project records without closing any agent session automatically.

A rollout that stops at code merge, CI success or a dormant feature flag is not a
completed delivery. Missing machine credentials or a failed production canary is
reported as a remaining deployment dependency, with the prepared artifact intact.

### 12.3 Production canary and acceptance

Use a dedicated, clearly labelled canary ticket and a bounded documentation-only
PR through the normal review flow. The receipt records:

- running application revision and applied migration;
- observer unit state and a successful authenticated collection;
- ticket, contract revision, PR number and evaluated SHAs;
- visible missing proof before completion;
- a subsequent commit superseding the former assessment;
- required CI evidence observed on the new revision;
- the same assessment visible through MCP and the session briefing;
- merge observed as integration, followed by the required explicit acceptance;
- an old completion command refused before requirements are met;
- observation continuing while no execution agent updates the ticket.

The baseline response-time target is visibility within 300 seconds of a provider
change for at most 20 simultaneously changing PRs, at most five API calls per PR,
no pagination or retries, and provider responses within one second. This covers
100 calls, up to 60 seconds before scheduling, and the 40-request/minute budget.
A controlled-clock scheduler test must prove this envelope and fair scheduling.
Paginated, rate-limited or larger workloads are outside this baseline target and
report measured lag/backlog. Production records p50/p95 lag over the measured
sample without claiming an unmeasured percentile. Stale evidence stays visible.

Production is accepted only after this path succeeds on the deployed service and
the receipt contains evidence URLs/timestamps rather than narrative assertions.
The canary ticket is kept with extraction disabled so it does not pollute durable
knowledge extraction.

### 12.4 Rollback

Stop the dedicated observer and disable contract mutations/claims first. Preserve
contract and evidence data. A read-only assessment may remain available and must
show disabled/stale health. Do not automatically fall back to unrestricted ticket
completion for contracted tickets.

After the first contract is activated, rollback to a binary predating the delivery
guards is forbidden while contracts remain. The rollback target is the guarded
application with observation and new contract operations disabled; defects in that
application require a forward repair. No route-only filter is accepted as a guard
for direct service calls. The deployment preflight records the minimum compatible
application revision and refuses an older rollback target. Rollback tests exercise
MCP, gateway and direct service calls in paused mode: all three retain the guards.

No destructive down-migration or wholesale systemd uninstall is part of rollback.
Unrelated sessions, worktrees, tickets, focus edits and services remain owned by
their existing operators.

## 13. Sources

- [GitHub check runs](https://docs.github.com/en/rest/checks/runs): revision-bound
  check records, provider identity and pagination.
- [GitHub pull request reviews](https://docs.github.com/en/rest/pulls/reviews):
  review IDs, decisions and commit references.
- [GitHub REST API practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api):
  conditional requests, rate limits and retry behavior.
- [GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps):
  independent service identity and repository permissions.

Provider documentation was consulted on 2026-09-07. The workflow semantics,
defaults, boundaries and acceptance tests above are Brain design decisions.
