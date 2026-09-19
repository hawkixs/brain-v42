# Independent review of the measured-facts-and-claims specification (Codex CLI, gpt-6-astra)

**Reviewed:** `docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md` at `d9a9691a`

**Reviewer:** OpenAI Codex CLI 0.153.4, model `gpt-6-astra`, reasoning effort `max`, sandbox
`read-only`, non-interactive (`codex exec`), requested by the operator on 2026-09-19 to obtain
an independent second opinion before any implementation.

**Run:** started 14:32:21 CEST, final message 14:48:50 CEST (16 min 29 s); 59 read-only shell
commands; 270 181 tokens. The reviewer answered in French; the same session was then asked
(`codex exec resume <id>`, effort `low`, 233 017 tokens) to render its review in English
verbatim — same verdict, same eleven findings and severities, same twenty-seven file:line
references, checked mechanically. The French original is kept outside the repository.

**Status of this record:** the reviewer's words, unedited below this line. The specification's
response to each finding is the next revision of the specification, not an edit of this file.

---

VERDICT: REWORK

The direction of ADR #27 is respected: durable reasoning, separate claims, named measurements, and no automatic archiving. However, several contracts can still produce misleading evidence. Lot A requires contract corrections; lot B’s model cannot yet be frozen for migration.

**SCOPE:**

Review of `d9a9691a2cd10e5a1408d7ad19d35c4f71420488`, branch `spec/measured-facts-and-claims`, clean working tree. The diff against the stated baseline contains only the specification.

In the findings, **S** refers to [2026-09-19-measured-facts-and-claims-design.md](/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/superpowers/specs/2026-09-19-measured-facts-and-claims-design.md:1).

| File inspected | Relevant lines read |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | 1–82; project facts and rules 1–789 |
| S | 1–536 |
| `src/brain_v42/mcp/tools/session_tools.py` | 1–145, 230–265, 390–700 |
| `src/brain_v42/services/schema_state_service.py` | 1–41 |
| `src/brain_v42/services/dream_run_service.py` | 1–277 |
| `src/brain_v42/metrics/collector.py` | 1–110, 465–784 |
| `src/brain_v42/repositories/pg_graph_ledger.py` | catalogue and validation 18–48, 137–180; lease 308–475; inventory 764–784; projection claims 953–1030 |
| `src/brain_v42/release.py` | 1–128 |
| `src/brain_v42/db/delivery_tables.py` | 1–60, 445–545 |
| `src/brain_v42/mcp/dream_capabilities.py` | 1–325, 400–588 |
| `src/brain_v42/mcp/tool_catalog.py` | 1–81 |
| `scripts/check_module_layering.py` | 1–184 |
| `tests/fixtures/briefing_full.md` | 1–42 |
| `tests/unit/mcp/test_session_tools.py` | 1–150, 1028–1210 |

Additional material inspected: server composition and shutdown, SQL engine, provenance middleware, automatic sessions, CRUD, digest computation and attestation repository, migrations 033/043/047/048/054, model probe, metrics tests and briefing fixture, tool documentation, graph/delivery runbooks, and deployment preflight. The passages used are referenced below.

GitNexus is **20 commits behind**: `DEGRADED_MCP_EVIDENCE`. The conclusions rely on the checkout’s source files. The delegated supporting checks were cross-checked against this tree.

Two checks without writes, executed with `python3 -B`, confirm:

- the absence of cycles in the current module graph and with the dependencies proposed for lot A;
- the rejection of `{"lag_seconds": 0.0}` by the existing attestation digest computation.

No files modified, no test suite, migration, or production operation executed. The full text of the Brain tickets, runbook `22189c08`, the private handoff, and the cited production states were not verified. ADR #27 is treated as accepted, as requested.

`confirmed` below means a verified contradiction or gap in the proposed contract, not a runtime defect in an already implemented feature.

**FINDINGS:**

1. **P1 — confirmed — A declared target does not prove the target actually queried.**  
   S:109–114, 214, and 386–387 promise to prevent production/test mix-ups, but the check compares two labels. The existing factory simply uses `settings.postgres_url`: a probe labelled `production` can therefore read `brain_test`. The model also does not require checking that `measurement.fact` matches the claim’s `fact_name`; two facts sharing a target and a JSON path could be confused. See [engine.py:30](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/db/engine.py:30).

   **Fix:** bind each probe to a verified, non-secret source identity supplied by composition; freeze that identity and the fact’s definition version. Check fact, concrete target, and parameters before any comparison. A precedent exists in [plan_index_repair_store.py:291](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/maintenance/plan_index_repair_store.py:291). Add tests for “test connection declared as production” and “another fact on the same target.”

2. **P1 — confirmed — The “current verdict” can remain false without any row being modified.**  
   S:370–375 selects the maximum `emitted_at` supplied by the issuer. A measurement mistakenly dated in the future dominates subsequent measurements; a UUID as a tiebreaker defines no causal order. Furthermore, S:393–398 does not check age, while S:414–417 displays claims that “hold.” An old measurement can therefore remain presented as current after its TTL expires or the verifier stops.

   **Fix:** define an observation identified and timestamped by a trusted source, check future dates and out-of-order arrivals, then specify a durable tiebreaker. Distinguish historical verdict, latest attempt, and current validity. Expiry must be derived at read time without rewriting history; an `unreadable` attempt must not silently bring an old `holds` back to the surface.

3. **P1 — confirmed — Uniqueness prevents reasserting a retired claim.**  
   Under S:332–355 and 409–410: creating C, retiring C, then reasserting exactly C produces the same digest and conflicts with global uniqueness. Reactivating the old row would rewrite its lifecycle. The digest also excludes `target`, although S:345 explicitly anticipates that the catalogue’s target may change. Finally, no durable relation identifies which claim replaces which other claim for the future `SUPERSEDES`.

   **Fix:** separate content identity from claim occurrence/revision; include target and probe definition in semantic identity. Allow a new occurrence after retirement, retain an explicit replacement link, and define reads over active occurrences only. For `brain_update`, it is essential to specify: absent `claims` field = unchanged; empty list = explicit retirement; supplied list = controlled replacement. The current CRUD is partial; see [crud_tools.py:350](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/mcp/tools/crud_tools.py:350). Provide revision checking against concurrent replacements.

4. **P1 — confirmed — The storage guarantees do not yet protect a claim’s complete history.**  
   Checking that the entry exists “in the same transaction” does not replace an FK or a locking protocol: a concurrent deletion can create an orphan. Entries can actually be deleted, notably in [pg_learning.py:184](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/repositories/pg_learning.py:184). Furthermore, S:376 protects verdicts but defines no SQL protection for claim content, whose mutation would retroactively change the meaning of the verdicts.

   The cited precedent does not implicitly provide that protection: [054_delivery_attestations.py:11](/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/054_delivery_attestations.py:11) expressly declares append-only **by code path**, without a trigger.

   **Fix:** choose a durable referential anchor and a deletion policy; `brain_entities` is an existing option with tombstones ([migration 033:1591](/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/033_graph_relation_ledger.py:1591)). Prohibit changes to semantic fields and constrain the retirement transition. Define the runtime role’s permissions and negative SQL tests. Also specify concurrent replay and key-reuse conflicts: the precedent explicitly compares content before returning an existing attestation ([pg_delivery_attestations.py:150](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/repositories/pg_delivery_attestations.py:150)).

5. **P1 — confirmed — The concurrency contract guarantees neither reader isolation nor the stated deadline.**  
   S:203–207 does not define ownership of the shared task, cancellation isolation, cleanup, or registry shutdown. Cancelling a reader directly awaiting that task can cancel the measurement for everyone. Conversely, a shielded but undrained task can outlive engine disposal; the server already has an explicit shutdown order ([server.py:220](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/mcp/server.py:220)).

   The bound in S:305 is also false: eight three-second probes, four at a time, require two waves. `wait_for` does not make a blocking or cancellation-resistant probe strictly bounded.

   **Fix:** registry-owned task, shielded individual waits, cleanup with an identity check, and `aclose()` before dependencies are closed. Apply a global producer limit, including direct `measure()` calls, and define a deadline covering capacity waiting, connection acquisition, and the query. Require cooperative probes and I/O timeouts; correct the latency promise or impose a global budget on the batch.

6. **P1 — confirmed — The cache rules contradict the promised freshness.**  
   S:194–202 uses wall-clock time: a backward clock adjustment can extend a result beyond its actual TTL. The condition `age <= min(ttl, max_age)` also allows a cache entry of exactly zero age with `max_age=0`, contrary to S:308. How `max_age` applies to the negative cache is undefined.

   **Fix:** injectable monotonic clock for TTLs, durations, and deadlines; UTC only for the published timestamp. Define zero as an explicit bypass of both caches while still sharing an active flight; reject negative ages. Test the same tick, clock jumps, and recovery after an unreadable result.

7. **P1 — confirmed — The measurement format is insufficiently immutable, and the digest precedent is incompatible.**  
   `frozen=True` at S:124 does not freeze the dictionary or its descendants. Modifying the original payload or the one exposed through the cache can change `value` without changing `digest`. The dataclass alone also does not prohibit inconsistent combinations of `status`, `value`, and `error_code`.

   S:139–141 cites the attestation convention, but that convention prohibits all floats and uses a versioned domain prefix ([delivery_hashes.py:25](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/models/delivery_hashes.py:25), [delivery_hashes.py:70](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/models/delivery_hashes.py:70)). The first proposed payload contains precisely `0.0`.

   **Fix:** define a validated `Measured | Unreadable` format, a deeply immutable representation, and a versioned canonical recipe for measurements. Explicitly choose finite floats or an integer unit, bound depth and UTF-8 size, and distinguish value digest from observation identity. Test nested mutations, non-finite values, and impossible states.

8. **P1 — confirmed — Existing readers cannot become strict probes without adaptation.**  
   Three assertions in S:91–96 and 503–505 require correction:

   - `killswitch_state()` masks an unreadable file with history or fallback values; without a recent night, it returns disabled flags independently of the file. This is not a single current measurement of runtime state ([dream_run_service.py:29](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/dream_run_service.py:29), [dream_run_service.py:113](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/dream_run_service.py:113), [dream_run_service.py:136](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/services/dream_run_service.py:136)).
   - `package_version()` returns a distribution version, never a SHA. `head_of_versions()` also ignores some unreadable files, which can leave an older head appearing as the result ([release.py:42](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/release.py:42), [release.py:90](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/release.py:90)).
   - The model probe actually calls a model through `POST`, with a 90-second timeout, and also has `BUSY`. It contradicts the absolute prohibition in S:160; `BUSY`/`OTHER` cannot become a mechanical refutation of “model dead” ([probe_model_liveness.py:146](/home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/probe_model_liveness.py:146), [probe_model_liveness.py:161](/home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/probe_model_liveness.py:161)).

   **Fix:** specify strict adapters, distinguish on-disk configuration, history, and effective state, measure the SHA from a release identity bound to the process, and treat indeterminate responses as unreadable. Explicitly decide whether lot C permits a bounded liveness inference. These adaptations remain deferred; their compatibility must not be presented as established.

9. **P1 — question — Who is authorized to turn a declaration into measured evidence?**  
   S:347 says only that a verdict is “expected” for `provenance=measured`. S:433 does not define whether `brain_claim_verdict_record` receives an identifier for a measurement produced by the registry or JSON supplied by the caller. Yet `X-Brain-Agent` is a declared identity, explicitly unverified ([dream_capabilities.py:280](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/mcp/dream_capabilities.py:280)). A JSON digest proves its content, not that a probe was executed.

   **Recommended fix:** the server produces or authenticates the observation, recomputes the verdict, and derives the authorized provenance. Arbitrary JSON supplied by an agent remains declared. Also define where the verdict’s reason is persisted: `type_mismatch` and `path_absent` currently have no dedicated column, although the measurement itself may be valid.

10. **P2 — confirmed — “Projection up to date” exceeds what the first probe proves.**  
    The predicate reused at S:243 and rendered at S:262 describes the PostgreSQL lease, not Neo4j integrity. The runbook says so explicitly and identifies, in particular, a Neo4j restore within the same generation as a blind spot ([GRAPH_LEDGER_RUNBOOK.md:447](/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/GRAPH_LEDGER_RUNBOOK.md:447), [GRAPH_LEDGER_RUNBOOK.md:493](/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/GRAPH_LEDGER_RUNBOOK.md:493)). An empty outbox and an armed lease therefore do not prove that the graph contains the expected data. The current computation of `healthy` is, moreover, in Python at [collector.py:742](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/metrics/collector.py:742), not in SQL.

    **Fix:** render “no lag observed in the outbox” and specify the scope of `healthy`. Preserve the loud case for exhausted rows, even when `pending=0`. Verification of Neo4j content remains separate evidence.

11. **P2 — confirmed — The planned canary and tests do not verify the behavior actually wired into the system.**  
    At S:481–484, the briefing already measures the fact: the first subsequent `brain_fact_get` can therefore legitimately return `source="cache"`. The referenced runbook also requires a shared loader without a lifecycle call for its briefing proof ([runbook:1948](/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/runbooks/2026-09-07-observable-delivery-workflows.md:1948)).

    S:456–458 does not cover compact gateways ([tool_catalog.py:67](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/mcp/tool_catalog.py:67)). An assertion against a fake repository likewise does not prove that the transport performs no writes: middleware can open or observe a session before each tool ([provenance_middleware.py:89](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/mcp/provenance_middleware.py:89), [pg_brain_session.py:340](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/repositories/pg_brain_session.py:340)).

    **Fix:** explicitly force a new measurement before the probe/cache test, then verify the briefing. Use the loader without lifecycle mutation, unless explicitly commanded by the operator. Test native and compact with their middleware. Specify whether “side-effect-free” concerns only the probe or also transport telemetry. Finally, name the subsequent-release path without a schema change, already documented in the [September 13 receipt:6](/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/receipts/2026-09-13-release-21ff55d2.md:6).

**ANSWERS TO §9:**

1. **Keep the negative cache at `min(ttl, 30 s)`.** It prevents storms during an outage; for the first fact, that means fifteen seconds. Also apply `max_age`, allow zero to force an attempt, and preserve the failure timestamp.

2. **Move the threshold into a declared, versioned, shared policy.** It may be exposed by the fact descriptor, but must not be presented as a measured property. The briefing and evaluator use the same definition; changing the default threshold never rewrites claims’ historical expectations.

3. **Keep flat names for this closed catalogue.** Namespaces would not fix the identity problems. Prohibit reusing a name with a different meaning, and define now the definition version and target identity used by lot D.

4. **Yes, provide a server budget for forced refreshes in lot A.** A read-only query consumes the shared PostgreSQL pool and resources; its cost is not borne by the caller alone. Globally limit producers, bound their waiting time, and cap forced refreshes. Saturation must be explicit, without returning cached data as though it satisfied `max_age=0`.

5. **Prefer one claims table with a durable FK anchor over five tables or a simple service check.** Examine `brain_entities`, which already has identity, project, and tombstones. Add rules for type, scope, deletion, and concurrency; an FK alone does not resolve these latter points.

6. **No, the proposed set is neither minimal nor sufficient without probe contracts and a truth table.** `eq`/`ne` and ordered comparisons cover most initial measurements. For paths, prefer an `exists` value distinguishing observed absence from permission denial. For models, `BUSY`/`OTHER` remain indeterminate. Define `present`, currently short-circuited by “path absent → unreadable,” and remove or strictly bound `regex`. A semantic contradiction in a document remains outside mechanical comparison unless an explicitly defined deterministic structured extraction exists.

7. **The main possible lie is a successful read of the wrong target.** Next come a mutable payload under an unchanged digest, a cache retained beyond its actual TTL, or reuse of the collector’s fallback zeros as a valid measurement. The shared query must propagate its errors; only the metrics adapter retains `available=false` and its sentinels. Preserve the single query with its common SQL instant and test expired lease, active recovery, unarmed generation, and exhausted rows. The absence of proof of Neo4j content must remain visible as a scope limitation.

**REQUIRED_GATES:**

Before implementation starts:

- **Revise and settle lot A’s contracts**: verified target, immutable format, canonicalization, task ownership and shutdown, global concurrency, deadlines, monotonic clock, and exact `max_age` rules.
- **Complete §7.1 with precise acceptance scenarios**: cancellation of the first reader and a follower; shutdown during a measurement; concurrency mixing direct calls and batches; capacity/pool waiting; expiry then recovery; SQL errors without false zeros; wrong target; payload mutation; invalid JSON values; expired lease, active recovery, and exhaustion alone. Include start/resume, legacy, native, and compact paths, as well as Dream denials.
- **Freeze lot B’s invariants before declaring it ready for migration**: claim occurrences and replacement, absent/empty PATCH semantics, concurrent update control, referential integrity, SQL immutability, measured provenance, verdict ordering and expiry, persisted reasons, and exact idempotency. Plan tests for concurrent create/delete, retirement/reassertion, a key reused with different content, and an attempted falsification using a foreign measurement.
- **Make the B/D boundary explicit**: do not publish unsupported events to the current projector. `claim`/`fact`, `ASSERTS`, and `MEASURED_BY` require an extension of the graph model, its constraints, and its writers; [pg_graph_ledger.py:162](/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/repositories/pg_graph_ledger.py:162) does not currently accept them. Their implementation may remain in lot D.
- **Make the migration and rollback path concrete**: next available revision and DR contract, explicit inventory of guards to move, evidence on a clone, and rollback runtime compatibility. Include the preflight, which currently pins `054` in several places ([check_delivery_deployment.py:1086](/home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/check_delivery_deployment.py:1086)). Downgrade refusal protects data; it does not by itself constitute a rollback procedure.
- **Correct lot A’s canary and the “read-only” scope** before freezing the tests. No migration and preservation of the legacy fixture remain appropriate choices.

These gates require specification decisions and verifiable criteria before development; they do not assume that tests for the nonexistent feature already pass.

**RESIDUAL_RISKS:**

- A measurement remains a point-in-time observation; the per-process cache explicitly accepts up to fifteen seconds of staleness for the first fact.
- PostgreSQL can attest its outbox and leases without attesting Neo4j content.
- Production dates, effective SQL roles, and private procedures remain to be verified in their operational context.
- Lots C/D and backfilling the existing stock remain legitimately deferred. Their absence is not grounds for rejection; the identities and evidence they will consume must nevertheless be defined now.

**REQUIRED_SIGNATURE:**
```text
role=independent-spec-reviewer
expected_model=gpt-6-astra
permissions=read-only
```