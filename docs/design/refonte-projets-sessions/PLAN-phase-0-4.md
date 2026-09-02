# PLAN Phases 0→4 — Redesign of PROJECTS + SESSIONS by instrumented accretion

- **Date**: 2026-08-18
- **Status: TARGET TO BE PROPOSED — NOTHING STARTS WITHOUT THE OPERATOR'S EXPLICIT
  FRAMING** (anchor ticket `d30cf6e5`: "DO NOT START without the operator's explicit
  framing"). No line of code exists, no migration is written. This plan is the
  proposed execution of the twin ADR
  `docs/design/refonte-projets-sessions/ADR-refonte-projets-sessions.md` (status
  PROPOSED); it can be read on its own.
- **Origin**: synthesis of three architect proposals judged by a panel of three
  lenses — baseline = proposal B (majority), weaknesses raised by the judges fixed,
  grafts from A and C integrated. Nothing here is new without evidence (ticket,
  learning, code verified on 2026-08-18) or a judge behind it.
- **OPERATOR FRAMING OF 2026-08-19 — five answers obtained, this plan is amended by
  them.** The single source is the **twin ADR, §0**. Binding summary: Q10 = list B
  covers, but the order now comes from the **"knowledge traceability"** axis; Q12 =
  **track (a)**, two session natures; Q1 = derived from Q12; Q15 (new) = **new
  terminal state**, migration **M-G** on the 037 CHECK; sequencing = **P0 → P1 → P2 →
  P4.4 → P3**, and **Q6 promoted to a Phase 0 exit criterion**. Still blocking for
  Phase 0: **Q2, Q3, Q6, Q14**.
- **SESSION 2 OF THE SAME DAY — PHASE 0 IS UNBLOCKED.** Single source: **ADR §0bis**.
  `start` becomes **AUTOMATIC**, on the key **`(project, connection)`** — which
  **inverts the default nature** (now `agent`; a single, retroactive gesture, the
  *claim*, promotes to `operator`). **Q9 settled by measurement: subagents INHERIT**,
  without a tag, and **Q1's corollary dissolves** ("exactly one" becomes true by
  construction). **Q2 = `from_project`**; **Q14 = (a) widen `knowledge_sources`**;
  **Q3 = storage of the proposal + shape of the ticket payload**, its sub-decisions
  (a) and (b) being **dissolved**; **Q6 = accepted**, unsigned drafts survive in a
  pending pool. **Timeout: an `operator` session is NEVER closed by inactivity**;
  tracing sessions are, at **4 SIGNED hours** (2026-08-20) — as an **ELIGIBILITY**
  threshold for the nightly sweep, never as a closing delay: worst-case real latency
  ≈ 28 h. *AMENDED on 2026-08-20 — ADR §0ter is authoritative (decision `c5160259`).*
  Still open, without blocking: Q4, Q5, Q7, Q8, Q11, Q13.

---

## 0. Minimal context for a self-contained read

brain-v42: "second brain" MCP server (Python 3.12, FastMCP 3, SQLAlchemy 2 async,
PostgreSQL 16 + pgvector, Neo4j as a relationship index). Sessions lifecycle v4
(migrations 032+037): seven explicit commands, dual-rail state machine (SQL CHECK +
Pydantic), non-blocking focus CAS, exclusive attribution ledger (PK `knowledge_id`),
idempotent replays. **Covenant**: `start`/`list`/`resume`/`capture`/`heartbeat`/
`end`/`abandon` are explicit user commands; the sole server-side exception is the
`auto_stale_7d` sweep — **armed in DRY since 2026-08-18, by operator decision**
(measured that day in the systemd drop-in `killswitches.conf`:
`BRAIN_DREAM_SWEEP_ENABLED=true`, `BRAIN_DREAM_SWEEP_DRY_RUN=true`, 18 sweepable
ghosts measured that day; WET has never been armed). **This "18" is already
stale**: re-measured on 2026-08-19, **29 `open` sessions, 21 of them sweepable
>7 d** (out of 467 rows) — dated, perishable, to be replayed before any arming
decision, never to be copied. An earlier version
of this plan wrote "never armed" — a copy-forward of a 2026-08-16 state, against its
own rule R3, corrected. Any proposal that has an agent, a hook or a client open/close
a session is a covenant change to be settled by the operator.
**Operator answer already established** (`2bd14b24`, 2026-08-06): a session serves
knowledge traceability, and the lifecycle must become "automatic, not declarative."
**The track is now CHOSEN — this paragraph used to say "none chosen," and that was
true up through 2026-08-18 inclusive.** Framing of 2026-08-19, Q12: **track (a), two
session natures** — tracing agent auto-closed with no ritual, operator with ritual
(ADR §0.1 and D11). This plan is therefore no longer "a transitional state
compatible with all three tracks": it has a target. Two consequences it still
handles poorly, and that must be read before executing it — the covenant is
**amended** for the agent nature (C1), and the 037 state machine is **extended** by
migration M-G (Q15, ADR §0.4), while the rest of the plan is still written under the
opposite assumption.

**Proven pains** (detail and evidence in the DOSSIER and the ADR): B1 subagent
ghosts (39 at once, critical); B2 lying heartbeat in both directions
(critical); B3 capture at 18%/attribution at 34% (high); B4 uncapturable tickets
+ batch fail-closed as a block; B5 rigid capture window; B6 focus with no content
guard; B7 no checkpoint; B8 dead `X-Brain-Session` (constraint — **RE-MEASURED AND
CONFIRMED on 2026-08-19 on Claude Code 2.1.234**, verdict unchanged:
`docs/upstream/2026-08-19-b8-session-join-rejeu.md`. This passage used to announce a
"planned" replay; it is **done**); B9
free-form `client_key`; B10 ghost drift (closed, never to regress); **B11
colon sub-projects — 86 artifacts with no nightly run at all, and 533 with no
cross-consolidation** (the "479 outside consolidation" from spec `dbb7c5ce` dated
from 2026-08-08 and has been re-measured: two of the six colon keys have been in the
dream pool since 2026-08-10, i.e. 84% of that mass); B12 undocumented project
system; B13
undifferentiated capture error (ids listed, one aggregated reason — verified); B14
actor not persisted on the session (verified). The pain → phase → measurement map is
in §6.

**Priority order — fixed by the operator on 2026-08-19, it no longer comes from
severity.** The chosen axis is **knowledge traceability**: **B3, B4, B5 first**. The
"critical / high / medium" scoring above remains the *severity* scoring and keeps its
descriptive value, but it **no longer drives sequencing**. That is what moves
Phase 4.4 right after Phase 2 and pushes Phase 3 down (see the header banner and ADR
§0.3).

**Guiding principle**: every object is a FACT observed by the server or a JUDGMENT
declared by the human, never both. The server observes and prepares; the operator
signs.

---

## 1. Cross-cutting rules (non-negotiable)

**R1 — Alembic pin (HARD constraint, ticket `c60d023d`).**
`_REQUIRED_ALEMBIC_HEAD = "045"` (`src/brain_v42/maintenance/plan_index_repair_store.py:63`,
guarded by `tests/unit/test_plan_index_repair_head_pin.py`) makes the plan-index
repair fail-closed in production at the smallest drift. So:
1. Every migration in this plan ships in **THE SAME COMMIT** as the batch below.
   **Third version of this recipe: the first two claimed to be "the full coupling"
   and were not.** Inventory redone by hand with grep, guards named one by one:
   - pin bump + its test (`tests/unit/test_plan_index_repair_head_pin.py`);
   - **README** and **MCP_TOOLS**: the `migration {head}` string
     (`test_documentation_contract.py:1840,1843`);
   - **ARCHITECTURE**: a **different** string, `migrations 001–{head} defined`, em
     dash included (same test, l.1839). Following the old recipe to the letter on
     ARCHITECTURE made the test red;
   - **CLAUDE.md**: to be updated (work contract), but **it cannot be in ANY commit
     of this repo** — `git check-ignore -v CLAUDE.md` → `.gitignore:74`, absent from
     `git ls-files`, and the test keeps its assertion behind `if CLAUDE:`
     (l.25-32: "CLAUDE.md is tracked only in the private archive"). In CI the clause
     is mute;
   - **SCHEMA.md**: table count (M-C…M-F each add one, "32 tables
     public" and the revision count move) **and** the sentence "The repository
     target is {head}.", doubly pinned by
     `tests/unit/test_recovery_contract_v4.py:437-446` — a duplicate the repo itself
     documents as "easy to miss when inventorying guards";
   - **`docs/OPERATIONS.md:118`** ("The repository migration target is 045.") —
     absent from all earlier lists;
   - `tests/unit/test_recovery_contract.py:279`: `assert script.get_heads() == ["045"]`,
     a literal **in a test named for revision 031**. Red on **every** bump;
   - `tests/unit/test_recovery_contract.py:292` and `…_v2.py:33-39`: the frozen
     `table_set` is re-derived from `METADATA.tables` minus a hard-coded exclusion
     set. **M-C, M-D, M-E and M-F each add a table** ⇒ four bumps out of six turn
     these two tests red, and they mention neither the pin nor sessions;
   - **regenerating `ops/recovery/` (item 5)** for M-A, M-B and M-D — and **BOTH v4
     assets, not just one**: `brain-v42-v4.sql` AND `brain-v42-v4-pgrestore.sql`
     (see item 5, "fourth mechanism" and "two assets");
   - the **renaming of the head-named guard test** (`test_repository_head_045_is_documented_…`).
   ("The same series of commits" remains refused — desynchronization window.)
2. **Never two heads in flight** (merged but not applied) at once — either within
   this plan's own heads, AND against 046 (item 6).
3. Rollout of each head: apply → **measure** `select version_num from
   alembic_version` (never copy forward — this doc has already lied for three days
   then ten days about this number) → restart the MCP server → canary.
   **Named exception, M-D**: between the `upgrade` and the restart, the live process
   still runs the pre-M-D code, which writes no history row. The deferred constraint
   trigger would then abort at COMMIT **any `brain_session_end` with
   `focus_outcome=applied`** — fail-closed, session left open —, and M-D has neither
   a killswitch nor a practicable downgrade. The migration therefore creates the
   trigger **disabled** (`ALTER TABLE … DISABLE TRIGGER`); the runbook activates it
   **after** the MCP restart, as a named operator gesture, canary included. Without
   this exception, the unavailability window would be imposed by the plan's own rule.
   **Cost to announce**: the entire disabled window is a **red** `ops/recovery/`
   attestation window (item 5 — it requires `tgenabled = 'O'`), hence short, dated,
   and asset regeneration falls within the activation gesture, not the upgrade one.
4. **The review the pin requires applies to OUR heads.** Failure message from
   `test_plan_index_repair_head_pin.py:45-52`: "review what {head} changes on the
   tables the repair writes (**indexed_plans, indexed_plan_chunks, project_contexts**) —
   new triggers, new constraints or new NOT NULL columns …". **M-D adds a constraint
   trigger on `project_contexts`.** Its commit carries the written review, in the
   pin's docstring format, showing the trigger is scoped to `UPDATE OF current_focus`
   and therefore inert for the repair's `UPDATE plan_scan_paths`
   (`plan_index_repair_store.py:294-308` and `:560-584`).
5. **The `ops/recovery/` recovery attestation breaks on three of the six heads —
   through FOUR mechanisms, and across TWO assets.** It wasn't named anywhere in this
   plan; two passes later it is, but still by half. The runbook requires of it "all
   statuses are pass" and "exactly 25 unique checks"
   (`tests/unit/test_recovery_contract_v4.py:480-486`).

   **TWO v4 assets, not one — the inventory only named `brain-v42-v4.sql`.** Finding
   from 2026-08-19: `ops/recovery/` also contains **`brain-v42-v4-pgrestore.sql`**,
   the variant meant for a database **restored via `pg_restore`** (the one used for
   isolated evidence: `docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`). This document,
   the ADR and the DOSSIER named it **zero times** — measured: `grep -c pgrestore`
   returned `0/0/0` across all three. Yet it is neither dead nor decorative:
   - `tests/integration/db/test_recovery_contract_v4_execution.py:106` is
     **parametrized over both** (`@pytest.mark.parametrize("asset", ["brain-v42-v4.sql",
     "brain-v42-v4-pgrestore.sql"])`) and runs them against a real database, in a
     READ ONLY transaction, checking all 25 checks and the absence of mutation;
   - `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` enforces **CTE
     parity**: the allowed gap is exactly `{observed_artifact_constraints,
     observed_session_constraints}`, and `not (_cte_names(live) - _cte_names(pgrestore))`
     forbids a CTE existing on the live side without existing on the pgrestore side;
   - `tests/unit/test_recovery_contract_v4.py:273-279` requires the runbook to
     distinguish the pgrestore and live gates.
   And it carries **the same structures** that M-A/M-B/M-D break: measured on
   2026-08-19, `brain-v42-v4-pgrestore.sql` has 12 lines carrying
   `expected_runtime_user_triggers`, `observed_column_fingerprints`,
   `expected_artifact_constraints` or `knowledge_sources`
   (15 counting `expected_session_indexes`), at the same logical locations as the
   live variant. **Direct consequence**: following R1.1, R1.5 and §8 to the letter
   would regenerate **one** of the **two** v4 assets, and CTE parity would turn
   `test_recovery_contract_v4_pgrestore.py` red at the first CTE added on the live
   side. Wherever these documents write "regenerate `ops/recovery/`", read **both v4
   assets**. This is the **fourth** iteration of the same defect in this dossier —
   a guard inventory declared complete and incomplete (pin alone, then pin plus four
   documents, then `ops/recovery/` forgotten, now half of `ops/recovery/`): the
   failure mode is not forgetting a guard, it is **trusting the completeness** of a
   list that hasn't been re-grepped.

   The mechanisms, head by head:
   - **M-A**: `observed_column_fingerprints` md5's the full ordered column list of
     `brain_sessions` ⇒ `session_column_mismatches > 0`; the md5 is also
     pinned in a unit test (`…_v3.py:170`, `"bf4c2a47…"`);
   - **M-B**: `expected_artifact_constraints` hard-codes the CHECK at **seven**
     values ⇒ `artifact_constraint_mismatches > 0`;
   - **M-D**: `expected_runtime_user_triggers` is a **closed list of thirteen
     triggers across five tables**, of which **seven on `project_contexts`**
     (`v4.sql:533-548`, reread on 2026-08-19; the seven are identical in
     production) and `runtime_trigger_mismatches` counts **unexpected** triggers on
     `expected_runtime_trigger_tables`, which contains `project_contexts` ⇒ any
     added trigger fails the check.
     **Collision with R1.3, seen on 2026-08-19**: R1.3 has this trigger born
     **disabled**, yet the expected-trigger join carries
     `AND observed_user_trigger.tgenabled = 'O'` (`v4.sql:913-918`) — a trigger
     *expected but switched off* counts as a mismatch exactly like a missing one.
     Off the list it is unexpected, on the list it is switched off: **no
     regeneration order makes the attestation green while the trigger is
     deliberately disabled.** Therefore: regeneration sequenced **with the
     activation gesture** and not with the `alembic upgrade`; disabled window =
     red attestation window, short, dated and announced; and the "DISABLE TRIGGER
     as a makeshift switch" mentioned in this phase's Rollback reopens this red
     window on every use — this is not a free killswitch.
   - **M-A (bis) — `expected_session_indexes`, the FOURTH mechanism, uncounted so
     far.** These documents only counted three (columns, artifact CHECK, triggers);
     there is one more, and it targets `brain_sessions` precisely.
     `ops/recovery/brain-v42-v4.sql:404-412` freezes the **CLOSED** list of
     `brain_sessions` indexes — three `(index_name, definition_md5)` entries:
     `brain_sessions_pkey`, `idx_brain_sessions_project_status_started`,
     `uq_brain_sessions_project_client`. It is checked **twice** in
     `session_constraint_mismatches`: `:665` counts expected indexes that are
     missing or whose `md5(pg_get_indexdef(...))` has moved, and `:687` counts
     indexes **present on the table and absent from the list**. An index added on
     `brain_sessions` therefore makes `session_constraint_mismatches > 0`, expected
     to be 0 like the three other counters. Doubled on the unit side by
     `SESSION_INDEX_DEFINITION_MD5`
     (`tests/unit/test_recovery_contract_v3.py:164-168`) and
     `test_v3_pins_the_exact_session_index_set` (`:488`) — and the three md5s are
     literally present in **four** assets (`v3`, `v3-pgrestore`, `v4`,
     `v4-pgrestore`, measured on 2026-08-19). **This mechanism only fires if a head
     adds an index**; it is therefore dormant as long as M-A limits itself to three
     nullable columns — and that is exactly why it must be counted BEFORE briefing
     the index decision of §3.3/§3.6, not after.
   The **four** counters are expected at **0**: without regeneration, the
   `brain_runtime_032_036_037` check goes `fail` **from the first head onward and
   stays there** — and the regeneration covers **both** v4 assets (live and
   pgrestore), not just one.
   **And "three of the six heads" is only true under an assumption that must be
   written down**: the one where no head in this plan adds an index on
   `brain_sessions`. If the index decision from §3.3/§3.6 (emitter D5 on
   `started_by_actor`) is taken, two outcomes: the index travels **inside M-A**, and
   then the head count doesn't move but M-A breaks **two** structures instead of one
   (columns *and* index) — the "Attestation" column in §8 must say so; or it is
   deferred into **its own head**, and then it is **four heads out of seven**, in a
   lane that forbids two in flight (R1.2). Not deciding amounts to letting §8 lie
   about the regeneration scope.
   **And part of it does not get fixed by regenerating**: `knowledge_sources`
   (v4.sql:1083-1090) is the UNION of the **six** capture tables — not tickets —
   and `artifact_source_matches` requires `source.project_key = session.project_key`.
   The first `'ticket'` artifact and the first `pk → pk:child` capture produce
   **permanent** `artifact_source_mismatches`, which no canary purge catches up on.
   These are two of this plan's **phase exit criteria** that would render the
   restoration proof false: **Q14 settles this before M-B.**
6. **046 (embedding dimension) is planned on this lane, not written**:
   `ls alembic/versions/` stops at `045_dream_run_model_width.py` (verified). Ticket
   `c60d023d` calls it "not urgent" and lists unplanned work (convergent terminal
   revision, NO-OP DDL, fail-closed pass 1, killswitch, HNSW rebuild, nonexistent
   test harness). **The "M-A does not merge until 046 is applied" gate is LIFTED**:
   it held six heads hostage to a ticket that is not urgent and not written, while the
   risk it invoked (two heads meeting at once) is already covered by item 2, which
   holds in **both** orders. This plan's heads remain labeled M-A…M-F, **relative**
   numbers: whichever of the two series arrives first takes the next number, the
   other waits for its measured application.
   Summary in §8.

**R2 — Strict TDD** (CLAUDE.md): every increment follows Red (test that fails for the
right reason) → Green (minimum) → Refactor → atomic Conventional Commits commit.
Coverage ≥ 60%. Green before commit: `pytest tests/unit`, `ruff check`,
`ruff format --check`, `mypy src/`.

**R3 — Killswitches**: every new runtime behavior is born behind its own flag,
shipped **closed**, with a test proving closed-by-default, and its production state
is **measured** (drop-in inspected, process inspected — the lesson of client-activity
measured "ARMED" while the code default is `false`). Summary in §8.

**Correction — R3 pre-decided the sub-question it claimed to leave open.** An earlier
version wrote: "a new explicit tool (checkpoint) has no killswitch — it's a user
command, gated by operator decision." That treats as SETTLED the branch that Q3(a)
declares OPEN, and uses it to justify the absence of a flag. But the shipped artifact
is **identical under both answers**: nothing to arm, nothing to disarm, and nothing
server-side distinguishes an agent call from a human call (B8: `X-Brain-Session` is
dead; `X-Brain-Agent` is a project, declared by the client). Because the checkpoint
**refreshes `last_heartbeat_at`**, the only signal the live sweep uses
(`pg_brain_session.py:520-522`, heartbeat-only predicate, sweep armed DRY measured in
the drop-in), an agent that checkpoints alone keeps its session alive indefinitely —
the false-alive that `2bd14b24` condemns — **without violating a single shipped
constraint**, and makes criterion 4.3 "zero abandonment of a session with a recent
checkpoint" self-satisfiable by its own writes. **Corrected rule**: the tool itself
has no flag (it is indeed a command), but **its heartbeat effect does have one** —
`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`, shipped **closed** (§8bis), unless
Q3(a)/(b) removes the effect from the contract. Shipping the effect without either of
the two is arming a covenant change by omission.

**R4 — Actor width**: every new actor column is `VARCHAR(64)`, aligned with
`MAX_ACTOR_LENGTH = 64` (`src/brain_v42/provenance.py:23`) and `access_log.actor`
`String(64)` — panel fix; none of the three proposals had verified it.

**R5 — Identity linking**: every session↔activity link goes through
`(project_key, started_by_actor)`. **Never through `access_log` alone**: this table
has no project column (verified `src/brain_v42/db/tables.py` — `entity_type,
entity_id, access_type, accessed_at, actor`). Ambiguity rule: 0 or ≥2 candidate
sessions ⇒ zero write + counter. The server never guesses.

**R6 — Perishable numbers**: every measurement quoted here (18%, 34%, 39 ghosts,
colon mass…) is dated and perishable; Phase 0 re-measures all of them; never copied
forward, always replayed via the script. **And an exact number can carry a false
inference**: the "10/59 contexts with NULL focus" were correct and did not prove what
they were made to say (§5.1); the "479 artifacts outside consolidation" were correct
on 2026-08-08 and stale ten days later (§6, 4.6). Re-measuring therefore includes
**rereading what the measurement says**, not just replaying the query.

---

## 2. Phase 0 — Measure, anchor, document, submit (ZERO mutation)

No migration, no behavior change, no flag.

### Content

1. **Scripted, dated baseline** — read-only SQL queries versioned under
   `docs/design/refonte-projets-sessions/` + a replayable script that produces a
   dated JSON snapshot:
   - `open` sessions / stale >24 h / >7 d; `client_key` distribution;
   - capture rate (closed capturing sessions / closed) and attribution rate
     (attributed artifacts / created) over the last 30 d — recompute of the 52/291
     and 226/661;
   - mass per colon key — **re-measured on 2026-08-18, and the remedy has moved**:
     533 artifacts across six keys, of which `red-shrik:agent` (312) and
     `red-lab:architect` (135) are **already in the dream pool since 2026-08-10**
     (drop-in read). 86 artifacts remain across four `red-lab:*` keys with no run
     at all. The "479 / 20.2%" from spec `dbb7c5ce` dated from 2026-08-08 and must
     no longer be quoted as-is;
   - **focus state, what is measurable and what is not.** Measurable
     today: the count of contexts with a NULL focus **along with their
     `focus_revision` and `focus_updated_at`** — this breakdown, and only this
     breakdown, distinguishes "never written" from "erased" (measurement from
     2026-08-18: 10/59 NULL, **all** at revision 0 and never dated ⇒ zero observed
     erasure); plus the `focus_revision` distribution and `focus_updated_at` age.
     **NOT measurable, and this is the correction**: an earlier version announced
     "`current_focus` writes **per writer** over 30 d" as a baseline deliverable and
     exit criterion. No source can produce it — `project_contexts` only keeps an
     unauthored counter (`focus_revision`), an overwritten last change
     (`focus_updated_at`) and `updated_at`; there is no history table
     (`project_focus_history` is precisely what Phase 3 creates); `access_log`
     records only reads and is **purged on every flush** (measured at 0 rows); the
     structlog logs only carry `project_context.upserted`/`created`, with no site or
     retention. This breakdown therefore becomes a **result of Phase 3**, not its
     prerequisite;
   - share of HTTP calls carrying a normalizable `X-Brain-Agent` (sizes
     `started_by_actor` and the "exactly one" rule);
   - **cost of the D5 emitter statement on the hot path** — measurement **added** on
     2026-08-19, because the index decision in §3.3 depends on it and nothing
     produced it. `EXPLAIN (ANALYZE, BUFFERS)` of the §3.6 statement — filter
     `(status, project_key, started_by_actor)` **plus** the correlated counting
     subquery — run against `brain_sessions` in production (read-only;
     `started_by_actor` not existing yet, substitute a column of equal
     selectivity, or measure the `status = 'open' AND project_key = :pk` skeleton
     and state the extrapolation). To be published with the day's cardinality:
     467 rows, 29 `open` on 2026-08-19, and the table's **three** real indexes
     (`brain_sessions_pkey`, `uq_brain_sessions_project_client`,
     `idx_brain_sessions_project_status_started`), **none** of which covers the
     actor. Criterion: the conclusion — index needed or not — is **written down**,
     and if it is "yes" it travels into M-A (R1.2) knowing it breaks
     `expected_session_indexes` (R1.5, fourth mechanism).
     ✅ **MEASURED AND CONCLUDED on 2026-08-19** (`baseline/README.md`), and **the
     question changed subject**: under the framing's `(project, connection)` key
     (ADR §0bis.2), the D5 emitter no longer filters on the actor —
     `started_by_actor` leaves the hot path and **needs no index**. A new question
     replaces it: **the connection column, on the other hand, MUST be indexed** —
     the `EXPLAIN` shows an uncovered equality forces a full-table Seq Scan
     (63 buffers vs 2), on the hottest path there is. Chosen shape: a **UNIQUE
     index**, so that "exactly one by construction" is enforced by the database and
     not merely asserted by the design. Measured detail: the correlated counting
     subquery consumed **27 of the plan's 36 buffers** and **disappears** under the
     connection key;
   - **the population that observation would touch, computed WITHOUT writing** —
     replaces the removed `access_log` instrument (§3.5): for each observed
     `(project_key, actor)` pair over a sliding window, the count of corresponding
     `open` sessions (0 / 1 / ≥2). This is the only honest measurement available to
     inform **Q1** before any arming, and it exercises the per-tool project resolver
     that Phase 1 must deliver.
     **This phase does NOT start from zero — an order of magnitude already exists
     and was cited nowhere** (finding from 2026-08-19). Ticket `7ffe0e8a` carries,
     under "Measurement from 2026-08-16 (perishable)," the line "**Simultaneous:
     `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2**": that is already
     a **partial** measurement of the "≥2 open sessions" population, the one R5/N2
     make a criterion and that 4.1 puts first among its exit measurements. Partial
     in two senses, both worth stating: it counts by **project**, not by
     `(project, actor)` pair — so it is a **ceiling**, real ambiguity being smaller
     as soon as two distinct actors share a project —, and since `started_by_actor`
     does not exist yet, **it cannot be refined retroactively**: per-actor breakdown
     will only start with M-A. Re-measured on 2026-08-19, the same query returns
     `auto-discord` 8, `brain-v42` 4, `red-arena` 4, then `datalake-v1`, `red-gift`,
     `claude-dev-pc`, `red-lab` at 2 — i.e. **24 of the 29 `open` sessions in a
     project that carries at least two**. In other words, under the "exactly one"
     rule, non-observation is not an edge case: at the ceiling, it is the majority
     of the fleet. Dated and **perishable** figures — Phase 0 replays them, it does
     not copy them;
   - expected cardinality threshold of future tables (checkpoints, staged) to set
     the exit ceilings for the following phases.
2. **Anchor tests** (pinning current behavior, so the effort does not regress
   anything unnoticed):
   - strict/tolerant canonicalization + aliases (protects B10);
   - `_validate_captures`: all-or-nothing, exact window (strict project,
     `created_at >= started_at`), **current error message shape pinned** — it will
     be changed in Phase 1 by changing the test first (Red);
   - `end` CAS: `applied` and `conflict`, closure guaranteed in both cases;
   - sweep as ONE statement (textual pin, on the model of
     `test_dream_sh_global_phases_outside_loop.py`);
   - idempotent replays of the four paths (start, exact capture, persisted end,
     abandon same reason);
   - covenant sentence present in the seven docstrings — test written to be
     extended: shipping the checkpoint (Phase 2) brings the enumeration to EIGHT,
     and the 8th tool's docstring, CLAUDE.md and this test move in the same commit
     as the tool;
   - closed-by-default of existing sweep flags re-proven.
3. **`docs/PROJECTS_SYSTEM.md`** (closes B12): the projects system end to end — the
   four connected bricks (format, three tables — `project_contexts` comes from
   **001**, `projects`/`project_aliases` from **033**, which only adds immutability
   and trigger-based aliases to the first —, colon convention, dream spec), the
   three-surface table, and the **census of the colon predicate**. This census was
   undercounted **twice**: "the sole exception" in a first version, then "three
   `src/` copies + two views" in the second. Count re-verified on 2026-08-18,
   **five `src/` copies**:
   `db/project_group_scope.py:24-26`;
   `services/project_group_ticket_service.py:129-137` (SQL copy) **and `:164-167`
   (a second copy, in Python, in the same `_lock_participants_scope` method)**;
   `services/proposal_service.py:377-383`;
   **`repositories/pg_project_context.py:202-213`** (`get_keys_by_group`, a
   `split_part` variant, invisible to a grep on `not_like("%:%")`).
   On the database side: **seven live views** (measured:
   `codex_brain_entity_v1`, `codex_feature_artifact_v1`, `codex_feature_v1`,
   `codex_roadmap_curation_proposal_v1`, `codex_ticket_extraction_proposal_v1`,
   `codex_ticket_message_v1`, `codex_ticket_v1`), all coming from **036** — two
   copied CTE bodies (`_RED_KEYS_CTE:23-45`, `_BRAIN_RED_KEYS_CTE:205-227`), as
   `split_part(…) <> project_key AND split_part(…) IN red_base`. 024 is not a
   second live object: 036 replaces its view with `CREATE OR REPLACE`.
   **Three distinct formulations of the same predicate**, then — the doc names them
   all. **Organized by the fact/judgment grid** (graft A via the panel). Public
   repo: no private network address, no path outside the repo, no secret.
4. **Separate checkpoint spec** (`docs/design/refonte-projets-sessions/SPEC-checkpoint.md`)
   — deliverable **added**, required by the audit of ticket `d04dc588` ("the
   smallest admissible batch remains documentary: 1. separate checkpoint spec;
   2. explicit product decision; 3. CAS/replay/heartbeat/end contract,
   migration/rollback, payload bounds and concurrency tests; 4. approval before
   code"). No phase delivered it. It must explicitly settle the **two** divergences
   from the ticket's MVP (append-only storage vs snapshot+CAS; and **payload
   shape** — `progress` + `blocker|null` + `next_step` published together "in one
   call" versus mutually exclusive `kind` + a single `note`), and the fate of the
   heartbeat effect (Q3(a)/(b)). Without it, M-C cannot be written.
5. **Submission to the operator**: list B1–B14 for confirmation/prioritization/
   completion ("quite a few things I'm not happy with" is not itemized — there may
   be un-ticketed irritants), plus the open questions from §9 with a note of which
   tranches they block.
6. **Replay of the B8 spike on the current Claude Code version** — step **added**
   on 2026-08-19, because B8 is scored "High (constraint)" on a stale measurement
   and no phase replayed it. `docs/upstream/2026-08-06-claude-otlp-session-join.md`
   is headed "**Measured version: Claude Code 2.1.220**"; `claude --version`
   returns **2.1.234** on 2026-08-19. The spike itself concludes "Re-measure at
   every Claude Code version bump rather than taking this conclusion on faith,"
   and its two relays (`2bd14b24`, `7ffe0e8a`) repeat the instruction word for
   word. **What B8 sizes, and thus what hangs on this thread**: D1
   (`started_by_actor` as a fallback identity), D5 (`skipped{no_actor}` over
   stdio), D8 (draft linking), R5 and N2 ("exactly one" rule), the named residue of
   D6, and **Phase 4.1's exit criterion #1**.
   **Method**: replay the spike's protocol, not a code read — two disposable
   loopback receivers, `claude -p` with a dedicated `--mcp-config` and
   `--strict-mcp-config`, and question 1 alone ("Does `${CLAUDE_CODE_SESSION_ID}`
   expand usefully in an MCP header?"). The spike's OTLP arm is not replayed here:
   it feeds into no decision in this plan.
   **Criterion**: the result is written, dated and **versioned with the measured
   version up front**, whatever it turns out to be. Two outcomes, two consequences
   declared in advance:
   *(a) unchanged* — `X-Brain-Session` still dead, B8 stops being "scored on a
   stale measurement" and accretion continues as-is; *(b) changed* — a client can
   now declare its session, which **invalidates no deliverable** (the
   `(project, actor)` fallback stays correct) but reopens an option ruled out by
   constraint, to be filed against Q1 and Q9 before any arming. The risk is
   asymmetric in that direction, and that is why the re-measurement is a Phase 0
   step and not a gate.
   **To add to item 1's baseline**: the share of HTTP calls carrying a
   normalizable `X-Brain-Session` (`provenance.normalize_session` → non-`None`),
   next to the share carrying a normalizable `X-Brain-Agent`. Today the baseline
   measures the actor and never the session — the blind spot that let the spike age
   fourteen versions without anything flagging it.

### Exit criteria (measurable)
- ✅ **SATISFIED on 2026-08-19** — baseline delivered under
  `docs/design/refonte-projets-sessions/baseline/`: `queries.sql` (12 measurements,
  **a single statement** hence a consistent snapshot, each carrying `proves` and
  `does_not_prove`), `explain.sql`, `snapshot.py` (replayable in one command) and
  the first `snapshot-20260819T191613Z.json`. **Read-only enforced by the engine**
  (`BEGIN READ ONLY`), proven both ways. The per-writer focus-write breakdown
  remains explicitly excluded — it belongs to Phase 3.
  **Three results not to miss**: 30-day attribution is at **30.4%** versus 34% on
  2026-08-16 — **B3 is degrading, not stagnating**; `client_key` yields **465
  distinct values for 469 sessions** (ratio 1.01), which quantifies B9; and the 10
  contexts with NULL focus are **all** "never written," zero erasure, confirming
  Q13's premise.
- ✅ **SATISFIED on 2026-08-19** — census first, writing second. **Five of the
  seven items were ALREADY pinned** and were not duplicated: canonicalization
  (`test_project_key_canonical.py`, 20 tests), sweep as ONE statement
  (`test_pg_brain_session_sweep.py:74`), closed-by-default flags
  (`test_dream_sh_sweep.py`), `applied`/`conflict` CAS
  (`test_brain_sessions_lifecycle.py`) and **the four idempotent replays**
  (`test_start_replays_same_project_client_key`, `test_capture_exact_retry_is_idempotent`,
  `test_end_exact_terminal_retry_is_idempotent_and_read_only`,
  `test_abandon_exact_retry_is_idempotent_and_read_only`).
  **Two real gaps, filled** (commit `0207209`, branch
  `test/phase0-session-anchors`): the **covenant sentence across the seven
  docstrings**, which had **no** coverage at all — it could disappear from all
  seven without a single suite turning red — and the **current shape of the
  `_validate_captures` error**, i.e. B13, pinned so Phase 1 breaks it in Red first.
  The docstring test **also** pins the number spelled out in the enrollment
  docstring: the checkpoint will bring it to "eight," in the same commit as the
  tool — deliberate friction.
  **Both are proven BY MUTATION TESTING**, never by color alone: removing the
  sentence from a docstring, making the count lie, removing the
  `created_at >= started_at` bound from the capture window, and replacing the
  aggregated reason with per-id reasons — this last mutation **simulates D2's
  arrival and trips exactly the two tests designed to trip first**. Sources
  restored bit for bit after each attempt (`git diff src/` empty). Full suite:
  **7985 passed, 55 skipped**, ruff + format + mypy clean.
- ✅ **SATISFIED on 2026-08-19** — `docs/PROJECTS_SYSTEM.md` written and committed
  (`e04df96`, branch `test/phase0-session-anchors`). Organized by the FACT/JUDGMENT
  grid as required. **The colon-predicate census was REDONE, not copied
  forward** — and it had to be: it had been wrong three times, the third time
  through a fix-up grep looking for `":" in ` that missed the written copy
  `":" not in`. Verified count: **five `src/` copies, seven views, three
  formulations**, i.e. twelve objects, two of them in the same method and one in a
  module that already imports the shared helper without using it
  (`proposal_service.py:17` versus `:377-383`, verified). **New drift, unguarded,
  named in the document**: the key regex exists in **seventeen places across
  sixteen files**, including **five `ops/recovery/` assets**, and no test links
  `_KEBAB` to the SQL CHECK — the 033 test pins a literal rewritten in the test.
  Widening it would break code/database consistency AND the restoration proof
  without a single suite turning red. Public-repo check passed (no private
  address, no path outside the repo, no secret).
- `SPEC-checkpoint.md` reviewed and submitted with Q3.
- ✅ **SATISFIED on 2026-08-19** — B8 spike replayed on **Claude Code 2.1.234**,
  result written, dated and versioned with the measured version up front:
  `docs/upstream/2026-08-19-b8-session-join-rejeu.md`. **Outcome (a): verdict
  UNCHANGED**, nothing is invalidated, accretion continues as-is. Both cases were
  played out — the original spike's false positive (intact parent environment ⇒
  the PARENT's identifier received and **accepted** by `normalize_session`)
  reproduces identically, and thus remains an active trap for any future attempt.
  Played **first** in Phase 0, because Q9 and the `(project, connection)` key
  rested on this premise; they are confirmed by the measurement. The instruction
  "re-measure at every Claude Code version bump" is not lifted: it is honored, and
  reaffirmed.
> ✅ **THIS CRITERION IS SATISFIED since framing session 2, 2026-08-19 (ADR §0bis).**
> Q2 = `from_project`; Q3 = storage of the proposal + shape of the ticket payload;
> Q6 = accepted with a pending draft pool; Q14 = (a) widen; Q10 settled earlier the
> same day. **The criteria that remain UNSATISFIED**: the baseline snapshot, the
> anchor suite, the B8 spike replay, the checkpoint spec, and **the M-G spec**.

- Operator answers obtained at minimum on: Q2 (ticket predicate — blocks M-B),
  Q3 (checkpoint — blocks M-C), **Q14 (what the recovery attestation must learn —
  blocks M-B)**, **Q6 (staged captures — PROMOTED here on 2026-08-19, because the
  "knowledge traceability" axis moves Phase 4.4 right after Phase 2)**, and
  Q10 — **the latter is SETTLED since 2026-08-19** (the list covers; the order
  changes, ADR §0).
- **M-G specification written and submitted** (new criterion, framing of
  2026-08-19): Q15 = route (3) commits to a new terminal state in the 037 CHECK.
  The state's name, its exact branch, the fate of `captured_knowledge_ids` /
  `abandonment_reason` / `focus_outcome`, the auto-close trigger and the fate of
  `next_focus` **are specified nowhere**. Until this criterion is met, D11's
  `agent` nature has no terminal state and cannot ship.

### Rollback / killswitch
Not applicable: nothing mutates.

---

## 3. Phase 1 — See without touching (migration M-A)

### Content

1. **Enumerated capture error** (closes B13): `BrainSessionInputError` carries
   `rejections: [{id, reason}]`, `reason ∈ {not_found, wrong_project,
   created_before_session, ambiguous_type, attributed_elsewhere, unsupported_type}`,
   **plus `capturable_subset`** (graft C adopted by the panel): the ids that would
   have passed. No semantic change — same batches accepted/rejected as before,
   proven by the Phase 0 anchor tests (updated in Red first for the message shape).
   The batch stays all-or-nothing.
2. **Capture suggestions** (pure read, never blocking):
   `project_uncaptured_since_start`
   (≤20, best-effort, computed after a successful close) in the `end` result;
   `project_uncaptured_since_start_count` in `resume` — **names aligned on
   2026-08-19** with the predicate specified below; the earlier version specified
   the predicate then announced "the field carries its exact name" while still
   keeping `uncaptured_candidates` / `uncaptured_candidate_count` two lines above,
   a contradiction within the same item. The "empty ledger with no reason" XOR
   error mentions the candidate count. Assumed role: these are the measurement
   instruments for dossier E3 (their actual usage is itself measured in soak — an
   answer to the simplicity judge's reservation).
   **"Candidate" predicate, specified — it was missing, and the whole instrument
   depends on it.** A candidate is an artifact from the six `CAPTURE_TABLES` such
   that: `project_key = session.project_key` **and**
   `created_at >= session.started_at` (same bounds as `_validate_captures`, so a
   suggestion is always capturable), **and** not already present in
   `brain_session_artifacts` (PK `knowledge_id` — exclusivity makes the test
   trivial). **The actor link is deliberately ABSENT from the predicate**, and this
   must be stated rather than left to be guessed: rule R5 would make it empty
   exactly for the two populations the instrument is meant to shed light on — stdio
   sessions with `started_by_actor` NULL (B8) and the B1 ghost regime (≥2 open
   sessions for the same carrier). Assumed consequence, **written into the rendered
   field**: these are artifacts created **in the project since `started_at`**, not
   "this session's work." They may belong to a sibling session. The field
   therefore carries its exact name — `project_uncaptured_since_start`, reused
   verbatim at the top of the item and in the ADR (D8) — and the E3 measurement
   uses it as a **ceiling**, never as a numerator.
3. **Migration M-A**: three nullable columns on `brain_sessions`, outside CHECK
   037, with no backfill (`NULL` = "before," doctrine 040/041):
   - `started_by_actor VARCHAR(64)` (R4 — closes B14);
   - `last_observed_at TIMESTAMPTZ` (written only under item 6's flag);
   - `intent VARCHAR(500)` (graft C: the human field for triaging ghosts).
   **INDEX decision to be briefed, not decided here — the word "index" was absent
   from this plan and the ADR** (finding from 2026-08-19). `started_by_actor` is
   created **with no index**, and it is the column item 6's emitter filters on for
   **every outermost tool call**, twice in the same statement (the `WHERE` and the
   correlated counting subquery). The real indexes on `brain_sessions`, measured
   on 2026-08-19 (`pg_indexes`), are exactly three — `brain_sessions_pkey` on
   `id`, `uq_brain_sessions_project_client (project_key, client_key)`,
   `idx_brain_sessions_project_status_started (project_key, status, started_at DESC)`:
   **none covers the actor**. The third covers `(project_key, status)` and leaves
   the actor equality as a residual filter, which at the measured cardinality
   (467 rows, 29 `open`) is very likely free — *likely* is not *measured*, and this
   is a hot path.
   **What is decided here**: nothing. **What is required**: (a) Phase 0 measures
   the real cost of the emitter statement against the production table (see §2,
   baseline), (b) M-A's commit carries the written conclusion — index or not —,
   (c) **if an index is added, it breaks `expected_session_indexes`**, the CLOSED
   list of `brain_sessions` indexes (`ops/recovery/brain-v42-v4.sql:404-412`,
   checked at `:665` and `:687`, doubled by `SESSION_INDEX_DEFINITION_MD5` in
   `test_recovery_contract_v3.py:164-168` and `:488`): this is the **fourth**
   attestation-breaking mechanism catalogued in R1.5, and it holds across **both**
   v4 assets. (d) Deferring it into its own head would cost one more production
   rendezvous in a lane that forbids two heads in flight (R1.2): if an index there
   must be, it travels **inside M-A**.
4. **Enriched `start`**: persists `started_by_actor` (best-effort, NULL if stdio
   with no header — we degrade without attributing, rule `7ffe0e8a`); accepts an
   optional `intent`; the result gains `open_sessions_same_carrier` (graft C: "the
   operator sees their own ghosts at the moment they would create one more").
5. **Enriched `list`**: `client_key_prefix` filter; `intent` display; documented
   `client_key` convention (recommended, never enforced — a server-side constraint
   would break every client over a sorting issue).
   **"Presence on read" is REMOVED from this phase.** An earlier version shipped it
   as an opt-in `with_observed_activity` parameter, computing a
   `last_knowledge_activity_at` by joining `access_log` (`actor = started_by_actor`,
   `accessed_at >= started_at`, **lookback capped at 7 d**) against the six
   knowledge tables, and presented it as "the honest liveness signal available
   BEFORE any arming, and the comparison measurement to settle question #1." The
   first pass's correction had only spotted the missing project column. **Three
   verified facts condemn it:**
   - `access_log` **is not a log, it's a continuously drained buffer** —
     `repositories/pg_access_log.py:38-113` aggregates then runs
     `sa.delete(access_log).where(id <= max_id)` **in the same transaction**; the
     caller is `DecayFlusher`, `interval_seconds=300` by default (`config.py:379`).
     A 7-day lookback on a table that retains at best ~5 minutes returns nothing;
   - the aggregation **folds the actor into counters**: the `actor` column does not
     survive the flush, so `actor = started_by_actor` has nothing left to join;
   - the only writers are **reads** (`search_hit`, `get_by_id`, `use`, `execute`),
     never creations.
   **Measurement, 2026-08-18**: `select count(*) from access_log;` → **0 rows**,
   while `select max(last_accessed_at), count(*) filter (where access_count>0) from
   learnings;` → `2026-08-18 20:11:06+00 | 2209` — the write-aggregate-purge cycle
   runs fine, and the instrument declared to inform **Q1** was structurally
   measuring zero. What replaces it is in Phase 0: the `(project, actor)` ×
   open-sessions population (0 / 1 / ≥2), computed **without writing**, which
   measures exactly what arming would touch.
6. **Observation emitter in the middleware, shipped CLOSED**
   (`BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=false`). Specification required by
   the panel — ONE statement, "exactly one" rule:

   ```sql
   UPDATE brain_sessions
      SET last_observed_at = NOW()
    WHERE status = 'open'
      AND project_key = :pk
      AND started_by_actor = :actor
      AND (SELECT count(*) FROM brain_sessions s2
            WHERE s2.status = 'open'
              AND s2.project_key = :pk
              AND s2.started_by_actor = :actor) = 1
   ```

   Emitted on the outermost call of a tool carrying a resolvable `project_key` and
   a normalized actor.
   **Hot path, and its cost is not measured.** This statement — a filter on
   `(status, project_key, started_by_actor)` **plus** a correlated counting
   subquery over the same three columns — runs on **every** outermost tool call,
   exactly where client-activity has already shown that one writer per call is not
   free (`1c40c36a`, burst loss beyond 8 concurrent calls, tracked and not closed).
   Yet `started_by_actor` is born **with no index** and none of the three
   `brain_sessions` indexes cover it (measured, §3.3). The index decision is
   briefed in §3.3 and **measured in Phase 0**; it is not settled here, and it is
   not free either — see the `expected_session_indexes` attestation breakage
   (R1.5).
   **Premise correction — the middleware does NOT see the project.** An earlier
   version wrote "the middleware already sees both, including behind
   `brain_call_tool`"; that is true for the actor, false for the project.
   `src/brain_v42/mcp/provenance_middleware.py:74-96` is complete and reads only
   **headers**: `get_http_headers(include={'mcp-session-id'})`,
   `normalize_agent(x-brain-agent)` (l.76), `normalize_session(x-brain-session)`
   (l.77), `normalize_transport(...)` (l.83), three ContextVars, the
   re-entrance guard, `call_next`. It **never** inspects
   `context.message.arguments`
   (`grep -rn '\.arguments' src/brain_v42/mcp/` → only
   `dream_capabilities.py:250,258`). The only code in the repo that resolves a
   project from a tool's arguments is `services/dream_project_scope.py`, and it
   does so via a **PER-TOOL policy table** (`PROJECT_TOOL_POLICIES:83-120`:
   `project_key`/`project_keys`/`owner_project_key`, `inject_project_key`, typed
   references to resolve in the database) — proof that this is per-tool work, not
   an already-available datum.
   **Deliverable added to this phase, without which the emitter has no `:pk` to
   set**: a **per-tool project resolver**, which reuses `PROJECT_TOOL_POLICIES`
   (a single table — same consolidation doctrine as the colon predicate) or states
   why it needs a second one. Tools outside the table are **not** observed and
   count as `skipped{no_project}`: the server never guesses a project. The same
   resolver serves the Phase 4.4 staged writer, which rested on the same premise.
   `rowcount = 0` ⇒ no write; a best-effort counting query then distinguishes
   `ambiguous` from `no_match` for the counter. Counters:
   `observed_activity_written` / `observed_activity_skipped{ambiguous, no_actor,
   no_project}`. Envelope modeled on the proven client-activity pattern (ticket
   `1c40c36a`): an observation failure **never** breaks the call it observes —
   proven by an **injected-failure test** (graft A: an exit criterion, not just
   counters).

### Exit criteria (measurable)
- Phase 0 anchors green (excluding tests deliberately turned Red then adapted for
  the error shape).
- Canary: a rejected mixed batch returns a reason **per id** + the capturable
  subset; `resume` shows a candidate count on a test session; `start` with
  `intent` shows it in `list`; `start` on a project where the same carrier already
  has an open session returns the warning.
- `started_by_actor` populated on new HTTP sessions (share measured against the
  Phase 0 baseline).
- Killswitch measured **closed** in production: drop-in inspected, process
  environment inspected (R3).
- Injected-failure test green: the emitter's simulated failure leaves the tool
  call intact.
- **The canary above consumes M-A's rollback window** — "`start` with `intent`
  shows it in `list`" writes a non-NULL `intent`, and M-A's downgrade is
  fail-closed as soon as a non-NULL `intent` exists. An earlier version only wrote
  this rule in Phase 2: the exit canary would therefore **permanently** close the
  rollback window of the head that M-C, M-D and the entire linear lane depend on.
  So, as in Phase 2: canary on **throwaway sessions**, **documented purge of the
  canary `intent`s** after validation, then a **blank downgrade on a staging
  database** proving the window is still open — before declaring the phase exited.
- Pin: M-A applied, `alembic current` measured at the new head, pin bumped in the
  same commit, **`ops/recovery/` regenerated — BOTH v4 assets, `brain-v42-v4.sql`
  and `brain-v42-v4-pgrestore.sql`** (M-A changes the `brain_sessions` column
  fingerprint, R1.5; plus `expected_session_indexes` if §3.3's index is chosen),
  plan-index repair re-proven functional.

### Rollback
M-A downgrade = drop three nullable columns — no lifecycle-state loss, but
**a named judgment loss**: `intent` is a declared human line (fact/judgment grid,
N7). Same doctrine as M-C ("it's judgment, we don't throw it away silently"):
downgrade **fail-closed if at least one non-NULL `intent` exists**, explicit
purging of intents then being a runbook operator gesture — and, as 039 shows
(`-x allow_project_context_trigger_downgrade=yes`), this guard **can** take a
named opt-in instead of a destructive purge; the choice is made when the
migration is written, not here.
`started_by_actor` and `last_observed_at` are observed facts, re-measurable,
disposable with no guard. Revert of the code commits. Killswitch: the emitter
(item 6) — the phase's only new runtime behavior.

---

## 4. Phase 2 — Capturing right + the checkpoint gesture (migrations M-B, M-C)

### Content

1. **Migration M-B — capturable tickets** (closes B4 together with Phase 1): CHECK
   `brain_session_artifacts_type_valid` widened to `'ticket'` (the current
   enumeration — verified — is `decision, learning, snippet, runbook, adr,
   indexed_plan, legacy`). `CAPTURE_TABLES`/validation gain a dedicated predicate:
   `tickets.from_project == session.project_key AND tickets.created_at >=
   session.started_at` (`from_project` = authorship — gated by Q2, a free veto
   before M-B; the `tickets` table has no `project_key`, verified). Ledger, PK
   exclusivity, idempotence, all-or-nothing: unchanged. Downgrade fail-closed if
   `'ticket'` rows exist (037 template).
2. **SHARED subtree predicate** (graft A, a B5/B11 brick): a single module
   generalizing `project_group_scope` (`base NOT LIKE '%:%' AND key LIKE base ||
   ':%'`, verified) — **one implementation only**, which is a CONSOLIDATION job,
   not a plain creation.
   **Scope corrected: it targeted two and missed two.** An earlier version
   announced "three `src/` copies" and "the two inline copies are absorbed in this
   phase." Recounted on 2026-08-18, there are **five** — and one of the two missing
   ones lives inside the very function this phase claimed to be cleaning up:
   1. `db/project_group_scope.py:24-26` — the reference, to be generalized;
   2. `services/project_group_ticket_service.py:129-137` — inline SQL copy,
      **absorbed in this phase**;
   3. `services/proposal_service.py:377-383` — inline SQL copy even though the
      module already imports `project_group_scope`, **absorbed in this phase**;
   4. `services/project_group_ticket_service.py:164-167` — **a second copy, in
      Python** (`project_key == base_key or (":" not in base_key and
      project_key.startswith(f"{base_key}:"))`), in the same
      `_lock_participants_scope` method as #2. **Absorbed**: the shared module must
      therefore expose **two forms** — a SQL predicate and a Python predicate — from
      a single definition, otherwise the consolidation merely relocates the
      divergence;
   5. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — the same
      semantics in a `split_part` variant, invisible to a grep on
      `not_like("%:%")`. **Absorbed** if the equivalence of the two formulations is
      proven in Red; otherwise catalogued as a third formulation, with the reason
      written down.
   Red: an equivalence test for the **five** predicates over a shared key set
   (including `pk`, `pk:child`, `pk:child:grand`, a key with no colon, a key whose
   prefix is not a base of the group). Green: import of the shared module.
   On the database side, these are not "two views from migrations 024 and 036" but
   **seven live views, all coming from 036** (measured), born of **two copied CTE
   bodies** as `split_part(…) <> project_key AND split_part(…) IN red_base` — a
   third formulation. They stay in place (an applied migration is not rewritten)
   but are catalogued in `docs/PROJECTS_SYSTEM.md` as boundary specimens, to be
   regenerated from the shared predicate at their next revision. Future uses
   (family capture now, `include_descendants` reads in Phase 4.6 if Q4 decides so):
   never a second ad hoc copy.
3. **Family capture, closed flag** (`BRAIN_SESSION_CAPTURE_SUBPARTITIONS=false`): on
   arming (operator gesture, after Q4), a session on `pk` can capture an artifact
   from `pk:child` — parent→child direction only, via the shared predicate.
4. **Migration M-C — checkpoints** (closes B7; **conditional on Q3**, the product
   approval that `d04dc588` requires): table `brain_session_checkpoints` —
   `id UUID PK`, `session_id FK → brain_sessions ON DELETE RESTRICT`, `seq INT`
   **supplied by the client** + `UNIQUE(session_id, seq)`, **`progress TEXT NOT
   NULL`, `blocker TEXT` (nullable), `next_step TEXT NOT NULL`** (≤2,000 each on
   the app side), `created_at`; **append-only enforced by a database trigger**
   (UPDATE/DELETE refused — house culture, panel graft); a fail-closed 200/session
   cap with an explicit message.
   **Replay idempotence — agent retries are the norm (a dossier invariant)**:
   `ON CONFLICT (session_id, seq) DO NOTHING` — an exact replay does not append a
   second row; the same `seq` with a different payload is a non-destructive
   conflict, explicitly rejected.

   > **AMENDED on 2026-08-20 — ADR §0bis.4 is authoritative, `SPEC-checkpoint.md`
   > derives from it.** Two propagation fixes, no new decision:
   > **(i)** this paragraph used to carry `kind VARCHAR(20)` CHECK ∈ {progress,
   > blocker, next_step, handoff} + `note TEXT` — **the ABANDONED shape**. The
   > signed shape publishes `progress` + `blocker|null` + `next_step` **TOGETHER,
   > in one call** (divergence (d) taken from ticket `d04dc588`: three mutually
   > exclusive `kind` values allow emitting a `progress` without ever a
   > `next_step`, and the freshness reader then cannot tell whether the snapshot is
   > complete). `handoff` disappears as a nature.
   > **(ii)** the note "**does not refresh the heartbeat a second time** (refresh
   > conditioned on `rowcount = 1`)" assumed a heartbeat effect that §0bis.4 has
   > **dissolved**: the checkpoint neither writes nor touches `last_heartbeat_at`,
   > ever. The append-only storage, `UNIQUE(session_id, seq)`, is **kept**.
   **TWO divergences from `d04dc588`'s MVP, not one** — the earlier version
   declared only one and elsewhere claimed to keep the doctrine "as is" (§7,
   line B7):
   - **storage**: append-only + `(session_id, seq)` where the ticket recommends a
     snapshot on `brain_sessions` + CAS `expected_checkpoint_revision`. Reason:
     append-only keeps the history of notes (that's judgment) and regains the P0
     properties (replay with no double effect, non-destructive conflict) through
     the key;
   - **payload shape** (reread in the ticket): its contract is
     `brain_session_checkpoint(session_id, expected_client_key,
     expected_checkpoint_revision, progress, blocker|null, next_step)`, a bounded
     response distinguishing *activity, milestone, blockage, freshness,
     focus_context*, criterion "**one call**." The `kind ∈ {progress, blocker,
     next_step, handoff}` + single `note` turns three fields **published together**
     into three **mutually exclusive** natures: publishing progress + blockage +
     next step would require three calls, three `seq`s, three heartbeat refreshes,
     and would break the "one call" criterion. **Q3(d) settles it; the Phase 0
     checkpoint spec writes it down before any code.**
   Downgrade fail-closed if checkpoints exist (it's judgment, we don't throw it
   away silently).
5. **Tool `brain_session_checkpoint(...)`** — signature settled by the Phase 0 spec
   per Q3(d): identity guard before mutation, and **refreshes
   `last_heartbeat_at` as a side effect** (real checkpoint only, never a replay) —
   the gesture "I note where I stand" replaces the empty gesture "I ping."
   **Calling policy: a declared fork, settled in Q3** — either each checkpoint
   remains an explicit user command (covenant intact, but adoption depends on the
   same human discipline that produced **24 stale sessions out of 29** (re-measured
   on 2026-08-19; this passage used to cite "21 out of 23," a 2026-08-16
   measurement, and "21" now denotes the sweepable-past-7-days count, not the stale
   one), and "checkpoint dates the living" only holds if it is called), or an
   agent may checkpoint spontaneously during a long autonomous session — a session
   mutation outside an explicit command, hence a **covenant change**, never armed
   without an operator decision.
   **And this fork now has a mechanism** (R3 corrected): nothing server-side
   distinguishes an agent call from a human call, the shipped artifact is
   **identical under both answers**, and the heartbeat effect alone lets an agent
   keep its session alive indefinitely — B2's false-alive, which would make
   criterion 4.3 self-satisfiable. Therefore: **the heartbeat effect is born behind
   `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT=false`** (§8bis), unless Q3(a)/(b)
   removes the effect from the contract — **a removal that would be a THIRD
   divergence from `d04dc588`** ("Real checkpoint refreshes heartbeat atomically;
   replay does not," reread on 2026-08-19), to be declared along with the other
   two. The tool itself has no flag — it is indeed a command.
   Shipping it
   brings the covenant to **eight** commands: covenant sentence in the 8th tool's
   docstring, CLAUDE.md enumeration and Phase 0 anchor test extended in the same
   commit. `list` gains `last_checkpoint_at`; `resume` returns recent checkpoints.
   Doctrine `d04dc588`: displayed freshness derives from the age of the last
   checkpoint (or heartbeat); focus drift is exposed separately and is never a
   cause of staleness. `heartbeat` stays unchanged and documented "prefer
   checkpoint" — never turned into a no-op (lying to an explicit covenant command
   would be a contract breach, the panel's ground for rejecting proposal A).

### Exit criteria (measurable)
- Canary: capturing a `[learning, ticket]` batch succeeds; a ticket from a
  different `from_project` is rejected with `reason=wrong_project`; family flag
  **measured closed** and, flag closed, the parent→child rejection returns
  `wrong_project` as before (anchor); flag armed on a test project, the
  `pk` → `pk:child` capture passes.
- **These two canaries make the recovery attestation FALSE, and not just for the
  canary's duration.** `ops/recovery/brain-v42-v4.sql:1083-1090` defines
  `knowledge_sources` as the UNION of the **six** capture tables — tickets are not
  among them — and `artifact_source_matches` (l.1091-1107) joins with
  `source_record.project_key = session_record.project_key`.
  `artifact_source_mismatches` (l.1109-1113) counts `source_matches <> 1`,
  expected to be **0**. So: (a) the first `knowledge_type='ticket'` artifact has no
  source row ⇒ **permanent** mismatch; (b) the `pk → pk:child` capture violates the
  project equality ⇒ **permanent** mismatch. Unlike schema fingerprints, **this is
  not fixed by regenerating an asset**: it must be decided whether the attestation
  learns about tickets and the subtree predicate. **Q14 is therefore a
  prerequisite of M-B, not a post-processing step** — without an answer, this
  phase turns a green restoration proof into a permanently red one.
- If Q3 is approved: a real `start → checkpoints → end` cycle on brain-v42; `list`
  shows `intent` + `last_checkpoint_at`; **`last_heartbeat_at` stays UNCHANGED
  after a checkpoint (ABSENCE-of-effect test)**; an exact checkpoint replay does
  not append (test); UPDATE/DELETE on a checkpoint refused by the trigger (test);
  the 201st checkpoint is refused with an explicit message (test).

  > **AMENDED on 2026-08-20 — ADR §0bis.4 is authoritative.** This criterion used
  > to require "**a checkpoint refreshes `last_heartbeat_at` (test)**." That is now
  > a test **NOT to write**: the heartbeat effect is dissolved, and a test
  > verifying it would wire in the behavior the spec forbids. It is **flipped**
  > into an absence-of-effect test — the one that will fail first if anyone
  > re-wires the heartbeat.
- **The canaries above consume M-B/M-C's rollback window** (their downgrades are
  fail-closed as soon as a row exists — and these are the canaries that create the
  first rows). So: canaries run on **throwaway sessions**, and a **documented
  purge of the canary rows** (`'ticket'` artifacts and checkpoints from canary
  sessions) run after validation, followed by a **blank downgrade on a staging
  database** proving the window is still open — before declaring the phase exited.
- Pin: M-B (then M-C) applied one after the other — never two in flight —,
  `alembic current` measured, pin bumped in the same commit each time.

### Rollback
- Family flag closed = Phase 1 behavior unchanged.
- M-B downgrade possible as long as no ticket has been captured; canary tickets
  are purged by the exit-criteria procedure, so the window stays open after
  validation (afterwards fail-closed on the first real captures, as intended).
- M-C downgrade fail-closed if checkpoints exist (intended) — same canary purge.
- **Rollback is sequential** (linear single-head Alembic chain — pin test):
  reverting M-A after M-B/M-C requires downgrading in reverse order.
  "Rollbackable" reads as "reversible from the current head," never "skippable."
- Killswitch: `BRAIN_SESSION_CAPTURE_SUBPARTITIONS` (the checkpoint, an explicit
  command, has no runtime flag — R3).

---

## 5. Phase 3 — The memory of focus (migration M-D)

### Content

1. **Preliminary census — the focus writers (premise corrected TWICE).**
   A first version claimed "only two writing sites"; the second corrected it to
   "six sites, of which only one bumps `focus_revision`, the upsert overwriting
   with no bump and no CAS." **The second statement is false too**, and migration
   032 is what says so.
   - **What already exists, measured on 2026-08-18 in production (head `045`,
     read-only)**: `alembic/versions/032_brain_sessions.py:19-34` creates
     `increment_project_focus_revision()` — "`IF NEW.current_focus IS DISTINCT FROM
     OLD.current_focus THEN NEW.focus_revision := OLD.focus_revision + 1`" — and the
     trigger `project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus
     ON project_contexts FOR EACH ROW`. `pg_get_triggerdef` shows it is still in
     place. **No writer can therefore change the focus text without a bump**: an
     `INSERT … ON CONFLICT DO UPDATE` fires the BEFORE UPDATE triggers, and the ON
     CONFLICT branch of `pg_project_context.get_or_create:281-290` does put
     `current_focus` in its SET. Cross-check:
     `grep -c focus_revision src/brain_v42/repositories/pg_project_context.py` =
     **0**, same for `scripts/scrub_xml_tool_call_leak.py`.
   - **The six sites, exactly** (docstring of `src/brain_v42/db/focus_stamp.py`):
     the `applied` CAS of `session_end` (`pg_brain_session.py:713-714`, which sets
     `focus_revision=expected_revision + 1` **explicitly** — the 037 CHECK requires
     it), `brain_update_project_focus` (`roadmap_service.py`, `focus_revision + 1`
     explicitly), `pg_project_context.update`, `update_focus`, `create`, and the
     live `brain_set_project_context` MCP tool's upsert. Plus one non-MCP writer:
     `scripts/scrub_xml_tool_call_leak.py` (`_PROJECT_CONTEXT_COLS =
     ("current_focus",)`) — **six PLUS the scrub, i.e. seven**.
   - **The exact statement — corrected a THIRD time on 2026-08-19.** The second fix
     wrote "two sites bump explicitly; the other five receive the trigger's bump
     the moment the text changes; and `brain_update_project_focus` is the **only**
     one that bumps even when the text does not change." Two errors:
     - **BOTH explicit sites bump on unchanged text.**
       `_apply_focus_if_current` (`pg_brain_session.py:713-714`) sets
       `focus_revision=expected_revision + 1` **without comparing the text**, and
       the 037 CHECK requires it (`applied` ⇒
       `focus_revision_at_end = end_expected_focus_revision + 1`). The code
       comment names the case: "Re-posting the previous prose verbatim is the
       copy-forward this column exists to expose." That is the **normal** regime
       of a session close, not an edge case of `roadmap_service`;
     - **the trigger only sees UPDATE.** `create` and the INSERT branch of
       `get_or_create` (`pg_project_context.py:51-71`, `:273-275`) write
       `current_focus` at row birth: no trigger, `focus_revision = 0` by column
       default (measured).
     Correct statement: **two sites set the revision themselves, whether the text
     changed or not; four receive the trigger's bump on UPDATE when the text
     changes; the two INSERT paths have neither a trigger nor a revision to
     increment.** Direct consequence for item 3 (the shared path) and for the
     database guard in item 2.
   - **What the upsert actually does**: it rewrites `current_focus` — **including
     to NULL when the argument is omitted**, verified — **with no CAS**. This is a
     real overwrite channel, and it is the core of B6. But it **does bump** (032
     trigger) and it **does date** (`focus_updated_at =
     focus_stamp(excluded.current_focus)`, under `IS DISTINCT FROM`, so a focus
     that moves to NULL counts). It is not silent: it is **unrecoverable**, for
     lack of history. That, and only that, is what M-D fixes.
   - **The cited figure was misread.** "10 of the 59 `project_contexts` have
     `current_focus IS NULL` — the overwrite channel is already biting": the
     number is exact (re-measured: `10/59`), the conclusion is not. The **ten**
     rows are at `focus_revision = 0` **and** `focus_updated_at IS NULL` (`perso`,
     `red-backup`, `red-cli`, `red-shrik:agent`, `red-daemon`, `red-llm`,
     `red-tsdb`, `red-lab:developer{,-gemini,-opus}`): focus **never written**, not
     erased — and an overwrite since 040 would have dated the column. **Zero
     production context bears the signature of an erasure** (NULL with a revision
     > 0). The channel exists in the code; production shows no evidence it has
     bitten. This plan therefore has no figure to add to Q13's file, and says so.
2. **Migration M-D — `project_focus_history`** (closes B6 for recoverability):
   - `project_key VARCHAR(50) NOT NULL` (no FK, like the knowledge tables — dropping
     a context must not take the audit trail down with it), `focus_revision BIGINT
     NOT NULL`, **`focus TEXT NULL`** — an erased focus (NULL) is precisely the
     destructive overwrite the audit trail must record; and a `NOT NULL` would abort
     `alembic upgrade` in production at seed time (`NotNullViolation` on the 10
     measured NULL focuses), a defect invisible in CI whose database is empty at
     seed time —, `actor VARCHAR(64) NULL` (R4 — corrected from the proposals'
     VARCHAR(128)), `source VARCHAR(20) NOT NULL` CHECK ∈ {session_end, focus_tool,
     context_upsert, generic_update, maintenance_scrub, migration_seed} — the enum
     covers the real writers —, `created_at`.
   - PK `(project_key, focus_revision)` — the generalized CAS's monotonicity makes
     it natural; `ON CONFLICT DO NOTHING` inserts make replays idempotent.
   - **Append-only enforced by trigger** (UPDATE/DELETE refused — panel graft, the
     audit trail becomes a constraint, not a convention).
   - **An earlier version's "guard trigger" is REMOVED.** It was meant to "refuse
     an UPDATE where `current_focus IS DISTINCT FROM` the old value without
     `focus_revision = old + 1`": that is **verbatim** what 032 does, by setting
     the value instead of refusing it. Worse, it would coexist poorly — PostgreSQL
     fires BEFORE ROW triggers in the **alphabetical order** of their name, which
     the plan did not fix. Named before
     `project_contexts_focus_revision_trigger` (e.g. `…_focus_history_guard`), it
     would see `NEW.focus_revision` not yet incremented and **reject every focus
     write from the four writers that write via UPDATE without setting the
     revision themselves** (`update`, `update_focus`, the upsert, the scrub —
     `create` writes via INSERT and escapes both the guard and the trigger) —
     `brain_set_project_context` fail-closed in production on every focus change.
     Named after, it is trivially satisfied: dead code.
   - **What remains, and is the real deliverable in the database**: a **deferred
     constraint trigger** (`AFTER UPDATE OF current_focus ON project_contexts …
     DEFERRABLE INITIALLY DEFERRED`) that, at end of transaction, requires the
     history row for `NEW.focus_revision`. It has no twin, depends on no
     alphabetical ordering, and catches any writer that bypasses the shared path.
     **The `OF current_focus` clause is mandatory**: without it, it would fire on
     every UPDATE of `project_contexts`, including the plan-index repair's
     (`plan_index_repair_store.py:294-308` and `:560-584`, which only write
     `plan_scan_paths`/`updated_at`) and would make them fail — exactly the review
     the pin's message requires (R1.4).
   - **What this trigger CANNOT see — gap named on 2026-08-19:** INSERTs.
     `pg_project_context.create` and the INSERT branch of `get_or_create` persist a
     `current_focus` at row birth (`focus_revision = 0`, column default). For every
     `project_context` created **after** M-D, revision 0 is thus a written focus
     that no database guard forces to be historicized — the seed only covers the
     59 contexts existing at upgrade time. **Three routes, to be settled when M-D
     is written, not discovered in production**: (a) `AFTER INSERT OR UPDATE OF
     current_focus` — the only one that holds N1 in the database, at the cost of a
     history row on every project creation, NULL focus included; (b) stay on
     UPDATE and let revision 0 be carried solely by the shared application path,
     writing down that the hard guard only starts at revision 1; (c) forbid
     `create`/`get_or_create` from writing a non-NULL focus at birth. **Not
     choosing is choosing (b) without saying so**, and shipping a false N1 for
     every new project.
   - **Created DISABLED, activated after the MCP restart** (R1.3): between the
     `upgrade` and the restart, the live process still runs the pre-M-D code,
     which writes no history row; the trigger would abort at COMMIT **every
     `brain_session_end` with `focus_outcome=applied`**, fail-closed, session left
     open, with no practicable killswitch or downgrade.
   - **Seed at upgrade**: one row per `project_context` with the current focus —
     **NULL included** — and its revision, `source='migration_seed'`: the anchor
     covers all 59 contexts, the enum is no longer orphaned.
   - **Where to test the seed — an earlier version's promise did not hold up.** It
     announced "tested against a NON-empty database containing NULL focuses
     (dedicated integration test)" citing `c60d023d` §"WHERE TO TEST." That section
     says the opposite of what it was made to say: it concludes that proving such
     an upgrade "requires `tests/integration` **AND A SECOND DATABASE** —
     `tests/integration/conftest.py` runs `alembic upgrade head` ONCE per session
     with the default env" (verified: `_run_alembic_upgrade` is a subprocess called
     by a `scope="session", autouse=True` fixture). The plan therefore promised a
     test without supplying either the second database or the fixture, and closed
     off the only in-session route (downgrade then re-upgrade) by making that
     downgrade fail-closed. **Two routes, to be chosen explicitly when M-D is
     written**: (a) refactor the seed into a **pure planner** (a function that
     returns the rows to insert from a set of contexts), unit-testable including
     on NULL focuses — the route the ticket recommends; or (b) ship the
     second-integration-database fixture, which does not exist and is a project of
     its own.
   - Downgrade: **fail-closed if rows other than the seed exist** — but **with a
     named opt-in**, not a destructive purge. An earlier version wrote
     "unconditionally fail-closed … an Alembic downgrade has no confirmation
     parameter." **That is false, and this repo implements the opposite** in the
     migration this plan cites three lines above as a template:
     `alembic/versions/039_project_context_timestamp_cas.py:17,337-339` —
     `_DOWNGRADE_OPT_IN = "allow_project_context_trigger_downgrade"`,
     `context.get_x_argument(as_dictionary=True)`, `raise RuntimeError(
     "project_context_trigger_downgrade_opt_in_required")` if the argument is not
     `"yes"`. That is, literally,
     `alembic -x allow_focus_history_downgrade=yes downgrade …`. The first pass's
     correction had replaced a vague promise with a false impossibility, and the
     cost was real: it concluded the audit trail must be **destroyed** to allow
     rolling back. M-D reuses 039's mechanism: the audit trail stays, the gesture
     is named.
3. **Transactional write through ONE shared path, at the SIX sites** (same
   consolidation doctrine as the colon predicate in Phase 2): a single function
   inserts the history row within the same transaction as EACH persisted write of
   `current_focus` — the six sites plus the scrub go through it. **It adds no
   bump of its own** and reads the revision **after** the write
   (`RETURNING focus_revision`) to historicize on that value. **Reason corrected
   on 2026-08-19**: the earlier version justified this by "032 already does it,
   and a second application-side increment would set `OLD+2`." `OLD+2` does not
   exist — the trigger **assigns**
   (`NEW.focus_revision := OLD.focus_revision + 1`), it does not add: the
   statement's value is overwritten, not accumulated. **The proof is the plpgsql
   source, and it alone** (`alembic/versions/032_brain_sessions.py:19-34`: an
   assignment, not an accumulation). *Corroboration withdrawn on 2026-08-19*:
   "production advances by one notch (CAS 209→210), not two" was misattributed —
   this CAS is a `brain_session_end` (DOSSIER §B6, two parallel sessions on the
   rev-209 snapshot), not a `roadmap_service` write, and nothing says the focus
   TEXT changed, the only condition that makes the trigger speak. **So the two
   explicit bumps must STAY**: removing them would break `end` (trigger silent on
   unchanged text, 037 CHECK still requiring `expected + 1`) and the CAS token of
   a blockers-only batch. `RETURNING` is the right rule because it holds true
   under both regimes — not because a double increment would threaten anything.
   An insert failure fails the entire focus write — fail-closed by design and
   tested ("an audit that can stay silent proves nothing"). A `conflict` CAS
   writes no row. **Key reservation to be briefed — resized on 2026-08-19**: the
   earlier version attributed it only to `brain_update_project_focus` (CAS token
   of a blockers-only batch) and treated it as an edge case.
   **`brain_session_end` produces the same effect, and it is its normal regime**:
   the CAS sets `expected + 1` without comparing the text, so any session that
   closes by re-posting the previous prose adds an identical focus-history row.
   The PK `(project_key, focus_revision)` stays unique — this is not a collision —,
   but the volume of content duplicates is that of session closes, not that of an
   occasional blockers-only batch. To be made readable in the reading tool
   (marking "focus unchanged" rather than filtering), and to be accounted for
   when sizing `brain_focus_history`.
   **Induced behavior change, submitted (Q13, a free veto before M-D)**:
   `brain_set_project_context` with `current_focus` **omitted stops erasing** the
   focus (omitted ≠ explicit erasure); an explicit erasure remains possible,
   versioned and audited. **To be settled on reasoning, not on a figure** —
   production shows no victim of it (item 1).
4. **Read-only tool `brain_focus_history(project_key, limit≤50, offset)`**:
   revision, focus, actor, source, created_at.
5. **`focus_diff` in the `end` result** (graft C): characters added/removed versus
   the CAS's base focus — visibility before any guard. The hard shrink guard
   (~60% threshold proposed by C) is **not** shipped: an arbitrary threshold
   disqualified by two judges; it remains open question #7.
6. **Runbook for recovering an overwritten focus** (a step-by-step procedure) + a
   drill run once.

### Exit criteria (measurable)
- **Every persisted mutation of `current_focus` leaves its history row** —
  verified by the **deferred constraint trigger** (test: a direct UPDATE of
  `current_focus` with no history row **aborts at COMMIT**, not at the
  statement) and by a canary on EACH writer, including
  `brain_set_project_context` (the upsert historicizes, including an explicit
  erasure). **The `focus_revision` bump is NOT an exit criterion of this phase**:
  it has held since 032 and is verified in production; anchoring it here would
  amount to testing Postgres.
  The `focus_revision` ↔ history join is not the criterion either: it would be
  100% green on a database that never received a write after M-D. The honest
  criterion is the deferred trigger, plus the per-writer canary.
  **The per-writer canary includes both INSERT paths** (`create`, `get_or_create`'s
  INSERT branch): they escape the deferred trigger (§5.2), so they are the only
  place where the criterion measures the application path **alone**. A new
  project created with a non-NULL focus must produce its revision-0 row — or, if
  route (b) is chosen, the criterion explicitly excludes it instead of forgetting
  it.
- A CAS conflict writes no row (test); replaying a persisted `end` writes no
  second row (test); UPDATE/DELETE refused by the trigger (test); **the seed is
  exercised on contexts with a NULL focus** — via the pure planner in a unit
  test, or via the second-database fixture if it ships (§5.2, "where to test").
- `end` returns a correct `focus_diff` on canary.
- Recovery drill run and documented.
- **The per-writer canaries consume M-D's rollback window** (each writes a row
  outside the seed): throwaway sessions and projects, documented purge, blank
  downgrade on staging before declaring the phase exited — **or** use of the
  named opt-in `-x allow_focus_history_downgrade=yes`, which makes the purge
  unnecessary.
- Pin: M-D applied, measured, pin bumped in the same commit, **plan-index repair
  review written** (R1.4) and **`ops/recovery/` regenerated — BOTH v4 assets**
  (R1.5: M-D adds a trigger on `project_contexts`, a table on the closed
  `expected_runtime_user_triggers` list; `brain-v42-v4-pgrestore.sql` carries the
  same list and CTE parity is tested,
  `test_recovery_contract_v4_pgrestore.py:29-33`).

### Rollback
M-D downgrade fail-closed outside the seed, **with a named opt-in** (039 template,
`-x allow_focus_history_downgrade=yes`); the shared write path and its six sites
ship in the same commit, atomic revert. Killswitch: not required for the audit
write, purely additive within existing transactions (R3 documents the exception) —
**but the constraint trigger itself ships DISABLED and is activated by an
operator gesture after the MCP restart** (§5.2), which serves as its switch
during the cutover window. **A switch that costs something, not a free one**
(2026-08-19): as long as it is off, the `ops/recovery/` attestation is red
(`tgenabled = 'O'` required, R1.5). Reusing it in an emergency is legitimate;
doing so without dating it is not.

---

## 6. Phase 4 — Arming and gated batch (operator gestures, in this order)

> **ORDER MODIFIED BY THE 2026-08-19 FRAMING — read this before the rest of this
> section.** The priority axis chosen by the operator is **knowledge
> traceability** (B3, B4, B5 first). Yet **4.4 closed pain #1 second-to-last**,
> behind Q6 *plus* a 14-day soak. New global order: **P0 → P1 → P2 → 4.4 → P3**,
> then 4.1, 4.2-3, 4.5 and 4.6 in the order below.
>
> Two dependencies survive the resequencing and cannot be worked around:
> **(i)** 4.4 depends hard on Phase 1 — the staged writer takes the per-tool
> project resolver and `started_by_actor` from there (§3.6); moving 4.4 ahead of
> P1 would deprive it of its `project_key`. **(ii)** 4.1 arms D5, which becomes
> **load-bearing** for the `agent` nature (Q12 = (a), ADR §0.2): under this
> answer, 4.1 is no longer a comfort-level arming and its rank deserves
> re-examination — **this plan has not done so**, and section 4.1 below is still
> written as if D5 were optional.
>
> **The body of the subsections has NOT been renumbered**: the titles "4.1"
> through "4.6" keep their original names so cross-references across the three
> documents remain valid. Only the execution order changes.

Nothing here ships armed by this plan. The phase is a **sequence of operator
decisions**, each with a before/after measurement. One arming pre-exists, outside
this plan: the DRY sweep, armed by the operator on 2026-08-18 (measured — see §0
and 4.2).

**Correction — "reversible via a file (drop-in/env)" was false for half of this
phase**, and an operator reading that promise before arming 4.4 would discover a
production rendezvous. Three gestures are **not** files:
- **4.2** changes the sweep's SQL predicate (`GREATEST(...)`) — this is a code
  deployment, and it appears in no phase-1-through-3 delivered content; it is
  therefore **explicitly attached to 4.2** and follows R2 (Red/Green) plus an MCP
  restart;
- **4.4** ships **M-E**, an Alembic head, downgrade fail-closed on unresolved
  `staged` rows;
- **4.5** ships **M-F**, an Alembic head, a non-empty guard.
All three sit in the pin's lane (§8) and fully follow R1. What remains reversible
in one file: 4.1, the WET arming of 4.3, the flags of 4.4 and 4.6 **once their
head has landed**.

### 4.1 — Arm observation (`BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=true`) — blocked by Q1
- Arming via a documented systemd drop-in, per the runbook. Soak ≥ 14 d.
- **Exit measurements, published — the order has been corrected: the criterion
  announced first could not fail.** An earlier version put first "**zero
  potential false-dead among OBSERVED sessions** — no session with a recent
  `last_observed_at` would be a sweep candidate," and relegated the only
  informative measurement to second place. But with 4.2's predicate
  (`GREATEST(last_heartbeat_at, COALESCE(last_observed_at, last_heartbeat_at)) <
  cutoff`), a recent `last_observed_at` ⇒ `GREATEST(...) > cutoff` ⇒
  non-candidate, **by construction**, regardless of the 14-day soak. This is what
  ADR §4 calls a "predicate impossibility": a design property, not a measurement
  result — and it is the same class of defect §5 had already named for B6 ("a
  trivially satisfiable criterion"), on the very criterion that unlocks the WET
  arming. Corrected order:
  1. **count of open sessions under ambiguity AND candidate for the sweep** — the
     residual false-dead, the one observation does NOT cover: under ambiguity
     (≥2 open sessions for the same carrier, B1 ghost regime, rule R5), NO session
     of the pair is observed, including the one actively being worked on; same for
     stdio with no header (`skipped{no_actor}`, B8). **This is the number that can
     fail, so this is the criterion**; and it does not start from a blank page —
     its **ceiling** is already measured, by project (`7ffe0e8a` on 2026-08-16;
     re-measured on 2026-08-19: 24 of the 29 `open` sessions in a project that
     carries at least two, cf. §2 baseline), which gives the order of magnitude to
     compare once the per-actor breakdown is available;
  2. `skipped{ambiguous}` and `skipped{no_project}` ratios (if high: back to the
     operator — corollary question #1, and per-tool resolver coverage — rather
     than widening silently);
  3. `observed_activity_written/skipped` ratio, compared against the population
     computed without writing in Phase 0: **this is the verification that the
     emitter observes what the baseline had predicted**, the only thing the soak
     really teaches;
  4. count of `observed_only` sessions (recent observation, neither heartbeat nor
     checkpoint for 7 d) — the measured bound of the "session forgotten under an
     active actor" residue the panel raised, and `list` exposes them for human
     triage;
  5. the "zero observed session candidate for the sweep" property is **verified
     as a test invariant**, not published as a soak result.

### 4.2 — Sweep dry: enriched predicate (DRY is ALREADY armed) — blocked by Q5
- **State measured on 2026-08-18**: the dry has been armed since 2026-08-18 by
  operator decision (drop-in `killswitches.conf`, on the current heartbeat-only
  predicate — `pg_brain_session.py`, verified). This step therefore does not arm
  the dry: it **enriches the predicate** the already-armed dry evaluates. An
  earlier version presented it as a future switch "blocked by Q5" — a
  copy-forward of a 2026-08-16 state, corrected; what remains blocked by Q5 is
  the predicate enrichment and the follow-on (4.3).
- Enriched predicate, **still ONE statement** (invariant C12, anchor test
  carried forward):

  ```sql
  ... WHERE status = 'open'
        AND GREATEST(last_heartbeat_at,
                     COALESCE(last_observed_at, last_heartbeat_at)) < :cutoff
  ```

  Since the checkpoint (Phase 2) refreshes `last_heartbeat_at`, this predicate
  encodes **the three signals — heartbeat, checkpoint, observation — into two
  columns and one single statement**, re-evaluated under a row lock. **But the
  rule "the three signals, not just one" is NOT an intrinsic property of the
  predicate**: it depends on two independently refusable armings. If Q1=no or
  observation is not armed (4.1), and/or Q3=no (no checkpoint), `GREATEST(...)`
  degenerates into heartbeat alone — the mode ADR §3.3 rules out because
  `2bd14b24` condemned that signal. The dependency is declared here and locked
  down in 4.3. Safe monotone change: it can only abandon *fewer* sessions.
- ≥ 7 d of candidate lists reviewed by the operator.

### 4.3 — Sweep wet (`DRY_RUN=false`) — HARD three-signal precondition
- **Arming precondition, fail-closed**: WET can only be armed if the three
  signals genuinely exist — **Q1=yes AND 4.1 armed + soaked, AND Q3=yes AND the
  checkpoint shipped**. Failing that, the predicate degenerates into heartbeat
  alone (the configuration `2bd14b24` condemns): arming anyway becomes a **new
  operator decision, taken by explicitly naming the degraded heartbeat-only
  mode** — never a default follow-through from 4.2. Q5 alone is not enough; an
  earlier version left this degraded path armable without declaring it.
- Criteria: the >7-day ghost stock (**21 sweepable, re-measured on 2026-08-19**,
  out of 29 `open` sessions among 467 rows — versus 18 on 2026-08-18; dated,
  perishable, to be re-measured before every arming decision) trends to zero
  with no manual cleanup; **zero abandonment of a session with recent observed
  activity or a recent checkpoint** — and the residue named in 4.1 (sessions
  under ambiguity or with no actor, never observed) is published with every
  abandonment list; the monthly manual-abandonment count trends to zero (closing
  measurement for the B1 regime).

### 4.4 — Prepared capture (migration M-E + `BRAIN_SESSION_STAGED_CAPTURE_ENABLED`) — blocked by Q6 (covenant amendment, explicitly submitted)

> **MOVED UP RIGHT AFTER PHASE 2 by the 2026-08-19 framing.** This is the tranche
> that closes **B3**, promoted to pain #1 by the "knowledge traceability" axis.
> Two consequences: **Q6 becomes a Phase 0 exit criterion** (it no longer gates
> an end-of-plan tranche), and its **14-day soak starts much earlier** — this is
> the main gain from the resequencing, since this soak was the critical path for
> closing B3. Unchanged hard prerequisite: **Phase 1** (per-tool project
> resolver and `started_by_actor`).
- **Migration M-E**: table `brain_session_staged_captures` — PK
  `(session_id, knowledge_id)` (a draft is a hypothesis: **no** PK on
  `knowledge_id` alone; only promotion into `brain_session_artifacts`, PK
  `knowledge_id`, confers exclusivity), `knowledge_type`, `observed_at`,
  `source='provenance'`, `status` CHECK ∈ {staged, promoted, dismissed}.
  Downgrade fail-closed if unresolved `staged` rows exist.
> **AMENDED on 2026-08-20 — §0bis.2 and §0bis.5 are authoritative.** The bullet
> below describes the writer's linkage via
> **`(project_key, started_by_actor)` with the "exactly one" rule (R5)**. This is
> **STALE**: the key is `(project, CONNECTION)`, and on the connection **the
> linkage is EXACT** — there is no more ambiguity rule to apply. §0bis.5 says so
> itself: *"Q6 loses its main implementation risk before it is even armed,"* the
> population of 24 ambiguous `open` sessions out of 29 ceasing to concern it.
>
> **WHAT STAYS TRUE in the bullet, and what matters**: "never `access_log`" (this
> table is a purged buffer, measured at 0 rows); consuming the **per-tool project
> resolver** shipped in Phase 1 — without it the writer has no `project_key`, the
> middleware only reading headers; the **500/session cap** and its
> `staged_capture_skipped{overflow}` process-metric counter; and the `1c40c36a`
> envelope (an observer failure cannot break the call it observes).
>
> See `SPEC-pool-brouillons.md`, which proposes the second cap that 500/session
> does not give: under automatic opening, nothing bounds the NUMBER of sessions.

- Middleware writer behind the flag, **`(project_key, started_by_actor)` linkage
  with the same "exactly one" rule (R5) — never `access_log`** (panel fix on
  graft C; and this table is a purged buffer, cf. §3.5). **It consumes the
  per-tool project resolver shipped in Phase 1** — without it it has no
  `project_key`, the middleware only reading headers (§3.6). 500/session cap:
  beyond that we stop observing and the `staged_capture_skipped{overflow}`
  metric counter counts it (counter location specified: process metrics, like
  the observation counters — answer to the panel's question "where does the
  counter live?"). `1c40c36a` envelope + injected-failure test, as in Phase 1.
- **Promotion only via an explicit command**: `capture` or
  `end(capture_staged=true)`, which goes back through `_validate_captures`
  **before** the ledger/reason XOR is evaluated. A rejection is logged as
  `dismissed`, not deleted. The server prepares, the operator signs.
- Exit (14-day soak armed on brain-v42 alone): attribution rate ≥ 2× the Phase 0
  baseline on the pilot project; zero tool failure caused by the writer;
  staged/promoted/dismissed ratios published.
- **Documented horizon, never shipped here** (graft A, endorsed by two judges):
  if signed promotion plateaus, creation-time attribution (same transaction as
  the artifact, `method='auto_provenance'`) is B3's full closure — a full
  covenant change (E3), to be settled by the operator on the quantified dossier
  4.1–4.4 produces (uncaptured candidates, `dismissed`, ambiguities).

### 4.5 — Explicit reattribution (migration M-F) — blocked by Q8
- Table `brain_session_attribution_moves`: `knowledge_id`, `from_session_id`,
  `to_session_id`, `reason TEXT NOT NULL`, `moved_by VARCHAR(64) NOT NULL`,
  `moved_at` — **with no trap FK** (the panel rejected proposal A's combination
  of an owner CHECK × `ON DELETE SET NULL`). Exclusivity becomes "a single
  current owner," the history fully logged; unlocks artifacts locked by swept
  ghosts. Operator gesture (maintenance script or tool — modality included in
  Q8), never silent.

### 4.6 — Family reads (`include_descendants`) — blocked by Q4
- Opt-in parameter on scoped briefing/search, via **the** shared predicate from
  Phase 2 (never a second copy), behind its own flag
  `BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED=false` — added to §8bis, which an
  earlier version omitted even though the recap claims to be exhaustive (R3).
  Closing brick for B11 without inflating the dream pool.
- **Interaction with the dream capability scope, specified — the scope has been
  ARMED in production since 2026-08-10**: under a `(project, phase)` bearer,
  `brain_service` forces `project_key = scope.project_key` under **strict
  equality** (`services/brain_service.py`, verified; property measured at
  cutover: 751/2760 learnings under `red` scope). Honoring
  `include_descendants` under scope would let a bearer scoped to `pk` read
  `pk:*` — widening an armed security perimeter. Rule: **under dream scope, the
  parameter is REFUSED fail-closed** (an explicit error — never silently
  ignored, which would hide the semantics). Widening a bearer to
  sub-partitions would be a separate operator security decision, outside this
  plan, with its own registry matrix.
- **Quantified so no one discovers it one night — and in the present tense, not
  the conditional.** An earlier version wrote "**if** E5 led instead to adding
  colon keys to the dream pool…": that is **already done, in part, since
  2026-08-10**. Drop-in `killswitches.conf` read on 2026-08-18:
  `BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…` — two of
  the six colon keys are full pool members, i.e. 447 of the 533 re-measured
  colon artifacts (84%). Spec `dbb7c5ce`'s "add the six keys" remedy is
  therefore **2/6 done**, and the pool is **at the ceiling of ten**.
  Consequence for the four remaining keys (86 artifacts): entering them requires
  raising `_MAX_POOL`
  (`tests/unit/test_dream_systemd_timeout_covers_the_pool.py:41`) **and**
  `TimeoutStartSec` together — the test fails if only one is changed — plus the
  full `MCP_HTTP_DREAM_TOKENS` registry matrix (six phases × project; fail-closed
  preflight: without it, **the whole night** fails). This is not a hypothesis
  about the future, it is the cost of the next notch.
- The `parent_key` column + CHECK (proposal A's design) remains the documented
  option for E5: **predicate first, column only after proof of usage** (panel
  doctrine).

### Phase-wide rollback
The **flag** gestures (4.1, 4.3's WET, the flags of 4.4 and 4.6) are drop-in/env
gestures reversible in a single file; the reverse order disarms them cleanly.
The **other three** are not, and follow R1: 4.2's enriched predicate is a code
deployment (commit revert + MCP restart); M-E and M-F are Alembic heads with
fail-closed downgrades, hence sequential rollback from the current head, never
skippable. They are written only if their questions are settled in their favor.

---

## 7. Pain → phase → closure measurement map

| Pain | Closes in | How we MEASURE it |
|---|---|---|
| **B1** subagent ghosts (critical) | Triage: P1 (`intent`, `open_sessions_same_carrier`, `started_by_actor`). Regime: P4.2-3 (armed sweep). **Residue conditional on Q9** (subagents) **and Q12** (automatic lifecycle, answer established by `2bd14b24`) — assumed | Stock of sessions >7 d → 0 with no manual cleanup; manual abandonments/month → 0; share of new sessions with `intent` and `started_by_actor` populated |
| **B2** lying heartbeat (critical) | False-dead: P4.1-2 (observation + GREATEST predicate, safe monotone) — **for observed sessions only**: under R5 ambiguity or stdio with no header, no observation — residue named in 4.1, defended by checkpoint/heartbeat alone. False-alive: P2 (checkpoint dates the living **if it is called** — calling policy in Q3, heartbeat effect behind its flag) + P4.3 (three signals, hard precondition) | **"Ambiguous AND sweep-candidate" count** (the only one that can fail); `skipped{ambiguous, no_actor, no_project}`; comparison against the population predicted in P0; `observed_only` published; zero erroneous abandonment in wet. "Zero observed session candidate for the sweep" is a **tested invariant**, not a soak measurement (§6, 4.1) |
| **B3** capture 18%/34% (high) | P1 (suggestions, instrument) → P4.4 (signed staged, ≥ 2× baseline) → full closure = E3 decision (documented horizon) | Capture and attribution rate vs. P0 baseline, per phase; staged/promoted/dismissed ratios |
| **B4** tickets + all-or-nothing batch | P1 (enumerated error) + P2 (M-B, `from_project` predicate) | Canary: `[learning, ticket]` batch passes; wrong `from_project` rejected as `wrong_project` |
| **B5** rigid window | **Partially**: P2 only relaxes the PROJECT axis (parent→child family flag, shared predicate — Q4 arming). **Assumed residue, not addressed by this plan**: the TEMPORAL axis (`created_at >= started_at`) remains intact — an artifact created before `start` stays uncapturable (lived experience `d30cf6e5`); relaxing it would touch the ledger's provenance semantics and is briefed by no phase or question — to be submitted if the pain persists | Canary flag armed: `pk` → `pk:child` capture passes; flag closed: anchor unchanged; temporal residue visible in the `created_before_session` error (P1) |
| **B6** focus with no guard (high) | P3 (append-only audit + deferred constraint trigger + `focus_diff` + runbook). Hard prevention = Q7 | **Deferred constraint trigger green** (an UPDATE of `current_focus` with no history row aborts at COMMIT) + canary on EVERY writer, **including the two INSERT paths the trigger cannot see** (`create`, `get_or_create`'s INSERT branch — §5.2); recovery drill succeeded; `focus_diff` returned on canary. **NOT the focus↔history join**: it would be 100% green on a database with no post-M-D write — a trivially satisfiable criterion, which §5 disqualifies and which an earlier version of this line reinstalled six sections later |
| **B7** no checkpoint | P2 (M-C + tool, gated by Q3, `d04dc588` freshness doctrine reused — **with TWO declared divergences, storage AND payload shape**, cf. §4.4 and Q3(c)(d); the separate checkpoint spec the ticket's audit requires ships in P0) | Real start→checkpoints→end cycle; `list` shows `last_checkpoint_at`; displayed freshness derived from checkpoint age |
| **B8** dead X-Brain-Session (constraint) — **scored on a stale measurement**: spike measured on Claude Code 2.1.220, `claude --version` = 2.1.234 on 2026-08-19 | **P0 first: spike replay** (§2, content #6) — this is the constraint that sizes D1, D5, D8, R5, N2, D6's residue and 4.1's exit criterion #1. Then assumed from P1 on. **AMENDED 2026-08-20**: identity is NO LONGER `(project, started_by_actor)` but `(project, CONNECTION)` (ADR §0bis.2), and stdio no longer degrades "without attributing" — it opens **NO** automatic session at all (ADR §0ter.2), the explicit cycle there staying unchanged | Spike replayed, result dated with its version up front; **share of calls carrying a normalizable `X-Brain-Session`** (a measurement absent from the baseline until now); share of new sessions with a non-NULL `started_by_actor` vs. baseline; `skipped{no_actor}` counter |
| **B9** free-form `client_key` | P1 (`intent` + documented convention + `client_key_prefix`) | Share of new sessions with `intent`; ghost triage demonstrated in `list` |
| **B10** ghost drift (closed) | P0 (anchor tests — never to regress) | Canonicalization anchor green in CI, continuously |
| **B11** sub-projects: **86 artifacts with no run, 533 with no cross-consolidation** (average — revised downward, cf. §0) | **Already 2/6 closed outside this plan**: `red-shrik:agent` and `red-lab:architect` have been in the dream pool since 2026-08-10 (447/533 artifacts, 84%). Remaining: P2 (shared predicate + family capture), P4.6 (`include_descendants`, refused under dream scope) for **cross** consolidation; the 4 keys outside the pool require `_MAX_POOL` + `TimeoutStartSec` + registry matrix. Underlying decision = **Q4** (an earlier reference to Q5 was wrong — Q5 is the sweep) | Colon mass **re-measured** in P0 (not copied from `dbb7c5ce`); share of this mass covered by a nightly run; after any arming: the parent's briefing counts the children's artifacts |
| **B12** no projects doc | P0 (`docs/PROJECTS_SYSTEM.md`, fact/judgment grid) | Doc reviewed/delivered; the four bricks are linked in it and the colon predicate is catalogued there **in full — five `src/` copies + seven views from 036, in three formulations** ("the sole exception" from an earlier version of this line is the formulation §2 declares false; do not reinstall it as a closure measurement) |
| **B13** undifferentiated error | P1 (per-id rejections + `capturable_subset`) | Canary: each rejected id carries its reason; anchor test for the new shape |
| **B14** actor not persisted | P1 (M-A, `started_by_actor VARCHAR(64)`) | Column populated on new HTTP sessions, share measured |

---

## 8. Migration recap (pin's lane — R1)

| Head | Phase | Content | Downgrade | `ops/recovery/` attestation | Conditional? |
|---|---|---|---|---|---|
| (046) | — | Embedding dimension, **planned, not written** (`alembic/versions/` stops at 045) — outside this plan; free ordering, never in flight at the same time (R1.2/R1.6) | — | its own responsibility | — |
| M-A | 1 | **4** nullable columns on `brain_sessions` (`started_by_actor` 64, `last_observed_at`, `intent` 500, **`nature`** — 2026-08-19 framing) **+ the CONNECTION column and its UNIQUE index**. ✅ **Index decision MEASURED AND CONCLUDED on 2026-08-19** (`baseline/README.md`), and it changed subject: **no index on `started_by_actor`** (it leaves the hot path under the `(project, connection)` key), but a **UNIQUE index on the connection**, mandatory — an uncovered equality forces a full Seq Scan on every outermost call | Fail-closed if a non-NULL `intent` exists (judgment) — canary purge **or** named `-x` opt-in | **`brain_sessions` column fingerprint to regenerate** (+ unit `COLUMN_DEFINITION_MD5`); **if an index is added, `expected_session_indexes` (CLOSED list, `v4.sql:404-412`, checked at `:665` and `:687`) breaks TOO** — plus `SESSION_INDEX_DEFINITION_MD5` (`test_recovery_contract_v3.py:164-168,488`) | No |
| M-B | 2 | Artifacts CHECK + `'ticket'` (8th value) | Fail-closed if `ticket` rows exist (canaries purged, §4) | **`expected_artifact_constraints` to regenerate**; and `knowledge_sources` does not know about tickets ⇒ **Q14 first** | Q2 (predicate), **Q14** |
| M-C | 2 | `brain_session_checkpoints` (append-only trigger, idempotent replay `(session_id, seq)`) | Fail-closed if rows exist (canaries purged, §4) | `table_set` (new table) | **Q3** |
| M-D | 3 | `project_focus_history` (`focus` NULLABLE, append-only trigger + **deferred constraint trigger** `AFTER UPDATE OF current_focus` on `project_contexts`, **created disabled**, seed including NULL, six writers + scrub consolidated; **no revision guard trigger — 032 already does it**; **INSERT scope to be settled at write time** — `create`/`get_or_create` escape `AFTER UPDATE`, §5.2) | Fail-closed outside the seed **with a named opt-in** (039 template, `-x allow_focus_history_downgrade=yes`) | **`expected_runtime_user_triggers` (13 triggers / 5 tables, closed) + `table_set` to regenerate — regeneration timed WITH the trigger activation, the disabled window being red (`tgenabled='O'`)** | No (Q13 modulates the upsert) |
| M-E | 4.4 | `brain_session_staged_captures` | Fail-closed if unresolved `staged` rows exist | `table_set` | **Q6** |
| M-F | 4.5 | `brain_session_attribution_moves` | Non-empty guard | `table_set` | **Q8** |
| **M-G** | **ONE HEAD WITH M-A** (signed 2026-08-20, ADR §0ter.1) — *rank in the lane still to be sequenced* | **New terminal branch of `brain_sessions_terminal_state_valid` for auto-closing `agent`-nature sessions** (2026-08-19 framing, Q15 = route (3); ADR §0.4 and D11). **The only head in this plan that touches the CORE** — all others work at the periphery. Exact branch unspecified; **dual Pydantic rail to move in lockstep with the CHECK** (C7) | Fail-closed if rows carry the new state; **the 037→036 downgrade must learn this state**, or else it silently loses terminal sessions | **`brain_sessions` terminal-CHECK fingerprint to regenerate, on BOTH v4 assets.** This is the most closely watched object in the attestation | **Q15 answered; the CONTENT still to be specified (Phase 0 exit criterion)** |

**Reading the "Attestation" column — four warnings** (two originally, two added by
the 2026-08-19 framing). (1) "Regenerate
`ops/recovery/`" means **both v4 assets**: `brain-v42-v4.sql` **and**
`brain-v42-v4-pgrestore.sql`. This second variant was named nowhere in the three
documents (`grep -c pgrestore` = 0/0/0 on 2026-08-19) even though it is alive and
tested — `tests/integration/db/test_recovery_contract_v4_execution.py:106` runs
**both** against a real database, and
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` enforces **CTE
parity** (allowed gap: `{observed_artifact_constraints,
observed_session_constraints}`, and no live-side CTE that doesn't exist on the
pgrestore side). Regenerating only one asset leaves the restoration proof half
false and turns parity red at the first CTE added.
(2) The table assumes **no head adds an index on `brain_sessions`**; if M-A's
index decision is taken, see R1.5 ("three of the six heads" becomes "three
heads, two structures" or "four heads out of seven"). ~~**(3) The head counts in
this table and in R1.5 predate M-G and have not been recalculated** — M-G is a
seventh head in the lane (eighth counting 046), and it touches the most closely
watched fingerprint. Recounting is part of M-G's specification, a Phase 0 exit
criterion.~~

> **AMENDED on 2026-08-20 — RECOUNTED, and note (3) is doubly stale.**
> Source: `SEQUENCEMENT-2026-08-20-couloir-du-pin.md`, decision `9d22bc6a` (S6).
>
> **(i) M-G is NOT a seventh head.** It consumes no rendezvous of its own: it
> ships **inside 046, with M-A** (ADR §0ter.1, signed). The lane counts
> **12 candidates in queue** and **4 outside the queue**, for **8 rendezvous**
> under the signed Order B: `046 = M-A+M-G` → `047 = M-B` → M-C and M-E
> **separate** (until S9 has demonstrated their downgrades' independence) →
> `049 = M-D` **isolated** → `050` = a trio of nullable `ADD COLUMN` → `051`/`052`.
> The recount this note demanded is **done**, and it did not live in M-G's spec
> but in a dossier of its own.
>
> **(ii) Note (2)'s premise fell too.** It assumes "no head adds an index on
> `brain_sessions`." **M-A's index decision is taken** (`c3d09355`): a **PARTIAL
> UNIQUE index** (`WHERE status = 'open'`) on the connection column. It therefore
> breaks `expected_session_indexes`, the CLOSED list in both v4 assets — the
> fourth attestation-breaking mechanism.
>
> **(iii) And the whole frame of these notes has changed.** Dossier §0.1 measures
> that **the attestation is ALREADY red in production** before any new head —
> `alembic_head` **039 ≠ 045** and `indexes` **128 ≠ 129**. So "which heads cost a
> regeneration of assets" is no longer the right question: **they all cost
> one.** This is what produced signature **S1** — a **single v5 DR contract at a
> DERIVED head**, one mint for the whole lane instead of 7-12. **(4) M-A gains a fourth column**, `nature` (ADR D1 and D11,
2026-08-19 framing): the M-A row above announces three and has not been
rewritten, for lack of a specification of its constraint and its database
default.

Every head, **in the same commit** (full R1.1): migration + bump
`_REQUIRED_ALEMBIC_HEAD` + pin test + README/MCP_TOOLS (`migration {head}`) +
ARCHITECTURE (`migrations 001–{head} defined`) + SCHEMA.md (tables, revisions,
"The repository target is {head}.") + `docs/OPERATIONS.md:118` +
`test_recovery_contract.py:279` + both frozen `table_set`s (`…py:292`,
`…_v2.py:33-39`) + regeneration of `ops/recovery/` — **`brain-v42-v4.sql` AND
`brain-v42-v4-pgrestore.sql`** — when the "Attestation" column indicates it +
renaming of the head-named guard test. CLAUDE.md is updated **outside the
commit** (gitignored). Never two lane heads in flight — 046 included, in
whatever order comes up; production measured before the next tranche; MCP
restart + canary after every application; **for M-D, activate the constraint
trigger AFTER the restart** (R1.3); canary purge — or downgrade opt-in —
replayed wherever the downgrade is fail-closed (§3, §4, §5).

## 8bis. Killswitch recap (R3)

| Flag | Born in | What it arms | Default |
|---|---|---|---|
| `BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED` | P1 | `last_observed_at` emitter (ONE statement, "exactly one") | `false` |
| `BRAIN_SESSION_CAPTURE_SUBPARTITIONS` | P2 | Parent→child capture via the shared predicate | `false` |
| ~~`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`~~ | ~~P2~~ | ❌ **REMOVED — no longer has a purpose since the 2026-08-19 framing (ADR §0bis.4).** This flag only existed to make fork Q3(a) armable, whose danger was: "an agent that checkpoints alone keeps its session alive indefinitely." Under Q12 = (a) + automatic opening, an agent session's liveness comes from `last_observed_at`, which moves on EVERY tool call — the checkpoint stops being special, and an `operator` session is never closed by inactivity. There is nothing left to arm | — |
| **`BRAIN_SESSION_IDLE_CLOSE_SECONDS`** | **(new, 2026-08-19 framing)** | **Observed-inactivity threshold that auto-closes an `agent`-nature session. 4 SIGNED hours as of 2026-08-20** — an **ELIGIBILITY** threshold for the nightly sweep, NOT a delay (worst-case latency ≈ 28 h; ADR §0ter.5). Its OWN setting — definitely not `MCP_HTTP_SESSION_IDLE_SECONDS` (900 s), which governs a network object and has no business driving a knowledge object. **NEVER touches an `operator` session** | to be specified |
| `BRAIN_SESSION_STAGED_CAPTURE_ENABLED` | P4.4 | Staged-draft writer | `false` |
| `BRAIN_DREAM_SWEEP_ENABLED` / `BRAIN_DREAM_SWEEP_DRY_RUN` | existing | 7-day sweep (enriched predicate P4.2) | `false` / `true` (code) — **production measured 2026-08-18: `true` / `true`, DRY armed by operator decision** |
| `BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED` | P4.6 | `include_descendants` family reads (REFUSED fail-closed under dream scope) | `false` |

Each: mandatory closed-by-default test, arming via runbook, production state
measured (drop-in + process) — never assumed.

---

## 9. Blocking open questions (detail in the twin ADR, §6)

> **2026-08-19 framing — four rows of this table changed state.** Q1, Q10 and
> Q12 are **answered**; Q6 remains open but **now blocks Phase 0**; **Q15 is
> new**. The single source for these answers is **ADR §0**, not this table.
> **Still open and blocking for Phase 0 exit: Q2, Q3, Q6, Q14.**

| # | Question | Blocks |
|---|---|---|
| Q1 | ✅ **ANSWERED (derived from Q12, 2026-08-19)** — for the `agent` nature, `last_observed_at` **is** the liveness signal, by construction; for the `operator` nature, D5 remains pure observation (ADR §0.2). **Corollary still OPEN**: "exactly one" vs. "all," over 24 of the 29 `open` sessions (measured 2026-08-19) | The corollary still blocks 4.1. **D5 stops being optional**: it carries the agent half of the model |
| Q2 | ✅ **ANSWERED (2026-08-19, session 2): `from_project`**, authorship — capture answers "what did this session PRODUCE." Measured: 231 tickets, **187 self** (question moot) and **44 cross-project** (where it settles it). All-or-nothing **confirmed**, uncontested | M-B unblocked, along with Q14 |
| Q3 | ✅ **ANSWERED (2026-08-19, session 2), after DISSOLVING its two trapped sub-decisions.** (a) and (b) no longer apply: an agent session's liveness comes from `last_observed_at`, so the checkpoint stops being special, and an `operator` session is never closed by inactivity ⇒ `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` disappears. What remained: **(c) storage = the PROPOSAL** (append-only `(session_id, seq)`, idempotent replay — agent retries are the norm, C6) and **(d) shape = THE TICKET** (`progress` + `blocker\|null` + `next_step` in ONE call; three mutually exclusive `kind` values would leave a half-empty snapshot with no way for the reader to know it). Divergence (d) **abandoned**, (c) **kept**. ADR §0bis.4 | M-C unblocked. The **separate checkpoint spec** is still due in P0 |
| ~~Q3 (original text)~~ | product approval (`d04dc588`) + (a) **circle of callers** (explicit command only, or an autonomous agent = covenant change) + (b) **heartbeat effect and its arming mechanism** (removed from the contract, or behind `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` shipped closed — without one of the two, (a) is settled by omission; **and "removing" is not the neutral option**: the ticket requires "Real checkpoint refreshes heartbeat atomically; replay does not," so removal is a **third** divergence from its MVP) + (c) **STORAGE divergence** (append-only + `(session_id, seq)` vs. the ticket's snapshot + CAS) + (d) **PAYLOAD SHAPE divergence**, undeclared until now: `progress` + `blocker\|null` + `next_step` published together "in one call" versus mutually exclusive `kind` + a single `note` | M-C (Phase 2); the **separate checkpoint spec** ships in P0 in every case |
| Q4 | Arm family capture? Ship `include_descendants` (refused under dream scope — widening a bearer = a separate security decision)? E5 direction (hierarchy vs. flatness)? **Reformulated on the measured state**: `red-shrik:agent` and `red-lab:architect` are **already in the pool** since 2026-08-10 (84% of the colon mass) — still to settle: (a) do we accept that the parent never sees the child, or do we want `include_descendants`? (b) do we bring in the 4 remaining `red-lab:*` keys (86 artifacts), at the cost of `_MAX_POOL` + `TimeoutStartSec` + registry matrix? (c) a **merge** of `red-shrik:agent` → `red-shrik` is a Q11 matter, not this one | P2 arming, P4.6 |
| Q5 | Sweep order/thresholds (7 d? dry duration — ALREADY armed, measured 2026-08-18?) — WET additionally requires 4.3's three-signal precondition | Phase 4.2-3 |
| Q6 | ✅ **ANSWERED (2026-08-19, session 2): ACCEPTED**, and a tracing session's unsigned drafts **SURVIVE** in a pending-signature pool, outside the session. Ruled out: auto-promotion at auto-close (that would be **E3 for the entire agent half** — full covenant) and abandoning the drafts (destroying what the agent nature produces). **Its technical fragility disappears**: the `(project_key, started_by_actor)` + "exactly one" linkage becomes exact on the connection. ADR §0bis.5 | M-E unblocked. **What the pool adds to M-E remains to be specified**: FK and `ON DELETE`, lifetime, cap, out-of-session signing tool |
| Q7 | Hard focus-content guard, or do the audit trail + `focus_diff` suffice? | (optional, post-P3) |
| Q8 | Logged reattribution right, or is orphaning the price of the proof? | M-F (Phase 4.5) |
| Q9 | ✅ **SETTLED (2026-08-19, session 2) BY MEASUREMENT: INHERITANCE.** Subagents inherit the carrier's session, with no tag, because they share its connection. **None of the three headers distinguishes them** — `X-Brain-Agent` carries the PROJECT, `X-Brain-Session` is dead (B8), `Mcp-Session-Id` carries the CONNECTION — and none ever will: header configuration is per MCP server, not per subagent. ADR §0bis.2 | Residual B1 closed by auto-close, not by the tag |
| Q10 | ✅ **ANSWERED (2026-08-19)**: the list **covers** — no B15. But the **priority is rejected**: the order comes from the **"knowledge traceability"** axis (B3, B4, B5 first), no longer from derived severity | Unblocked. **Drives the P0 → P1 → P2 → P4.4 → P3 resequencing** and Q6's promotion |
| Q11 | Project rename/merge: a separate project desired? | (out of plan) |
| Q12 | ✅ **ANSWERED (2026-08-19): track (a) — two session natures.** Tracing agent auto-closed with no ritual, operator with ritual. The nature is **declared** at `start` (so B8 does not block), **default `operator` forced by C2**. See ADR §0.1 and D11 | Unblocked, but **amends C1** and **opens Q15**. This plan is no longer "compatible with all three tracks": it has a target |
| Q13 | `brain_set_project_context`: does an omitted `current_focus` stop erasing the focus (omitted ≠ explicit erasure)? **Question reformulated: its quantified motivation was false.** The 10/59 NULL contexts are **all** at `focus_revision = 0` and `focus_updated_at IS NULL` ⇒ never written, not erased; **zero erasure measured in production**. The channel exists in the code, it has not been observed biting. To be settled on reasoning: a default to fix before it bites, or an assumed upsert semantics best left unchanged under existing clients? | M-D (Phase 3) |
| **Q15** | ✅ **ANSWERED (2026-08-19): route (3) — new terminal state.** *NEW question, raised by no earlier version.* The CHECK `brain_sessions_terminal_state_valid` forbids `ended` without a non-empty `summary` **and** `next_focus`, and requires `captured_knowledge_ids = {}` for `abandoned` (`037_session_lifecycle_v4.py:14-91`): an agent session auto-closed **with no ritual** has no terminal state available. Routes ruled out: (1) `abandoned` — the terminal snapshot would declare zero captures on the main capture path; (2) synthesized summary — that is track (c) through the back door, objection C9 | **Migration M-G on the core** (§8). Amends **C7**. **Its CONTENT is still to be specified — Phase 0 exit criterion** |
| Q14 | ✅ **ANSWERED (2026-08-19, session 2): route (a) — WIDEN.** `knowledge_sources` opens up to tickets and its project predicate to the subtree; the attestation stays green AND keeps proving what it claims. (b) "document the gap" ruled out because it digs a hole in the RESTORATION proof, hence in the DR story, already an open blocker. **Work on BOTH v4 assets**, not one | M-B unblocked |
| ~~Q14 (original text)~~ | **What the recovery attestation must learn.** `ops/recovery/brain-v42-v4.sql` defines a captured artifact's legitimacy as the UNION of the **six** knowledge tables and by `source.project_key = session.project_key`. Capturable tickets (M-B) and the `pk → pk:child` capture would produce **permanent** `artifact_source_mismatches` — a red attestation, while its runbook requires "all statuses are pass." (a) widen `knowledge_sources` to tickets and its predicate to the subtree, (b) restrict the check to the six types and document the gap, or (c) give up one of the two widenings? | **M-B (Phase 2)**, and the family flag |

---

*Sources and verifications identical to the twin ADR (code as of 2026-08-18; tickets
`d30cf6e5`, `2bd14b24` — including the established operator answer —, `d04dc588`
(contract reread word for word: `expected_checkpoint_revision, progress,
blocker|null, next_step`, "one call" criterion), `7ffe0e8a`, `c60d023d`; spike
`docs/upstream/2026-08-06-claude-otlp-session-join.md` — verdict "JOIN
IMPOSSIBLE"; ticket `2dfbb83d` is closed as SHIPPED, not negative; spec `dbb7c5ce`
(figures from 2026-08-08, **re-measured here**); learnings `7bc821a1`, `367e27ae`,
`1c40c36a`; panel judgments — not archived in the repo, to be treated as drafting
context, not as evidence).
**Production measurements from 2026-08-18, read-only**: head `045`;
`10/59 project_contexts` with `current_focus IS NULL`, **all at `focus_revision = 0`
and `focus_updated_at IS NULL`**; the **seven** user triggers on `project_contexts`,
including `project_contexts_focus_revision_trigger` (032); `access_log` at **0
rows**; **seven** `public` views containing `split_part`; colon mass
`red-shrik:agent` 312 / `red-lab:architect` 135 / four remaining `red-lab:*` 86
(`red-shrik` parent: 245); drop-in `killswitches.conf` — sweep `ENABLED=true` /
`DRY_RUN=true`, pool at ten including two colon keys.
**Code verifications added in this pass**: `alembic/versions/032_brain_sessions.py:19-34`
and `001_initial.py:244-247`; `036_codex_contract_views.py:23-45,205-227,230`;
`039_project_context_timestamp_cas.py:17,337-339` (downgrade `-x` opt-in);
`repositories/pg_access_log.py:38-113` + `services/decay_flusher.py` + `config.py:379`;
`mcp/provenance_middleware.py:74-96` and `services/dream_project_scope.py:83-120`;
`repositories/pg_project_context.py:202-213,281-290`;
`services/project_group_ticket_service.py:129-137,164-167`;
`repositories/pg_brain_session.py:520-522,713-714`; `services/roadmap_service.py`;
`ops/recovery/brain-v42-v4.sql` (352-380, **404-412**, 437-470, 533-557, **665, 687**,
895-945, 1083-1113, 1135-1180) **and `ops/recovery/brain-v42-v4-pgrestore.sql`**;
`tests/unit/test_recovery_contract{,_v2,_v3,_v4}.py`,
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33`,
`tests/unit/test_recovery_contract_v3.py:164-168,488`,
`tests/integration/db/test_recovery_contract_v4_execution.py:106`;
`tests/unit/test_documentation_contract.py:25-32,1820-1825`;
`tests/unit/test_plan_index_repair_head_pin.py:45-52`; `tests/integration/conftest.py:129-155`;
`docs/OPERATIONS.md:118`. Migrations directory verified: `alembic/versions/`, versioned
head 045, **no 046**. No commit, no brain write, no DB write,
no file touched outside `docs/design/refonte-projets-sessions/`.*

**2026-08-19 pass — what it changed, and why.** Two of the four corrections
address statements **introduced by the previous fix**: a false premise can
survive its own correction, and that is this dossier's failure mode.

| What was written | What is true | Where |
|---|---|---|
| "`brain_update_project_focus` is the **only** one that bumps on unchanged text" | The `end` CAS (`pg_brain_session.py:713-714`) does too, without comparing the text, and the 037 CHECK requires it: the normal regime of a session close | §5.1 |
| "a second application-side increment would set `OLD+2`" | The 032 trigger **assigns** (`NEW.focus_revision := OLD.focus_revision + 1`), it does not add ⇒ the two explicit bumps must **stay** | §5.3 |
| The constraint trigger "catches the writer that bypasses the shared path" | `AFTER UPDATE` does not see INSERTs: `create` and `get_or_create`'s INSERT branch write a focus at `focus_revision = 0` outside its scope — a named gap, three routes, N1 bounded | §5.2, exit criteria |
| Trigger "created disabled" + "`ops/recovery/` regenerated" (two independent corrections) | `v4.sql:913-918` requires `tgenabled = 'O'`: off the list it is unexpected, on the list it is switched off — **no regeneration makes the attestation green during the disabled window** | R1.3, R1.5, §5 Rollback |

*Plus: `d04dc588` reread — removing the heartbeat effect would be a **third**
divergence (Q3(b)); suggestion fields renamed to
`project_uncaptured_since_start(_count)`, the earlier version specifying the
predicate then keeping the old name two lines above;
`expected_runtime_user_triggers` clarified to **thirteen** triggers across five
tables, seven of them on `project_contexts`. Measurements from 2026-08-19,
read-only, unchanged since the day before: head `045`; `10/59` NULL focuses all
at revision 0 and never dated; `access_log` 0 rows; seven `split_part` views;
colon mass 312/135/64/15/5/2 = 533 against `red-shrik` 245; sweep drop-in
`true`/`true`, pool at ten.*

**Residue-folding pass, 2026-08-19 (second pass of the day).** Six corrections,
three of them major. None adds new content to the plan: each fixes an inventory
or a score the plan believed complete.

| What was missing or false | What is true | Where |
|---|---|---|
| "regenerate `ops/recovery/`" read as **one** asset | **Two** v4 assets: `brain-v42-v4-pgrestore.sql` carries the same structures (12 lines with the four tokens, 15 with the indexes, measured), is run against a real database (`…v4_execution.py:106`, `parametrize` over both) and held to **CTE parity** (`…v4_pgrestore.py:29-33`). `grep -c pgrestore` across the three documents returned **0/0/0** | R1.1, R1.5, §3 and §5 criteria, §8 |
| "The attestation breaks via **three** mechanisms" | **Four**: `expected_session_indexes` (`v4.sql:404-412`, checked at `:665`/`:687`, doubled by `SESSION_INDEX_DEFINITION_MD5`, `test_recovery_contract_v3.py:164-168,488`) freezes the **CLOSED** list of `brain_sessions` indexes | R1.5 |
| "three of the six heads," stated unconditionally | True **only if** no head adds an index on `brain_sessions`. Otherwise: three heads / two structures for M-A, or **four heads out of seven** | R1.5, §8 |
| The word "index" absent from the plan | `started_by_actor` is born **with no index**, and the D5 emitter filters on it **twice per outermost call**. Indexes measured on 2026-08-19: `brain_sessions_pkey`, `uq_brain_sessions_project_client`, `idx_brain_sessions_project_status_started` — none covers the actor. Decision **briefed, not settled**; cost **measured in Phase 0** | §3.3, §3.6, §2 (baseline), §8 |
| B8 "High (constraint)" with no date | **Scored on a stale measurement**: spike measured on Claude Code **2.1.220**, `claude --version` = **2.1.234** on 2026-08-19. Spike replay = **a Phase 0 step** (§2 content #6) with a criterion, and the baseline now measures the share of calls carrying a normalizable `X-Brain-Session` | §0, §2, §7 |
| Ambiguity population presented as still to discover in P0 | **Partial** measurement already in `7ffe0e8a` (2026-08-16: `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2), a ceiling by project, not by pair; re-measured on 2026-08-19: **24 of the 29 `open`** sessions in a project with ≥2 | §2 (baseline) |
| "production advances by one notch (CAS 209→210)" | **Corroboration withdrawn** — this CAS is a `brain_session_end` (DOSSIER §B6), not `roadmap_service`, and nothing says the focus TEXT changed. Proof = plpgsql source alone (`032_brain_sessions.py:19-34`) | §5.3 |
| "18 sweepable" with no caveat | **29 `open` / 21 sweepable >7 d / 24 stale >24 h** out of 467 rows, measured on 2026-08-19 | §0, §6 (4.3) |

*Measurements from this pass, read-only, dated and **perishable**: head `045`;
`brain_sessions` 467 rows, 29 `open`, 21 >7 d, 24 >24 h; three indexes on
`brain_sessions`, none on an actor; `open` sessions per project ≥2 →
`auto-discord` 8, `brain-v42` 4, `red-arena` 4, four projects at 2;
`claude --version` = 2.1.234; `grep -c pgrestore` = 0/0/0 across the three
documents before the pass. No commit, no brain write, no DB write, no file
touched outside `docs/design/refonte-projets-sessions/`.*