# SPEC — The pool of unsigned drafts (migration M-E, signature S8)

> **Status: PROPOSAL SUBMITTED FOR SIGN-OFF.** `§0bis.5` answers **Q6 = accepted** —
> *"unsigned drafts SURVIVE their session's auto-close, in a pool
> awaiting signature, outside any session"* — then lists what **remains to be specified**:
> *"the FK to `brain_sessions` and its `ON DELETE`, the pool's lifetime, its cap,
> and the out-of-session signing tool are **not** specified"*. This document
> **PROPOSES** them.
>
> **Last documentation gate of Phase 0.** Along with `SPEC-checkpoint.md` and `SPEC-M-G.md`,
> it brings the 6th piece of content to completion.
>
> **Authoritative sources**: ADR `§0bis.5` (Q6), `§0bis.2` (connection key), `§0ter`
> (2026-08-20 signatures), PLAN `§4.4` and `§8` (M-E line), decision `fc8651ad`,
> the `SEQUENCEMENT-2026-08-20-couloir-du-pin.md` folder (S6: M-E as a **separate** head).
> *Written on 2026-08-20. No migration line yet — S2-S5 are not signed.*

---

## 0. The misunderstanding to clear up first: survive what?

`§0bis.5` says a draft must *"be able to survive the session that observed it"*.
Read quickly, this sounds like "survive the DELETION of the session row", and you go
looking for a clever `ON DELETE`. **That is the wrong problem.**

A session is never **deleted** in this system: it **ends**. `ended`,
`abandoned`, and soon `closed_inactive` (M-G) are **states**, not erasures —
the `brain_sessions` row stays. **Closing therefore touches no foreign key**, and
the survival requirement of `§0bis.5` is satisfied **without a single byte of FK**: the draft
stays, its session too, only the session's `status` has changed.

What the FK governs is a **different and rare** case: someone runs a `DELETE`
on `brain_sessions`. That happens neither in the ritual, nor in the sweep, nor in auto-close.

**Consequence for how the spec is driven**: the `ON DELETE` question is real but
**small**, and must not be treated as the heart of the matter. The heart is **who
signs, when, and what happens if no one ever signs**.

---

## 1. PROPOSAL 1 — the FK and its `ON DELETE`: `RESTRICT`

**Adopted: `session_id … REFERENCES brain_sessions(id) ON DELETE RESTRICT`.**

### 1.1 `SET NULL` is structurally impossible here — and that is good news

The sequencing folder recalls the in-house `CHECK` + `ON DELETE SET NULL` trap
(skill `postgres-check-vs-on-delete-set-null`): a `CHECK` is **not** deferrable in
PostgreSQL, so a `SET NULL` cascade that violates a `CHECK` makes the parent's `DELETE`
fail, with no recourse.

**This trap cannot bite on this table**, and for a reason stronger than mere
precaution: PLAN `§4.4` fixes the **PK as `(session_id, knowledge_id)`**. A primary-key
column is `NOT NULL` by definition — `ON DELETE SET NULL` would be **rejected by
Postgres at declaration time**, not on the first `DELETE`. The mistake is impossible to
make silently.

*(To be stated explicitly in the migration, as a comment: without this note, the next
reader who wants to "make the draft survive its session's deletion"
will try `SET NULL`, hit the PK, and be tempted to break the PK to get there —
destroying, in the process, the very property the PK carries, §1.3.)*

### 1.2 `RESTRICT` rather than `CASCADE`

`CASCADE` would destroy the unsigned drafts of a deleted session — which is
exactly what `§0bis.5` ruled out ("discarding unsigned drafts would amount to
destroying precisely what agent nature produces"). `RESTRICT` refuses deletion
as long as drafts exist.

**An accepted cost, and it needs to be named**: a session with unresolved drafts
becomes **undeletable** until they are signed or rejected. This is consistent with
the fail-closed downgrade already planned for M-E ("fail-closed if unresolved `staged`
rows exist"), and it is the same stance `SPEC-checkpoint.md` takes for
checkpoints. **Two tables that make their session indelible is a property of
the system, not an accident** — it deserves to be written down once, here.

### 1.3 The PK does not move, and why

`(session_id, knowledge_id)` — **not** a PK on `knowledge_id` alone. A draft is an
**hypothesis**: two sessions can observe the same artifact without either one depriving
the other. **Only promotion into `brain_session_artifacts`** (PK `knowledge_id`) confers
exclusivity. This asymmetry is the whole mechanism: the pool is permissive, the ledger
is exclusive.

---

## 2. PROPOSAL 2 — lifetime: NO automatic expiration

**Adopted: a `staged` draft NEVER expires on its own.**

This is the choice that needs the most justification, because it produces a table that
does not empty itself.

| Option | Why ruled out |
|---|---|
| Expiration after N days (purge) | **Destroys unsigned knowledge with no human action** — the exact opposite of Q6, and along the "knowledge traceability" axis that governs the plan's ordering |
| Expiration → automatic `dismissed` | Same destruction, with a trace. But `dismissed` is a **judgment** ("I don't want it"); the server would be manufacturing it — objection C9, the one that killed route (2) of Q15 |
| Automatic reattachment to the next session of the same project | Attributes with no human action. This is **E3**, the full covenant change that `§0bis.5` rejects |
| **No expiration** | Nothing is destroyed, nothing is attributed. The pool grows — a **visible and measurable** cost, not a silent loss |

**The cost is real and is managed by measurement, not by a clock.** Proposal: expose
`staged_pool_size` and `staged_pool_oldest_age` as process metrics. A growing pool
is a signal that no one is signing — **information about usage**, not a storage
incident. The response to "the pool is big" is an operator action, never a scheduled
`DELETE`.

> **TO SIGN**: do we accept a table that never gets purged? *Recommendation: yes.
> `brain_session_artifacts` and `brain_session_checkpoints` are not purged either, and
> the expected volume (see §3) is far below the corpus, measured at 4,405 entities on
> 2026-08-20.*

---

## 3. PROPOSAL 3 — caps: TWO, not one

PLAN `§4.4` already fixes **500 per session**, with a
`staged_capture_skipped{overflow}` process metric counter. **This cap is kept**
and is not up for debate again.

**It is not enough**, and that is what this spec adds: 500/session bounds what **one**
session can produce, not what the **pool** can accumulate. Under automatic opening,
tracer sessions are born on their own and close on their own; nothing bounds their count.

**Proposal: a second cap, on the whole pool**, `BRAIN_SESSION_STAGED_POOL_MAX`,
proposed at **5,000** `staged` rows — ten full sessions.

**On overflow: stop observing, discard nothing.** The exact same stance as the
per-session cap — the new writer is refused, the existing rows stay intact. A full pool
degrades observation; it does not destroy what has already been observed.

> **TO SIGN**: the value 5,000, and the very principle of a second cap.
> ⚠️ **Do not repeat the tracer-threshold mistake**: if this cap is evaluated in the
> nightly sweep rather than at write time, it is an **eligibility threshold**, not a
> bound — and the pool can overshoot between two passes. *Recommendation: evaluate it
> at write time, where the bound is true in the strict sense.*

---

## 4. PROPOSAL 4 — the out-of-session signing tool

```
brain_staged_capture_sign(
    project_key: str,
    knowledge_ids: list[UUID],       # 1..100, unique
    target_session_id: UUID | None = None,
) -> StagedSignResult
```

**It does NOT belong to the session lifecycle**, and that is its most important
property: it is called **outside a session**, on a pool that survived the closing of
the one that observed it. It therefore carries **no** `expected_client_key` — it
addresses no live session.

**What it does**: moves drafts from `staged` to `promoted`, and writes the
corresponding rows into `brain_session_artifacts` — where the `knowledge_id` PK
**enforces exclusivity**. An artifact already attributed to another session is
**refused**, not reattributed: reattribution is **Q8/M-F**, a different head, a
different decision.

**The mandatory counterpart**: `brain_staged_capture_dismiss(...)`, same shape, `staged` →
`dismissed`. Without it, a draft no one wants to keep has **no way out** — it
would stay `staged` forever and would block deletion of its session (§1.2). The
three values of the CHECK `∈ {staged, promoted, dismissed}` must each be reachable
by some action; an unreachable value is a design flaw, not a reserve.

> **TO SIGN, and this is the real product question**: `target_session_id` — is a
> promoted draft attributed **to the session that observed it** (authorship, consistent
> with Q2 = `from_project`) or **to the signing operator's current session** (the
> signer)? *Recommendation: to the session that OBSERVED it, `target_session_id`
> omitted by default.* This is the exact analogy of Q2: capture answers "what did
> this session **produce**". Signing is not producing.
>
> **The covenant then moves to NINE or TEN commands**, not eight. `SPEC-checkpoint.md`
> §4 announces eight (the seven plus the checkpoint); `sign` and `dismiss` add two.
> **The three specs must agree on this count before the first delivery** — the anchor
> test `test_session_covenant_docstrings_anchor.py` spells the number out in full,
> and it will go red the moment a tool is added. *This is not a counting detail:
> it is the contract surface an agent reads.*

---

## 5. What M-E breaks, and what it does not

- **Attestation**: a NEW table ⇒ `table_set` changes in **both** v4 assets. Under
  signature **S1** (v5 contract at a **derived** `alembic_head`), this is now **the
  only** attestation cost of this head — the revision no longer matters.
- **No index on `brain_sessions`**, so `expected_session_indexes` does not move.
- **A head SEPARATE from M-C**, per signature **S6**: grouping is only legitimate if
  the downgrades can fail independently, and **S9 has not demonstrated that**. Both
  downgrades are fail-closed on unresolved rows; grouped, one's failure would block
  the other's rollback.
- **`WRITE_MODELS_BY_TABLE`** (`test_model_column_width_contract.py`): if M-E ships a
  writer Pydantic model, it must be **registered** there. Verified on 2026-08-20: the
  table carries **12 entries** there and `brain_sessions` **is not among them** — an
  unregistered model is not audited, and its guard is silent. The 046 checklist already
  carries this gap (`SEQUENCEMENT §3.2`); **M-E must not reopen it for its own table.**

---

## 6. What remains UNSPECIFIED

1. **The four proposals above** — none is signed.
2. **The covenant count** (§4) — eight, nine, or ten, to be reconciled between the
   three specs.
3. **The draft writer itself** stays gated behind
   `BRAIN_SESSION_STAGED_CAPTURE_ENABLED`, shipped **closed**. This spec describes
   the pool and its exit, **not** the observation policy.
4. **A stale site, spotted and not fixed here**: PLAN `§4.4` still describes the
   writer as keying by **`(project_key, started_by_actor)` under an "exactly one"
   rule**. That has been false since `§0bis.2` — on the connection, **the binding
   is exact**, and `§0bis.5` itself notes that "Q6 loses its main implementation
   risk". Amending the PLAN on this point takes the same action as for D1/D4: a
   boxed note, a signature.

---

*Written on 2026-08-20 in Phase 0 — ZERO mutation. No migration will be written before
S2-S5 are signed and M-E's rank in the corridor is reached (S6: after
046 and 047, as a head separate from M-C).*
