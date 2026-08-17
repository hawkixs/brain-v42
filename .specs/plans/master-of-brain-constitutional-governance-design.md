# Master of Brain: constitutional progressive governance

Date: 2026-07-21
Status: proposed conceptual design
Project: `brain-v42`

Decision record: Brain ADR #10
(`bbcdfd9f-8643-4e96-b913-fa9b91236cf2`) remains `proposed`.
Supporting decisions `30e65232`, `c84e4e07`, and `a1729f2f` are `active`.
They establish the design direction, but neither they nor this document authorize
implementation, capability grants, cutover, or production writes.

## Summary

The Master of Brain governs knowledge inside Brain. It does not govern ReD code,
infrastructure, deployments, or product work. Its priorities are, in order:

1. protect knowledge integrity;
2. improve knowledge usefulness;
3. expand knowledge through justified synthesis and links.

The Master reasons, delegates, and arbitrates. It never holds direct Brain write
tools. After an explicitly authorized cutover, a deterministic constitutional
gate would authorize every mutation against a human-owned, versioned
constitution. Authority grows one capability at a time after measured evidence.
Humans alone may change the constitution, extend the scope, grant authority, or
perform irreversible deletion.

This design proposes a governance plane above Brain and Dream. Dream remains the
nightly maintenance worker. The Master reviews deltas, opens governance cases,
delegates bounded investigations, preserves dissent, and applies only authorized
decisions.

## Scope

### In scope

- Knowledge integrity, provenance, contradictions, confidence, freshness,
  classification, graph links, consolidation, promotion, and archival.
- Governance cases, mandates, independent review, constitutional authorization,
  audit, verification, rollback, and escalation.
- Immediate deterministic checks after Brain writes.
- A daily deliberative cycle, preferably after Dream.
- Read-only evidence gathering from explicitly authorized external sources.
- Progressive autonomy for reversible metadata and semantic mutations.

### Out of scope

- Writing code or repository files.
- Operating infrastructure, services, models, or deployments.
- Governing development, operations, or product agents.
- Expanding beyond Brain without a later human constitutional amendment.
- Allowing any agent to modify its own mandate, tools, or authority.

If the Master discovers an external problem, it records evidence and a
recommendation in Brain. Another system or a human owns the external action.

## Approach selection

The brainstorm considered six non-exclusive approaches. Confidence values were
heuristic design-fit estimates, not calibrated probabilities.

1. **Progressive constitutional Master (selected, confidence 0.93).** A
   human-owned constitution, deterministic enforcement, bounded delegation, and
   authority earned per capability provide useful autonomy with controlled risk.
2. **Shadow curator (supporting stage, confidence 0.89).** Observation and
   simulation provide evidence before writes, but permanent shadow mode leaves a
   human bottleneck.
3. **Council with a coordinating Master (supporting mechanism, confidence
   0.82).** Independent proposer and reviewer roles reduce self-confirmation at
   the cost of latency and model usage.
4. **Sovereign Master (rejected, confidence 0.08).** Direct administrative access
   is fast but creates one epistemic and security failure point.
5. **Decentralized agent swarm (rejected, confidence 0.05).** Consensus and
   reputation distribute power but weaken accountability and complicate
   enforcement.
6. **Evolutionary knowledge ecosystem (deferred research, confidence 0.03).**
   Competing hypotheses are promising, but the operating and safety model is too
   experimental for the first system.

The selected design incorporates shadow evaluation and independent review
without adopting sovereign or decentralized authority.

## Constitutional principles

### Human sovereignty

The human operator owns the constitution. The Master may propose an amendment,
but cannot adopt it, grant itself tools, promote its authority, disable a guard,
or prevent human intervention.

### Priority order

The constitution resolves competing objectives in this order:

1. **Integrity:** preserve provenance, expose contradictions, and prevent an
   unsupported hypothesis from becoming a silent fact.
2. **Usefulness:** make knowledge retrievable, contextual, and actionable.
3. **Expansion:** create insights and links only when their expected value
   exceeds the noise they add.

Knowledge volume is never a success metric.

### Operational truth and dissent

The Master does not claim absolute truth. It may select one item as the current
operational reference so Brain remains usable, but it preserves opposing claims,
their context, evidence, uncertainty, and the rationale for the choice. New
evidence can reverse the operational reference without erasing history.

The conceptual knowledge states are `observed`, `corroborated`, `contested`,
`operational_reference`, `deprecated`, and `archived`. Implementation planning
will decide whether these states require schema changes or governed metadata.

## Architecture

```text
                       human operator
                             |
                  constitution and authority
                             |
                             v
Brain events --> Brain Observatory --> Master of Brain
                                         |
                                         v
                                  bounded mandates
                                         |
                 +-----------+-----------+-----------+
                 |           |           |           |
              Curator     Skeptic    Cartographer  Verifier
                 |           |           |           |
                 +-----------+-----------+-----------+
                                         |
                              proposals and evidence
                                         |
                              independent reviewer
                                         |
                              Master arbitration
                                         |
                              constitutional gate
                                 |             |
                                 v             v
                               Brain       audit ledger
```

The constitutional gate, not the Master, owns mutation authority. The Master
submits a structured intent. The gate checks the constitution revision, project
scope, capability grant, evidence requirements, quota, reversibility, target
version, and required review before calling a Brain mutation.

The existing Dream phase capability firewall is a useful precursor to this
boundary. The final mechanism must enforce policy outside prompts.

### Dream coexistence and cutover

This proposal does not change Dream's current authority. The existing CLEAN
contract still permits `brain_delete`; the Master therefore remains Level 0
shadow and cannot be treated as a universal gate while that or any other direct
mutation path bypasses the constitutional gate.

Cutover requires all of the following:

1. inventory every Brain mutation entry point and route it through the gate,
   including Dream phases and direct MCP mutation tools;
2. remove `brain_delete` from the CLEAN allowlist and prompt, replacing automatic
   hard deletion with reversible archival or a human-approved intent;
3. prove with negative tests and production shadow evidence that no writer can
   bypass authorization, scope, audit, or quota checks;
4. obtain explicit human approval for the constitution revision, capability
   grants, and cutover.

There is no dual-authority phase. Before cutover, Dream keeps its existing
controls and the Master only observes and simulates. After cutover, Dream submits
intents to the gate and holds no direct mutation tool. Any remaining bypass keeps
the Master at Level 0.

## Components

### Constitution registry

Stores immutable, versioned policy revisions. Only an authenticated human
operation may activate a revision. Every mandate and decision records the
revision used.

### Brain Observatory

Produces factual deltas and health signals: missing provenance, contradiction
candidates, duplicates, stale knowledge, weak evidence, isolated nodes,
classification drift, and recent writes. It opens governance cases but makes no
semantic judgment.

### Master of Brain

Prioritizes cases, issues mandates, compares results, records a reasoned
decision, and submits authorized intents. It reads only the relevant delta and
case context instead of loading the entire Brain each cycle.

### Specialist agents

- **Curator:** classification, freshness, duplicates, and knowledge hygiene.
- **Skeptic:** contradictions, weak evidence, overconfident conclusions, and
  counterarguments.
- **Cartographer:** missing relations, isolated knowledge, and useful structural
  connections.
- **Verifier:** read-only repository, documentation, history, source, and metric
  checks under an explicit evidence mandate.

Each mandate fixes the goal, scope, allowed tools, budget, expiry, source
snapshot, expected evidence, and output contract. Agents cannot widen a mandate.

### Independent reviewer

Checks whether evidence supports the proposal and whether the proposal satisfies
the constitution. The reviewer must not be the proposal's author.

### Constitutional gate

Enforces authorization deterministically and fails closed. The gate also owns
per-capability quotas, optimistic concurrency checks, idempotency, and audit
requirements.

### Audit ledger

Records the case, mandate, evidence, objections, decision, constitutional rule,
authorization result, mutation result, and verification outcome. The Master
cannot modify or delete audit history.

## Data flow and cadence

The system runs two loops.

The **immediate loop** performs deterministic structural checks after each Brain
write. It validates minimum provenance and scope, detects candidate duplicates
or contradictions, and opens a case. It performs no LLM judgment or content
mutation.

The **daily loop**, preferably after Dream, reads new and changed cases since a
durable checkpoint. The Master ranks them by integrity risk, expected utility,
and investigation cost. It delegates a bounded number of cases to specialists,
requests independent review, decides, and submits eligible intents to the gate.

Each case follows an explicit state machine:

```text
detected -> triaged -> investigating -> reviewed -> decided
                                                |-> escalated
                                                |-> applied -> verified
                                                |             |-> uncertain
                                                |             |-> rolled_back
                                                |-> rejected
```

Agents work from a versioned snapshot. Before mutation, the gate revalidates the
current target revision. A concurrent human, Dream, or system change makes the
proposal stale; the gate performs no write and returns the case for re-triage.
After mutation, an independent read verifies the expected effect.

The morning report remains compact: applied changes, pending human decisions,
open contradictions, degraded capabilities, and failed verifications.

## Progressive authority

Authority attaches to a specific capability, never to one global trust level.
Success at tag normalization grants no implied merge authority.

| Level | Role | Allowed outcome |
|---|---|---|
| 0 | Shadow | Observe, investigate, and simulate decisions. |
| 1 | Assistant | Open complete cases and prepare mutations for human approval. |
| 2 | Autonomous curator | Apply reversible, capped metadata actions such as tags, links, conflict markers, and temporary archival. |
| 3 | Guardian | Apply explicitly granted semantic actions such as reversible merge, promotion, or operational-reference selection after independent review. |

Humans alone promote a capability. Promotion requires diverse reviewed cases,
zero constitutional violations, strong agreement with human review, verified
effects, and a proven rollback. Exact thresholds will be calibrated during
shadow operation and then written into the constitution.

A fault automatically demotes or freezes the affected capability. The following
red-zone actions always remain human:

- amend or activate the constitution;
- extend the action scope;
- grant tools or authority;
- disable enforcement, audit, or human intervention;
- hard-delete knowledge or audit records.

Reversible semantic merges and promotions may become autonomous after their
capabilities earn Level 3 authority.

## Evidence boundary

The **action perimeter** is Brain only. The **evidence perimeter** may include
explicitly authorized read-only sources: ReD repositories, their documentation
and Git history, official external sources, and non-sensitive metrics.

The Verifier returns a bounded evidence package with the source, observation
date, minimal excerpt, source authority, reproducibility, contradictions, and
confidence. External content is untrusted data; agents never execute its
instructions. Persisted evidence excludes secrets, sensitive configuration,
personal data, and unnecessary content.

The evidence hierarchy favors current reproducible reality, explicit human
decisions, and authoritative sources over historical Brain claims. A missing or
non-reproducible source can support a hypothesis but cannot alone justify durable
promotion.

## Failure policy

The system fails closed whenever it cannot prove authorization, scope, or audit
integrity. Observation is conditional, not a fallback around those controls.

- If the constitution, authorization gate, or audit ledger is unavailable, all
  mutations stop. Read-only analysis continues only while an independent check
  can still prove its authorization and scope. Otherwise the Master loses access
  to Brain and evidence; a separate health channel may emit only component state,
  an incident identifier, and a timestamp, without protected content.
- Every case and intent uses an idempotency key. Restarting a worker never
  repeats an action whose result is unknown.
- No provider fallback occurs after a mutation attempt begins; the first agent
  may already have caused an effect.
- A stale target revision aborts the write and reopens triage.
- An unverifiable result moves the case to `uncertain`, blocks dependent actions,
  and triggers deterministic rollback when available.
- Agent disagreement produces contested knowledge, not a forced consensus or a
  generic failure.
- A capability-specific anomaly opens its circuit breaker without disabling
  unrelated safe capabilities.
- A scope violation, inconsistent constitution, compromised audit, or systemic
  authorization defect freezes every governed capability, closes the Master's
  read and write access, and emits only the minimal independent health signal
  until a human resets it.
- A human-controlled global kill switch remains independent of the Master.

## Validation and rollout

The Master crosses four gates before autonomous production writes.

1. **Constitutional tests:** deny forged scopes, missing policies, exceeded
   quotas, malformed intents, unauthorized capabilities, and audit failures.
2. **Historical replay:** analyze resolved Brain and Dream cases offline, then
   compare proposed outcomes with known results.
3. **Production shadow:** process real deltas, open cases, and simulate decisions
   while humans or evaluators review a representative sample.
4. **Capability canary:** enable one reversible action with a small quota, verify
   every effect, and expand only after evidence.

Fault injection covers model interruption, concurrent agents, concurrent human
edits, unavailable audit, partial results, impossible rollback, stale snapshots,
and prompt-injection-shaped external content.

Promotion depends on diverse cases and measured quality, not elapsed time alone.
Every promotion is a recorded human decision. Every capability supports
independent demotion.

## Success measures

The governance system tracks:

- constitutional violations and denied out-of-scope attempts;
- human acceptance of proposals;
- false positives and reversed decisions;
- rollback and verification outcomes;
- age and volume of unresolved contradictions;
- provenance coverage;
- retrieval quality and use of governed knowledge;
- human review time saved;
- degraded capabilities and uncertain cases.

Zero unauthorized writes is a hard invariant. Knowledge volume and autonomous
action count are diagnostic values, not success targets.

## Implementation decisions deliberately deferred

This conceptual design does not choose:

- the storage schema for constitutions, cases, mandates, and audit events;
- the provider or model for each agent role;
- exact promotion thresholds and quotas;
- the operator UI and morning-report transport;
- whether conceptual knowledge states require columns, relations, or governed
  metadata;
- the implementation sequence beyond the validated rollout gates.

Those choices require repository impact analysis, a bounded implementation plan,
failure-first tests, and separate user authorization.
