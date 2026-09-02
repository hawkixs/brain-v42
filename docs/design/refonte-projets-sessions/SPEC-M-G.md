# SPEC — M-G: the terminal state of `agent` sessions

> **Status: PROPOSAL SUBMITTED FOR SIGN-OFF.** The ADR's `§0.4` answers **Q15 = route (3)**
> — a new terminal state — then writes in black and white: *"The **name** of the state, its
> exact branch, and the fate of `captured_knowledge_ids`, `abandonment_reason` and
> `focus_outcome` on that branch **are not specified here**"*. **This document PROPOSES
> them. It does not settle them.** Every proposal carries its alternative and its cost.
>
> **Unblocked by `§0ter.1`** (M-A + M-G in a single head): this spec was waiting for
> that answer and nothing else.
>
> **Authoritative sources**: ADR `§0.4` (Q15), `§0ter` (signatures of 2026-08-20),
> `§0bis.1` (natures), amended `§0bis.3` (threshold), `§5.2`/`§5.3` (attestation, the pin
> lane), PLAN `§8` (M-G line). **The CHECK quoted below was reread from
> `alembic/versions/037_session_lifecycle_v4.py` on 2026-08-20**, not copied from the ADR.

---

## 1. The problem, reread at the source

`brain_sessions_terminal_state_valid` (037, `_TERMINAL_STATE_V4`) is a disjunction of
**three** branches — `open`, `ended`, `abandoned` — and **none** of them accommodates a session
closed without ritual:

| Branch | What it REQUIRES, and what it blocks |
|---|---|
| `ended` | `summary` **and** `next_focus` non-empty, `focus_outcome NOT NULL`, **and** (`captured_knowledge_ids > 0` **XOR** `nothing_to_capture_reason` non-empty) |
| `abandoned` | `summary IS NULL`, `next_focus IS NULL`, **`cardinality(captured_knowledge_ids) = 0`**, `abandonment_reason` non-empty |

**The point that kills route (1)**, and one that must be seen in the SQL to be believed:
`abandoned` **forces the terminal ledger to zero captures**. An `agent` session that did
its work and attributed artifacts would therefore declare, in its terminal snapshot,
that it captured nothing — on the main capture path, and under the very "knowledge
traceability" axis the operator chose. The real ledger survives in
`brain_session_artifacts`, but the snapshot lies.

**The point that kills route (2)**: `ended` requires both a `summary` **and** a `next_focus`.
Synthesizing them server-side means copying measurable state into the one
channel of judgment that cannot be derived — objection C9, the `FocusArg` doctrine.

Hence route (3), and this spec.

---

## 2. PROPOSAL 1 — the state's name: `closed_inactive`

**Adopted: `closed_inactive`.**

| Candidate | Why rejected |
|---|---|
| `expired` | Suggests the content lapsed, not the session. The captured knowledge itself does not expire |
| `auto_ended` | "ended" as a prefix here: a `LIKE 'ended%'` or a hurried read would conflate them, and the distinction from the ritual is **the entire** point of this state |
| `timed_out` | Implies a deadline. `§0ter.5` just established that 4 h is an **eligibility** threshold, not a deadline — the name would be lying again |
| `swept` | Describes the **mechanism** (the sweep), not the **state**. The mechanism can change; the state should not |
| **`closed_inactive`** | States the cause (observed inactivity) and the outcome (closed), without promising a deadline or blending into `ended` |

**Accepted cost**: this is a fourth vocabulary in a machine that already had three. Every
reader of `status` must now learn that there are **two** ways to be properly
terminated — `ended` (ritual) and `closed_inactive` (without ritual) — plus one way to be
terminated without really being so (`abandoned`).

---

## 3. PROPOSAL 2 — the exact CHECK branch

```sql
OR (
    status = 'closed_inactive'
    AND ended_at IS NOT NULL
    AND nature = 'agent'                     -- ⬅ see §3.1: the guard that matters
    AND summary IS NULL                      -- no ritual: nothing to summarize
    AND next_focus IS NULL                   -- ⬅ see §3.2
    AND abandonment_reason IS NULL           -- this is NOT an abandonment
    AND end_expected_focus_revision IS NULL  -- no CAS attempted
    AND focus_outcome IS NULL                -- ⬅ see §3.3
    AND focus_at_end IS NULL
    AND focus_revision_at_end IS NULL
    AND nothing_to_capture_reason IS NULL    -- ⬅ see §3.4
    -- captured_knowledge_ids: NO constraint. That's the entire point.
)
```

### 3.1 `nature = 'agent'` in the CHECK — proposed, and debatable

**For**: makes it impossible in the database for an `operator` session (hence *claimed*) to reach this
state. The `§0bis.3` guarantee — *"a claimed session is NEVER closed by
inactivity"* — stops being an application-level promise and becomes a database constraint.

**Against, and a serious one**: the ratified resolution (d) leaves `nature IS NULL` under the
7-day sweep regime. Sessions **predating M-A** have no nature. If one day
someone wanted to close a `NULL` session into this state, the CHECK would forbid it — and
the workaround would be a backfill of `nature`, i.e. changing the rules
retroactively, which (d) rules out.

> **TO SIGN**: `nature = 'agent'` (hard guarantee, less flexible) **or** no nature
> constraint (flexible, application-level guarantee only)? *Recommendation: the hard guarantee.
> The case "close a NULL session into closed_inactive" isn't on the catalog, and a
> CHECK is loosened more easily than it's tightened.*

### 3.2 `next_focus IS NULL` — and why it's good news

`§0.4` asked the question without closing it: *"does an agent session ended in this new
state apply a `next_focus`? If not, it performs no CAS, and **that's good
news**"*.

**Proposal: NO.** It writes no `next_focus` and attempts no CAS. Otherwise,
every auto-close would bump `focus_revision` and produce
**systematic** `focus_outcome = 'conflict'` on concurrent operator sessions —
noise manufactured by the server, on the one channel of judgment.

**Reinforced by the sweep's context**: the closing is **batched**, once a night
(§0ter.4). N sessions closed in one statement, each attempting a CAS, would produce N−1
conflicts by construction. That's an additional argument, independent of the first.

### 3.3 `focus_outcome IS NULL` — a consequence, not a choice

Follows from §3.2: no CAS attempted ⇒ no CAS result to declare. `NULL` means
"no attempt", which is exactly the fact.

*Alternative rejected: a third value, `not_attempted`. It would add vocabulary
to say what `NULL` already says, and would force touching the Pydantic enum of
`focus_outcome` in addition to that of `status` — two rails instead of one.*

### 3.4 `captured_knowledge_ids` FREE, `nothing_to_capture_reason` FORBIDDEN

**This is the entire point of the migration.** An auto-closed `agent` session keeps its
ledger **as is**: zero, one or a hundred artifacts, with no constraint and no justification.

`nothing_to_capture_reason` is **forbidden** (`IS NULL`) and this isn't a detail: on the
`ended` branch it is the **fail-closed counterpart** to an empty ledger — the operator must
*say why* they captured nothing. That's a **judgment call**. A server that filled it
in for an agent session would manufacture judgment, exactly the objection C9 that killed
route (2). Leaving it `NULL` on an empty ledger states the truth: *nobody was
asked.*

**Consequence to accept**: an agent session with an empty ledger becomes **indistinguishable** from
an agent session that had nothing to capture. B3 is therefore not measured on this state. That's the
price of not manufacturing judgment, and it must be written into the exit criteria
rather than discovered while measuring them.

---

## 4. PROPOSAL 3 — the trigger

**The nightly sweep, a single statement, 4 h eligibility threshold** (`§0ter.4` and `§0ter.5`).

- **Eligible**: `status = 'open'` **AND** `nature = 'agent'` **AND**
  `last_observed_at < now() - interval '4 hours'`.
- **Never**: a `claimed` session (⇒ `nature = 'operator'`), regardless of age —
  only the existing 7-day sweep can take it.
- **Never** mid-call. `provenance.py` already tracks call
  depth (`enter_call` / `exit_call` / `is_outermost_call`): forbidding the literal "cut mid-way"
  case is free.
- **`nature IS NULL`**: out of scope, stays under the 7-day sweep regime (resolution (d)).

**What this is NOT, and must never be announced otherwise**: 4 h is **not** a
closing deadline. A trace session that goes inactive right after a pass lives until the
next one — **worst-case real latency ≈ 28 h**.

> **PROPOSAL — a single statement, shared with Q5.** `§0ter.5` establishes that Q5 (sweep
> thresholds) and the 4 h threshold answer each other **together**. Two concurrent nightly passes on
> `brain_sessions` for two rules describing the same gesture would be needless
> machinery. The existing statement is already **ONE** `UPDATE … RETURNING` (textually
> pinned by `test_pg_brain_session_sweep.py`): M-G must **extend this statement**,
> not add another.
>
> **Guard to reaffirm explicitly**: the "single statement" test exists precisely
> to forbid the `SELECT`-then-`UPDATE` window. Extending it must not reopen it.

---

## 5. What the single M-A+M-G head carries

Signed off at `§0ter.1`. **One head**, hence **one** rendezvous, **one** bump of
`_REQUIRED_ALEMBIC_HEAD` with its test **in the same commit**, and no window where
`agent` sessions are born without a reachable terminal state.

**Five objects move together. None can be deferred:**

1. **The CHECK** `brain_sessions_terminal_state_valid` — fourth branch.
2. **The Pydantic rail (C7)** — `BrainSessionStatus` (`models/brain_session.py:27-29`)
   gains `CLOSED_INACTIVE = "closed_inactive"`. **With the CHECK, never after.**
3. **BOTH `ops/recovery/` v4 assets** — `brain-v42-v4.sql` **and**
   `brain-v42-v4-pgrestore.sql`, kept in CTE parity. Two fingerprints move:
   - the **terminal CHECK fingerprint** (`md5` of the constraint definition);
   - **`expected_session_indexes`** (`v4.sql:404`, checked at `:665` and `:687`), a
     **closed** list that the partial UNIQUE connection index brought by **M-A** breaks
     — the fourth attestation-breaking mechanism (`§5.2 (ii)`).
   *This is the most concrete reason for the single head: one regeneration for two
   causes of breakage which, apart, would demand two.*
4. **The `037→036` downgrade**, already fail-closed, must **learn the new state**: refuse
   if `closed_inactive` rows exist, otherwise it silently loses terminal
   sessions.
5. **The covenant rewritten by nature** (`§0ter` (d)) — the sentence changes in the docstrings
   of the lifecycle tools, and `test_session_covenant_docstrings_anchor.py` **turns red**.
   That's the Red step that opens the delivery, not a casualty.

> **Index trap, inherited and not to be rediscovered.** The UNIQUE index on the connection
> column (M-A) **must be PARTIAL** — `WHERE status = 'open'`. A plain unique, on the
> pattern of `uq_brain_sessions_project_client`, **would burn the connection for life**
> at the very first auto-close, destroying the "being cut costs a reconnect, not
> a loss" property. **Under M-G, this trap gets worse**: `closed_inactive` moves
> rows out of `status = 'open'` precisely, in bulk, every single night.

---

## 6. The price of fail-open, written here because this is where it's paid

Ratified at `§0ter.4`: auto-open is **fail-open**. If opening fails and
the call proceeds anyway, **artifacts created before the successful open fall outside
the capture's `created_at >= started_at`** window.

**B5 becomes sharp again, occasionally.** This is not a side effect discovered
after the fact: it's the accepted cost of not taking down the whole MCP server over a
database hiccup — same stance as the client activity emitter (`1c40c36a`), whose failure cannot
break the call it observes.

**Interaction with §3.4, and it's unpleasant**: a session whose opening fail-opened AND
which closes as `closed_inactive` presents an empty ledger **without
`nothing_to_capture_reason`** — indistinguishable from a session that had nothing to capture.
The loss is therefore **silent by construction**.

> **PROPOSAL — make it noisy without manufacturing judgment.** Count
> fail-opens in a dedicated metric (the client activity emitter already knows how) and
> refuse to read B3 over a window where that counter is nonzero. This doesn't fix the
> window — it refuses to measure a coverage known to be false. *Unsigned.*

---

## 7. Exit criteria — measurable, and what they DO NOT prove

| Criterion | `proves` | `does_not_prove` |
|---|---|---|
| `alembic current` = new head, pin bumped in the same commit | The head advanced without a two-head window | Nothing about behavior |
| BOTH v4 assets green after regeneration | CHECK fingerprint **and** index list agree with the database | Nothing about other tables |
| Downgrade refused fail-closed with ≥ 1 `closed_inactive` row | The rollback doesn't lose a terminal session | Doesn't prove it succeeds without those rows — must also be tested |
| One night: N `agent` sessions inactive > 4 h moved to `closed_inactive`, **0** `operator` taken, **0** ledger emptied | Scope and preservation | **Proves nothing about active sessions**: must also show that a session that observed a call within 4 h **is not** taken |
| Project's `focus_revision` **unchanged** after the pass | No CAS induced, no manufactured `conflict` | — |

---

## 8. What remains UNSPECIFIED

1. **`nature = 'agent'` in the CHECK** (§3.1) — both options carry a real cost.
2. **The fail-open metric** (§6) — proposed, unsigned.
3. **The `last_observed_at` column** is assumed delivered by **M-A**. If M-A names it
   differently, §4 follows that name. *This spec does not name M-A's columns.*
4. **The Q5 threshold** — answered **together with** the 4 h, in the same statement.
5. **The exact wording of the covenant sentence by nature** — `§0ter` (d) says
   "rewritten", not "how". It must be written before delivery, along with an
   update to the anchor test.
6. **THE HEAD'S RANK IN THE PIN LANE.** PLAN `§8` still carries, in plain
   letters, `M-G | **to be sequenced — undecided**`. `§0ter.1` says **with what** it ships
   (M-A), not **when** — and the lane forbids two heads in flight, so the order is a
   constraint, not a preference. Other candidates exist in the same lane: the
   "nine columns" of `c60d023d`, `G7 NOT NULL`, the sweep counter. **A sequencing
   dossier for candidates 046+ is underway and must be submitted and signed before a
   single line of migration is written.** This spec describes the CONTENT of M-G; it
   claims no rank.

   > **AMENDED on 2026-08-20 — the dossier exists and its ORDER is signed.**
   > `SEQUENCEMENT-2026-08-20-couloir-du-pin.md`, decision `9d22bc6a`. **S6** places M-G at
   > **`046 = M-A + M-G`, first head of the lane**, across 8 rendezvous (Order B, 048
   > split out). The rank is therefore no longer open.
   >
   > **But the authorization to write is not, either.** S2 (connection column),
   > S3 (nature in the database), **S4 (hard or soft CHECK — this is §3.1 of this spec)** and
   > S5 (this spec in full) remain **UNSIGNED**. The requirement of this point 6 therefore
   > stands as is: no line of migration.
   >
   > **Measured fact validating §2**: `brain_sessions.status` is `varchar(20)` and
   > `closed_inactive` is **15** characters long — **it fits**, without widening the
   > column (measured on 2026-08-20, `information_schema.columns`). A longer name would
   > have added an `ALTER TYPE` to the most watched head of the lane.
   >
   > **Second measured fact, which relieves M-G of a risk**: **ZERO views** depend on
   > `brain_sessions` — verified from two angles (`view_column_usage` and
   > `pg_depend`/`pg_rewrite`). Unlike 045, no view needs to be dropped and
   > recreated around the constraint change.

   > **AMENDED AGAIN — decision `1b742dc7` (2026-08-25), which amends S6 from `9d22bc6a`.**
   > **048 is assigned to `attribution_mode`** (repair of derived capture,
   > `alembic/versions/048_attribution_mode.py`). **M-C, M-E, M-D, the `ADD COLUMN` trio and the
   > rest shift by one slot: the lane now counts 9 rendezvous instead of 8.** M-G's rank
   > does not move — it remains `046 = M-A + M-G`, first head.
   >
   > **The amendment SHIFTS NUMBERS, it RELEASES NO GUARD WHATSOEVER**: M-C and M-E remain
   > **separate** heads as long as S9 has not **demonstrated the independence of their downgrades** —
   > not "as long as S9 has not concluded", which a negative conclusion would satisfy and
   > would lift the separation; M-D remains **isolated**, the "never two heads in flight" rule
   > holds, criterion **(c)** on independent downgrades holds. The insertion
   > only costs a documentation renumbering because **S1** moved the DR contract to a
   > single v5 **DERIVED** from `alembic_head` — one more head no longer rewrites `_expected_v4()`.
   >
   > The rank-by-rank detail lives in `SEQUENCEMENT-2026-08-20-couloir-du-pin.md`, amendment
   > block at the top; **do not copy these numbers here** — a second source of truth
   > would only drift on reading. What would make them false: a head consumed by a candidate
   > outside the dossier, or a signed insertion.
7. **The name of the inactivity column** — this spec writes `last_observed_at` because
   that's the name used by D5 and `§0bis.4`. If **M-A** delivers it under a different name,
   `§4` follows that name without argument: M-A is authoritative on its own columns.
   *(Deliberate repeat of point 3: this is where an implementer will look.)*

---

*Written on 2026-08-20 in Phase 0 — ZERO mutations. No migration is written here, and
none should be until the pin lane sequencing dossier (candidates
046+) is submitted and signed.*
