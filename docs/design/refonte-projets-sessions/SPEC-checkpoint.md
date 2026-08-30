# SPEC — `brain_session_checkpoint` (migration M-C)

> **Status: PROPOSAL SUBMITTED FOR SIGN-OFF.** Nothing here is settled. This document
> exists because the audit of ticket `d04dc588` judged that "the smallest admissible
> batch remains documentation", and neither the ADR nor the PLAN delivered this spec.
> Without it, **M-C is not writable**.
>
> **Sources of record**: ADR `§0bis.4` (answer to Q3), ADR `§2 / D4` (the mechanism),
> ADR `§3.2` (the two divergences from the ticket's MVP), ADR `§0ter` (signatures of
> 2026-08-20), PLAN `§2 content no. 4` (the deliverable) and `§8` (the M-C head).
> *Written on 2026-08-20. No number carried forward: the measurements cited carry their own date.*

---

## 0. What the checkpoint has become, and why that changes everything

The checkpoint has **changed nature** between its proposal and today, and a spec
written against the old reading would be wrong from its very first line.

**Before** (graft C, D4): a **liveness** mechanism. It refreshed
`last_heartbeat_at`, the sweep's only signal. D4 flagged the danger: *an agent that
checkpoints alone keeps its session alive indefinitely — the false-alive state that
`2bd14b24` condemns — and makes criterion 4.3 self-satisfiable.*

**After** (`§0bis.4`, under Q12 = (a) + automatic opening): a **purely judgment**
object. An `agent` session's liveness comes from `last_observed_at`, which moves on *every*
tool call; the checkpoint stops being special. On the `operator` side, the timeout does not
bite at all. Its sole job goes back to being **B7 — semantic freshness**.

> ### ⚠️ Internal ADR contradiction, to be settled explicitly
>
> The text of **D4** (`§2`) still reads: "**Side effect: refreshes
> `last_heartbeat_at`** on a real checkpoint, never on a replay". The **`§0bis.4`**,
> which is later, concludes that `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` "**no longer
> has a purpose**".
>
> **Both cannot be true.** This spec keeps `§0bis.4` — it is
> later, and it resolves Q3(a)/(b), which D4 only flagged as booby-trapped.
> **Proposal: D4 must be amended in place** with a note "heartbeat effect removed,
> see §0bis.4", as §0.4 and §0bis.3 were. Without this amendment, an
> implementer who reads D4 first will wire an effect that the spec forbids.

**Direct, non-negotiable consequence**: the checkpoint **neither writes nor touches**
`last_heartbeat_at`. Not on a real checkpoint, not on a replay. The flag
`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` **is not shipped** — a flag with no purpose is
debt, not a precaution.

---

## 1. The two divergences from the ticket's MVP, settled

The audit required the spec to "explicitly settle the two divergences". They are
settled **in opposite directions**, and that is deliberate.

### 1.1 Storage — divergence MAINTAINED and owned

| | Ticket `d04dc588` | **Chosen** |
|---|---|---|
| Form | Snapshot on `brain_sessions` | **Dedicated append-only table** |
| Idempotence | CAS `expected_checkpoint_revision` | **`UNIQUE(session_id, seq)` + `ON CONFLICT DO NOTHING`** |

**Rationale.** A checkpoint note is **judgment** (the FACT/JUDGMENT grid, `§1.3`):
overwriting it with a snapshot destroys the history the checkpoint exists to produce. And the
ticket's two P0 properties — *exact replay without double effect*, *non-destructive conflict* —
are **reobtained via the key** instead of the CAS.

**What this costs, written out rather than left unsaid.** The CAS gave a **conflict**
signal: two concurrent writers on the same revision, one of the two knows it. The key,
instead, makes the retry *silently* idempotent: a second caller reusing the same `seq` with
**different content** is absorbed without a word by `ON CONFLICT DO NOTHING`. Since agent
retries are the norm (invariant C6) and `seq` is supplied by the client, this case
is not theoretical.

> **ALREADY SETTLED BY THE PLAN — this spec only makes it implementable.** The
> PLAN `§4` states it: *"the same `seq` with a different payload is a non-
> destructive conflict, **explicitly rejected**"*. So this is not a new proposal, and I
> had first presented it as one — corrected after rereading the PLAN.
>
> **Proposed mechanism to obtain it** (the PLAN states the WHAT, not the HOW):
> `ON CONFLICT DO NOTHING … RETURNING` returns zero rows both for an exact replay and
> for a content collision. Reread the existing row when `RETURNING` is empty,
> compare the triple, then **`replayed: true` if identical / `CheckpointSeqConflict` if
> different**. The exact replay stays free; the conflict stops being silent.

### 1.2 Payload shape — divergence ABANDONED, the ticket wins

| | D4 proposal | **Chosen (= ticket)** |
|---|---|---|
| Form | `kind ∈ {progress, blocker, next_step, handoff}` + single `note` | **`progress` + `blocker\|null` + `next_step`, published TOGETHER** |
| Calls | Three mutually exclusive natures | **One call** |

**Rationale, literally.** Three exclusive `kind`s allow emitting a `progress` without
ever a `next_step` — and **the freshness reader then cannot know whether the
snapshot is complete**. Publishing progress + blocker + next step would require three
calls, three `seq`s, and would blow past the ticket's explicit "**one call**" criterion.

`handoff` disappears as a *nature*. **Proposal**: it does not need a field — a
handoff is a checkpoint whose `next_step` is addressed to someone else, and the text
says it better than an enum.

---

## 2. Tool contract

```
brain_session_checkpoint(
    session_id:      UUID,
    expected_client_key: str,        # see §2.1 — kept
    seq:             int,            # ≥ 1, monotone, supplied by the CLIENT
    progress:        str,            # 1..2000, non-empty after btrim
    next_step:       str,            # 1..2000, non-empty after btrim
    blocker:         str | None = None,   # None or 1..2000 after btrim
) -> CheckpointResult
```

```
CheckpointResult {
    session_id, seq, created_at,
    replayed: bool,                  # true = row already present, identical
    checkpoint_count: int,           # after this call, to read the ceiling
}
```

### 2.1 `expected_client_key` is KEPT here — and this is not a contradiction

`§0ter.3` removes the guard **from the connection-resolved path**. The checkpoint is
**not that path**: it addresses an explicit `session_id`, so the mistargeting between
parallel sessions that the guard prevents **is possible** there. The guard stays, exactly as on
`resume`, `capture`, `heartbeat`, `end`, and `abandon`.

*Reminder of the existing, unchanged contract: it is an isolation guard, not
authentication.*

### 2.2 Payload bounds — fail-closed, all of them

| Bound | Value | Behavior on overflow |
|---|---|---|
| `progress`, `next_step`, `blocker` | **2000 characters** each | **`ValueError`, never silent truncation** |
| `progress`, `next_step` non-empty | after `btrim` | `ValueError` |
| `seq` | integer ≥ 1 | `ValueError` |
| Checkpoints per session | **200** | `ValueError` fail-closed (carried over from D4) |

**Why reject rather than truncate**, when `parse_and_validate` "forgivingly"
truncates `topic` to 200: there, a model is producing; here, it is a **judgment
object**. Truncating a judgment at 2000 characters produces a sentence that *looks*
complete and is not. The ceiling is generous; crossing it is a caller error.

**The ceiling of 200 is per session, not per night**: under automatic opening, a
tracer session lives at most until the sweep. 200 judgment notes within a single session is already
a signal in itself.

### 2.3 What the tool does NOT do

- It **does not touch** `last_heartbeat_at` (§0).
- It **does not touch** `current_focus` or `focus_revision`. No focus CAS, so
  no `focus_outcome = 'conflict'` induced on sibling sessions.
- It **attributes no artifact**. The capture ledger stays `brain_session_capture`.
- It **neither opens nor closes** any session. The covenant holds: *no hook, no
  auto-close invokes a lifecycle boundary* — and the checkpoint is not one.

### 2.4 Read surfaces (PLAN `§4`)

- **`brain_session_list`** gains `last_checkpoint_at`.
- **`brain_session_resume`** returns **recent checkpoints**. *Bound not specified —
  proposal: the last 5 by `seq` descending, to stay under the briefing
  ceiling. To be signed off.*
- **`d04dc588` freshness doctrine**: displayed freshness derives from the age of the last
  checkpoint **or the heartbeat**; focus drift is exposed separately and **is
  never a cause of expiry**.
- **`brain_session_heartbeat` stays unchanged**, documented as "prefer checkpoint", and
  **never turned into a no-op** — lying to an explicit covenant command would be a
  contract breach (the panel's reason for rejecting proposal A).

> **Caution — "or the heartbeat" remains true, the reverse no longer is.** Freshness
> may READ `last_heartbeat_at`; the checkpoint no longer WRITES it (§0). The two sentences
> coexist, and it is easy to read the first as authorizing the second.

---

## 3. Migration M-C

```sql
CREATE TABLE brain_session_checkpoints (
    id            bigserial PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES brain_sessions(id) ON DELETE RESTRICT,
    seq           integer NOT NULL,
    progress      text NOT NULL,
    next_step     text NOT NULL,
    blocker       text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT brain_session_checkpoints_seq_positive CHECK (seq >= 1),
    CONSTRAINT brain_session_checkpoints_progress_nonempty CHECK (btrim(progress) <> ''),
    CONSTRAINT brain_session_checkpoints_next_step_nonempty CHECK (btrim(next_step) <> ''),
    CONSTRAINT brain_session_checkpoints_blocker_nonempty CHECK (blocker IS NULL OR btrim(blocker) <> ''),
    CONSTRAINT uq_brain_session_checkpoints_session_seq UNIQUE (session_id, seq)
);
```

**Append-only guaranteed BY A DATABASE TRIGGER**, not by the absence of a code path — house
culture, 039 pins by SHA256 rather than by trust: a `BEFORE UPDATE OR
DELETE` trigger that `RAISE EXCEPTION`s.

> **`ON DELETE RESTRICT`, carried over from PLAN `§4` — and it is the right choice for a
> different reason than its own.** With `RESTRICT`, no cascading deletion exists: the
> append-only trigger can refuse **every** `UPDATE` and **every** `DELETE` without an
> exception to write. A `CASCADE` would force distinguishing a direct `DELETE` from a cascaded `DELETE` inside
> the trigger — one more exception in the guard that protects the judgment ledger.
> *(I had first proposed `CASCADE` without having reread the PLAN. Corrected: the PLAN governs.)*
>
> **Cost to accept**: deleting a session that has checkpoints becomes impossible without
> deleting them first — and the trigger forbids that. **A session with checkpoints is therefore
> indelible.** This is consistent with "append-only", but it is written nowhere
> else, and an operator would discover it on the first `DELETE`.
>
> ⚠️ **Neighboring trap, cited so nobody "fixes" the `RESTRICT`**: `CHECK` +
> `ON DELETE SET NULL` is a documented trap in the file (skill
> `postgres-check-vs-on-delete-set-null`). Neither `SET NULL` nor `CASCADE` here.

**Index**: `uq_brain_session_checkpoints_session_seq` serves both the idempotence key **and**
per-session reads. No other index is proposed — and especially none on
`brain_sessions`, whose index list is **closed** by `expected_session_indexes` in
the two v4 assets (fourth attestation-breaking mechanism, `§5.2 (ii)`).

### 3.1 Rollback

The downgrade `DROP TABLE`s. **It loses judgment data** — it must therefore be
**fail-closed**, following the `037→036` downgrade's model: refuse if at least one
checkpoint exists, and require an explicit operator action to override.

### 3.2 What M-C does NOT break, verified rather than asserted

- **Attestation**: a NEW table, no index on `brain_sessions`, no `brain_sessions` CHECK
  touched → the terminal CHECK fingerprint and
  `expected_session_indexes` do not move. **To be re-verified at the moment of writing the
  migration**, not to be trusted on this line.
- **Pin corridor**: M-C is **one head**, sequenced with the `_REQUIRED_ALEMBIC_HEAD` bump
  and its test in the **same commit**. It does **not** ship with
  the M-A+M-G head (`§0ter.1`): never two heads in flight.

---

## 4. The covenant moves to EIGHT

Shipping the tool brings the covenant enumeration from **seven to eight**. Three objects
move **in the same commit as the tool**, and that friction is deliberate:

1. the **docstring** of the 8th tool carries the covenant sentence;
2. **CLAUDE.md** enumerates eight commands;
3. `tests/unit/mcp/tools/test_session_covenant_docstrings_anchor.py`:
   `_EXPECTED_TOOL_COUNT` moves to `8`, and the spelled-out word moves to `"eight"`.
   *The test was written for exactly this* (commit `0207209`).

> **⚠️ "EIGHT" MIGHT NOT BE THE RIGHT NUMBER — see `SPEC-pool-brouillons.md` §4.**
> This count assumes the checkpoint is the only tool added. The draft-pool
> spec proposes **two more** — `brain_staged_capture_sign` and
> `brain_staged_capture_dismiss` — which would bring the enumeration to **ten**. The three
> specs must agree **before the first delivery**: the anchor test carries the
> number spelled out and will go red once per tool. *This is not counting, it is
> the surface of the contract an agent reads before calling.*
>
> **Interaction with `§0ter` (d), not to be missed.** The ratified resolution says
> "**covenant sentence REWRITTEN by nature**, not removed". The 8th tool must therefore carry
> the **by nature** variant, not the historical sentence. The two changes touch the
> same anchor test. **Proposal: ship them in the same commit** — otherwise the test
> goes red twice for two different reasons, and the second will be read as noise.

---

## 5. Concurrency — the tests the audit requires

`d04dc588` asked for "concurrency tests". Four, minimum:

1. **Exact replay** — two identical calls `(session_id, seq, progress, next_step, blocker)`:
   a single row, second call `replayed: true`, `created_at` **unchanged**.
2. **Content collision** — same `(session_id, seq)`, different content:
   `CheckpointSeqConflict` (§1.1), no row written, no row modified.
3. **Two concurrent `seq`s** — two writers, distinct `seq`s, in parallel:
   two rows, none lost, stable ordering by `seq`.
4. **Ceiling under a race** — 200 reached by two simultaneous writers: the 201st fails
   **fail-closed**, and the real count never exceeds 200.

Plus two tests of **absence of effect**, which fail first if someone rewires the
heartbeat: `last_heartbeat_at` **unchanged** after a real checkpoint, and `focus_revision`
**unchanged** after a checkpoint.

---

## 6. What remains UNSPECIFIED, and awaits sign-off

1. **Content collision detection** (§1.1) — proposed, not signed off.
2. **The D4 amendment** on the heartbeat effect (§0) — proposed, not signed off. *This is the most
   urgent: as long as D4 says the opposite of §0bis.4, the ADR contradicts itself.*
2bis. **The amendment to PLAN `§4`**, which still carries **the abandoned shape**:
   `kind VARCHAR(20)` CHECK ∈ {progress, blocker, next_step, handoff} + `note TEXT`,
   plus "**does not refresh the heartbeat a second time** (refresh conditioned on
   `rowcount = 1`)", which assumes a heartbeat effect that `§0bis.4` has dissolved. Its
   **exit criteria** carry the same debt: *"a checkpoint refreshes
   `last_heartbeat_at` (test)"* is a test to **remove**, not to write.
   *Same thread rule as for the ticket: a living document makes what cites it wrong.*
3. **The call policy.** D4 names it "a fork, declared, not slipped in", and it
   is **not** settled: does the checkpoint remain an **explicit** command from
   the user (covenant intact, adoption dependent on the same human discipline that
   produced 24 stale sessions out of 29, re-measured on 2026-08-19), or can an agent
   checkpoint on its own? **This spec does not settle it** — but it observes that under
   `§0bis.4`, checkpointing no longer keeps anything alive, so **the false-alive risk
   that made the fork dangerous has disappeared**. The question goes back to being an
   ordinary product choice.
4. **The B7 freshness threshold**: past what age of the last checkpoint is a session
   "semantically stale"? Unspecified. To be answered **together with Q5 and the
   4 h threshold** — same family, and `§0ter.5` says they must be answered together.
5. **The explicit product approval** that `d04dc588` requires (its open question #3).
   It gates the delivery, not this spec.

---

*Written on 2026-08-20 in Phase 0 — ZERO mutation. No line of code, no migration,
no flag. This document is a Phase 0 deliverable, not its execution.*
