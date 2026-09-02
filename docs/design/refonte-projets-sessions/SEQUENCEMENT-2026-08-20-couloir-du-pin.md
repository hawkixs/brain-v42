# PIN CORRIDOR SEQUENCING DOSSIER — brain_v42

**Established 2026-08-20. First-hand measurements are timestamped; everything else is flagged.**
**Status: PARTIALLY SIGNED on 2026-08-20 — decision `9d22bc6a-0d01-4502-b232-e2a6b9c85945`.**
**S6 AMENDED — decision `1b742dc7` (2026-08-25): 048 is taken by `attribution_mode`, the corridor now counts 9 rendezvous. Amendment block below.**

> **WHAT IS SIGNED** — **S1**: the DR contract moves to a **SINGLE v5 with `alembic_head` DERIVED**
> (precedent `test_alembic_env.py:254-259`), so **a single mint for the whole corridor** instead
> of 7-12; the exact revision remains proven by the `_REQUIRED_ALEMBIC_HEAD` pin on the code side.
> **S6** (**AMENDED** by `1b742dc7`, next block): **Order B with 048 ungrouped**, i.e. **8 rendezvous** — `046 = M-A+M-G` → `047 = M-B`
> → M-C and M-E as **separate** heads until S9 has demonstrated the independence of their
> downgrades → `049 = M-D` **isolated** (attestation collision during its
> trigger-disabled window) → `050` = trio of nullable `ADD COLUMN` → `051`/`052`.
>
> **WHAT REMAINS OPEN, AND STILL BLOCKS THE FIRST LINE OF 046**: **S2**
> (connection column), **S3** (nature in the database), **S4** (hard or soft CHECK), **S5**
> (SPEC-M-G in full). The operator must read `SPEC-M-G.md` to decide them.
>
> **NO MIGRATION LINE MAY BE WRITTEN** — the self-imposed requirement from
> `SPEC-M-G.md` stands: S6 gives the ORDER, not the authorization to write 046.

> **AMENDMENT — decision `1b742dc7` (2026-08-25), which amends S6 from `9d22bc6a` (2026-08-20).**
> **048 is assigned to `attribution_mode`.** The repair of the derived capture produced
> `alembic/versions/048_attribution_mode.py` — nullable `attribution_mode VARCHAR(24)` column
> on `brain_session_artifacts`, a four-mode CHECK, partial index on the derived mode. The slot
> that S6 reserved for M-C (or M-E) is therefore taken. **M-C, M-E, M-D, the `ADD COLUMN` trio and
> everything after it SHIFT BY ONE SLOT; the corridor now counts 9 rendezvous instead of 8.**
>
> | Candidate | S6 rank, ungrouped Order B | Amended rank (`1b742dc7`) |
> |---|---|---|
> | C1 = M-A + M-G | 046 | 046 — unchanged |
> | C2 = M-B | 047 | 047 — unchanged |
> | *(outside the dossier)* | — | **048 = `attribution_mode`** |
> | C3 = M-C | 048 | **049** |
> | C4 = M-E | 049 | **050** |
> | C5 = M-D | 050 | **051** |
> | C8 + C9 + C12 = `ADD COLUMN` trio | 051 | **052** |
> | C11 = durable access log | 052 | **053** |
> | C7 = embedding dimension | 053 | **054** |
>
> *The "S6 rank" column applies the ungrouping that S6 signs. The S6 text above writes
> `049 = M-D`, `050` = trio, `051`/`052`: these are the numbers from the **grouped** table in §2,
> incompatible with the ungrouping of the same sentence — M-C and M-E as separate heads take
> 048 and 049. The inconsistency is in the signed text; `1b742dc7` does not settle it, and only
> the ungrouped reading gives the 8 rendezvous that S6 announces.*
>
> **THE AMENDMENT MOVES NUMBERS, IT RELEASES NO GUARD.** Standing word for word:
> M-C and M-E remain **separate** heads until S9 has demonstrated the independence of
> their downgrades; M-D remains **isolated** (attestation collision during its
> trigger-disabled window); the rule **never two heads in flight**; criterion **(c)** from §2 —
> downgrades that can fail together without one's fail-closed blocking the
> other's legitimate rollback.
>
> **Why the insertion costs so little.** S1, signed on the same corridor, moved the
> DR contract to a **single v5 with `alembic_head` DERIVED** precisely so that one more head no
> longer costs a rewrite of `_expected_v4()`. The price of an insertion is thus reduced to a
> documentation renumbering — exactly what S1 had bought.

> **WHAT WILL MAKE THESE NUMBERS WRONG**, and that no test guards: a head consumed by a
> candidate outside the dossier — it has already happened twice — or a signed insertion. Measure
> them, never copy them forward: `ls alembic/versions/` for the repository head,
> `select version_num from alembic_version` for production.
>
> **NOT SETTLED HERE**: the merged 047 is `047_end_without_the_capture_receipt`, which does not
> carry M-B. The count of 9 keeps the `047 = M-B` from the signed order; if M-B must still take
> a head of its own, it is 10. `1b742dc7` does not rule on this.
>
> **Scope of this block: the NUMBERS alone.** The other claims in the signature block
> above state the 2026-08-20 status and are not replayed here.

---

## 0. MEASURED STATE OF THE CORRIDOR — 2026-08-20, ~22:05

| Fact | Measured value | Command | Verdict |
|---|---|---|---|
| Production head | `045` | `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version;"` | **CONFIRMED** |
| Pin | `_REQUIRED_ALEMBIC_HEAD = "045"` (`plan_index_repair_store.py:63`) | read | **CONFIRMED** |
| Repository head | `045`, 45 files, contiguous linear chain 001→045 | `ls alembic/versions/*.py` | **CONFIRMED** |
| No 046 anywhere | 0 blob, 0 file | `git rev-list --all --objects \| awk '$2 ~ /046/'` + `find` | **CONFIRMED** |
| Corridor empty on the PR/branch side | `gh pr list --state open` empty, PR #7 merged (`14c0385`) | verif 2 | **CONFIRMED** |
| Index `public` | **129** vs `catalog_counts.indexes = 128` pinned in `ops/recovery/brain-v42-v4.json` | `select count(*) from pg_indexes where schemaname='public'` + JSON read | **CONFIRMED (first-hand)** |
| FK `public` | 26 = 26 pinned | psql | **CONFIRMED** |
| Tables `public` | 32 = 32 pinned | psql | **CONFIRMED** |
| `alembic_head` of the assets | **`039`** (`v4.json`, `v4.sql:1789,1791`, `v4-pgrestore.sql`) vs prod `045` | read | **CONFIRMED (first-hand)** |
| `brain_sessions.status` | `varchar(20)` — `closed_inactive` is **15** characters, **it fits** | `information_schema.columns` | **CONFIRMED** |
| Views on `brain_sessions` | **ZERO** (`view_column_usage` + `pg_depend`/`pg_rewrite`, two angles) | psql | **CONFIRMED** |
| `v4.json` named in the 5 design documents | **0/0/0/0/0** | `grep -c v4.json` | **CONFIRMED** |
| SPECs written today | `SPEC-M-G.md` (15,750 B, 21:50), `SPEC-checkpoint.md` (16,881 B, 21:50) — **"PROPOSAL SUBMITTED FOR SIGN-OFF"** | `ls --time-style=full-iso` | **CONFIRMED** |
| ADR §0ter | **exists**, l.368+: "M-A and M-G go out as ONE SINGLE head" | read | **CONFIRMED** — *refutes investigation 2, which read an ADR dated 08-19* |
| PLAN §8, M-G line | **updated**: "**ONE HEAD WITH M-A** (signed 2026-08-20, ADR §0ter.1) — *rank in the corridor still to be sequenced*" | `PLAN:1421` | **CONFIRMED** — *refutes both investigations, PLAN edited at 22:05* |

> **Freshness caveat, decisive.** ADR and PLAN were rewritten at **22:05**, i.e. *after* the two verification passes. Any conclusion of the form "the document does not say X" must be replayed at the moment of use. The `docs/design/` corpus is **untracked** (`git status: ?? docs/design/`): no diff protects these readings.

### 0.1 THE DISCOVERY THAT DOMINATES THE WHOLE DOSSIER

**The recovery contract pins `alembic_head`. So NO head is "without attestation cost" — attestation turns red on every cutover, even for a plain `ADD COLUMN`.** Measured: `v4.json` carries `{"id":"alembic_head","kind":"alembic_head_equals","revision":"039"}`, `v4.sql:1789-1791` likewise.

Immediate consequence, **already true before any new head**: live attestation is red on **at least two** checks I measured myself (`alembic_head` 039≠045, `indexes` 128≠129). The runbook announces `24/25` (l.154-155) *and* requires `25/25` as the **authorization gate** before `repair` (l.63, l.66, l.583): **the document contradicts itself, and reality is worse than its pessimistic branch**. The third failure announced by ticket `eb067b57` (`view_column_mismatches=1`) is **UNVERIFIABLE** on my end — I did not execute the `.sql` (read-only scope).

This transforms the sequencing question. It is not "which heads cost an asset regeneration" — **they all cost it**. It is: *does the recovery contract follow the head, or does it describe a shape?* See §4, signature **S1**.

---

## 1. THE CANDIDATE TABLE

**Surveyed from seven independent angles**: `ls alembic/versions/`, `git rev-list --all --objects`, `git grep '\b045\b'`, PostgreSQL tickets (31 open ones re-read), recent `decisions`, roadmap `features`, live PostgreSQL catalog. Each angle produced at least one candidate the others did not have.

### 1.1 Live queue — heads from the redesign plan

| # | Candidate | Content (1 line) | Gate | Couplings | Who is waiting on it | Verdict |
|---|---|---|---|---|---|---|
| **C1** | **M-A + M-G** (will take number **046**) | 4 nullable columns on `brain_sessions` (`started_by_actor` 64, `last_observed_at`, `intent` 500, `nature`) + **CONNECTION column and its PARTIAL UNIQUE index `WHERE status='open'`** + 4th terminal branch `closed_inactive` + C7 Pydantic rail | **SIGNED** (decision `c5160259`, ADR §0ter.1). SPEC-M-G written, **not signed**. Five write holes remain (§1.4) | Pin + 11 guards + **6 attestation mechanisms** (column fingerprint, `COLUMN_DEFINITION_MD5`, `expected_session_indexes`, `SESSION_INDEX_DEFINITION_MD5`×4 files, **TWO** CHECK md5 + `expected_session_constraint_fragments`, `catalog_counts.indexes` 129→130). **No view dance.** MCP restart required (`PgBrainSessionRepo` wired `server.py:372-379`) | The entire minimal slice: B1, B2, B9 closed, B3 measurable. **And, by rule 3, THE REST of the whole corridor** | **CONFIRMED** |
| **C2** | **M-B** — 8th value `'ticket'` on the artifacts CHECK | `brain_session_artifacts`: widened CHECK + **`knowledge_sources` widened to tickets and its predicate on the subtree** (Q14=(a), decision `e1f62ea1`) | **No open gate.** Q2 and Q14 answered on 08-19 | `expected_artifact_constraints` (7→8 values) **+ `knowledge_sources` CTE (`v4.sql:1083-1090`)** ⇒ hits the pgrestore **CTE parity** (`test_recovery_contract_v4_pgrestore.py:29-33`, allowed gap = exactly `{observed_artifact_constraints, observed_session_constraints}`). No view dance | B4/B5 on the knowledge-traceability axis (axis chosen by Q10) | **CONFIRMED** |
| **C3** | **M-C** — `brain_session_checkpoints` table | New table + append-only trigger, idempotent replay on `(session_id, seq)` | `SPEC-checkpoint.md` **written 08-20 at 21:50, NOT SIGNED** (carries a section "Internal ADR contradiction, to be settled") | `table_set` in **4 places**: `test_recovery_contract.py:292` + `_v2.py:33-39` (derived from METADATA — the new table must be added to `post_contract_tables`) + `v4.json` (32→33) + `SCHEMA.md` "32 `public` tables". To be instructed: does the trigger enter `expected_runtime_user_triggers`? | B7 | **CONFIRMED** |
| **C4** | **M-E** — `brain_session_staged_captures` table + out-of-session pool | New table + pool of unsigned drafts surviving outside a session, behind a closed flag | **POOL SPEC ABSENT** — the only Phase 0 spec still due (`ls` of the directory: absent). Explicit gate: "FK and ON DELETE, lifetime, cap, out-of-session sign-off tool" | `table_set` (4 places). **Named trap**: CHECK-NOT-NULL + FK `ON DELETE SET NULL` makes the parent DELETE impossible, and CHECKs are not deferrable in PostgreSQL | Phase 4.4, **promoted ahead of Phase 3** by Q10 | **CONFIRMED** |
| **C5** | **M-D** — `project_focus_history` + deferred constraint trigger | New table + append-only trigger + **`AFTER UPDATE OF current_focus` constraint trigger on `project_contexts`, created DISABLED** | No gate (Q13 modulates, does not block). **INSERT scope to be settled at write time** | **ONLY head triggering the HARD pin review** (`project_contexts` is one of the 3 tables named in `test_plan_index_repair_head_pin.py:45-52`). **Unsolvable collision**: `v4.sql:917` requires `tgenabled='O'` ⇒ **no regeneration order makes attestation green during the disabled window**. `expected_runtime_user_triggers` = 13 triggers / 5 tables, of which **7 on `project_contexts`**. **Derogatory rollout order**: upgrade → restart MCP → **then** trigger activation, a named operator action | B6. Pushed to last by the Q10 resequencing | **CONFIRMED** |
| **C6** | **M-F** — `brain_session_attribution_moves` | Log of artifact reattributions between sessions | **Q8 OPEN** — "logged reattribution right, or orphaning = the price of proof?". Blocks only its own head, nothing else | `table_set` alone. Lightest downgrade in the book | Nothing | **CONFIRMED** |

### 1.2 Live queue — candidates outside the redesign plan

| # | Candidate | Content (1 line) | Gate | Couplings | Who is waiting on it | Verdict |
|---|---|---|---|---|---|---|
| **C7** | **Honest embedding dimension** (ticket `c60d023d`) | **Convergent terminal** revision: reads the live typmod of the **9** `embedding vector(1536)` columns, emits an ALTER only on divergence ⇒ NO-OP on prod | Gate "M-A waits for 046" **LIFTED** (ADR §5 pt 5, PLAN l.252-262). **But decision `24495130` (08-18, `active`, not superseded) still orders "046 in the second batch"** | **No view dance** (measured at column level: 0 view projects `embedding`). **9 HNSW indexes to rebuild** (`m=16, ef_construction=64`, pgvector 0.8.2). The 9 `embedding_<table>` checks in `v4.json` pin `dimensions:1536`. **Strictly self-contained migrations** — none imports `brain_v42` | Nothing. Ticket self-declared not urgent | **CONFIRMED** |
| **C8** | **`thinking_tokens` on `dream_runs`** (ticket `76e11c9f`) | 1 nullable column — **dropped from 045 by operator arbitration**, ticket created to make the renunciation explicit | None. **Issue (2) acceptable: a WRITTEN renunciation does NOT consume a head** | No asset (`dream_runs` outside `expected_column_fingerprints`). No dance (ADD COLUMN; precedent 042). Downgrade = bare `DROP COLUMN` | The order of the codex→agy→claude rails, decided on a comparative cost skewed by ~38% | **CONFIRMED** |
| **C9** | **Sweep volume counter** on `dream_runs` | 1 nullable best-effort column (`sessions_swept`) | **NO WRITTEN OWNER** — 0 grep, 0 brain_search, 0 ticket, 0 decision (4 angles, two concurring investigations) | Identical to C8, character for character | Nothing. The gap is real (`session_sweep.py:99-113`: 8 fields, no volume; `count` stays in `render_report`) | **CONFIRMED** (real gap) / **UNVERIFIABLE** (demand) |
| **C10** | **G7 — `project_key NOT NULL`** on `decisions`/`learnings`/`snippets` | 3 × `ALTER COLUMN … SET NOT NULL` | **NO WRITTEN OWNER.** Named once, in `SPEC-M-G.md:256`, with no definition elsewhere. The G series exists (G9 in decision `74bf3e6f`) | No asset expected (the 3 tables are outside `expected_column_fingerprints`, which holds 12 objects: 2 session tables + 10 views). **No dance** (SET NOT NULL ≠ retype). `ACCESS EXCLUSIVE LOCK` + scan | Nothing. **Zero NULL measured** — three different denominators on the same day (4,232 / 4,235 / 4,237) | **CONFIRMED** (zero-NULL) / **UNVERIFIABLE** (owner) |
| **C11** | **Durable access log** (ticket `b93e32be`) | `access_log` retention **or** aggregate log `(entity, actor, day)` — **1 to 2 new tables** | Content not settled by the ticket; storage cost to be sized | `table_set` (4 places). No view dance | **The only candidate whose waiting cost is strictly increasing and NOT RECOVERABLE**: `access_log` = 0 rows (normal regime), 754 `access_count_human>0` and 718 non-null `last_accessed_at_human` **for zero source events**. 044 made this irreversible on `access_factor` (weight **0.3**) | **CONFIRMED** |
| **C12** | **Structured priority/readiness fields on `tickets`** (ticket `b68b9692`) | "structured priority, readiness, dependency and reason/evidence fields; direct gate before launch" | None. Open for 18 days, backed by a **READY precision measured at 1/3, two false positives out of 27 tickets** | ADD COLUMN on `tickets` ⇒ **no dance** (6 views depend on it, none broken by an ADD COLUMN). A roadmap feature at `building` status asks for the same batch (`deadline`, priority, readiness, `blocked_by`) + "align `db/tables.py:1252` and **both SQLite mirrors**" | Nothing blocking | **CONFIRMED** — *candidate absent from the first two investigations* |

### 1.3 Off queue — conditional, latent, dropped

| # | Candidate | Status | Verdict |
|---|---|---|---|
| **C13** | **Poison pill option A** — `ExtractionStatus` terminal state | **DEFERRED by signature** (decision `74bf3e6f`, "Strategy C alone"). **Dated review: ticket `191b2dba`, 2026-09-03.** The only candidate where a 045-style view/GRANT dance is at stake — **conditional**: `tickets.extraction_status` is `varchar(10)`, projected by `codex_ticket_v1`. **Measured escape hatch**: `ticket_extraction_attempts.status` (`varchar(10)`, clean CHECK) has **ZERO** view dependency — housing the state there removes any dance | **CONFIRMED** |
| **C14** | **Project hierarchy (`parent_key`)** — Q4 | **LATENT.** Q4 open, non-blocking; Phase 4.6 **has NO line in the §8 table**. `projects` has 7 columns, no parent. Eighth head could still surprise us | **CONFIRMED** |
| **C15** | **Codex v2 contract (9 read views)** — ticket `52d6b319` | **DROPPED with reservation.** 10 `codex_*_v1` views already exist, all with `GRANT SELECT` to `codex_ro`. What remains is the `:9210` write gateway — not DDL. **Not compared name for name** | **UNVERIFIABLE** |
| **C16** | **`recovery v5` / DR catalog debt** — tickets `eb067b57` + `8eaefe36` | **NOT A HEAD — the prerequisite of all** (§0.1). `8eaefe36` carries an external audit verdict of **NEEDS_SPLIT**. Measured holes: `grep -ci sequence v4.sql` = **0** (9 sequences uncovered); **55 non-internal triggers** in prod against 13 named in the contract; `pg_get_constraintdef` absent from both historical constraint sites | **CONFIRMED** |

### 1.4 What is NOT writable today, even for the signed head

| Hole | Source | Verdict |
|---|---|---|
| **Name, type and width of the CONNECTION column** | grep across the 5 documents: the central piece of the model is never called anything other than "the CONNECTION column". `SPEC-M-G.md:249`: "*This spec does not name M-A's columns*" | **CONFIRMED** |
| **Type, CHECK and IN-DATABASE default for `nature`** | `PLAN §8` warning (4) acknowledges it; `SPEC-M-G §3.1` raises the `nature='agent'` question in the CHECK and marks it **"TO BE SIGNED"** | **CONFIRMED** |
| **`M-G` must extend TWO CHECKs, not one** | Catalog: `brain_sessions_status_valid` (032) bounds to `{open,ended,abandoned}` **in addition to** `brain_sessions_terminal_state_valid` (037). The documents name only the second. Three attestation breakages, not one: md5 `4f21eff9…` **and** `9abfd0c6…`, **plus** `expected_session_constraint_fragments` (3 hardcoded literals) | **CONFIRMED** |
| **Wording of the covenant sentence by nature** | ADR §0ter (d) says "rewritten", not "how". `test_session_covenant_docstrings_anchor.py` will turn red — an intended Red action | **CONFIRMED** |
| **Rank of the head in the corridor** | `SPEC-M-G §8 point 6`: "**A sequencing dossier for the 046+ candidates is in progress and must be submitted and signed before a single migration line is written**" — this is the present document | **CONFIRMED** |

---

## 2. TWO PASSAGE ORDERS

### Prerequisite common to both: the grain, reread to the letter

**The rule forbids TWO HEADS IN FLIGHT (merged but not applied). It does NOT forbid a head from carrying several objects.** Verified in the repository: 037 carries a whole lifecycle, **041 carries three columns across eleven tables**, **043 carries twelve triggers across six tables**. Multi-object heads are the **norm** of this repository, not an exception. **CONFIRMED.**

And the operator's own argument, ADR §0ter.1: *"Two heads therefore do not mean 'two small steps' but two sequential production rendezvous, with, between the two, exactly the window that the functional argument condemns. The procedural, applied here, produces the risk it is supposed to reduce."* This reasoning is **transferable** to any grouping where the separation creates an incoherent window — it is **not** transferable to a grouping done for pure convenience.

### Historical basis for the duration estimates

| Measure | Value | Verdict |
|---|---|---|
| Cadence of the last 4 cutovers | 042 on 08-08, 043 **and** 044 on 08-10, 045 on 08-16 — **4 heads in 8 days** | **CONFIRMED** |
| Commit→prod window | 68 min (042); same morning (043, 044, 13 min apart); **prod before commit** (045, on operator request) | **CONFIRMED** |
| Runbook `22189c08` estimate | `estimated_duration = "45-90 min excluding application"`, **`execution_count = 0`** — never logged as executed | **CONFIRMED** (measured in the database) |
| **None of the 4 cutovers regenerated the assets** | mtime of `v4.json` and `v4-pgrestore.sql` = **2026-08-01**, unchanged since | **CONFIRMED (first-hand)** |
| Counter-example "two heads in flight" | 038+039 merged on 08-01, applied on 08-03, measured on 08-04 → **2-3 days** | **CONFIRMED** |

> **Major caveat on every estimate below.** The 2-day/head cadence is measured on **four heads that regenerated no asset**. It therefore does **not** bound a head carrying attestation — whose real cost is **UNVERIFIABLE**, with no precedent since 2026-08-01. The durations are **estimates**, not measurements.

---

### ORDER A — "as we go": one candidate = one head = one rendezvous

**Sequence**: C1 → C2 → C3 → C4 → C5 → C6 → C7 → C8 → C9 → C10 → C11 → C12 (→ C13 if the 09-03 review wakes it up, → C14 if Q4 settles).

| Metric | Value | Basis |
|---|---|---|
| **Production rendezvous** | **12 minimum, 14 at the ceiling** | Count from §1 |
| **DR contract versions** | **12 to 14** (v5, v6, v7…) if S1 = "the contract follows the head" | §0.1: `alembic_head` pinned ⇒ every head invalidates the contract |
| **Plausible duration** | **10 to 16 calendar weeks** | 12 heads × (½ workday + ~1h prod window), **serialized** by rule 3; but 12 attestation regenerations, **none of which has a costed precedent** ⇒ unboundable upper bound |

**Risks:**
1. **The dominant cost is the DR contract, not the migrations.** `test_v4_json_is_the_exact_v3_delta` **derives** `v4.json` from `v3.json` via a delta hardcoded in `_expected_v4()` and asserts the serialization **byte for byte**. Regenerating = **rewriting a test function**, not editing a number. Twelve times. **CONFIRMED (read from `test_recovery_contract_v4.py:44-84`).**
2. **The runbook's `25/25` gate stays uncrossable for the entire crossing** — it already is (§0.1). Twelve rendezvous is twelve chances to route around it, i.e. to normalize working around a disaster-recovery gate.
3. **Functional incoherence window** between C1 and C2: none (C1 is already the grouping that removes it). But between C3 and C4 (checkpoints delivered, drafts not yet), the traceability axis stays half-open for several weeks.
4. **Twelve pin reviews**, of which **only one is substantial** (C5). Eleven ritual reviews dilute the one that matters — a known failure mode of this repository.

**Real advantages:** each downgrade stays unitary; a prod failure only takes down one object; the procedural grain is never up for debate.

---

### ORDER B — grouping by cost affinity

**Grouping criterion, stated explicitly:** two candidates may share a head if and only if **(a)** they regenerate the **same** attestation mechanism, or **(b)** their separation creates a functionally incoherent window — **and** if **(c)** their downgrades can fail together without one's fail-closed blocking the other's legitimate rollback.

| Head | Grouped content | Affinity invoked | Test (c) | Verdict |
|---|---|---|---|---|
| **046** | **C1** = M-A + M-G | **(b)** — M-A alone spawns `agent` sessions with no reachable terminal state (ADR §0ter.1). **Already signed** | Two fail-closed (`intent` not NULL; `closed_inactive` rows) — accepted by the signature | **CONFIRMED (signed)** |
| **047** | **C2** = M-B | Only one touching `knowledge_sources` and CTE parity. No affinity with the others | — | **PROPOSED** |
| **048** | **C3 + C4** = M-C + M-E | **(a)** — two new tables ⇒ **a single** `table_set` regeneration in 4 places instead of two. **weak (b)**: checkpoints without the draft pool leave the knowledge axis half-open | **RISK**: M-E's fail-closed (unresolved `staged` rows) would block an M-C rollback. **Test (c) NOT SATISFIED by default** — requires independent per-table downgrades | **PROPOSED WITH RESERVATION** |
| **049** | **C5** = M-D | Only one to lay down a trigger, only one to trigger the hard review, only one with a derogatory rollout order. **Can share NOTHING** | — | **CONFIRMED (isolation mandatory)** |
| **050** | **C8 + C9 + C12** = `thinking_tokens` + sweep counter + ticket steering fields | **(a)** — three nullable `ADD COLUMN`, zero column fingerprint, zero dance, **strictly independent** bare `DROP COLUMN` downgrades | **SATISFIED** — three independent DROPs, no fail-closed | **PROPOSED (the safest grouping in the dossier)** |
| **051** | **C11** = durable access log | New table(s) + design choice not settled. Could join 048 if S3 arrives in time | — | **PROPOSED** |
| **052** | **C7** = embedding dimension | Isolated: its real cost is not the DDL but a **nonexistent test harness** (the 045/040/039 tests are **source parsing**, `tests/integration/conftest.py` runs `alembic upgrade head` once per session with the default env). Requires either a pure planner `_plan(rows, dim) -> list[str]`, or a second-database fixture | — | **PROPOSED** |
| **off queue** | **C6** (Q8), **C10** (no owner), **C13** (09-03 review), **C14** (Q4) | — | — | — |

| Metric | Value | Basis |
|---|---|---|
| **Production rendezvous** | **7** (046 to 052), **6** if C11 joins 048 | Table above |
| **DR contract versions** | **7** if S1 = "the contract follows the head"; **1** if S1 = "the contract describes a shape" | §0.1 |
| **Plausible duration** | **5 to 9 weeks** | 7 heads, of which 3 heavy (046, 048, 049) and 1 trivial (050) |

> **Amended numbers.** The ranks in this table, in these metrics, **and in the Risks that follow**
> are the ones **proposed on 2026-08-20**, before the ungrouping signed by S6 and before the
> insertion of 048 (`attribution_mode`, decision `1b742dc7`). The effective rank of each
> candidate is read from the amendment block at the top. The CONTENT of the rows — affinity
> invoked, test (c), reservation on C3+C4 — is NOT amended.

**Risks:**
1. **All-or-nothing downgrade** on grouped heads. Neutralized on 050 (three independent DROPs), **not neutralized** on 048 — that is the reservation. (2026-08-20 ranks: this "048" designates the M-C+M-E group, **not** the actually merged `048_attribution_mode`, whose downgrade is clean.)
2. **A grouped head is a bigger head to write**: more test surface exactly when the window between merge and cutover must stay short.
3. **Loss of migration-log readability**: "050" no longer tells one story but three.
4. **The 050 grouping is a convenience, not a necessity** — it satisfies criterion (a) only in the weak sense ("no asset" is not "the same asset"). It must therefore be **signed as an assumed convenience**, not justified as a constraint.

**Answer to the question asked — "can candidates that regenerate the same assets or dance the same view share a head without violating the grain?"**
**YES, without violating the written grain**: the rule concerns heads *in flight*, not the content of a head, and the repository already ships multi-object heads (041, 043). **BUT sharing is only legitimate if test (c) passes** — otherwise you trade a rendezvous cost for an impossible rollback, which is a bad trade in a corridor whose entire doctrine is fail-closed. **CONFIRMED.**

---

## 3. SINGLE RECOMMENDATION

> **Adopt ORDER B, with the M-C + M-E head ungrouped by default (hence one more rendezvous — 9 since the `1b742dc7` amendment, see the block at the top) until test (c) is demonstrated; and SETTLE THE DR CONTRACT DOCTRINE (signature S1) BEFORE writing the first line of 046.**

**Three reasons, in order of weight:**

1. **S1 dominates everything else.** If the DR contract follows the head, Order A costs 12 contract mints and Order B costs 7 — a factor of 1.7. If the contract describes a shape (`alembic_head` derived rather than pinned, exactly what `test_alembic_env.py:254-259` already does elsewhere in this repository by writing *"The head is DERIVED, not pinned: the invariant is a single head, not the head equals N"*), both orders cost **a single** mint. **Deciding S1 changes the cost of the whole corridor more than the choice of order does.**
2. **Order B respects the written grain** and applies to the rest of the corridor the reasoning the operator signed off on for C1.
3. **The real queue is not homogeneous**: one head (C5) must be isolated, one (050) can be trivial, one (C1) is signed. A uniform order treats all of them poorly.

### 3.1 FIRST HEAD TO WRITE: **046 = M-A + M-G**

It is **signed**; the dossier does not choose it, it fixes its rank (first) and its checklist.

### 3.2 EXACT CHECKLIST FOR 046

**Step 0 — NON-technical prerequisites, before the first line (all blocking):**
- [ ] **S1 settled** (DR contract doctrine) — otherwise the head does not know which asset to produce.
- [ ] **S2 signed**: name, type, width, nullability of the CONNECTION column.
- [ ] **S3 signed**: type, CHECK and IN-DATABASE default for `nature`.
- [ ] **S4 signed**: `nature = 'agent'` **inside** the CHECK, or application-level guarantee only (`SPEC-M-G §3.1`, marked "TO BE SIGNED").
- [ ] **S5 signed**: `SPEC-M-G.md` as a whole (current status: proposal).
- [ ] **Replay** `select version_num from alembic_version` — never copy `045` forward.
- [ ] **Replay** `git rev-list --all --objects | awk '$2 ~ /046/'`: the corridor must be empty **at that instant**.

**Content of migration `046_*.py`:**
- [ ] 4 nullable columns on `brain_sessions`, outside CHECK 037, **no backfill** (040/041 doctrine: `NULL` = "before"): `started_by_actor VARCHAR(64)` (aligned with `MAX_ACTOR_LENGTH=64` and `access_log.actor`), `last_observed_at TIMESTAMPTZ`, `intent VARCHAR(500)`, `nature` (type per S3).
- [ ] CONNECTION column (name/type per S2) + **`CREATE UNIQUE INDEX … WHERE status = 'open'`**. **The partial index is mandatory**: a full unique index would burn the connection for life on the first auto-close — and `closed_inactive` precisely takes rows out of `status='open'` in bulk every night.
- [ ] 4th `closed_inactive` branch on `brain_sessions_terminal_state_valid` — `captured_knowledge_ids` **with no constraint at all** (that is the whole point).
- [ ] **`brain_sessions_status_valid` widened too** — CHECK 032 rejects the 4th status *before* the terminal CHECK. **Measured: `status` is `varchar(20)` and `closed_inactive` is 15 characters — no type widening, hence no view/GRANT dance.**
- [ ] `revision = "046"`, `down_revision = "045"` — the `{001..head}` contiguity is asserted (`test_documentation_contract.py:150-168`).

**Head pin and guards (SAME COMMIT) — 13 points, re-grepped against head 045:**
- [ ] `src/brain_v42/maintenance/plan_index_repair_store.py:63` → `"046"`
- [ ] `tests/unit/test_plan_index_repair_head_pin.py` (derives the head, asserts `len(heads)==1`)
- [ ] **Written review required by the pin's docstring** — short here: 046 touches **none** of `indexed_plans`, `indexed_plan_chunks`, `project_contexts`, lays down no trigger, no NOT NULL column without a default. **Write it anyway** (rule 6).
- [ ] `README.md:238` — `migration 046`
- [ ] `docs/ARCHITECTURE.md:4` — `migrations 001–046 defined` (**EM DASH**, string different from README)
- [ ] `docs/MCP_TOOLS.md:10` — `Migration 046 …` (the guard tests `.lower()`)
- [ ] `docs/OPERATIONS.md:118` **and `:138`** (descriptive prose of the current head — **second occurrence not listed by R1**)
- [ ] `docs/SCHEMA.md` — **five literals**: `"46 revisions (001 → 046)"`, `"| 046 |"`, `"The repository target is 046."`, `"Revision 046 is the head of the repository."`, `"A fresh schema at head 046 contains 32 public tables"` (the table count **does not change**: no new table)
- [ ] `tests/unit/test_recovery_contract.py:279` → `assert script.get_heads() == ["046"]`
- [ ] `tests/unit/test_recovery_contract_v4.py:444` → `"The repository target is 046."`
- [ ] **RENAME** `test_repository_head_045_is_documented_…` → `…_046_…` (`test_documentation_contract.py:1772`) — the name carries the head, that's the anti-drift safety net
- [ ] `tests/unit/test_documentation_contract.py:1790` → `assert _repository_head() == "046"`
- [ ] **`tests/unit/test_model_column_width_contract.py`**: register `brain_sessions` in `WRITE_MODELS_BY_TABLE` — **absent today**, and its comment says an unregistered model **is NOT audited**. `intent VARCHAR(500)` and `started_by_actor VARCHAR(64)` arrive with a Pydantic rail. **Forgetting this registration turns NOTHING red.**

**Off commit (gitignored files / not guarded in CI):**
- [ ] `CLAUDE.md` (`.gitignore:74`) — **not just hygiene**: `test_documentation_contract.py:1817` asserts behind `if CLAUDE:`, so `pytest tests/unit` **stays RED locally** until CLAUDE.md says `migration 046`.
- [ ] `docs/ARCHITECTURE.md:6` and `docs/PLAN_INDEX_REPAIR_RUNBOOK.md:142` carry `Repository target: 040` — **5 revisions behind, no test guards them** (`grep 'Pending schema delivery' tests/` = 0). Decide their fate.
- [ ] `docs/ARCHITECTURE.md:4` and `:128` carry `31 PG tables modeled` — **no test asserts it** (`grep 'PG tables' tests/` = 0). Inert for 046 (0 new table), **biting for C3/C4/C5/C6/C11**.

**Attestation assets — six breakage mechanisms, all certain:**
- [ ] `observed_column_fingerprints['brain_sessions']` (`v4.sql:437-470`, compared `:565`) — the md5 covers `is_nullable` and the full column order; +5 columns changes it.
- [ ] `COLUMN_DEFINITION_MD5['brain_sessions'] = 'bf4c2a47e41aa69872119982b390f45a'` (`test_recovery_contract_v3.py:170-172`).
- [ ] `expected_session_indexes` (`v4.sql:404-412`) — **CLOSED** list checked **twice** (`:665` absent-or-md5-divergent, `:687` present-outside-the-list). 3 indexes → 4.
- [ ] `SESSION_INDEX_DEFINITION_MD5` — these md5s live **literally in FOUR files** (v3, v3-pgrestore, v4, v4-pgrestore).
- [ ] `expected_session_constraints` (`v4.sql:279-280`) — **TWO** md5s move (`4f21eff965e8da6178bb2d1030fc03f8` **and** `9abfd0c69ce694043e32e1935d17ff4f`) — **plus** `expected_session_constraint_fragments` (`v4.sql:283-337`), which hardcodes the **three** status literals: a fourth state requires a fourth line.
- [ ] **`ops/recovery/brain-v42-v4.json`** — the **third** asset, named **zero times** in the five design documents. `catalog_counts.indexes`: **129 measured → 130** with 046's index. **Its regeneration is NOT an edit**: `test_v4_json_is_the_exact_v3_delta` derives it from `v3.json` and asserts it byte for byte — `_expected_v4()` must be rewritten.
- [ ] **CTE parity** between `v4.sql` and `v4-pgrestore.sql`: allowed gap = exactly `{observed_artifact_constraints, observed_session_constraints}` (`test_recovery_contract_v4_pgrestore.py:29-33`).
- [ ] **Posture anomaly to fix in passing**: `ops/recovery/brain-v42-v4.sql` is at **0644** while its eleven siblings are at **0600** — the runbook mandates 0600.

**Downgrade:**
- [ ] Fail-closed if a non-NULL `intent` exists (human judgment) — unless a canary purge **or** a named `-x` opt-in, template `039_project_context_timestamp_cas.py:337-339`.
- [ ] Fail-closed if `closed_inactive` rows exist.
- [ ] **Teach the state to the 037→036 downgrade** — its guard (`037:229-266`) tests `status <> 'ended'` and reinstalls `_TERMINAL_STATE_V3` via `ADD CONSTRAINT`, which validates existing rows. **Premise correction: it is not a LIAR, it becomes IMPOSSIBLE.** ADR §8 ("silently loses terminal sessions") is **REFUTED** on reading the source. A spec written on this premise would add a safety net where one already exists and would miss the real work.
- [ ] Exercise **both upgrade AND downgrade** on `brain_test`, never on `brain` (runbook `22189c08`, step 4).

**Production rendezvous:**
- [ ] `BRAIN_ALEMBIC_ALLOW_PROD=1 python -m alembic upgrade head`
- [ ] **MEASURE** `select version_num from alembic_version` — never copy it forward (this sentence lied for 3 days then 10 days in this repository).
- [ ] **Restart `brain-mcp-http`** — required: `PgBrainSessionRepo` is wired at `server.py:372-379`. No order waiver (M-D's does not apply here).
- [ ] v4 HTTP canary.
- [ ] Redate the measurements in README / ARCHITECTURE / MCP_TOOLS / SCHEMA (runbook step 7).
- [ ] Verify pin ↔ prod alignment after merge (runbook step 8).

**Red action that opens the delivery:** `test_session_covenant_docstrings_anchor.py` **must turn red** (covenant rewritten by nature, ADR §0ter (d)). This is intended, not a casualty.

---

## 4. REQUIRED OPERATOR SIGNATURES

### 4.1 Blocking BEFORE the first line of 046

| # | Signature requested | Why it blocks | Options |
|---|---|---|---|
| **S1** | **Recovery contract doctrine.** Does the contract follow the head (v5, v6, v7… one mint per cutover) or describe a **shape** (`alembic_head` derived, as `test_alembic_env.py:254-259` already does)? | **§0.1**: without an answer, 046 does not know which asset to produce, and attestation stays red no matter what. **Dominates the cost of the whole corridor.** Open contradiction: PLAN §8 requires **regenerating v4**; ticket `eb067b57` sets "**do not modify the v4 assets**" as a **NON-GOAL** and asks for v5 | (i) v5 per head; (ii) single v5 + `alembic_head` derived; (iii) decouple attestation from the head |
| **S2** | **Name, type, width, nullability of the CONNECTION column** | Central piece of the model, never named in any of the 5 documents | — |
| **S3** | **Type, CHECK and IN-DATABASE default for `nature`** | `PLAN §8` warning (4) explicitly acknowledges it | — |
| **S4** | **`nature = 'agent'` in the terminal CHECK, or application-level guarantee alone** | `SPEC-M-G §3.1`, marked "TO BE SIGNED". Spec recommendation: the hard guarantee | hard / soft |
| **S5** | **`SPEC-M-G.md` as a whole** (status: "PROPOSAL SUBMITTED FOR SIGN-OFF") — including the name `closed_inactive`, `next_focus IS NULL`, `focus_outcome IS NULL` | The content of the M-G half is not settled | — |
| **S6** | **The present passage order** | Required by `SPEC-M-G §8 point 6` | Order A / Order B / hybrid |

### 4.2 Blocking later, not now

> **Deadlines renumbered** by amendment `1b742dc7`: they apply the ungrouping signed
> by S6 then the insertion of 048. No signature in this table is lifted, withdrawn or
> relaxed — only its rendezvous rank changes.

| # | Signature | Blocks | Deadline |
|---|---|---|---|
| **S7** | `SPEC-checkpoint.md` (written, not signed; carries an "Internal ADR contradiction, to be settled") | C3 | before 049 |
| **S8** | **Draft pool SPEC** — FK and `ON DELETE`, lifetime, cap, out-of-session sign-off tool. **Not written** | C4 | before 050 |
| **S9** | C3+C4 grouping: **is test (c) satisfied** (independent per-table downgrades)? | shape of 049/050 | before 049 |
| **S10** | M-D INSERT scope (`create` and the INSERT branch of `get_or_create` escape `AFTER UPDATE`) + **red attestation window assumed and dated** while the trigger is disabled | C5 | before 051 |
| **S11** | **Q8** — logged reattribution right vs orphaning as the price of proof | C6 | whenever the operator wants |
| **S12** | `thinking_tokens`: **column** or **written renunciation** (issue (2) does not consume a head) | C8 | before 052 |
| **S13** | The `ADD COLUMN` trio grouping (C8+C9+C12) is an **assumed convenience**, not a constraint | shape of 052 | before 052 |
| **S14** | **Reconcile or supersede decision `24495130`** ("the 046 dimension goes out in a second batch", `active`, not superseded) with the gate lift by ADR §5 pt 5 | C7 | before 054 |
| **S15** | C11: `access_log` retention **or** aggregate log; size the storage | C11 | before 053 |
| **S16** | C10 (G7) and C9 (sweep counter): **instruct them or formally drop them** — two proposals with no written owner, named by the most recent spec | queue | before 052 |
| **S17** | Dated review of poison-pill A — **2026-09-03**, ticket `191b2dba` | C13 | 2026-09-03 |

### 4.3 WHAT CAN GO OUT WITHOUT THE OPERATOR

> **MEASURED CORRECTION, 2026-08-20 — one item in this table and two items in the §3.2
> checklist rest on a FALSE claim. This is not a signature: it is a refutation by
> measurement, raised by the worker who executed the item and watched it fail.**
>
> **WHAT IS REFUTED.** The claim *"`docs/ARCHITECTURE.md:6` and
> `PLAN_INDEX_REPAIR_RUNBOOK.md:142` carry `Repository target: 040` — 5 revisions
> behind, **no test guards them**"* is false on its second half. **Two tests
> pin these strings character for character**:
> - `tests/unit/test_recovery_contract_v4.py:433` — `assert "Repository target: 040." in architecture`
> - `tests/unit/test_recovery_contract_v4.py:460` — `assert "Repository target: 040. This section claims no live head; measure it." in section`
>
> **These are not stale dates: they are SECTION CONTRACTS.** They guarantee
> that these passages claim **no live head** — the very discipline this
> repository imposes on itself. Editing both files turned both tests red; the edit was
> **reverted**, guards green.
>
> **WHY THE CLAIM GOT THROUGH**: the verification grep searched for
> `'Pending schema delivery'` and `'PG tables'` in `tests/`. The tests themselves pin
> `"Repository target: 040."`. **Pattern blind spot** — exactly the trap of "a
> census can be wrong three times, each time via a different grep blind spot."
> Count with several independent patterns, never with a single one.
>
> **THE SECOND ITEM (`31 PG tables`) IS INVALID FOR A DIFFERENT REASON, and the nuance
> matters.** Its claim about the tests is **accurate** — no test asserts this prose. But
> the item is moot because **the number is CORRECT**: `len(METADATA.tables)` = **31**,
> and the database carries **31** tables outside `alembic_version` (two independent
> patterns, measured on 2026-08-20). There is nothing to redate. *Worth knowing regardless: a
> new table is not without a guard — attestation pins the LIST (`table_set`, 32 entries with
> `alembic_version`), and `test_schema_indexes_027.py:350` sets a floor of
> `len(METADATA.tables) >= 18`.*
>
> **AND THE DOCUMENTATION IS NOT STALE**: `docs/SCHEMA.md:3` says "The repository target is 045" and
> `docs/ARCHITECTURE.md:4` says "migrations 001–045 defined". The "040" entries are **section**
> statements, about the delivery their section documents.
>
> **WHAT REMAINED VALID IN THE ITEM: `chmod 0600` alone.** The runbook mandates it (l. 45,
> *"mode `0600`"*) and `ops/recovery/brain-v42-v4.sql` was the only outlier among eleven assets.
> **Done on 2026-08-20: 11/11 uniform.** Git does not track this mode (only the executable bit
> is), so no commit follows from it.
>
> **The "Redate…" line in the table below is therefore INVALID**, and the corresponding
> items in checklist §3.2 (l. 225-226) are to be withdrawn from 046.



| Work | Justification |
|---|---|
| **Write the draft-pool SPEC** (S8) and submit it | Writing a proposal is not signing it. It's the last documentary gate of Phase 0 |
| **Replay the `ops/recovery/brain-v42-v4.sql` attestation in READ ONLY** against production and publish the real receipt | Read-only. Establishes `23/25` or otherwise **by measurement** instead of the current inference — corrects a number that three documents state differently (25/25, 24/25, 22/25) |
| **Fix the mode `0644` → `0600`** on `ops/recovery/brain-v42-v4.sql` | The runbook already mandates it; no new decision |
| **Redate `docs/ARCHITECTURE.md:6` and `PLAN_INDEX_REPAIR_RUNBOOK.md:142`** (`Repository target: 040`) and the `31 PG tables` counts | Wrong numbers that no test guards; a pure correction |
| **Carry §0ter over into PLAN §8** (partially done at 22:05: M-G line up to date, note (3) on head counts still stale) | Transcription of an already-signed decision |
| **Recount the corridor's heads** — Phase 0 exit criterion explicitly due | The present dossier does it: **12 candidates in the queue, 4 off queue** |
| **Write C7's test harness** (pure planner `_plan(rows, dim) -> list[str]`) | Test code, no migration; unblocks C7 without consuming a head |
| **Instruct the `WRITE_MODELS_BY_TABLE` hole** for `brain_sessions` | Silent guard; registering it requires no decision |

---

## 5. BLIND SPOTS

| # | Blind spot | Impact | Verdict |
|---|---|---|---|
| **AM1** | **No test confronts `v4.json` against the LIVE catalog.** The guard is a `v3 + delta` identity: the file can be **perfectly self-consistent AND wrong about production** — which is exactly today's state. Breakage is loud in CI but **falseness is silent** | The DR contract can lie without any suite turning red | **CONFIRMED** *(corrects anomaly A1 from investigation 1, which announced a silent breakage: it's the opposite — loud breakage, silent falseness)* |
| **AM2** | **The design corpus is UNTRACKED and moves continuously** (ADR 21:43 then 22:05, PLAN 22:05, two SPECs appearing at 21:50). Three readings of the same file gave three truths on the same day | Any conclusion "the document does not say X" expires within hours | **CONFIRMED** |
| **AM3** | **No test was run** — not by either verification pass, nor by me (`ModuleNotFoundError: No module named 'neo4j'`, venv not activated). All claims about the guards are **source readings** | A guard could have drifted without anyone seeing it | **CONFIRMED (assumed limit)** |
| **AM4** | **Attestation was not replayed.** The third failure (`view_column_mismatches=1`) is carried over from ticket `eb067b57`; only `alembic_head` and `indexes` are first-hand | The real receipt might be worse than 23/25 | **UNVERIFIABLE** |
| **AM5** | **The hard pin review for M-D has not been instructed.** It must demonstrate that the `UPDATE OF current_focus` scoped trigger is inert for the repair's `UPDATE plan_scan_paths` (`plan_index_repair_store.py:294-308`, `:560-584`). **Not done** | C5 not writable until this review exists | **CONFIRMED** |
| **AM6** | **M-C's trigger has not been confronted with `expected_runtime_user_triggers`** (closed list of 13, indexed by `expected_runtime_trigger_tables`). Reasoned, not replayed | C3 could carry a 5th unbudgeted attestation mechanism | **UNVERIFIABLE** |
| **AM7** | **`table_set` is DERIVED from METADATA minus one hardcoded exclusion.** Adding a table to `db/tables.py` without adding it to `post_contract_tables` (`test_recovery_contract.py:281-289` **and** `_v2.py:33-39`) turns red — but the mechanism is described nowhere in the design documents | C3/C4/C5/C6/C11 will discover this coupling at write time | **CONFIRMED (first-hand)** |
| **AM8** | **G7 (C10) and the sweep counter (C9) have NO written owner**, even though they are named by `SPEC-M-G.md:256` as competitors in the corridor. Four negative angles each | Two queue candidates exist only in a single sentence | **CONFIRMED** |
| **AM9** | **G7's real scope exceeds its three tables.** `project_key` is nullable on **eight** objects; real NULLs measured: `brain_entities` 13/5,590, `search_log` 246/2,321, `dream_runs` 873/1,672 (**semantic**: "written before 042"). The "already-empty hole" is only true for the three chosen tables | A widened G7 is not free | **CONFIRMED** |
| **AM10** | **The `project_key` default that actually bit was NORMALIZATION**, not nullity (learning `7bc821a1`: `brain_v42` underscore ⇒ phantom project). G7 closes an empty hole and leaves open the one that bleeds | Argument against C10 | **CONFIRMED** |
| **AM11** | **No systemd visibility** in this environment — the `brain-mcp-http` unit and the dream timer could not be inspected. The "production rendezvous" rests on code text and wiring | Real restart duration unbounded | **UNVERIFIABLE** |
| **AM12** | **Any session-population measurement is stale within hours**: 472 rows / 8 `open` today against 467-469 / 29 the day before. The PLAN figures (24/29, 21 sweepable) must be replayed, never copied forward | Exit criteria based on these figures need recalibrating | **CONFIRMED** |
| **AM13** | **C15 (codex v2 contract) dropped without a name-for-name comparison** of the 9 views requested against the 10 delivered | A DDL candidate could be hiding there | **UNVERIFIABLE** |
| **AM14** | **`C13`: the poison-pill dossier treats `tickets.extraction_status` as a given.** Six views depend on `tickets` (only one projects `extraction_status`), and `ticket_extraction_attempts.status` — `varchar(10)`, clean CHECK, **zero view dependency** — offers an escape hatch from any dance, whatever the name width | The book's only view/GRANT dance may be avoidable by table choice | **CONFIRMED** |
| **AM15** | **`C14` (Q4, `parent_key`) has NO line in the §8 table**: Phase 4.6 is not budgeted. Eighth surprise head possible | The count of 12 candidates is a floor | **CONFIRMED** |
| **AM16** | **Six active worktrees** on this repository. CLAUDE.md documents a GitNexus rule born from a stale worktree resolved as the canonical index (893 commits behind). An `impact` run to instruct a head must check `npx gitnexus list` first | Impact analysis possibly resolved against a stale index | **CONFIRMED** |
| **AM17** | **The denominator of every census moves faster than the census.** Three values for the same G7 count on the same day (4,232 / 4,235 / 4,237); three values for the same attestation receipt (25/25, 24/25, 22/25 — and my measurement says ≤23/25). **This dossier's failure mode is not forgetting a candidate, it's trusting an un-replayed number** | Applies to the present document | **CONFIRMED** |

---

## 6. CORRECTIONS APPLIED TO THE THREE INVESTIGATIONS (traceability)

| Source claim | Verdict | Measured correction |
|---|---|---|
| "SPEC M-G and SPEC-checkpoint not written" (investigation 1) | **REFUTED** | Both have existed since 21:50 on 2026-08-20, "PROPOSAL SUBMITTED FOR SIGN-OFF". Only one documentary gate remains: the pool (M-E) |
| "The ADR does not reflect the merge, jumps from §0bis.5 to §1" (investigation 2) | **REFUTED** | §0ter exists (l.368+), 4 occurrences of `2026-08-20`. ADR rewritten at 21:43 then 22:05 |
| "PLAN §8 still carries `M-G — to be sequenced — not settled`" (investigation 2, verif) | **REFUTED** | `PLAN:1421` now carries "**ONE HEAD WITH M-A** (signed 2026-08-20)". *`SPEC-M-G §8 pt 6` still quotes the old wording — the spec is already one notch stale on this point* |
| "No test guards `v4.json` ⇒ silent breakage" (investigation 1, A1) | **REFUTED** | `test_v4_json_is_the_exact_v3_delta` freezes it byte for byte. The real defect is the reverse (AM1) |
| "No trace of two heads in flight" (investigation 3) | **REFUTED** | Two precedents: `038` revision collision (2 files, 35 min, 2026-08-01) and 038+039 merged on 08-01 / applied on 08-03 |
| "The seven-guard runbook dates from the eve of 042" (investigation 3) | **REFUTED** | `created_at = 2026-08-09 11:14:52 UTC` — the **day after** 042 (08-08 18:03). It only governed 043/044/045 |
| "The 037→036 downgrade silently loses sessions" (ADR §8, echoed by the investigations) | **REFUTED** | Source `037:229-266`: it **RAISES**. It becomes impossible, not a liar |
| "M-A = five nullable columns" (investigation 1) | **REFUTED** | `PLAN §8`: **four** nullable **+ the connection column handled separately**, precisely because its nullability is not specified. Counting five inadvertently closes an open question |
| Ticket `b68b9692` absent from the first two investigations | **ADDED** (C12) | Opened 2026-08-02, requests "structured priority, readiness, dependency fields", READY precision measured at 1/3 |
| "The view/GRANT dance replays on no candidate" | **CONFIRMED, and reinforced** | Additional measurement: `status` is `varchar(20)`, `closed_inactive` is 15 characters ⇒ not even a type widening on the signed head |

---

**Reference files (absolute paths)**
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/maintenance/plan_index_repair_store.py:63`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_plan_index_repair_head_pin.py:38-52`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract_v4.py:23,44-84,444`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract_v4_pgrestore.py:29-33`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_recovery_contract.py:279,281-295`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_documentation_contract.py:150-168,1772-1819`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/tests/unit/test_model_column_width_contract.py` (hole: `brain_sessions` absent)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/ops/recovery/brain-v42-v4.json` (`alembic_head:039`, `indexes:128`)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/ops/recovery/brain-v42-v4.sql:267-282,404-412,437-470,917,1789-1791` (mode 0644, anomaly)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/037_session_lifecycle_v4.py:229-266`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/alembic/versions/045_dream_run_model_width.py`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src/brain_v42/maintenance/session_sweep.py:79-115`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/ADR-refonte-projets-sessions.md:368-489` (§0ter)
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/PLAN-phase-0-4.md:1415,1421`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/SPEC-M-G.md:170-270`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/design/refonte-projets-sessions/SPEC-checkpoint.md`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/PLAN_INDEX_REPAIR_RUNBOOK.md:63,66,142,154-155,583`
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs/ARCHITECTURE.md:4,6,128` (stale heads and counts, unguarded)
