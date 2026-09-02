# ADR — PROJECTS + SESSIONS overhaul: instrumented accretion
## "The server observes and prepares; the operator signs"

- **Date**: 2026-08-18
- **Status**: **PROPOSED** — this status will never move to *accepted* without the
  operator's explicit go-ahead. Anchor ticket `d30cf6e5` says: "DO NOT START without
  the operator's explicit go-ahead." **Nothing in this document authorizes starting
  anything.** No line of code, no migration exists.
- **Scope**: brain-v42's PROJECTS and SESSIONS systems ("second brain" MCP server,
  Python 3.12, FastMCP 3, SQLAlchemy 2 async, PostgreSQL 16 + pgvector, production
  Alembic head measured at 045 on 2026-08-16 — to be re-measured, never copied
  forward).
- **Genesis**: synthesis of three architect proposals (A "assumed overhaul", B
  "incremental evolution", C "operator first") judged by a panel of three lenses
  (technical, operator, simplicity). Vote: B majority (2/3). This ADR starts from B,
  fixes every weakness the judges flagged on B, and grafts in the ideas from A and C
  that the panel explicitly kept. Nothing here is new without proof (ticket, learning,
  verified code) or a judge behind it. **Assumed provenance limit**: the three
  proposals and the panel's verdicts are not archived in the repository — only ADR,
  DOSSIER and PLAN exist. The scores and "required by the panel" attributions are
  therefore unverifiable for the reader: treat them as drafting context, never as
  evidence. The evidence that holds up remains the cited tickets, learnings and code
  checks, each independently re-verifiable.
- **Twin document**: `docs/design/refonte-projets-sessions/PLAN-phase-0-4.md`
  (detailed phased plan). Each of the two stands on its own.
- **Operator framing — TWO sessions on 2026-08-19.** The second (**§0bis**) refines
  Q12 on opening, settles Q9 by measurement, dissolves Q1's corollary and settles the
  four remaining blockers: **Q2, Q3, Q6, Q14**. **PHASE 0 IS UNBLOCKED.** Still open,
  without blocking: Q4, Q5, Q7, Q8, Q11, Q13. Read §0bis **before** §0: it corrects
  one of its derivations — the default nature flips under automatic opening.
- **Operator framing — first session, 2026-08-19**: **five answers secured** (Q10 and
  its axis, Q12, Q1 derived, Q15 new, sequencing). They are recorded in **§0** below,
  their single source, and propagated through the body of the document. The status
  remains **PROPOSED**: Q2, Q3, **Q6** and Q14 are still missing to unblock Phase 0.
  Two answers touch what accretion promised to spare — the covenant (Q12) and the 037
  state machine (Q15): §1.3 has been corrected accordingly.

---

## 0. Operator framing — session of 2026-08-19

First framing session. What is not listed here remains open: a question absent from
this table has **not** been settled, whatever the body of the document says
elsewhere.

| # | Question | Operator answer | What it entails |
|---|---|---|---|
| **Q10** | Does the B1–B14 list cover "a fair number of things I don't like"? | **It covers it — no irritant to add.** The **priority**, though, needs revisiting | No B15. Derived-severity scoring stops driving the order |
| **Q10 bis** | Which axis drives the order, then? | **Traceability of knowledge** — B3, B4, B5 first | Phase resequencing (§0.3) |
| **Q12** | Automatic lifecycle: route (a), (b) or (c)? | **(a) — two session natures**: agent trace, auto-closed without ritual, operator with ritual | Amends the covenant (C1) for the agent nature; drops Q1; opens Q15 |
| **Q1** | `last_observed_at`: acceptable observation or disguised auto-heartbeat? | **Derived from Q12, plus a free-standing question** (§0.2) | D5 becomes load-bearing. Its **corollary** ("exactly one" vs "all") stays open |
| **Q15** | *(new — asked by none of the three documents)* How does an agent session reach a terminal state? | **(3) — new terminal state in the 037 CHECK** | Migration **M-G** on the core; amends C7; invalidates the §1.3 finding |
| **Seq.** | P4.4 before P3? Q6 promoted into Phase 0? | **Yes to both** | §0.3 |

**Still open and blocking**: Q2, Q3, **Q6**, Q14 (Phase 0 exit); then Q4, Q5, Q7,
Q8, Q9, Q11, Q13, and Q1's corollary.

### 0.1 What (a) requires, and that the dossier hadn't covered

**The nature is DECLARED, not detected — so B8 does not block (a).** The server has
no way to tell an agent call from a human call: D4 says so in black and white,
`X-Brain-Session` is dead and `X-Brain-Agent` is a project declared by the client.
It doesn't need to. The nature becomes a `start` parameter and a **fourth nullable
column** on D1, on the exact doctrine of the other three (`NULL` = "opened before
M-A", no backfill). This is the regime already in force elsewhere: ledger provenance
is "declared by the client, not cryptographically proven", and the identity guard
"does not authenticate the client" (C3). (a) therefore doesn't require solving B8
first — it requires taking on one more declaration.

**The default is forced by C2; it isn't a matter of taste.** The two declaration
errors don't cost the same:

- an agent that declares itself `operator` opens a ritual session that never closes:
  that's **B1 unchanged**, and the sweep already knows how to handle this case;
- an operator session that leaves as `agent` auto-closes without ritual: `summary` and
  `next_focus` are **never written**, and the only non-derivable judgment channel is
  lost **without a trace**.

The first is recoverable, the second isn't. **Default = `operator`; `agent` must be
declared explicitly.** An implementation that flips this default violates C2
(fail-closed on endings), and violates it silently — the worst form.

### 0.2 Q1 drops, and becomes a different answer per nature

A session of agent nature has no ritual, hence no `brain_session_heartbeat` — an
explicit command. Its **only** liveness signal becomes `last_observed_at` (D5).
"Acceptable observation or disguised auto-heartbeat?" therefore no longer applies to
this nature: it is the liveness signal **by construction**, and this isn't a slipped
covenant, since answer Q12 has just explicitly amended it for this nature. For the
operator nature, nothing moves: `last_heartbeat_at` remains the only trace of the
explicit command, and D5 remains pure observation.

Two consequences. **(i)** D5 stops being a comfort feature: it becomes load-bearing
for the agent nature, and `BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED` stops being a
precautionary killswitch and becomes the operating condition of half the model.
**(ii)** Q1's **corollary** doesn't drop and remains whole: "exactly one" or "all"
when several sessions match (actor, project) — on a population measured on
2026-08-19 of **24 `open` sessions out of 29** logged in a project that holds at
least two.

### 0.3 Resequencing — the "knowledge traceability" axis flips the plan

The PLAN was ordered by derived severity. Under the chosen axis, it's inverted:

- **B3** (18% capture, 34% attribution) becomes pain point #1 — and it was closed by
  D8 / **Phase 4.4**, the second-to-last slice, behind Q6 (covenant amendment) *plus*
  a 14-day soak;
- **B6** (focus discipline) occupied **all of Phase 3** and its M-D migration, even
  though it drops down the ranking.

**New order: P0 → P1 → P2 → P4.4 → P3**, then the remaining activations (4.1, 4.2-3,
4.5, 4.6) in the order of PLAN §6.

P1 stays first and this isn't negotiable: 4.4's writer strictly depends on the
per-tool project resolver **and** on `started_by_actor`, both born in Phase 1 (PLAN
§6.4.4 and §3.6). Moving 4.4 ahead of P1 would strip it of its `project_key`.

**Q6 is promoted to a Phase 0 exit criterion**: it now gates the slice that
immediately follows Phase 2, no longer an end-of-plan slice. The Phase 0 exit
criteria become **Q2, Q3, Q6, Q10 and Q14**.

Secondary benefit, not sought but real: pushing Phase 3 back pushes back the window
during which the recovery attestation is red — M-D creates its constraint trigger
**disabled**, and `brain-v42-v4.sql:913-918` requires `tgenabled = 'O'` of every
expected trigger.

### 0.4 Q15 — the terminal state of agent sessions (new question)

None of the three documents asked this question, because none had reread the
terminal CHECK with a ritual-free closure in mind.
`brain_sessions_terminal_state_valid`
(`alembic/versions/037_session_lifecycle_v4.py:14-91`, reread on 2026-08-19)
requires:

```
status = 'ended'      →  summary IS NOT NULL AND btrim(summary) <> ''
                         AND next_focus IS NOT NULL AND btrim(next_focus) <> ''
                         AND focus_outcome IS NOT NULL
status = 'abandoned'  →  summary IS NULL AND next_focus IS NULL
                         AND cardinality(captured_knowledge_ids) = 0
                         AND abandonment_reason IS NOT NULL
```

**A session cannot reach `ended` without a non-empty summary AND a non-empty
`next_focus`, and this is guaranteed in the database, not by convention.** A
ritual-free auto-closed agent session therefore has **no terminal state available**
in the 037 machine. Three routes were put forward:

| Route | What it costs |
|---|---|
| (1) end in `abandoned` | Zero migration. But the CHECK forces `captured_knowledge_ids = {}`: the actual ledger survives (`brain_session_artifacts`, PK `knowledge_id`, FK CASCADE), while **the terminal snapshot of every successful agent session would declare zero captures** — on the main capture path, and under the very axis the operator just chose. And `abandoned` would mean three things at once: swept ghost, operator giving up, agent that succeeded |
| (2) server-synthesized summary | Zero migration too. But this is **route (c) through the back door**, with the objection already on file: measurable state copied into the one non-derivable judgment channel (C9, `FocusArg` doctrine) |
| **(3) new terminal state — CHOSEN** | Migration **M-G** on the state machine itself. Amends C7 |

**Operator answer: (3).** What it entails, and what **remains to be written**:

- an **M-G migration** extending `brain_sessions_terminal_state_valid` with a terminal
  branch, with its fail-closed downgrade. The state's **name**, its exact branch, and
  the fate of `captured_knowledge_ids`, `abandonment_reason` and `focus_outcome` in
  that branch **are not specified here**;
- the **double rail**: the Pydantic branch moves with the CHECK, never after (C7);
- the **pin lane** (C10, ticket `c60d023d`): M-G is a head, sequenced with the bump of
  `_REQUIRED_ALEMBIC_HEAD` and its test **in the same commit**. *Amended on 2026-08-20
  (§0ter.1): this head is SHARED with M-A — one, not two;*
- the **regeneration of both** `ops/recovery/` v4 **assets** — the `brain_sessions`
  terminal CHECK's fingerprint is part of that;
- the **037→036 downgrade**, already fail-closed, must learn the new state;
- an open question that (3) doesn't close: does an agent session ended in this new
  state apply a `next_focus`? If not, it doesn't do a CAS, and **that's good news** —
  otherwise every auto-close would bump `focus_revision` and produce systematic
  `focus_outcome = 'conflict'` results on concurrent operator sessions.

**M-G touches the core that accretion was built not to touch.** This is the declared
price of the pair (a) + (3), accepted by the operator, and it invalidates a finding
of §1.3 — corrected on the spot.


---

## 0bis. Operator framing — session 2 of 2026-08-19

Second session, the same day. It **refines Q12 = (a)** on a point the first hadn't
made explicit — opening — and **settles the last four Phase 0 blockers**. Same-day
measurements, read-only: head `045`; `brain_sessions` 324 `ended` / 117 `abandoned`
/ 28 `open`; `tickets` 231, of which **187 self** (`from_project = to_project`) and
**44 cross-project**.

> **CAVEAT added 2026-08-20 — these three numbers don't add up, and none is corrected
> here.** `324 + 117 + 28 = 469`, whereas the rest of the document measures **467
> rows** (§1.1, §3.3, folding pass). And this line's `28 open` contradicts the **29**
> cited six times elsewhere (§0.2, §0bis.2, §0ter.2, §1.1, §3.3). Both gaps are real
> and **unresolved**: the 2026-08-19 measurement can no longer be replayed, the table
> having moved since. **Derive no reasoning from this line** — replay it. *Nothing
> depends on it: §0bis's conclusions rest on the "24 out of 29" ratio, not on this
> total.*

| # | Operator answer | What it entails |
|---|---|---|
| **Opening** | **`start` becomes AUTOMATIC.** One single explicit gesture — the *claim* — marks the session as human | **Flips the default nature** (§0bis.1). Amends C1 a second time: the server now also OPENS |
| **Q9** | *(settled by measurement, not by arbitration)* Subagents **inherit** the carrier's session | §0bis.2. No tag to set: they share the connection |
| **Q1 corollary** | *(dissolved)* "exactly one" becomes true **by construction** | §0bis.2. The opening key is `(project, connection)` |
| **Timeout** | A **claimed** session is **never** closed for inactivity. Traces: generous threshold, own setting | §0bis.3 |
| **Q2** | **`from_project`** — authorship | Capture answers "what did this session produce". Unblocks M-B |
| **Q14** | **(a)** widen `knowledge_sources` to tickets, and its predicate to the subtree | The attestation stays green and keeps proving what it claims. **On BOTH v4 assets** |
| **Q3** | **Proposal's storage, ticket payload shape** | Append-only `(session_id, seq)` for idempotent replay; `progress` + `blocker\|null` + `next_step` in **one call**. (a) and (b) are **dissolved** (§0bis.4) |
| **Q6** | **Accepted**, and a trace's unsigned drafts **survive** in a waiting pool | §0bis.5. New sub-question, born from auto-closing |

**PHASE 0 IS UNBLOCKED**: Q2, Q3, Q6, Q10 and Q14 are answered. Still open, but
**none blocks Phase 0**: Q4, Q5, Q7, Q8, Q11, Q13.

### 0bis.1 Automatic opening flips the default nature

**Correction of a §0.1 derivation, to read before applying it.** §0.1 concludes
"default = `operator`, forced by C2". That was only right **under the assumption
that `start` remains an explicit command**. Under automatic opening, that conclusion
flips: if every auto-opened session were born `operator`, every agent tool call
would create a ritual session that never closes — **B1 industrialized**.

**New default: `agent` (trace).** And the danger §0.1 was trying to avoid — losing
the judgment channel without a trace — doesn't come back, for a clean reason: **the
claim IS the declaration of intent to write judgment.** No claim ⇒ nobody expects a
summary ⇒ auto-closing destroys nothing. C2's rule is therefore honored in a
different form, and §0.1 isn't contradicted in principle, only in its application.

**The claim is RETROACTIVE** — at any point while the session is open, not just at
opening time. This is what removes the last failure mode ("I forgot to claim at the
start"). It **promotes** a trace into `operator`; it does not build a session.

**A broader covenant amendment than §0's:** the server no longer just auto-closes,
it **auto-OPENS**. C1 is amended a second time. The objection filed in §3.3 against
automatic opening ("B8 proves it would misattribute under stdio") is thereby
significantly weakened over HTTP — see §0bis.2 — but **stands whole under stdio**,
which has no headers at all. Production = HTTP loopback; stdio = dev/fallback. To be
written as a known degradation, never passed over in silence.

### 0bis.2 The connection key — measured, already wired in, and read by nobody

The question the operator asked was: *how do we easily tag subagents?* **The
measured answer is that we can't, and shouldn't.** What the server sees on every
call, verified in the code on 2026-08-19:

| Header | What it actually carries | Distinguishes a subagent? |
|---|---|---|
| `X-Brain-Agent` | **The project.** `~/.claude.json` sends `${PWD}`, which `normalize_agent` reduces to the basename; the repo's `.mcp.json` sends the literal `brain-v42` | **No** — identical string for the operator, its agents and its subagents |
| `X-Brain-Session` | Dead: `normalize_session → None` in the nominal case (B8) | **No** |
| `Mcp-Session-Id` | **The connection**, struck by the **SERVER** (`uuid4().hex`, `streamable_http_manager`) | **No — and that's exactly the property we want** |

**No signal distinguishes a subagent from its carrier, and none ever will**: headers
come from the client's configuration, which is **per MCP server**, not per subagent.
Tagging would require an upstream capability that doesn't exist — same class as B8.
**So subagents inherit** (the answer to Q9), for free, because they share their
carrier's connection.

**Three properties fall out of this:**

1. **`Mcp-Session-Id` is the only one of the three identifiers the client does NOT
   DECLARE.** It is struck server-side. Opening automatically on this key means
   attributing on the one non-forgeable signal in the chain — markedly sturdier than
   the "declared, not proven" regime that governs everything else.
2. **It distinguishes two Claude Code windows on the same project**, which
   `X-Brain-Agent` cannot do. `config.py` already carries this reasoning regarding
   stateless mode: "without a connection identifier, four engines launched in the same
   directory declare the same actor and collapse into ONE row."
3. **Q1's corollary dissolves.** "Exactly one" vs "all" no longer applies: on a
   `(project, connection)` key, it is exactly one **by construction**. The measured
   ambiguous population (24 `open` sessions out of 29 in a project with ≥2) stops being
   a problem to arbitrate.

**It's already there and nobody reads it.** `provenance.py` defines
`normalize_transport`, `set_current_transport` and `get_current_transport`; the
middleware calls `set_current_transport()` on **every** call; the value goes out to
the metrics sidecar (`metrics/client_observation.py`). And
**`get_current_transport()` has ZERO call sites** anywhere in the repository —
measured. The needed discriminant is wired, fed, and unused.

**Production precondition, verified**: `mcp_http_stateless` defaults to `False`
(`config.py:229`) and **no drop-in and no `.env` overrides it** — the server
therefore runs stateful and the connection identifier exists. In stateless mode
there is none: that's the documented fallback lever, and it **would break this
key**. Treat this as a hard precondition, not a configuration detail.

### 0bis.3 The trace timeout

Operator constraint: *"I want a good timeout, so that unfinished sessions don't get
cut off mid-way — that must not happen."*

**The main guarantee isn't the threshold, it's the nature.** A **claimed** session
is **NEVER closed by the inactivity timeout** — that is the very meaning of the
claim. Only the existing 7-day sweep can take it, and that's exactly what it was
built for. The feared risk is therefore **structurally impossible** on the
operator's sessions, not merely unlikely.

The timeout applies only to traces, with three guarantees:

1. **Never while a call is in flight.** The machinery exists: `provenance.py` tracks
   call depth (`enter_call` / `exit_call` / `is_outermost_call`). Forbidding the
   literal "cut off mid-way" costs nothing.
2. **The threshold counts OBSERVED inactivity**, and `last_observed_at` moves on
   *every* tool call. A session that is being worked on never approaches it.
3. **Getting cut off costs a SPLIT, not a loss.** The closed session's ledger is kept;
   the next activity opens a new trace. The worst case is cosmetic.

**Proposed value: 4 hours of observed inactivity, on its OWN setting** —
deliberately not the 900 s of `MCP_HTTP_SESSION_IDLE_SECONDS`, which governs a
network object and has no business driving a knowledge object. 4 h survives a lunch
break and any plausible thinking gap, and stays 42x shorter than the 7-day sweep, so
ghosts don't pile up. ~~**Proposed, not signed**~~ — the operator asked for
"generous" without giving a number.

> **AMENDED on 2026-08-20 — §0ter.5 is authoritative.** The number is **signed**, but
> its reading has changed: housed in the nightly sweep (resolution (d) ratified), the
> 4 h value is an **ELIGIBILITY threshold** evaluated once per night, **not a closing
> delay**. A trace that goes inactive right after a pass stays alive until the next
> one: real worst-case latency ≈ **28 h**. The "42x shorter than the 7-day sweep"
> above therefore compares two eligibility thresholds, not two delays. The three
> numbered guarantees, though, hold unchanged.

### 0bis.4 What Q3 loses, and what it becomes

Q3 had four sub-decisions, two of them trapped — (a) the circle of callers and (b)
the heartbeat effect and its arming mechanism, which D4 flagged as arming **by
omission**. **Both dissolve under Q12 = (a) + automatic opening.**

The danger D4 named was: *the checkpoint refreshes `last_heartbeat_at`, the sweep's
only signal, so an agent that checkpoints alone keeps its session alive indefinitely
— the fake-alive `2bd14b24` condemns — and makes criterion 4.3 self-satisfying*.
Under the new model, an agent session's liveness comes from `last_observed_at`,
which moves on **every** tool call. The checkpoint stops being special: it's one
observation among others, and an active agent keeping its session alive is the
**correct** behavior. On the `operator` side, the timeout never bites at all.
`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` therefore no longer applies.

**The checkpoint changes nature**: it stops being a liveness mechanism and becomes a
**pure judgment object**. It moves back to the right side of §1.3's FACT/JUDGMENT
grid, and its sole job becomes B7 again — semantic freshness.

**Answer on what remained, and it is deliberately mixed:**
- **(c) storage — the proposal.** Append-only, `UNIQUE(session_id, seq)`, `ON CONFLICT
  DO NOTHING`: replaying a retry is **idempotent**, which the ticket's
  `expected_checkpoint_revision` CAS doesn't give. Agent retries are the norm
  (invariant C6), not the exception.
- **(d) payload shape — the TICKET.** `progress` + `blocker|null` + `next_step`
  published **together, in one call**, against three mutually exclusive `kind` values
  on a single note. Reason: three `kind` values allow emitting a `progress` without
  ever a `next_step`, and the freshness reader can't tell whether the snapshot is
  complete. Divergence (d) is therefore **abandoned**; the storage one (c) is **kept
  and accepted**.

### 0bis.5 Q6 — accepted, and unsigned drafts survive

**Q6's technical fragility disappears.** The draft writer had to link its
observations to a session via `(project_key, started_by_actor)` and the "exactly
one" rule — fragile against a population with 24 ambiguous out of 29. **On the
connection, the link is exact** (§0bis.2). Q6 loses its main implementation risk
before it's even armed.

**But "the server prepares, the operator signs" assumes an operator, and a trace
auto-closes with nobody there to sign.** This sub-question is born from auto-closing
and appears nowhere in the dossier.

**Answer: unsigned drafts SURVIVE** their session's auto-close, in a pool awaiting
signature, outside any session. Nothing is lost; nothing is attributed without a
human gesture. Both alternatives were explicitly ruled out: auto-promotion on
auto-close would be **E3 for the entire agent half of the model** — a full covenant
change, not the half-step Q6 proposed — and discarding unsigned drafts would amount
to destroying exactly what the agent nature produces, under a priority axis that is
knowledge traceability.

**What this adds to M-E, still to be specified**: a draft must be able to outlive
the session that observed it. The `status` CHECK `∈ {staged, promoted, dismissed}`
holds, but the FK to `brain_sessions` and its `ON DELETE`, the pool's lifetime, its
cap, and the out-of-session signing tool are **not** specified.

---

## 0ter. Operator framing — session of 2026-08-20

Third session, the next day. It settles no new question: it **signs the three last
questions §0bis had opened by resolving the previous ones**, plus the four
implementation resolutions that had been *proposed* but never actually submitted
(decision `23bf6088`), plus the trace threshold that §0bis.3 explicitly left
"proposed, not signed". Origin decision: `c5160259-a33a-4dfc-b343-992746604b7a`.

**These three questions blocked the first line of code.** They no longer do.

| # | Operator answer | What it entails |
|---|---|---|
| **(a) Heads** | **M-A and M-G ship as ONE SINGLE head.** | One regeneration of both v4 assets, **one** production rollout, **one** pin bump. Above all: no more window where `agent` sessions exist without a reachable terminal state (§0ter.1) |
| **(b) stdio** | **NO AUTOMATIC SESSION AT ALL under stdio.** The explicit `start`/`resume`/`end` cycle stays available there, unchanged | Auto-opening exists only under **HTTP**, on the `(project, connection)` key. Known degradation, **written down**, not just endured (§0ter.2) |
| **(c) Guard** | **`expected_client_key` REMOVED from the connection-resolved path, KEPT everywhere else** | The new path doesn't carry a key that guards nothing. The five existing explicit paths (`resume`, `capture`, `heartbeat`, `end`, `abandon`) don't move (§0ter.3) |
| **(d) Resolutions** | **The FOUR resolutions from `23bf6088` RATIFIED as proposed** | Fail-open; trace closing in the nightly sweep; `nature IS NULL` under the 7-day regime; covenant phrase rewritten by nature (§0ter.4) |
| **Threshold** | **4 h SIGNED — but under a reading that has changed** | **ELIGIBILITY** threshold for the nightly sweep, never a closing delay. Real worst-case latency ≈ **28 h**. Amends §0bis.3 (§0ter.5) |

**THE MINIMAL SLICE IS UNBLOCKED** — M-A+M-G as one head, auto-opening, the *claim*
tool, inactivity closing, all behind a closed flag. **`SPEC M-G` becomes writable**:
it was only waiting on (a). Still open, and none blocking: Q4, Q5, Q7, Q8, Q11, Q13.

> **Hard precondition carried forward**: `mcp_http_stateless=False`. The entire
> connection key depends on it. It isn't re-signed here because it was never put in
> question — but it remains the single point whose flip would bring down both (b) and
> (c) at once.

### 0ter.1 One head, and why the procedural argument gives way

The two arguments weren't of the same nature, and that's what settled it. "Two
heads" is **procedural** — the grain of the pin lane, one head at a time, each with
its bump and its test in the same commit. "One head" is **functional**: `M-A`
shipped alone is an *inert and unhealthy* delivery, because the `agent` sessions it
gives birth to have no reachable terminal state until `M-G` is there (§0.4).

Yet the lane forbids **two heads in flight** (§5.3). Two heads therefore don't mean
"two small steps" but **two sequential production rollouts**, with, in between,
exactly the window the functional argument condemns. The procedural argument,
applied here, produces the very risk it is supposed to reduce.

**What the single head entails, and must be held together**: both `ops/recovery/` v4
assets regenerated in one pass — the terminal CHECK fingerprint **and** the closed
`expected_session_indexes` list, which `M-A`'s UNIQUE connection index breaks
(fourth mechanism, §5.2 *(ii)*); a single bump of `_REQUIRED_ALEMBIC_HEAD`; and the
Pydantic double rail (C7), which moves **with** the CHECK, never after.

### 0ter.2 stdio: no session at all, and the degradation is written down

§0bis.2 had left the point open: the B8 objection against auto-opening "stands whole
under stdio, which has no headers at all." Two outcomes were possible — no session,
or falling back to `(project, actor)`.

**The fallback is ruled out** because it would reintroduce exactly the ambiguity the
connection key dissolves: the measured **24 of 29 `open` sessions** logged in a
project holding at least two (2026-08-19). It would attribute on **declared** data —
an `X-Brain-Agent` header the client chooses — where the entire value of the model
is to attribute on the one **non-forgeable** signal, the server-struck
`Mcp-Session-Id`.

**What is lost, and is written down rather than kept quiet**: auto-opening cannot be
exercised locally under stdio. Development of this feature therefore happens against
the HTTP loopback transport, not against the fallback. This is acceptable because
production **is** HTTP loopback and stdio is the dev/fallback path — but it is a
real degradation of development comfort, not a configuration detail.

### 0ter.3 The guard removed where it guards nothing

`expected_client_key` prevents **mistargeting between parallel sessions**: you
address a UUID, you prove you know the key that goes with it. It has never
authenticated anyone, and the contract already says so ("this is an isolation guard,
not authentication").

On the connection-resolved path, **mistargeting is impossible by construction**:
there is no UUID being addressed, the connection *is* the identity. Keeping the key
there would force the new path to carry a token that protects nothing — noise in a
contract that is precisely being simplified.

**It stays exactly as is on the five explicit paths** (`resume`, `capture`,
`heartbeat`, `end`, `abandon`), where the UUID is addressed and the risk exists.
None of these contracts move, and that is intentional: signing doesn't make the
guard useless, it establishes that a new path doesn't need one.

### 0ter.4 The four resolutions, ratified as proposed

They had been **proposed and never actually seen** — decision `23bf6088` carried
them as recommendations, not as settled. They are now signed, without amendment:

| Resolution | Argument retained |
|---|---|
| **Auto-opening FAIL-OPEN** | Cost asymmetry: a hiccup in the database must not bring down the whole MCP server. Same posture as the client-activity emitter (`1c40c36a`), whose failure can never break the call it observes |
| **Trace closing in the NIGHTLY SWEEP** | Zero new machinery, and guards already **proven**: re-evaluation under row lock, confirmed CLEAN by the night-of-19-to-20 audit (21/21 sessions, zero false positive, focus intact) |
| **`nature IS NULL` stays under the 7-day sweep regime** | Don't change the rules retroactively. Sessions predating `M-A` have no nature; subjecting them to the new threshold would judge them under a contract they never knew |
| **Covenant phrase REWRITTEN by nature, not removed** | The contract must stay readable **where the agent reads it** — in the tool's docstring. Accepted consequence: anchor test `0207209` will go red. That is intended, and it is the Red move that opens the delivery |

**The price of fail-open, written once and for all**: if automatic opening fails and
the call goes through anyway, artifacts created before the successful opening fall
**outside the `created_at >= started_at`** capture window. **B5 therefore bites
again, occasionally.** This isn't a side effect discovered after the fact: it is the
accepted cost of not bringing the server down, and `SPEC M-G` must carry it.

### 0ter.5 4 h: signed, and its meaning has changed

§0bis.3 proposed "4 hours of observed inactivity" and concluded with "**Proposed,
not signed**". The number is signed. **Its reading, though, is no longer the same**,
and that's the point not to miss.

Under resolution (d) — closing housed in the **nightly sweep** — 4 h stops being a
delay. It's an **eligibility threshold** evaluated once per night: a trace that goes
inactive right after the nightly pass becomes eligible four hours later, but **stays
alive until the next pass**. Real worst-case latency is on the order of **28 h**,
not 4.

**Never announce "4 h" as a closing-delay guarantee.** §0bis.3 is amended on this
point: its three guarantees (never while a call is in flight, observed inactivity
rather than wall clock, splitting rather than loss) hold unchanged, and the main
guarantee remains the nature — **a `claimed` session is never closed for
inactivity**, whatever the threshold.

Q5 (sweep thresholds) and this threshold will be answered **together**, in the same
nightly statement: separating them would produce two competing passes over the same
table for two rules that describe the same gesture.

---

## 1. Context

### 1.1 What exists (verified in the code on 2026-08-18)

**Sessions** — v4 lifecycle (migrations 032 + 037, in production since 2026-07-24):
seven explicit MCP commands (`start`/`list`/`resume`/`capture`/`heartbeat`/`end`/
`abandon`), a double-rail state machine (SQL CHECK 037 + Pydantic), a non-blocking
focus CAS (`applied`/`conflict`), an exclusive attribution ledger (PK `knowledge_id`
in `brain_session_artifacts`), idempotent replays. The **covenant** (CLAUDE.md, the
seven tools' docstrings): these commands are explicit user gestures; the sole server
exception is the 7-day `auto_stale_7d` sweep. Production state **measured on
2026-08-18** (the dream unit's `killswitches.conf` drop-in inspected — rule N6
applied to this very document): the sweep has been **armed in DRY since 2026-08-18,
by operator decision** (`BRAIN_DREAM_SWEEP_ENABLED=true`,
`BRAIN_DREAM_SWEEP_DRY_RUN=true`, 18 sweepable ghosts measured that day); WET has
never been armed. **This "18" is already stale — re-measured on 2026-08-19
(read-only): 29 `open` sessions, 21 of them sweepable past seven days** (`count(*)
filter (where status='open')` and the same filter on `last_heartbeat_at < now() -
interval '7 days'`, over 467 rows). A dated and **perishable** figure: replay it
before any arming decision, never copy it forward — not even from this paragraph. An
earlier version of this paragraph copied "never armed" from a 2026-08-16 ticket —
the state had changed before the document was finalized, exactly the failure mode N6
forbids.

**Projects** — four scattered building blocks: the format
(`src/brain_v42/models/project_key.py`, strict canonicalization on write / tolerant
on read, aliases `brain`/`brain_v42` → `brain-v42`), three tables —
`project_contexts`, the actual operational object, created by **001**
(`alembic/versions/001_initial.py`, "8. Create project_contexts table"); `projects`
and `project_aliases`, the registry, created by **033**, which only adds key
immutability and alias normalization to `project_contexts`, via triggers (measured:
`project_contexts_project_key_immutable_trigger`,
`project_contexts_project_alias_trigger`) — an earlier version of this line
compressed the three into "migration 033", a regression from the DOSSIER, which
states it correctly; and a colon convention (`red-shrik:agent`).

The subpartition predicate `base NOT LIKE '%:%' AND key LIKE base || ':%'` lives in
**five hand-copied instances** in `src/` — two more than an earlier version of this
paragraph claimed, which had nonetheless congratulated itself for fixing "everywhere
but one spot." The inventory, re-verified on 2026-08-18, grep by grep:
1. `db/project_group_scope.py:24-26` — the reference implementation;
2. `services/project_group_ticket_service.py:129-137` — inline SQL copy;
3. `services/proposal_service.py:377-383` — inline SQL copy even though the module
   imports `project_group_scope`;
4. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — **the same
   semantics in a `split_part` variant** ("the key contains a colon AND its prefix is a
   base key of the group"), invisible to a grep on `not_like("%:%")`;
5. `services/project_group_ticket_service.py:164-167` — **a second copy, in Python**
   (`project_key == base_key or (":" not in base_key and
   project_key.startswith(f"{base_key}:"))`), in the very method
   `_lock_participants_scope` as copy #2.

On the database side, these aren't "two views from migrations 024 and 036" but
**seven live views, all coming from 036** (measured: `select table_name from
information_schema.views where table_schema='public' and view_definition like
'%split_part%'` → `codex_brain_entity_v1`, `codex_feature_artifact_v1`,
`codex_feature_v1`, `codex_roadmap_curation_proposal_v1`,
`codex_ticket_extraction_proposal_v1`, `codex_ticket_message_v1`,
`codex_ticket_v1`). They are born from **two copied CTE bodies** in
`alembic/versions/036_codex_contract_views.py` (`_RED_KEYS_CTE:23-45` for six of
them, `_BRAIN_RED_KEYS_CTE:205-227` for `codex_brain_entity_v1`), both written as
`split_part(project_key, ':', 1) <> project_key AND split_part(…) IN red_base` — a
**different** wording from the one in `src/`. 024 isn't a second living object: it
had put down `codex_brain_entity_v1` (`024:80`), which 036 replaces with `CREATE OR
REPLACE VIEW` (`036:230`). Real total: **five `src/` instances + two CTE bodies
serving seven views**, in three distinct wordings — graft A's thesis ("ad-hoc
semantics drift") is proven even stronger than the document said. The dream spec
`dbb7c5ce`, for its part, filters on strict equality.

**Observation** — `ProvenanceMiddleware`
(`src/brain_v42/mcp/provenance_middleware.py`) reads `x-brain-agent` and
`x-brain-session` on every tool call, sees the actual tool behind `brain_call_tool`,
persists the actor in `access_log.actor` (`String(64)`, migration 041) — and feeds
neither heartbeat, nor ledger, nor liveness.

### 1.2 The proven pains (instruction dossier, condensed)

| # | Pain | Evidence | Severity |
|---|---|---|---|
| B1 | Declarative lifecycle ≠ ephemeral subagents: 39 ghost sessions in one cleanup | ticket `2bd14b24` | **Critical** |
| B2 | `last_heartbeat_at` lies both ways on the same day (false-dead for a live session + 39 false-alive) | ticket `2bd14b24` | **Critical** |
| B3 | 18% of closed sessions capture, 34% of artifacts get attributed — while the middleware sees everything | ticket `7ffe0e8a`, 2026-08-16 measurements (perishable) | **High** |
| B6 | Focus content discipline rests on the agent alone; the CAS only guarantees the arithmetic | ticket `d30cf6e5` | **High** |
| B8 | `X-Brain-Session` is dead (`normalize_session → None` nominal): client-side session identity is impossible | 2026-08-06 spike, verdict "JOIN IMPOSSIBLE" (`docs/upstream/2026-08-06-claude-otlp-session-join.md`), relayed by `7ffe0e8a` — ticket `2dfbb83d`, for its part, was closed SHIPPED on 2026-08-16, not negative (citation corrected) | **High (constraint) — RE-MEASURED AND CONFIRMED on 2026-08-19.** An earlier version of this cell said "scored on a STALE measurement" (spike on 2.1.220, `claude --version` at 2.1.234): **the replay was done**, on 2.1.234, and the verdict is **unchanged** — `docs/upstream/2026-08-19-b8-session-join-rejeu.md`. Both cases were replayed: intact parent environment ⇒ the identifier received is the **PARENT's** (false positive reproduced); `CLAUDE_CODE_SESSION_ID` removed ⇒ literal unexpanded, `normalize_session → None`. Control `${PWD}` correctly expanded: the mechanism works, the variable just doesn't exist at the right time. B8 is therefore no longer scored on stale data |
| B11 | Colon sub-projects: **86 artifacts out of 533 have NO nightly run at all**; for the six keys, the parent never sees the child (strict equality). `red-shrik:agent` (312) always outweighs its parent (245) | spec `dbb7c5ce` §2 **re-measured** on 2026-08-18 + dream pool read from the drop-in | **Medium** (revised down — see below) |
| B4 | Non-capturable tickets (`CAPTURE_TABLES` = six tables); mixed batch fail-closed as a block | lived through `d30cf6e5` + code | Medium |
| B5 | Rigid capture window: exact project + `created_at >= started_at` | code `pg_brain_session.py` | Medium |
| B7 | No semantic checkpoint; freshness is silence | ticket `d04dc588` | Medium |
| B9 | Free-form `client_key`, mechanical proliferation post-terminal | ticket `2bd14b24` | Medium |
| B12 | Projects system with no end-to-end doc | ticket `d30cf6e5` | Medium |
| B10 | Underscore ghost drift — closed, but load-bearing: "the wrong key is impossible to persist" must never regress | learnings `7bc821a1`, `367e27ae` | Historical |
| B13 | `_validate_captures`'s error is undifferentiated: rejected ids are listed but there is a **single aggregated reason** for all of them (verified) | code + panel | Medium |
| B14 | `brain_sessions` doesn't persist the actor who starts it, even though `access_log.actor` has existed since 041 | code + panel | Medium |

**B11 was scored on state that was ten days stale — correction.** An earlier version
carried forward "479 artifacts outside consolidation" from spec `dbb7c5ce`
(2026-08-08 measurements) without rereading the live pool. Yet the remedy that spec
proposed first — "either add the six keys to the pool" — is **2/6 executed since
2026-08-10**: the `killswitches.conf` drop-in (read 2026-08-18) carries
`BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…`. Re-measured
today (`group by project_key` over the five knowledge tables): `red-shrik:agent`
312, `red-lab:architect` 135 — **447 of the 533 colon artifacts, i.e. 84%, now get
their own night**. The residue with no run at all is **86 artifacts** across four
keys (`red-lab:orchestrator` 64, `:reviewer` 15, `:sentinel` 5, `:developer` 2).
What remains entirely unsolved for the six, though, is **cross** consolidation: the
pipeline filters on strict equality, so `red-lab`'s night will never see
`red-lab:architect`, even if it's in the pool. That's the half the shared predicate
and `include_descendants` (D3/D10) address — not the number 479, which has melted
away. And the pool is **at its cap of ten**: adding the four remaining keys requires
raising `_MAX_POOL` **and** `TimeoutStartSec` together (PLAN 4.6 phrases this
conditionally, wrongly — it's already the situation).

The operator announced "a fair number of things I don't like" **without detailing
them**: this list is derived from the evidence and must be submitted to them for
confirmation, prioritization and completion (open question #10).

**One operator answer is already SETTLED and frames this entire document** (ticket
`2bd14b24`, 2026-08-06): a session serves **knowledge traceability**, and "the
lifecycle must be automatic, not declarative" — which ultimately dooms the current
model where both opening AND closing are declarative acts. Three mechanism routes
were presented to them ((a) two session natures — agent trace auto-closed without
ritual, operator with ritual; (b) no ritual at all anymore, non-derivable judgment
migrated to a dedicated object; (c) auto-end with a server-derived summary). **Route
(a) WAS CHOSEN by the operator on 2026-08-19** (§0, Q12): two session natures. This
paragraph used to say "none was chosen — they explicitly reserved this choice for
themselves" and that was accurate through 2026-08-18 inclusive; leaving it as-is
after the framing session would have been this dossier's classic mistake — a
snapshot that lies the moment the subject moves.

What this changes for reading the rest of the document: the accretion described
below keeps the declarative lifecycle **only for the `operator` nature**, no longer
"on a transitional basis" for everyone. The instrumented signals (observation,
checkpoint, sweep) remain sound — that was the plan's bet and it holds — but D5
stops being optional: it becomes the **sole** liveness signal of the `agent` nature
(§0.2). Two invariants are amended accordingly, C1 and C7.

An earlier version of this document presented these routes as discarded
alternatives, "confirmed by the panel" — that was a slipped covenant call, corrected
at the time (see §3.3). The choice recorded here, though, comes from the operator.

### 1.3 The structuring finding (the insight the panel kept from B)

Of the fourteen pains, **eleven closed without touching the 037 state machine or the
covenant** — that was true of the plan as originally proposed, and **the 2026-08-19
framing session invalidated it**: Q12 = (a) amends the covenant for the agent
nature, and Q15 = (3) opens the M-G migration on the terminal CHECK itself (§0.4).
**This count of eleven has not been recomputed** and must not be cited as-is:
recomputing it is part of Phase 0's content. What survives of the finding, and
remains true, is narrower — and it's the essential part: the core (states, CAS,
exclusivity, idempotence) is sound and proven in production; it's its **blind
periphery** that hurts: freshness signals, capture validation, focus observability,
error ergonomics. And the reading grid that organizes all of it (carried over from
proposal A on the panel's advice): **every object in the model is either a FACT the
server observes, or a JUDGMENT a human declares — never both.** The server never
writes judgment; the agent never writes a fact the server already observes.

---

## 2. Decision

Evolve **through instrumented accretion**, without rewriting the core, across ten
components. Every new runtime behavior is born behind a **closed** killswitch, armed
only by a documented operator gesture; anything that touches the covenant is
**submitted for approval**, never slipped in.

> **Amended by the 2026-08-19 framing session (§0) — read this preamble with its
> limit.** "Without rewriting the core" no longer quite holds: Q15 = (3) adds
> migration **M-G**, which extends the terminal CHECK 037 with a branch. The core is
> not *rewritten* — CAS, exclusivity, idempotence and the double rail are intact — but
> it is **extended**, and by deliberate target choice, not by repair necessity.
> Likewise, "submitted, never slipped in" has been kept: the covenant was indeed
> submitted (Q12) and it was **amended**, for the agent nature alone. Lastly, the ten
> components have become **eleven**: D11 (session nature) is born of the framing
> session, and D1 gains a fourth column.

**D1 — Identity and intent at the source (closes B14, B9-triage, accepted B8
constraint).**

> **AMENDED on 2026-08-20 — §0bis.2 and §0ter.2 are authoritative.** This graft is
> kept as it was submitted; **its linking key is stale**. It states further down
> "**The identity that works is `(project, actor)`**". That's no longer true: the
> retained key is **`(project, CONNECTION)`**, struck server-side (`Mcp-Session-Id`),
> hence **non-forgeable** — where `X-Brain-Agent` is declared by the client. Falling
> back to `(project, actor)` would reintroduce the ambiguity the connection key
> dissolves: **24 of the 29 `open` sessions** logged in a project holding at least two
> (measured 2026-08-19).
>
> **Corollary on stdio, signed on 2026-08-20**: stdio no longer "degrades" to "no
> attribution" — it **opens NO automatic session at all**. The explicit
> `start`/`resume`/`end` cycle stays available there, unchanged (§0ter.2).
>
> **What stays true**: B8's load-bearing role and its date, `access_log` having no
> project column, and the fact that `started_by_actor` remains useful — but as a
> session **attribute**, no longer as an opening **key**. Its indexing decision has in
> fact changed target (§0bis.2, decision `c3d09355`): it's the CONNECTION column that
> must be indexed.
Three nullable columns on `brain_sessions`, outside the 037 state machine, without
backfill (040/041 doctrine: `NULL` = "before"):
- `started_by_actor VARCHAR(64)` — the `X-Brain-Agent` actor observed at `start`.
  **Width 64, not 128**: aligned with `MAX_ACTOR_LENGTH = 64`
  (`src/brain_v42/provenance.py:23`) and `access_log.actor` `String(64)` — a fix
  requested by the panel, none of the three proposals had checked this point.
- `last_observed_at TIMESTAMPTZ` — last server observation (see D5). **Not** a
  heartbeat: `last_heartbeat_at` remains the only trace of the explicit command.
- `intent VARCHAR(500)` — a human-written line, "why this session", an optional
  `start` parameter, shown in `list` (graft C): it's the intent missing from ghost
  triage, not a `client_key` convention.
- **`nature` — fourth column, added by the 2026-08-19 framing session (Q12 = (a), see
  §0.1 and D11).** Nullable like the other three, without backfill (`NULL` = "opened
  before M-A"). **Declared** by the client at `start`, never detected: B8 makes
  detection impossible and (a) doesn't need it. **DEFAULT `agent` — corrected by
  framing session 2 (§0bis.1).** This paragraph first said "default `operator`, forced
  by C2", which was correct **as long as `start` stayed explicit**; under automatic
  opening this default would spawn a ritual session on every agent call, i.e. B1
  industrialized. The judgment channel isn't lost for all that: **the claim is the
  declared intent to write it**, and it is retroactive.
> ✅ **MEASURED AND SETTLED on 2026-08-19** (`baseline/README.md`), **and it changed
> target.** Under the `(project, connection)` key of §0bis.2, the D5 emitter no longer
> filters on the actor: `started_by_actor` drops off the hot path, becomes
> informational (`list` display, triage) and **needs no index**. What replaces it is
> more serious: **the connection column MUST carry a UNIQUE index**. That day's
> `EXPLAIN` shows that an uncovered equality forces a **Seq Scan of the whole table**
> (63 buffers versus 2 for an index scan), and it's the server's hottest path.
> Uniqueness, as a bonus, has the database **enforce** the "exactly one by
> construction" property the framing merely asserts. Measured in passing: the
> correlated counting subquery was consuming **27 of the 36 buffers** in the plan and
> **disappears** under this key. Caveat: at 469 rows everything is sub-millisecond —
> the conclusion is about the plan's SHAPE, not an observed slowdown.
>
> *Original text, which posed the question:*

**INDEX decision to be investigated, added 2026-08-19 — the word "index" was absent
from this document as from the PLAN.** `started_by_actor` is created **without an
index**, and it's the column D5 filters on at **every outermost tool call**, twice
in the same statement (the `WHERE` clause and the correlated counting subquery that
implements "exactly one"). `brain_sessions`'s real indexes, measured on 2026-08-19,
are exactly three — `brain_sessions_pkey` (on `id`),
`uq_brain_sessions_project_client (project_key, client_key)` and
`idx_brain_sessions_project_status_started (project_key, status, started_at DESC)`:
**none covers the actor**. The third carries the `(project_key, status)` prefix and
leaves the actor equality as a residual filter, which at the measured cardinality
(467 rows, 29 `open`) is plausibly free — *plausibly* isn't *measured*, and it's a
hot path with a precedent of loss (the client-activity burst loss, `1c40c36a`,
tracked and not closed). This document **does not decide**: it requires that Phase 0
measure D5's statement against the real table and that M-A's commit carry the
written conclusion. Three consequences to know before making it: (1) an index on
`brain_sessions` breaks `expected_session_indexes`, the attestation's CLOSED list
(§5.2, fourth mechanism), **on both v4 assets**; (2) deferring it to its own head
costs one more production rollout in a lane that forbids two heads in flight (§5.3)
— so if an index is needed, it travels **inside M-A**; (3) deciding nothing amounts
to shipping the emitter on a hot path whose cost has never been measured. The
identity that works is `(project, actor)`: `X-Brain-Session` is dead (B8, 2026-08-06
spike "JOIN IMPOSSIBLE") and **`access_log` has no project column** (verified in
`db/tables.py`: `entity_type, entity_id, access_type, accessed_at, actor`) — every
link goes through `started_by_actor`, never through `access_log` alone. **B8's
load-bearing role, and its date — not to be lost from sight.** This isn't one pain
among fourteen: it's the constraint that *sizes* the `(project, actor)` fallback,
hence D1 (this column), D5 (the emitter, `skipped{no_actor}` under stdio), D8 (draft
linkage), R5 and N2 (the "exactly one" ambiguity rule), D6's named residue, and
Phase 4.1's first exit criterion. Yet it is **scored on a stale measurement**: the
spike says "Version measured: Claude Code 2.1.220" while `claude --version` returns
**2.1.234** on 2026-08-19, and both its sources (`2bd14b24`, `7ffe0e8a`) equally
demand "re-measure on every version bump". None of the three documents scheduled
this re-measurement, and the Phase 0 baseline never measured the session header:
PLAN §2 (item #6) now carries it as a **step**, with its exit criterion. Until the
spike is replayed, writing "B8" means "B8 as measured on 2.1.220". The risk is
asymmetric — an `X-Brain-Session` coming back alive would invalidate no deliverable,
it would open an option this plan ruled out by constraint — which justifies
continuing to design under B8, not ceasing to date it.

**D2 — Errors that explain (closes B13, half of B4).** Capture's
`BrainSessionInputError` becomes structured: `rejections: [{id, reason}]` with
`reason ∈ {not_found, wrong_project, created_before_session, ambiguous_type,
attributed_elsewhere, unsupported_type}` + `capturable_subset` (graft C) listing the
ids that would go through. The batch stays **all-or-nothing** (a partially applied
batch has two possible stories — replay semantics forbid it) but the agent replays
the valid sub-batch in a single informed call.

**D3 — Capturable tickets + family window (closes B4, a brick of B5/B11).** An 8th
value, `ticket`, in the `brain_session_artifacts_type_valid` CHECK (which already
counts seven — `decision, learning, snippet, runbook, adr, indexed_plan, legacy`,
verified in `db/tables.py`; the PLAN enumerates them correctly, this line wrongly
said "7th") and in the validation: predicate `tickets.from_project ==
session.project_key AND created_at >= started_at` (`from_project` = authorship,
consistent with "self-tickets are valid"; `to_project` in open question #2).
Parent→child capture of a subpartition (`pk` captures an artifact of `pk:child`)
goes through **THE shared subtree predicate** — a single implementation, which
**consolidates the FIVE `src/` instances inventoried in §1.1** (graft A spoke of
"lone divergence"; the re-verified inventory proves its thesis even stronger: three
SQL copies, one `split_part` variant and one Python copy, plus two CTE bodies
serving seven views on the database side — PLAN Phase 2 §2 carries the resorption
scope, also corrected: it targeted two and missed two, one of them inside the very
function it claimed to be cleaning up) — behind a closed flag.

**D4 — The checkpoint as a gesture (closes B7, fills B2's fake-alive gap).**

> **AMENDED on 2026-08-20 — §0bis.4 is authoritative, and `SPEC-checkpoint.md` derives
> from it.** This graft is kept as it was submitted to the panel; **two of its claims
> are stale** and must be read as such.
>
> 1. **THE HEARTBEAT EFFECT IS DISSOLVED.** The graft says "**Side effect: refreshes
>    `last_heartbeat_at`**" on a real checkpoint, never on a replay. Under Q12 = (a) +
>    automatic opening, an `agent` session's liveness comes from `last_observed_at`,
>    which moves on EVERY tool call; the checkpoint stops being special, and an
>    `operator` session is never closed for inactivity.
>    `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` **no longer applies** and isn't shipped.
>    **The checkpoint neither writes to nor touches `last_heartbeat_at`** — not on a real
>    checkpoint, nor on a replay. The spec turns this into a test for the ABSENCE of an
>    effect.
> 2. **THE PAYLOAD SHAPE HAS CHANGED.** `kind ∈ {progress, blocker, next_step, handoff}`
>    + a single `note` is **abandoned** in favor of `progress` + `blocker|null` +
>    `next_step` published **together, in one call** (§0bis.4, divergence (d) carried
>    over from ticket `d04dc588`). The append-only storage, `UNIQUE(session_id, seq)` +
>    `ON CONFLICT DO NOTHING`, on the other hand, **is kept**.
>
> **What stays true in the graft**: the append-only trigger in the database, the
> client-supplied monotonic `seq`, exact-replay idempotence, the fail-closed
> 200/session cap, and the calling-policy fork — which remains open, but whose danger
> (the fake-alive) has vanished along with the heartbeat effect.

Major graft from C, promoted to the plan's core by all three judges: explicit tool
`brain_session_checkpoint(session_id, expected_client_key, seq, note ≤2000, kind ∈
{progress, blocker, next_step, handoff})`. Append-only table **guaranteed by a
database trigger** (house culture: 039 pins by SHA256, not by absence of a code
path), client-supplied monotonic `seq`, `UNIQUE(session_id, seq)` + `ON CONFLICT DO
NOTHING`: **an exact checkpoint replay is idempotent** — no second row, and the
heartbeat isn't refreshed a second time (agent retries are the norm, an invariant of
the dossier). Fail-closed 200/session cap. ~~**Side effect: refreshes
`last_heartbeat_at`** on a real checkpoint, never on a replay.~~
**AMENDED 2026-09-02 — heartbeat effect REMOVED, see §0bis.4.** This side effect is
not shipped, and the sentence above is struck rather than deleted so the change
stays legible. An `agent` session's liveness comes from `last_observed_at`, which
every tool call moves; the checkpoint is a pure JUDGMENT object and writes no
heartbeat — not on a real checkpoint, not on a replay. The flag
`BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` is not shipped either: a flag with no
purpose is debt, not a precaution. The clause above about the heartbeat "not being
refreshed a second time" on a replay is void for the same reason — there is no first
refresh. Delivered in commit `37aaabb`; the absence of effect is a TEST
(`tests/unit/services/test_brain_session_checkpoint.py`), not a comment, because
until this amendment an implementer reading D4 before §0bis.4 would have wired
exactly what the spec forbids. **The calling policy
is a fork, declared, not slipped in**: if every checkpoint remains an explicit user
command, the covenant stays intact — but adoption then depends on the same human
discipline that produced **24 stale sessions out of 29** (re-measured on 2026-08-19;
this paragraph used to cite "21 out of 23", a 2026-08-16 measurement, and "21" today
denotes the count of those sweepable past 7 days, not the stale ones) (2026-08-16
measurement), and "the checkpoint dates the living" (closing B2's fake-alive gap)
only holds **if it's actually called**; if instead an agent can checkpoint
spontaneously in a long autonomous session — the use case that motivates B7 — that's
a session mutation outside an explicit command, hence a **covenant change**:
extending the circle of callers, submitted in open question #3.

**Correction: this fork has NO arming mechanism, and PLAN rule R3 settles it in
advance without seeing it.** R3 exempts the checkpoint from a killswitch on the
grounds that "it's a user command, gated by operator decision" — that is, by
treating as settled the branch Q3(a) declares open. Material consequence: the
delivered artifact is **identical under both answers**, there's nothing to arm or
disarm, and nothing server-side distinguishes an agent call from a human call (B8:
`X-Brain-Session` is dead, `X-Brain-Agent` is a project and stays client-declared).
Yet the checkpoint **refreshes `last_heartbeat_at`**, the sweep's only liveness
signal: an agent that checkpoints alone keeps its session indefinitely alive — the
fake-alive `2bd14b24` condemns — without violating a single shipped constraint, and
makes criterion 4.3 "zero abandonment of a recently checkpointed session"
self-satisfying through its own writes. **A design consequence, to be settled in
Q3(a) BEFORE shipping**: either the heartbeat effect is removed from the contract
(the checkpoint stays dated judgment, liveness stays heartbeat + observation), or
the tool is born behind a `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` flag, shipped
closed, the fork's sole armable object. Shipping the heartbeat effect without either
of the two arms the covenant change by omission. **And removing it isn't a return to
the ticket** (2026-08-19 reread): `d04dc588` states "A real checkpoint refreshes
heartbeat atomically; a replay doesn't." Dropping the effect would be a **third**
divergence from its MVP — to be declared as such, never presented as the
conservative option.

`d04dc588`'s **freshness** doctrine, kept as-is: freshness derives from the age of
the last checkpoint; focus drift is exposed separately and is **never** a cause of
staleness. **Two divergences from the ticket's MVP, not just one** — an earlier
version of this paragraph only declared the first, while claiming to keep the
doctrine "as-is":
- **Storage**: the ticket recommends a snapshot on `brain_sessions` + CAS
  `expected_checkpoint_revision` + exact replay with no new timestamp; here,
  append-only + `(session_id, seq)`. Reason: append-only keeps the history of notes
  (it's judgment, N7 grid), and the ticket's P0 properties (exact replay with no
  double effect, non-destructive conflict) are regained via the key instead of the
  CAS.
- **Payload shape** (an undeclared divergence until now, reread in the ticket): its
  contract is `brain_session_checkpoint(session_id, expected_client_key,
  expected_checkpoint_revision, progress, blocker|null, next_step)`, with a bounded
  response distinguishing *activity, milestone, blockage, freshness, focus_context*,
  and the explicit criterion "**one call**". The proposal's `kind ∈ {progress,
  blocker, next_step, handoff}` + a single `note` turns three semantic fields
  **published together** into three **mutually exclusive** natures: publishing
  progress + blockage + next step would require three calls, three `seq`s, three
  heartbeat refreshes, and would break the "one call" criterion. This isn't a
  serialization detail, it's the contract the ticket's audit requires specifying
  before any code ("the smallest admissible batch is still documentary: 1. a separate
  checkpoint spec"). **Neither the ADR nor the PLAN delivers this spec**: it's added
  to Phase 0's content, and Q3(c) now carries both divergences.

Gated on the explicit product approval this ticket requires (open question #3).
Shipping the tool brings the covenant to **eight** commands: covenant phrase in the
8th docstring, CLAUDE.md enumeration and the Phase 0 anchor test extended in the
same commit.

**D5 — Activity observation, fail-closed and submitted (closes B2's false-dead
case).** The middleware gains an emitter behind
`BRAIN_SESSION_OBSERVED_ACTIVITY_ENABLED=false`: on the outermost call of a tool
carrying a resolvable `project_key` and a normalized actor, **a single UPDATE** sets
`last_observed_at = NOW()` on the `open` session for `(project_key,
started_by_actor)` — **only if exactly one** matches (a scalar counting subquery in
the same statement, specified in the PLAN — the panel required this specification).
Zero or multiple matches ⇒ no write, `observed_activity_skipped{reason}` counter.
**This statement is a hot path and its cost is not measured**: it filters on
`(status, project_key, started_by_actor)` twice — once for the UPDATE, once for the
correlated count — on every outermost call, on a column created **without an index**
and not covered by the three existing indexes (see D1, 2026-08-19 measurement). The
index decision is covered under D1 and **measured in Phase 0**, with its consequence
for `expected_session_indexes` (§5.2).

**Sizing correction — the middleware does NOT see the project.** An earlier version
of this component (and of D8) said "the
middleware already sees both". That's true for the actor, false for the project.
`src/brain_v42/mcp/provenance_middleware.py:74-96` is complete and only reads
**headers**: `get_http_headers(include={'mcp-session-id'})`,
`normalize_agent(x-brain-agent)`, `normalize_session(x-brain-session)`,
`normalize_transport(...)`, three ContextVars, the re-entrancy guard, `call_next`.
It never inspects `context.message.arguments` (`grep -rn '\.arguments'
src/brain_v42/mcp/` only returns `dream_capabilities.py:250,258`). The only code in
the repository that resolves a project from a tool's arguments is the dream
capability layer (`services/dream_project_scope.py`), and it does so via a
**PER-TOOL policy table** (`PROJECT_TOOL_POLICIES:83-120`:
`project_key`/`project_keys`/`owner_project_key`, `inject_project_key`, typed
references to resolve against the database). Resolving a project is therefore
per-tool work, not an available piece of data. **Consequence for scope**: D5 and D8
each gain an explicit delivery brick — a **per-tool project resolver**, which must
either reuse `PROJECT_TOOL_POLICIES` (a single table, consolidation doctrine), or
state why it stands up a second one; tools outside the table are **not** observed
and count `skipped{no_project}`. Without this brick, the emitter has no `:pk` to put
in its UPDATE, and Phase 1 wouldn't compile. The emitter is wrapped on the proven
model of client-activity (ticket `1c40c36a`): its failure never breaks the call it
observes, and a **fault-injection test** proves it (graft A, explicit exit
criterion). **Assumed and submitted covenant boundary**: I contend that this is an
observation (like `access_log.actor`), not a heartbeat — but the question is put to
the operator (#1) and the killswitch stays closed until it is settled.

**D6 — The three-signal sweep, still ONE statement (closes B2 on arming, B1 in
steady state).** The 7-day sweep predicate becomes `GREATEST(last_heartbeat_at,
COALESCE(last_observed_at, last_heartbeat_at)) < cutoff`, still re-evaluated under
row lock in the single `UPDATE … RETURNING` (the invariant carved from the
2026-08-06 false-dead incident). Structural elegance emerging from the synthesis:
the checkpoint (D4) refreshes `last_heartbeat_at`, so **all three signals — explicit
heartbeat, checkpoint, observation — fit in two columns and one statement**. **But
the "three signals, not just one" rule is NOT an intrinsic property of the
predicate**: it depends on two independently refusable arming decisions. If Q1=no
(observation never armed) and/or Q3=no (no checkpoint), `GREATEST(...)` degrades to
heartbeat alone — precisely the "arm the sweep as-is" that §3.3 rules out. Hence the
hard precondition, carved into the PLAN (4.3): **arming WET requires Q1=yes with 4.1
armed and soaked, AND Q3=yes with the checkpoint shipped; failing that, arming WET
again becomes a fresh operator decision, made by explicitly naming the degraded
heartbeat-only mode** — never a default chaining. DRY, for its part, is already
armed (operator decision of 2026-08-18, on the current heartbeat-only predicate —
measured, §1.1). The predicate change goes in the only safe direction: it can only
abandon *fewer* sessions, never more. **Accepted and bounded residue** (weakness #1
flagged by the technical judge on B): a session forgotten under an actor still
active on the project becomes durably alive under observation. Three bounds: (1) the
"exactly one" rule — as soon as a new session from the same carrier opens on the
project, the old one stops being observed and becomes sweepable again;
**symmetrically, the new session — alive, actively worked on — also stops being
observed** (count=2): under ambiguity, NO session of the carrier is observed, and a
live session becomes sweepable again at 7 days if its carrier neither checkpoints
nor heartbeats; the same exposure applies to headerless stdio sessions
(`skipped{no_actor}`, B8 constraint). The false-dead case therefore only becomes a
predicate impossibility for OBSERVED sessions — the residue is named and measured,
not denied (see §4 and PLAN 4.1); (2) `list` exposes `observed_only` (recent
observation but neither heartbeat nor checkpoint for 7 days) for human triage; (3)
both the count of `observed_only` sessions AND the count of open sessions under
ambiguity that are sweep candidates are soak measurements published BEFORE any WET
arming. **Order of magnitude already measured, and cited by none of the three
documents** (2026-08-19): ticket `7ffe0e8a` gives, as of 2026-08-16, "Simultaneous:
`auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2" — a **partial**
measurement of the "≥2 open sessions" population. Partial because it counts per
project and not per `(project, actor)` pair: it's a **ceiling** on ambiguity in the
N2 sense, and it can only be refined after M-A, `started_by_actor` not existing yet.
Re-measured on 2026-08-19: `auto-discord` 8, `brain-v42` 4, `red-arena` 4, four
projects at 2 — i.e. **24 of the 29 `open` sessions** in a project holding at least
two. The "exactly one" rule therefore doesn't leave aside an edge case; at the
ceiling, it leaves aside the majority of the fleet, and that is exactly why the
residue is the 4.1 criterion and not a predicate impossibility. Dated and perishable
figures — replay them.

**D7 — Focus memory (closes B6 on recoverability; prevention remains an open
question).** **Second correction on the same spot — the first fix had replaced one
false premise with another.** This document first claimed "only two write sites";
the following pass corrected it to "six sites, of which only one bumps the revision,
the upsert overwriting the focus without a bump". **This second statement is also
false, and the database proves it.**

*What already exists, measured on 2026-08-18 in production (head `045`, read-only).*
Migration **032** — which the DOSSIER cites correctly and which the ADR had lost —
creates `increment_project_focus_revision()`
(`alembic/versions/032_brain_sessions.py:19-34`): "`IF NEW.current_focus IS DISTINCT
FROM OLD.current_focus THEN NEW.focus_revision := OLD.focus_revision + 1`", and the
trigger `project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus ON
project_contexts FOR EACH ROW`. It is still in place (`pg_get_triggerdef` reread
today). **No writer can therefore change the focus text without a bump**: an `INSERT
… ON CONFLICT DO UPDATE` fires the BEFORE UPDATE triggers, and the ON CONFLICT
branch of `pg_project_context.get_or_create` does put `current_focus` in its SET.
Cross-check that makes this visible: `grep -c focus_revision
src/brain_v42/repositories/pg_project_context.py` = **0**, same for
`scripts/scrub_xml_tool_call_leak.py` — four of the six sites (`create`, `update`,
`get_or_create`, `update_focus`) plus the scrub never name the column. **Precision
from the 2026-08-19 pass**: those that write via UPDATE do get the bump; `create`,
and `get_or_create`'s INSERT branch, write via INSERT — the trigger is `BEFORE
UPDATE`, it doesn't fire there, and the row is simply born at the column's default
value (`focus_revision = 0`, measured). There is nothing to bump at birth; there is,
however, a focus written with no trace (see M-D).

*The six sites, exactly.* Docstring of `src/brain_v42/db/focus_stamp.py` ("six call
sites across three modules"): `end`'s `applied` CAS (`pg_brain_session.py:713-714`,
which sets `focus_revision=expected_revision + 1` **explicitly** — the 037 CHECK
requires it, "`applied` ⇒ `focus_revision_at_end = end_expected_focus_revision +
1`"), `brain_update_project_focus` (`roadmap_service.py`, which also sets
`focus_revision + 1` explicitly), `pg_project_context.update`, `update_focus`,
`create`, and the live MCP tool `brain_set_project_context` (upsert). Add to that a
non-MCP writer, `scripts/scrub_xml_tool_call_leak.py` (`_PROJECT_CONTEXT_COLS =
("current_focus",)`): **seven writers in total**, six plus the scrub.

*Third correction on the same spot, 2026-08-19 — the second fix had left two false
claims behind.* It said "two sites bump explicitly; the other five get the bump from
the trigger; and `brain_update_project_focus` is **the only one** that bumps even
when the text does NOT change". Reread line by line:
- **BOTH explicit sites bump on unchanged text**, not just one.
  `_apply_focus_if_current` (`pg_brain_session.py:713-714`) sets
  `focus_revision=expected_revision + 1` **without comparing the text** — and the 037
  CHECK requires it (`applied` ⇒ `focus_revision_at_end = end_expected_focus_revision
  + 1`), even when the session closes on the same prose. The code comment names it:
  "Re-posting the previous prose verbatim is the copy-forward this column exists to
  expose". `roadmap_service.py` does the same to consume the CAS token of a
  blockers-only batch. Two writers, not one — and it's the **normal** regime of a
  session ending, not an edge case.
- **The trigger only sees UPDATEs.** `create` and `get_or_create`'s INSERT branch
  (`pg_project_context.py:51-71` and `:273-275`) persist a `current_focus` at the
  row's birth: no BEFORE UPDATE trigger fires there, the row is born at
  `focus_revision = 0` (column default, measured) and `focus_updated_at` is set there
  in Python, not by `focus_stamp`. Consequence for M-D, developed below: a context's
  revision 0 is a written focus that **nothing in the database** can force to be
  historized.
The exact statement is therefore: **two sites set the revision themselves, on
changed text as well as unchanged; four get the bump from the trigger when the text
changes under an UPDATE; and the two INSERT paths have neither a trigger nor a
revision to increment.**

*What the upsert really does.* Its ON CONFLICT branch rewrites `current_focus` —
**including to NULL when the argument is omitted**, verified — **without a CAS**:
this is a real overwrite channel, and it's the core of B6. But it **does bump** (032
trigger) and it **does date** (`focus_updated_at =
focus_stamp(excluded.current_focus)`, under `IS DISTINCT FROM`, so a focus moving to
NULL counts). It isn't mute; it's simply **not recoverable**, for lack of history.
That's what M-D must fix, and nothing else.

*The cited numeric evidence didn't prove what it was made to say.* An earlier
version said "this path already bites: 10 of 59 `project_contexts` have
`current_focus IS NULL`". The number is correct (re-measured today: `10/59`, head
`045`), the conclusion isn't. The **ten** rows are at `focus_revision = 0` **and**
`focus_updated_at IS NULL` (`perso`, `red-backup`, `red-cli`, `red-shrik:agent`,
`red-daemon`, `red-llm`, `red-tsdb`, `red-lab:developer{,-gemini,-opus}`). Revision
0 + never dated = focus **never written**, not focus erased — and an overwrite by
the upsert since 040 would have dated the column. **Zero production context carries
the signature of an erasure** (NULL with revision > 0). The channel exists in the
code; production shows no sign that it has bitten. This document therefore has **no
measured proof** to add to Q13's dossier, and says so.

The design covers **all** writers, under the same consolidation doctrine as the
colon predicate (a single implementation):
- **A single application write path** (shared function) through which the six sites
  and the scrub all pass: it inserts the history row **in the same transaction** as
  the focus write. It reads the revision **after** the write (`RETURNING
  focus_revision`) and historizes on that value — never on a precomputed revision.
  **Reason corrected on 2026-08-19**: an earlier version justified this point with "it
  doesn't need to bump, 032 already does it, and a second application increment would
  set `OLD+2`". `OLD+2` doesn't exist: the trigger **assigns** (`NEW.focus_revision :=
  OLD.focus_revision + 1`), it doesn't add — the value set by the statement is
  overwritten, not accumulated. **The proof is the plpgsql source, and it alone**:
  `alembic/versions/032_brain_sessions.py:19-34` writes `NEW.focus_revision :=
  OLD.focus_revision + 1` — an assignment, not a `+= 1` on the statement's value.
  *Corroboration withdrawn on 2026-08-19*: an earlier version added "and revisions
  advance by one step (dossier's CAS 209→210), not two." That CAS is a
  `brain_session_end` (DOSSIER §B6: two parallel sessions on snapshot rev 209), not a
  `roadmap_service` write, and nothing establishes that the focus TEXT actually
  changed — yet that is exactly the condition that makes the trigger speak. The
  example therefore couldn't settle either of the two hypotheses it was supposed to
  settle. The practical consequence is the opposite of what the false reason
  suggested: **both explicit bumps must STAY.** Removing them would break `end` — the
  trigger stays mute on unchanged text, and the 037 CHECK still requires `expected +
  1` — and would break the CAS token of a blockers-only batch. `RETURNING` remains the
  right rule because it holds true under both regimes, not because a double increment
  would be a threat.
- Append-only `project_focus_history` table, **guaranteed by trigger** (graft A on B's
  table): PK `(project_key, focus_revision)` — the generalized CAS's monotonicity
  makes it natural —, **`focus TEXT NULL`** (an erased focus is precisely the
  destructive overwrite the audit must be able to record), `actor VARCHAR(64)`,
  `source ∈ {session_end, focus_tool, context_upsert, generic_update,
  maintenance_scrub, migration_seed}` — the enum covers the real writers, `ON CONFLICT
  DO NOTHING` for replay idempotence. **Key reserve, to be worked out in M-D — resized
  on 2026-08-19**: the earlier version attributed this noise only to
  `brain_update_project_focus`'s blockers-only batches. **`brain_session_end` does the
  same, and it's its normal regime** — the CAS sets `expected + 1` without comparing
  the text, so every session ending that recopies the previous prose adds an identical
  focus row at the next revision. The PK stays unique (no collision), but the expected
  volume of content duplicates is that of session endings. Make it readable in the
  reading tool (flag "unchanged focus") rather than filter it out, and account for it
  in sizing.
- **The in-database guard proposed by the earlier version is WITHDRAWN.** It was meant
  to "refuse any UPDATE where `current_focus IS DISTINCT FROM` the old one without
  `focus_revision = old + 1`" — that is word for word what 032 already does, by
  setting the value instead of refusing it. Worse, it would coexist badly: PostgreSQL
  fires BEFORE ROW triggers in **alphabetical order** of their name. Named before
  `project_contexts_focus_revision_trigger` (e.g. `…_focus_history_guard`), it would
  see `NEW.focus_revision` not yet incremented and **would reject every focus write
  from the four writers that write via UPDATE without setting the revision
  themselves** (`update`, `update_focus`, the upsert, the scrub — `create` writes via
  INSERT and escapes both the guard and the trigger) — `brain_set_project_context`
  fail-closed in production on every focus change. Named after, it is trivially
  satisfied: dead code. In neither case does it protect anything. **What remains
  useful in the database**, and is M-D's real deliverable: a **deferred constraint
  trigger** (`AFTER UPDATE OF current_focus … DEFERRABLE INITIALLY DEFERRED`) that, at
  end of transaction, requires the history row at `NEW.focus_revision`. It has no
  twin, depends on no alphabetical order, and catches the writer that bypasses the
  shared path. It must be **scoped to `UPDATE OF current_focus`**: without that clause
  it would fire on any UPDATE of `project_contexts`, including the plan-index repair's
  (`plan_index_repair_store.py:294-308`, which only writes
  `plan_scan_paths`/`updated_at`), and would make it fail. **A gap named, not closed
  by this trigger — discovered on 2026-08-19**: `AFTER UPDATE` does **not** see
  INSERTs. `pg_project_context.create` and `get_or_create`'s INSERT branch write a
  `current_focus` at the row's birth (revision 0, column default). For every
  `project_context` created **after** M-D, revision 0 is therefore a persisted focus
  that no database guard forces to be historized — the migration's seed, for its part,
  only covers the 59 contexts existing at upgrade time. Three routes, to be settled
  when M-D is written: (a) extend the constraint trigger to `AFTER INSERT OR UPDATE OF
  current_focus` — the route that holds invariant N1 in the database, at the cost of a
  mandatory history row on every project creation (even when the focus is born NULL);
  (b) leave it on UPDATE and have the shared application path alone carry revision 0,
  documenting that the hard guard only starts at revision 1; (c) refuse to let
  `create`/`get_or_create` write a non-NULL focus at birth. **None is free, and
  deciding nothing amounts to choosing (b) without saying so** — that is, shipping an
  N1 invariant that is false for every new project.
- **Induced behavior change on `brain_set_project_context`, submitted**: its
  overwriting of the focus becomes recoverable and attributed; and it is proposed that
  the omitted `current_focus` argument **stop erasing** the focus (distinguish
  "omitted" from "explicit erasure") — open question #13, a zero-cost veto before M-D.
  To be settled **on the reasoning, not on a number**: production shows no victim of
  it.
The migration **seeds** one row per `project_context` with the current focus —
**NULL included** (`source='migration_seed'`): the anchor covers the 59 measured
contexts, not just the 49 with a non-null focus, and the seed cannot abort on a NOT
NULL constraint that no longer exists. Insert failure = failure of the entire focus
write: fail-closed, deliberately ("an audit that can stay silent proves nothing").
Read-only tool `brain_focus_history`. `end`'s result gains `focus_diff` (characters
added/removed vs the base focus — graft C): visibility first; a hard content guard
(shrinkage threshold) remains an open question (#7) since its threshold would be
arbitrary (two judges disqualified it as-is).

**D8 — Prepared capture, signed by the operator (carries B3 beyond suggestions —
gated covenant).** Major graft from C, corrected by the technical judge: table
`brain_session_staged_captures` (PK `(session_id, knowledge_id)` — a draft is a
hypothesis, **not** an attribution; only promotion into `brain_session_artifacts`,
PK `knowledge_id`, confers exclusivity), `status ∈ {staged, promoted, dismissed}`,
cap of 500/session (beyond that: observation stops and the
`staged_capture_skipped{overflow}` metric counter tracks it — counter location
specified, an answer to the panel's question). Middleware writer behind
`BRAIN_SESSION_STAGED_CAPTURE_ENABLED=false`, **linked via `(project_key,
started_by_actor)` under the same "exactly one" rule, never via `access_log`**
(panel fix: `access_log` has no project — and it's purged, see §3.2). **It therefore
depends on the per-tool project resolver specified in D5**: without it, this writer
has no more `project_key` than the observation emitter. Promotion goes back through
`_validate_captures` and only happens on an explicit command (`capture` or
`end(capture_staged=true)`). Before this mechanism, from the first phase onward:
read-only suggestions — `project_uncaptured_since_start` (≤20) in `end`'s result,
`project_uncaptured_since_start_count` in `resume`, never blocking. **The name is
the contract** (aligned on 2026-08-19 with the predicate specified in PLAN §3.2,
which doesn't link by actor): these artifacts are the **project's** since
`started_at`, not "this session's work"; calling them `uncaptured_candidates`
implied the opposite and would make the E3 measure read as a numerator instead of a
cap. **Documented horizon** (graft A kept by two judges): if the signed-promotion
rate plateaus, attribution at creation time (`method='auto_provenance'`, same
transaction as the artifact) is the structurally simplest route to close B3 entirely
— that's a full covenant change, informed by the quantified dossier this plan
produces (uncaptured candidates, `dismissed` drafts, ambiguity ratios), and left to
the operator to settle E3.

**D9 — Explicit and journaled reattribution (prepared answer to E9 — gated).** Graft
from A kept by all three judges, stripped of its trap (FK to a spans table that
doesn't exist here; A combined an owner CHECK with `ON DELETE SET NULL`, a
contradiction flagged by the technical judge): table
`brain_session_attribution_moves` — `knowledge_id`, `from_session_id`,
`to_session_id`, `reason TEXT NOT NULL`, `moved_by VARCHAR(64) NOT NULL`, `moved_at`
— with no trapped FK. Exclusivity becomes "a single current owner", history is fully
journaled. **Shipped only if the operator settles question #8 in favor of the right
to reattribute**; otherwise orphaning remains the price of proof.

**D10 — The projects system: document, anchor, predicate before column (closes B12,
protects B10, prepares B11).** Zero change to the projects core in this plan:
format, the 033 registry, key immutability, canonicalization — intact (B10 is an
anti-drift asset already won). Delivered: end-to-end doc `docs/PROJECTS_SYSTEM.md`
organized by the fact/judgment grid (graft A via the simplicity judge), anchor
tests, and **ONE** shared subtree predicate (D3). Doctrine settled for E5
(simplicity judge's graft): **the shared predicate for all reads BEFORE any
`parent_key` column** — the column + prefix CHECK designed by proposal A remains the
documented option if the operator steers E5 toward a real hierarchy; it only comes,
if ever, after proof of the predicate's usage. A read parameter
`include_descendants` (briefing, scoped search) is prepared behind a closed flag
(`BRAIN_READ_INCLUDE_DESCENDANTS_ENABLED`, added to the PLAN §8bis killswitch recap
— it was missing) as a closure brick for B11 without inflating the dream pool.
**Interaction with the dream capability scope, ARMED in production since 2026-08-10,
specified**: under a `(project, phase)` bearer, `brain_service` forces `project_key
= scope.project_key` under **strict equality** (verified in
`services/brain_service.py`; property measured at cutover — 751/2760 learnings
visible under `red` scope). Honoring `include_descendants` under scope would let
`pk:*` be read by a bearer scoped to `pk` — an expansion of an armed security
perimeter. Design rule: **under dream scope, the parameter is REFUSED fail-closed**
(explicit error, never silently ignored); widening a bearer to subpartitions would
be a separate operator security decision, out of this plan's scope. Arming the flag
and consolidating `red-shrik:agent` are operator decisions (question #4 — and it
alone: #5 is the sweep, an earlier cross-reference to it was wrong).

**D11 — Two session natures (born from the 2026-08-19 framing session, Q12 = (a);
closes B1 and B2's fake-alive case at the root, amends C1 and C7).** This component
does **not** come from the panel: it is the translation of the operator's answer to
Q12, and it is the only one in the document in that position. It is therefore the
least examined of the eleven — read it as an agreed target, not as a finished
design.

- **Session AUTO-OPENED on the `(project, connection)` key**, `agent` nature by
  default (§0bis.1). A single explicit and **retroactive** gesture, the *claim*,
  promotes it to `operator`. The connection key (`Mcp-Session-Id`) is the only
  identifier in the chain the client **does not declare** — it is struck by the server
  (§0bis.2), which places this link above the "declared, not proven" regime that
  governs everything else.
- **Subagents INHERIT their carrier's session** (answer to Q9), with no tag at all:
  they share its connection. This is not a comfort call but a measured constraint —
  none of the three headers distinguishes a subagent from its carrier, and none ever
  will, header configuration being per MCP server (§0bis.2).
- **`operator` nature**: nothing changes. The seven commands stay explicit, the
  closing ritual remains the only moment non-derivable judgment gets written,
  `last_heartbeat_at` remains the only trace of explicit presence.
- **`agent` nature**: no ritual. No `heartbeat` — liveness comes from
  `last_observed_at` (D5), which becomes load-bearing for it (§0.2). Auto-closes on
  observed inactivity (**4 h signed** as the nightly-sweep eligibility threshold, its
  own setting, §0bis.3 amended by §0ter.5), into the **new terminal state from M-G**
  (Q15 = (3), §0.4). The attribution ledger stays exclusive and immutable: that is the
  entire point of the agent nature under the "knowledge traceability" axis.
- **An `operator` session is NEVER closed by the inactivity timeout** (§0bis.3). This
  is the central guarantee the operator asked for, and it holds by NATURE, not by the
  threshold's value. Only the existing 7-day sweep can take it.
- **What is not settled and blocks writing M-G**: the state's name; the threshold and
  trigger for auto-closing (observed inactivity? a short-threshold sweep per nature?);
  whether an agent session applies a `next_focus` (§0.4 argues no); and the behavior
  of an agent session whose ledger is empty — does it need an equivalent of
  `nothing_to_capture_reason`, when no human is there to write it?
- **What D11 does not resolve**: Q9 (subagents — own session or inherit from the
  carrier) becomes sharper, not less. Under D11, "own session" means "a session of
  agent nature", hence nearly free; the arbitration shifts from cost to noise.

### Invariants preserved and added

**Ten of the dossier's twelve invariants are kept to the letter; two are amended by
the 2026-08-19 framing session** — an earlier version of this paragraph said "all
twelve", and that was true before the framing session. Kept: fail-closed on endings,
identity guard, non-blocking CAS, PK exclusivity, replay idempotence,
canonicalization/immutability, non-derivable focus, `_REQUIRED_ALEMBIC_HEAD` pin
(see §5), FK RESTRICT, single-statement sweep. **Amended, explicitly and by the
operator**:

- **C1 (covenant)** — the seven commands stay explicit **for the `operator` nature**;
  the `agent` nature opens declared and closes on its own (D11, Q12 = (a)). The 7-day
  sweep remains the only other server exception.
- **C7 (unrepresentable states are impossible)** — the 037 terminal CHECK gains a
  branch via M-G (Q15 = (3), §0.4). The property itself is **preserved**: this isn't a
  carve-out, it's one more state, made just as impossible to bypass by the same double
  rail. The Pydantic double rail moves with the CHECK, never after.

Added to these:
- **N1**: every persisted write to `current_focus` — from any of its **six write sites
  PLUS the maintenance script**, i.e. seven writers (an earlier version counted the
  scrub *among* the six, hence a "7th writer, tomorrow" that is actually the eighth) —
  leaves its history row in the same transaction, under a deferred constraint trigger
  in the database. The `focus_revision` **bump**, itself, is not an *added* invariant:
  it has held since 032 and is verified in production; N1 doesn't reinstate it, it
  takes it as given and attaches recoverability to it — an overwritten (or erased)
  focus becomes readable again with its provenance, and no future writer can historize
  silently. **Exact scope of the database guarantee, 2026-08-19**: the constraint
  trigger is `AFTER UPDATE`, so N1 only holds **in the database** for writes via
  UPDATE. Both INSERT paths (`create`, `get_or_create`'s INSERT branch) write revision
  0 outside its scope; until D7's route (a) is chosen, N1 there rests on the shared
  application path alone — a convention, exactly what N5 refuses elsewhere. Saying so
  is the condition for not believing the invariant to be stronger than it is.
- **N2**: an ambiguous observation or link (0 or ≥2 candidate sessions) writes
  **nothing** and increments a visible counter — the server never guesses.
- **N3**: a capture rejection names each rejected id **and its reason**, plus the
  capturable subset.
- **N4**: a `staged` draft is never an attribution; promotion goes back through
  validation and only happens on an explicit command; a rejection is journaled
  (`dismissed`), not deleted.
- **N5**: append-only data (checkpoints, focus history) is guaranteed by a database
  trigger, not by the absence of a code path.
- **N6**: every new runtime behavior is born behind its killswitch, shipped closed,
  with a test proving the closed-by-default state, and its production state is
  **measured** (drop-in inspected, process inspected — the client-activity "ARMED"
  lesson).
- **N7**: every new object is a fact OR a judgment, never both.

---

## 3. Discarded alternatives

### 3.1 Proposal A — "assumed overhaul" (discarded as a base; four grafts kept)

Splitting the session into three objects (`activity_spans`, `attributions`,
`engagements`), focus as a journal + materialized rendering, projects hierarchy in
the database (`parent_key` + CHECK), 4-5 new Alembic heads. Discarded by the panel
(scores 58/71/56) for **verified** reasons:
- **The plan as written doesn't apply**: the attributions migration declares an FK to
  `activity_spans` created a phase later; the "mandatory owner" CHECK makes its Phase
  2 exit criterion unreachable for any agent without an open session; and the
  combination of the owner CHECK × `ON DELETE SET NULL` on span purging is the classic
  trap that blocks any purge or violates the CHECK (technical judge).
- **Maximum exposure to the pin lane**: 4-5 heads coupled to `_REQUIRED_ALEMBIC_HEAD`
  where ticket `c60d023d` recalls that a frozen head has already produced four
  incidents, and a 046 planned on the same lane (two judges — the panel said "already
  queued"; verified: no 046 exists in `alembic/versions/`, see §5.5).
- **Churn with no paying pain point**: renaming `brain_sessions`→`engagements`,
  heartbeat turned into a silent no-op (lying to a client invoking an explicit
  covenant command is a contract breach, not a deprecation), a reattribution tool
  shipped while question E9 is explicitly unsettled (technical and simplicity judges).
- **Double focus representation** (journal + materialized rendering + a `compact` kind
  that reintroduces the rewriting the journal claims to forbid): the guarantee sold is
  weaker than the machinery paid for; the append-only audit gives recoverability
  without duality (simplicity judge).
- **Two hot writes per tool call** with a measured and unclosed loss precedent
  (client-activity burst loss beyond 8 concurrent calls): the overhaul reintroduced
  through the observation channel the very failure mode it wanted to kill.

**Kept from A** (with the judges' explicit approval): the fact/judgment grid (N7,
and the Phase 0 doc's grid); the subtree predicate implemented ONCE (D3/D10); the
`attribution_moves` without the FK trap (D9); the fault-injection test as an exit
criterion (D5); attribution at creation time as an E3 **horizon** informed by this
plan's measurements, not as a delivery (D8); the `parent_key` + CHECK design as a
documented option for E5, predicate first (D10).

### 3.2 Proposal C — "operator first" (discarded as a base; five grafts kept)

Three layers — observe/prepare/sign —, checkpoint, staged captures, presence
computed at read time, a focus-shrinkage guard. Discarded by the panel (scores
81/84/73) for:
- **Its central flaw is a false schema assumption, verified**: "presence computed at
  read time from access_log (activity of the (project, actor) pair)" doesn't work
  as-is — `access_log` has **no** project column; it would need to join `entity_id`
  against six tables by `entity_type` (expensive per `list` call) and coverage would
  be partial. "The 'no new writer' claim of its Phase 2 rests on an unverified schema
  assumption — precisely the flaw this proposal accuses others of" (technical judge).
- **And a second, more damning flaw this document had missed when readopting the
  mechanism**: `access_log` **is not a journal, it's a buffer continuously flushed
  out**. `repositories/pg_access_log.py:38-113` aggregates then executes
  `sa.delete(access_log).where(access_log.c.id <= max_id)` **in the same
  transaction**; the caller is `DecayFlusher`, periodic, `interval_seconds=300` by
  default (`config.py:379`). Aggregation additionally folds the actor into counters:
  the `actor` column doesn't survive the flush. **Measured on 2026-08-18: `select
  count(*) from access_log;` → 0 rows**, while `select max(last_accessed_at), count(*)
  filter (where access_count>0) from learnings;` → `2026-08-18 20:11:06+00 | 2209` —
  the write-aggregate-purge cycle runs fine. A third limit of the same order: the only
  writers are **reads** (`search_hit`, `get_by_id`, `use`, `execute` — four sites),
  never creations. A "7-day lookback" on this table therefore returns nothing.
- **~60% shrinkage guard**: a length heuristic on free prose in a fail-closed error
  path, arbitrary by its own admission — guaranteed false positives on a legitimate
  compaction (two judges).
- **Staged machinery shipped upfront** (a full state machine, cap, promotion,
  fail-closed downgrade on drafts — "paying the price of proof for an object that
  isn't one") plus 4+ flags to govern, while the arming decision itself hasn't been
  made (simplicity judge).
- **"Pin bumped in the same commit series"**: looser wording than "the same commit" —
  a head/pin desync window, exactly the `c60d023d` failure mode (technical judge).

**Kept from C** (with the judges' explicit approval): the full checkpoint with the
heartbeat effect (D4); `intent` + `open_sessions_same_carrier` (D1 and the PLAN);
`capturable_subset` (D2); staged captures **in Phase 4, gated covenant, linkage
corrected** (D8); `focus_diff` (D7); the principle "no tool whose sole reason for
existing is to please the machine".

**NOT kept, contrary to what an earlier version stated: presence computed at read
time.** This document used to announce keeping it "in a corrected and bounded form"
(opt-in, entity-by-entity join, lookback capped at 7 days) and turned it into the
comparison instrument meant to inform question #1 — the one blocking Phase 4.1. The
fix only addressed the missing project column; it missed that the table is **purged
every five minutes** and retains only reads (§3.2 above, 0-row measurement). An
instrument that structurally measures zero cannot inform a decision:
`with_observed_activity` is **removed** from Phase 1. What replaces it to inform Q1
is named in the PLAN's Phase 0 — the emitter's counters in "compute without writing"
mode, which measure exactly the population arming would touch, without writing
anything.

### 3.3 Underlying alternatives discarded (inherited from B, confirmed by the panel)

- *Status quo + arm sweep/auto-heartbeat as-is*: this would calibrate a signal ticket
  `2bd14b24` condemned ("a signal that lies both ways must be replaced").
- *Direct auto-capture into the ledger*: would violate explicit, immutable attribution
  on the strength of a heuristic; staging (D8) makes the heuristic reversible and
  signable, and informs decision E3 instead of making it.
- *Partial capture of mixed batches*: a partially applied batch has two possible
  stories — breaks replay semantics; the enumerated error + `capturable_subset` do
  better with no state ambiguity.
- *Server-side constraint on `client_key`*: would break every existing client over a
  sorting problem; `intent` + a list filter do the human triage.
- *Implicit server sessions / dropping the ritual entirely / derived summary*: **NOT
  discarded — these are routes a/b/c the operator explicitly reserved for themselves**
  (settled answer `2bd14b24`: lifecycle "automatic, not declarative"; see §1.2 and
  question #12). No panel has a mandate to settle what the operator reserved for
  themselves. This document only files two technical objections into this decision's
  dossier: the derived summary (route c) would copy measurable state into the one
  non-derivable judgment channel (`FocusArg` doctrine — a reservation already noted in
  the ticket itself), and B8 proves automatic opening would misattribute under stdio.
  The accretion proposed here is a transitional state compatible with all three
  routes, not a vote against them.
  > ✅ **Settled on 2026-08-19: the operator chose route (a)** (§0, Q12). The two
  > objections above keep their value and aren't moot: the one against (c) (copying
  > measurable state into the judgment channel, C9) **served again as-is** to rule out
  > route (2) of Q15, which was (c) through the back door; the one drawn from B8 turned
  > out to be **non-blocking for (a)**, the nature being *declared* at `start` and not
  > detected (§0.1). What wasn't examined — and what the choice revealed — is that (a)
  > has **no legal terminal state** in the 037 CHECK: hence Q15 and migration M-G
  > (§0.4).
- *Immediate hierarchy in the database*: shared predicate first, an eventual column
  after proof of usage (D10).

---

## 4. Consequences

**Positive.**
- Each slice is deliverable and testable on its own. Rollback, though, is
  **sequential, not independent**: the Alembic chain is linear with a single head
  (enforced by the pin test), so reverting M-A requires first downgrading M-D, M-C,
  M-B — whose downgrades are fail-closed as soon as a row exists, and it's the
  phase-exit canaries that create those rows. The PLAN therefore requires, **at EVERY
  phase that lays down a head**: canaries on disposable sessions, documented purging
  of canary rows, and a dry-run downgrade proving the window is still open before
  declaring the phase exited. **Correction: this rule was only written for Phase 2**
  (M-B/M-C) even though Phase 1 is subject to it too — its mandatory canary, "`start`
  with `intent` shows it in `list`", writes a non-NULL `intent`, and M-A's downgrade
  is fail-closed as soon as a non-NULL `intent` exists. The exit canary therefore
  permanently closed the rollback window of **the head M-C, M-D and the entire linear
  lane depend on**. The purge + dry-run-downgrade procedure is extended to Phase 1
  (and to Phase 3, whose canary on each writer writes rows outside the seed). The 037
  core and the sweep soak remain valid.
- The attribution rate becomes steerable (Phase 0 baseline → suggestions → gated
  staging → E3 dossier) instead of depending on agent virtue.
- Liveness becomes honest: three signals in two columns and one statement; for an
  **observed** session, the false-dead case becomes a predicate impossibility. **A
  corollary not to be confused with a soak measurement**: since it's a *predicate*
  impossibility, "zero observed sessions as sweep candidates" cannot fail — a recent
  `last_observed_at` implies `GREATEST(...) > cutoff` by construction, whatever the 14
  days of observation. An earlier version of the PLAN made this the **main** exit
  criterion of Phase 4.1, the one that unlocks WET arming: it's the same class of flaw
  the PLAN's §5 had correctly named for B6 ("a trivially satisfiable criterion"). The
  criterion that actually measures something is the other one: **the count of open
  sessions under ambiguity AND candidates for the sweep**, plus the
  `skipped{ambiguous}` ratio. Corrected in PLAN 4.1. Named residue (D6): a session
  under ambiguity (≥2 open from the same carrier — B1's ghost regime) or a headerless
  stdio session is never observed and stays exposed to the 7-day sweep as it is today,
  with checkpoint and heartbeat as its only defenses.
- Ghosts become triageable (`intent`, `started_by_actor`, `observed_only`, a warning
  at `start`) and then self-cleaning (sweep armed by the operator).
- An overwritten focus is one query away from recoverable, with its author; freshness
  stops being silence (`d04dc588` becomes closable).
- The measurements produced (uncaptured candidates, ambiguities, `dismissed`,
  `observed_only`) are exactly the dossier the operator needs to settle E1–E9
  knowingly: this plan usurps none of their decisions, it instruments them.

**Negative, accepted.**
- B1 is only half-closed as long as the subagent question (#9) and the
  automatic-lifecycle question (#12 — `2bd14b24`'s settled answer) remain unsettled:
  nothing server-side prevents a subagent from opening a session; the armed sweep
  turns the permanent regime into a self-cleaning one — that's the useful half.
- B3 will not climb to ~100% without attribution at creation time (E3 horizon, a
  covenant decision reserved for the operator).
- Two clocks coexist (`last_heartbeat_at`, `last_observed_at`): a readable but real
  duality, documented side by side — the way the two staleness thresholds already are
  in `models/brain_session.py`.
- Two to six Alembic heads — **two** firm (M-A, M-D) and **four** conditional (M-B
  pending on Q2 *and* Q14, M-C on Q3, M-E on Q6, M-F on Q8; the "three/three" count
  from an earlier version predated Q14 being opened). Each is a production rollout
  coupled to the pin — strict discipline required (§5).
- A session forgotten under an active actor remains durably alive under observation
  (D6 residue, bounded and measured).

---

## 5. Migrations and pin — sequencing rule (HARD constraint)

`_REQUIRED_ALEMBIC_HEAD = "045"`
(`src/brain_v42/maintenance/plan_index_repair_store.py:63`, guarded by
`tests/unit/test_plan_index_repair_head_pin.py`) makes the plan-index repair
fail-closed at the slightest unapplied head (ticket `c60d023d`). This plan's rule,
the strictest of the three proposals, required by two judges:
1. Every migration ships in **the same commit** as the batch below. Two successive
   versions of this document gave an incomplete recipe — "pin + test alone", then "pin
   + four documents" — each declaring itself "the complete coupling". **It is always
   less complete than what the repository demands, so an inventory of the guards was
   redone, listing them one by one, grep in hand**:
   - the pin bump and its test (`tests/unit/test_plan_index_repair_head_pin.py`);
   - **README** and **MCP_TOOLS**, which expect the string `migration {head}`
     (`test_documentation_contract.py::test_documented_migration_head_matches_repository`);
   - **ARCHITECTURE**, which does **not** expect that string but `migrations 001–{head}
     defined` — **em dash** included (same test, l.1839). Following the old recipe to the
     letter on ARCHITECTURE produced a red test;
   - **CLAUDE.md**, which is **outside the repository**: `git check-ignore -v CLAUDE.md`
     returns `.gitignore:74`, `git ls-files` doesn't contain it, and the test itself
     keeps its assertion behind `if CLAUDE:` ("CLAUDE.md is tracked only in the private
     archive", l.25-32). It must therefore be updated — that's the work contract — but
     **it can be in no commit at all**, and in CI the clause is mute. Counting it as "a
     commit document" is a category error;
   - **SCHEMA.md**: table count (M-C through M-F each add one, "32 public tables" and the
     revision count move) **and** the sentence "The repository target is {head}.",
     additionally pinned by `tests/unit/test_recovery_contract_v4.py:437-446` — a
     duplicate the repository itself documents as "easy to miss when inventorying
     guards", having cost "one more pass" across three consecutive rollouts;
   - **`docs/OPERATIONS.md:118`** ("The repository migration target is 045.") — absent
     from all earlier lists;
   - `tests/unit/test_recovery_contract.py:279`: `assert script.get_heads() == ["045"]`,
     a literal **inside a test named for revision 031**. It goes red on **every** bump,
     M-A as much as M-F;
   - `tests/unit/test_recovery_contract.py:292` and
     `tests/unit/test_recovery_contract_v2.py:33-39`: the frozen `table_set` is
     re-derived from `METADATA.tables` minus a hardcoded exclusion set. **M-C, M-D, M-E
     and M-F each add a table** — four bumps out of six turn these two tests red, and
     neither mentions pin or sessions;
   - the **rename of the head-named guard test**
     (`test_repository_head_045_is_documented_…`).
2. **The versioned `ops/recovery/` recovery attestation is impacted by three of the six
   heads — and no earlier version of this document ever said the word.** *(The "three
   of six" is only true if no head adds an index on `brain_sessions` — see point (ii)
   below; and "the attestation" is **two** files, see point (i).)*
   `ops/recovery/brain-v42-v4.sql` fingerprints exactly what M-A, M-B and M-D change,
   and the runbook requires of it "all statuses are pass" and "exactly 25 unique
   checks" (`tests/unit/test_recovery_contract_v4.py:480-486`).

**Two scope corrections, 2026-08-19 — there are TWO v4 assets, and FOUR breakage
mechanisms.**

*(i) The forgotten asset.* `ops/recovery/` also contains
**`brain-v42-v4-pgrestore.sql`**, the variant meant for a database restored via
`pg_restore` — the one behind the runbook's isolated proofs
(`docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`). This document, the PLAN and the
DOSSIER named it **zero times** (`grep -c pgrestore` = 0/0/0), all three writing
`brain-v42-v4.sql` as if the asset were unique. Yet it is **alive and tested**:
`tests/integration/db/test_recovery_contract_v4_execution.py:106` places it in the
`parametrize` **alongside** the live variant and runs **both** against a real
database in a READ ONLY transaction;
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` enforces **CTE parity** —
the allowed gap is exactly `{observed_artifact_constraints,
observed_session_constraints}`, and `not (_cte_names(live) - _cte_names(pgrestore))`
forbids a CTE being born on the live side without being born on the pgrestore side
too; `tests/unit/test_recovery_contract_v4.py:273-279` requires the runbook to
distinguish the two doors. And it carries **the same structures** M-A/M-B/M-D break:
measured on 2026-08-19, the pgrestore variant counts 12 rows carrying
`expected_runtime_user_triggers`, `observed_column_fingerprints`,
`expected_artifact_constraints` or `knowledge_sources` — 15 with
`expected_session_indexes`. **Consequence**: applying point 1's rule and the PLAN's
§8 to the letter would regenerate **one** of the **two** v4 assets, and CTE parity
would go red at the first CTE added. Wherever these documents say "regenerate
`ops/recovery/`", read **both v4 assets**. This is the **fourth** iteration of the
same flaw in this dossier — a guard inventory declared complete and incomplete; the
failure mode isn't forgetting, it's trusting a list that hasn't been re-grepped.

*(ii) The fourth mechanism.* These documents inventoried three ways to break the
attestation (column fingerprint, artifact CHECK, trigger list). There is a
**fourth**, and it targets `brain_sessions`: **`expected_session_indexes`**
(`v4.sql:404-412`) freezes the **CLOSED** list of the table's indexes —
`brain_sessions_pkey`, `idx_brain_sessions_project_status_started`,
`uq_brain_sessions_project_client`, each with its definition md5 — and
`session_constraint_mismatches` checks it **twice**: `:665` for expected indexes
that are missing or whose `md5(pg_get_indexdef(...))` moved, `:687` for indexes
**present but absent from the list**. An index added on `brain_sessions` is
therefore enough to make `session_constraint_mismatches > 0`, expected at 0. Doubled
on the unit side by `SESSION_INDEX_DEFINITION_MD5`
(`tests/unit/test_recovery_contract_v3.py:164-168`) and
`test_v3_pins_the_exact_session_index_set` (`:488`); the three md5s live literally
in **four** assets (`v3`, `v3-pgrestore`, `v4`, `v4-pgrestore`). This mechanism is
**dormant** as long as no head adds an index — one more reason to inventory it
before informing D1/D5's index decision, not after. **And "three of the six heads"
then stops being true**: if the index travels inside M-A, that's still three heads
but **two** structures broken for M-A; if it's deferred to its own head, that's
**four heads out of seven**, in a lane that forbids two of them in flight (point 3).

The three mechanisms already inventoried, head by head:
   - **M-A** — `observed_column_fingerprints` computes an md5 over the **complete**
     ordered column list of `brain_sessions`; three more nullable columns change this md5
     ⇒ `session_column_mismatches > 0`. The same md5 is pinned on the unit side
     (`test_recovery_contract_v3.py:170`: `COLUMN_DEFINITION_MD5["brain_sessions"] =
     "bf4c2a47…"`);
   - **M-B** — `expected_artifact_constraints` hardcodes the CHECK's definition at
     **seven** values ⇒ `artifact_constraint_mismatches > 0`;
   - **M-D** — `expected_runtime_user_triggers` is a **closed list of thirteen triggers
     across five tables**, **seven of them on `project_contexts`** (reread on 2026-08-19,
     `v4.sql:533-548`; the seven are identical in production), and
     `runtime_trigger_mismatches` adds an `unexpected_runtime_trigger` counter over
     `expected_runtime_trigger_tables`, which contains `project_contexts` ⇒ every trigger
     added makes the check fail. **Collision between two corrections from the previous
     pass, seen on 2026-08-19.** Point 4 causes this trigger to be born **disabled** to
     survive the upgrade→restart window. Yet the expected-trigger join carries `AND
     observed_user_trigger.tgenabled = 'O'` (`v4.sql:913-918`): a trigger **expected but
     disabled** counts as a mismatch just like a missing one. Both outcomes are therefore
     red — off the list it's *unexpected*, on the list it's *expected and switched off* —
     and **no regeneration order makes the attestation green while the trigger is
     deliberately off**. Consequences to write into M-D's runbook: asset regeneration is
     sequenced **with the activation gesture**, not with the `alembic upgrade`; the
     disabled window is a red-attestation window that is **accepted and dated**; and the
     "DISABLE TRIGGER as an emergency switch" the PLAN's §5 proposes for rollback is not
     neutral — it reopens this red window on every use. These counters — **four** with
     `session_constraint_mismatches` (point *(ii)*) — are expected **at 0**. Without
     updating the assets, the `brain_runtime_032_036_037` check goes `fail` **from the
     plan's very first migration onward and stays that way**. Every head involved
     therefore carries, in its commit, the regeneration of the attestation — **of BOTH v4
     assets** (point *(i)*) — and of its unit fingerprints. **What this plan cannot
     decide on its own**: the attestation also breaks on the **data**, at the very first
     canary — `knowledge_sources` (v4.sql:1083-1090) is the UNION of the **six** capture
     tables (not tickets) and `artifact_source_matches` requires
     `source_record.project_key = session_record.project_key`. An artifact with
     `knowledge_type='ticket'` therefore has no source row at all, and a family capture
     `pk → pk:child` violates the equality — two **permanent**
     `artifact_source_mismatches`, which no canary purge catches up with. It must be
     decided whether the attestation should learn about tickets and the subtree
     predicate: **open question #14**, to be settled before M-B, not during.
3. **Never two heads in flight** (unapplied) at the same time — this plan's own heads
   against each other, AND against 046 (point 5).
4. Production is **measured** (`select version_num from alembic_version`) — never
   copied forward — before opening the next slice; then MCP restart and canary. **Order
   imposed for M-D, and for it alone**: between `alembic upgrade` and the restart, the
   live process still runs pre-M-D code, which writes no history row at all. The
   deferred constraint trigger would therefore abort at COMMIT **every
   `brain_session_end` with `focus_outcome=applied`** and every focus write,
   fail-closed, session left open. M-D is therefore the only head whose migration
   **creates the trigger disabled** (`ALTER TABLE … DISABLE TRIGGER`) and whose runbook
   activates it **after** the MCP restart, in a named operator gesture — otherwise the
   unavailability window is imposed by the plan's own rule, not by an accident. **Price
   of this exception, measured on 2026-08-19**: throughout the disabled window, the
   recovery attestation is red (point 2 — it requires `tgenabled = 'O'` of every
   expected trigger). The window must therefore be **short, dated and announced**, and
   `ops/recovery/` asset regeneration placed in the same gesture as activation, not as
   the upgrade.
5. A **046 (embedding dimension) is planned on this lane, not yet written**: `ls
   alembic/versions/` stops at `045_dream_run_model_width.py` (verified). Ticket
   `c60d023d` itself calls it "not urgent" and lists unplanned work (convergent
   terminal revision, NO-OP DDL on prod, a fail-closed read-only pass 1, killswitch,
   HNSW rebuild, a test harness that doesn't exist yet). An earlier version of this
   document made it a **hard gate** — "M-A doesn't merge until 046 is applied" —, which
   held this project's six heads hostage to a ticket its own author calls not urgent
   and not written. **The gate is lifted**: the risk it invoked (two heads in one
   rollout) is already covered by rule 3, which holds in **both** directions. What
   remains, and is enough: this plan's heads are numbered **relatively** (M-A through
   M-F); whichever of the two series arrives first takes the next number, and the other
   waits for its measured application. The per-head review the pin's docstring requires
   stays due in both directions.
6. **The review the pin requires also applies to OUR heads, not just other people's.**
   The failure message from `test_plan_index_repair_head_pin.py:45-52` is explicit: "Do
   not bump it blindly: review what {head} changes on the tables the repair writes
   (**indexed_plans, indexed_plan_chunks, project_contexts**) — new triggers, new
   constraints or new NOT NULL columns without a default would each change the repair's
   behaviour." **M-D lays down a constraint trigger on `project_contexts`**, one of the
   three. The pin's docstring gives the expected format of this review (043 "TOUCHES
   `indexed_plans` […] so the review couldn't settle for 'it touches nothing'"); M-D's
   commit carries its own, written, showing that the trigger is scoped to `UPDATE OF
   current_focus` and therefore inert for the repair's `UPDATE plan_scan_paths`
   (`plan_index_repair_store.py:294-308` and `:560-584`). An earlier version applied
   this vigilance only to 046.

The migrations directory is `alembic/versions/` (versioned head: 045) — the
proposals disagreed on this path; it has been verified.

---

## 6. Open questions — for the operator alone to settle

None is settled *by this document*; each blocks or shapes a slice of the PLAN.

> **State after the 2026-08-19 framing session — §0 is authoritative.** Three
> questions in this list are **answered** (Q1 by derivation, Q10, Q12), one is **new**
> (Q15) and one has **changed rank** (Q6, promoted to a Phase 0 exit criterion). The
> corresponding entries below carry their answer up top; their original text is kept
> below it, because it documents what was submitted. **Do not read an entry without
> reading its answer box.**
>
> **Update after session 2 of the same day (§0bis): Q2, Q3, Q6, Q9 and Q14 are in turn
> answered, and Q1's corollary is DISSOLVED. PHASE 0 IS UNBLOCKED.** Still open,
> without blocking Phase 0: **Q4, Q5, Q7, Q8, Q11, Q13**.
>
> **Update after the session of 2026-08-20 (§0ter) — decision
> `c5160259-a33a-4dfc-b343-992746604b7a`.** The three questions §0bis had opened *by
> resolving* the earlier ones are **signed**: (a) M-A and M-G in **a single head**;
> (b) **no automatic session under stdio**; (c) `expected_client_key` **removed from
> the connection-resolved path**, kept on the five explicit paths. The **four
> resolutions** from `23bf6088` are **ratified as proposed**, and the trace threshold
> is **signed at 4 h** — as an **eligibility** threshold for the nightly sweep, never
> as a delay (§0ter.5). **THE MINIMAL SLICE IS UNBLOCKED; `SPEC M-G` becomes
> writable.** The list of still-open questions doesn't move: **Q4, Q5, Q7, Q8, Q11,
> Q13** — none blocking.
>
> **Q5 and the 4 h threshold will be answered TOGETHER**, in the same nightly
> statement.

1. > ✅ **ANSWERED on 2026-08-19, by derivation from Q12 = (a) — see §0.2.** For the
   > `agent` nature, `last_observed_at` **is** the liveness signal, by construction and
   > with no slipped covenant, since Q12 has just amended it for this nature. For the
   > `operator` nature, D5 remains pure observation. **The corollary remains OPEN**:
   > "exactly one" vs "all", on 24 `open` sessions out of 29 logged in a project holding
   > at least two (measured 2026-08-19).
   >
   > *Originally submitted text:*

**Observation vs covenant (blocks Phase 4.1 arming)**: is `last_observed_at`,
written by the middleware, an acceptable *observation* (proposed framing), or a
disguised auto-heartbeat, hence a covenant change? Corollary: if several sessions
match (actor, project), stay at "exactly one" (proposed) or accept "all"?
2. > ✅ **ANSWERED on 2026-08-19 (session 2): `from_project`, authorship.** Capture
   > answers "what did this session PRODUCE"; the session that writes the ticket is in
   > `from_project`, and that's the exact analogy of the six existing tables. Measured
   > today: **231 tickets, 187 self** (`from_project = to_project`, where the question
   > doesn't arise) **and 44 cross-project**, where it matters. The mixed batch **stays
   > all-or-nothing** — undisputed. Unblocks M-B, together with Q14.
   >
   > *Originally submitted text:*

**Tickets predicate (Phase 2)**: `from_project` (authorship, proposed) or
`to_project` (destination)? Zero-cost veto before migration M-B. The mixed batch
stays all-or-nothing either way — do you contest this point?
3. > ✅ **ANSWERED on 2026-08-19 (session 2) — and considerably NARROWED before it was.**
   > The two trapped sub-decisions, **(a)** the circle of callers and **(b)** the
   > heartbeat effect and its arming, **dissolve** under Q12 = (a) + automatic opening:
   > an agent session's liveness comes from `last_observed_at`, which moves on every tool
   > call, so the checkpoint stops being special, and an `operator` session is never
   > closed for inactivity. `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT` no longer
   > applies. The checkpoint becomes a **pure judgment object** whose sole job is B7.
   >
   > Answer on what remained, **deliberately mixed**: **(c) storage = the proposal**
   > (append-only, `UNIQUE(session_id, seq)`, `ON CONFLICT DO NOTHING` — a retry replay
   > is idempotent, which the ticket's CAS doesn't give, and agent retries are the norm,
   > invariant C6); **(d) payload shape = THE TICKET** (`progress` + `blocker|null` +
   > `next_step` in **one call**, because three exclusive `kind` values let a `progress`
   > be emitted without ever a `next_step`, and the freshness reader can't tell whether
   > the snapshot is complete). Divergence (d) is **abandoned**, the storage one (c) is
   > **kept**. Detail: §0bis.4.
   >
   > *Originally submitted text:*

**Checkpoint MVP (blocks half of Phase 2)**: explicit product approval of `d04dc588`
— freshness = age of the checkpoint, focus drift exposed separately, never a cause
of staleness. Four named sub-decisions: (a) the **circle of callers** — an explicit
user command only (covenant intact, but adoption bounded by the same human
discipline that produced **24/29** stale — 2026-08-19 measurement, the 2026-08-16
one said 21/23), or spontaneous agent checkpointing in a long autonomous session (a
session mutation outside an explicit command = **covenant change**); (b) the
heartbeat side effect on the command — **and, if (a) stays open, by what mechanism
it gets armed or not**, since the delivered artifact is identical under both answers
(remove the effect, or put it behind `BRAIN_SESSION_CHECKPOINT_HEARTBEAT_EFFECT`
shipped closed: without either of the two, the "autonomous agent" answer is armed by
omission — see D4). **Note that "removing the effect" isn't the neutral option**:
the ticket requires "A real checkpoint refreshes heartbeat atomically; a replay
doesn't", so this removal is a **third** divergence from its MVP; (c) the **STORAGE
divergence vs the ticket's MVP** (append-only + idempotence via `(session_id, seq)`
instead of the `expected_checkpoint_revision` CAS — the ticket's P0 properties kept
in another form); (d) the **PAYLOAD SHAPE divergence**, undeclared until now: the
ticket wants `progress` + `blocker|null` + `next_step` published **together in one
call**, the proposal turns it into three mutually exclusive `kind` values on a
single note. Accepting (d) means giving up the ticket's "one call" criterion;
refusing it means going back to its signature. **Either way, the separate checkpoint
spec the ticket's audit requires remains due — it's added to Phase 0's content.**
4. **Family / sub-projects (Phase 2 flag, Phase 4.6)**: arm parent→child capture? Ship
   `include_descendants` for reads — knowing that under the dream capability scope
   (ARMED in production), the parameter is **refused fail-closed** by design, and that
   widening a bearer to `pk:*` would be a separate security-perimeter decision (D10)?
   And the underlying direction E5: a real hierarchy (documented `parent_key` option,
   predicate first) or an accepted flat design? (This question lives entirely here —
   not in #5, which is the sweep.) **Question reworded on measured state, not on the
   spec's state.** "Who consolidates `red-shrik:agent`?" no longer has the same object:
   `red-shrik:agent` and `red-lab:architect` have been **in the dream pool since
   2026-08-10** and therefore have their own nights (447 of 533 colon artifacts, 84%).
   What remains to be settled, in this order: (a) do we accept that the parent
   **never** sees the child (the pipeline's strict equality), or do we want cross
   consolidation — i.e. `include_descendants`? (b) the **86 artifacts** of the four
   `red-lab:*` keys still outside the pool (`orchestrator` 64, `reviewer` 15,
   `sentinel` 5, `developer` 2): adding them costs exceeding the cap of ten, hence
   `_MAX_POOL` **and** `TimeoutStartSec` together, plus the full
   `MCP_HTTP_DREAM_TOKENS` matrix (fail-closed preflight: otherwise the whole night
   fails); (c) do we eventually want to **merge** `red-shrik:agent` into `red-shrik` —
   which is no longer a nightly-consolidation question but one of rename/merge, hence
   #11?
5. **Sweep (blocks Phase 4.2-3)**: arming order and thresholds (7 days confirmed?
   duration of the dry run?) — proposed criterion: all three signals silent, not just
   one.
6. > ✅ **ANSWERED on 2026-08-19 (session 2): ACCEPTED**, and a trace's unsigned drafts
   > **survive** in a pool awaiting signature, outside any session. Nothing is lost,
   > nothing is attributed without a human gesture. Explicitly ruled out: auto-promotion
   > on auto-close (that would be **E3 for the entire agent half** — a full covenant, not
   > Q6's half-step) and discarding drafts (destroying exactly what the agent nature
   > produces, under an axis that is traceability). **Its technical fragility disappears
   > along the way**: the `(project_key, started_by_actor)` + "exactly one" link becomes
   > an exact link on the connection. What this adds to M-E remains to be specified
   > (§0bis.5).
   >
   > *Reminder of the rank, secured earlier the same day:* the "knowledge traceability"
   > axis had moved Phase 4.4 up right after Phase 2 (§0.3), making Q6 a Phase 0 exit
   > criterion — a criterion now **satisfied**.
   >
   > *Unchanged text:*

**Staged captures (blocks Phase 4.4 — now right after Phase 2)**: is
prepared-and-signed capture an acceptable covenant amendment? And, eventually,
attribution at creation time (E3, closing B3 entirely) — a decision to be made on
the quantified dossier phases 1–4 produce.
7. **Focus guard (B6)**: is the audit + `focus_diff` (recoverability + visibility)
   enough, or do you want a hard content guard, at the price of an arbitrary threshold
   on the one free judgment channel?
8. **Reattribution (Phase 4.5)**: an explicit, journaled right to reattribute
   (`attribution_moves`, design ready), or does orphaning remain the price of proof?
9. > ✅ **SETTLED on 2026-08-19 (session 2) BY MEASUREMENT, not by arbitration:
   > INHERITANCE.** Subagents inherit their carrier's session, with no tag at all,
   > because they share its connection. This is no longer a design preference: **none of
   > the three headers distinguishes a subagent from its carrier** — `X-Brain-Agent`
   > carries the PROJECT, `X-Brain-Session` is dead (B8), `Mcp-Session-Id` carries the
   > CONNECTION — and none ever will, header configuration being per MCP server, not per
   > subagent. Tagging would require an upstream capability that doesn't exist. Detail:
   > §0bis.2.
   >
   > *Originally submitted text:*

**Subagents (shapes the residual extent of B1)**: own session or inheritance from
the carrier (D1 sweep design: "the subagent's activity IS the operator's")? The plan
works under either answer but recommends inheritance.
10. > ✅ **ANSWERED on 2026-08-19 — see §0.** The list **covers it**: no additional
    > irritant, hence no B15. But the **priority is rejected**: the order no longer comes
    > from derived severity, it comes from the **"knowledge traceability"** axis — B3, B4,
    > B5 first. Consequence: resequencing P0 → P1 → P2 → **P4.4** → P3, and promoting Q6
    > to a Phase 0 exit criterion (§0.3).
    >
    > *Originally submitted text:*

**Your pains**: the B list is derived from the evidence; "a fair number of things I
don't like" may contain more. Phase 0 submits the list to you — confirm, prioritize,
complete before any go-ahead.
11. **Project rename/merge**: out of this plan's scope (033's immutability is
    preserved); to be scoped separately if you wish (a tooled option — script +
    `project_aliases` — sketched by A and C).
12. > ✅ **ANSWERED on 2026-08-19: route (a) — two session natures.** See §0.1 (what it
    > requires), D11 (the component) and §0.4 (Q15, which it opens). Three immediate
    > effects: the nature is **declared** at `start`, not detected — B8 therefore doesn't
    > block (a); the **default is `operator`**, forced by C2; and agent session
    > auto-closing runs into the 037 terminal CHECK, hence Q15.
    >
    > *Originally submitted text:*

**Automatic lifecycle — the arbitration you reserved for yourself** (ticket
`2bd14b24`, settled answer: "automatic, not declarative"): which route — (a) two
session natures (agent trace auto-closed without ritual, operator with ritual), (b)
no ritual at all anymore, non-derivable judgment migrated to a dedicated object, or
(c) auto-end with a derived summary (`FocusArg` doctrine objection filed to the
dossier, §3.3)? This plan's accretion is a transitional state compatible with all
three; this choice determines the final target, and nobody but you makes it.
13. **Semantics of `brain_set_project_context` (M-D module, Phase 3)** — *question
    reworded: its quantified motivation was false.* Under D7's discipline, it is
    proposed that an omitted `current_focus` argument **stop erasing** the focus
    (distinguish "omitted" from an "explicit erasure"). **The figure "10 contexts out of
    59 at NULL" does NOT support this proposal and has been removed from its argument**:
    the ten rows are at `focus_revision = 0` and `focus_updated_at IS NULL`, so their
    focus was never written, not erased (2026-08-18 measurement, D7). Zero erasure
    measured in production. The question therefore stands **on reasoning alone**: the
    channel exists (the ON CONFLICT branch rewrites `current_focus` to NULL when the
    argument is omitted — verified in the code), it simply has never been observed
    biting. Is this a defect to fix before it bites, or an accepted upsert semantics ("I
    set the context's full state") better left unchanged under existing clients?
    Zero-cost veto before M-D.
14. > ✅ **ANSWERED on 2026-08-19 (session 2): route (a) — widen.** `knowledge_sources`
    > opens up to tickets, and its project predicate to the subtree. The attestation stays
    > green **and keeps proving what it claims to prove** — routes (b) "document the hole"
    > and (c) "give up" were ruled out: (b) digs a hole in the RESTORE proof, i.e. in the
    > DR story, which is already an open blocker. **Work on BOTH v4 assets**
    > (`brain-v42-v4.sql` and `brain-v42-v4-pgrestore.sql`, held in CTE parity), not one.
    >
    > *Originally submitted text:*

**What the recovery attestation must learn (blocks M-B)**:
`ops/recovery/brain-v42-v4.sql` defines an artifact's legitimacy as the UNION of the
**six** knowledge tables and the equality `source.project_key =
session.project_key`. Capturable tickets (M-B) and family capture `pk → pk:child`
(D3) would each produce **permanent** `artifact_source_mismatches` — the
attestation, whose runbook requires "all statuses are pass", would turn red and stay
that way. Unlike schema fingerprints, this isn't fixed by regenerating an asset:
should we (a) widen `knowledge_sources` to tickets and its project predicate to the
subtree, (b) restrict the check to the six historical types and document the hole,
or (c) give up one of the two widenings? None of these routes is examined elsewhere
in this dossier.
15. > ✅ **ANSWERED on 2026-08-19: route (3) — new terminal state.** A **new** question,
    > asked by no earlier version of this dossier.
    >
    > **The terminal state of a session of agent nature.** The
    > `brain_sessions_terminal_state_valid` CHECK forbids `ended` without a non-empty
    > `summary` **and** `next_focus`, and requires `captured_knowledge_ids = {}` for
    > `abandoned` (`037_session_lifecycle_v4.py:14-91`). A session auto-closed **without
    > ritual** therefore has no terminal state available. Three routes submitted — (1)
    > `abandoned`, at the cost of a terminal snapshot declaring zero captures on the main
    > capture path; (2) a server-synthesized summary, i.e. route (c) of Q12 through the
    > back door, with its C9 objection; (3) **new terminal state**.
    >
    > **Chosen: (3)**, migration **M-G** on the 037 CHECK. Full detail, costs and what
    > remains to be specified: **§0.4**. This is the answer that brings down §1.3's
    > finding and amends C7.

---

*Sources: instruction dossier `docs/design/refonte-projets-sessions/DOSSIER.md`;
tickets `d30cf6e5`, `2bd14b24` (including the settled operator answer from
2026-08-06), `d04dc588`, `7ffe0e8a`, `c60d023d`; spike
`docs/upstream/2026-08-06-claude-otlp-session-join.md` (verdict "JOIN IMPOSSIBLE" —
ticket `2dfbb83d`, closed SHIPPED on 2026-08-16, is not the source of this verdict);
spec `dbb7c5ce`; learnings `7bc821a1`, `367e27ae`, `1c40c36a`; panel judgments
(three lenses) on proposals A/B/C. Code checks from 2026-08-18:
`src/brain_v42/models/project_key.py`; `src/brain_v42/provenance.py:23`
(`MAX_ACTOR_LENGTH = 64`); `src/brain_v42/db/tables.py` (`access_log` with no
project column, `actor String(64)`; CHECK `brain_session_artifacts_type_valid`;
`tickets.from_project`/`to_project` `String(50)`, `created_at`);
`src/brain_v42/repositories/pg_brain_session.py` (`CAPTURE_TABLES` six tables,
`_validate_captures` — ids listed, single aggregated reason —, `abandon_stale` ONE
statement, heartbeat-only predicate; `_apply_focus_if_current:713-714` which sets
`focus_revision=expected_revision + 1`); the **five** `src/` instances of the colon
predicate — `db/project_group_scope.py:24-26`,
`services/project_group_ticket_service.py:129-137` **and `:164-167`**,
`services/proposal_service.py:377-383`, `repositories/pg_project_context.py:202-213`
— and `alembic/versions/036_codex_contract_views.py` (two CTE bodies, **seven** live
views measured); `alembic/versions/001_initial.py:244-247` (`project_contexts`);
**`alembic/versions/032_brain_sessions.py:19-34`**
(`increment_project_focus_revision()` + `project_contexts_focus_revision_trigger`,
re-measured in production today); `src/brain_v42/db/focus_stamp.py` ("six call sites
across three modules", `IS DISTINCT FROM` "so a focus moving to or from NULL
counts") and `repositories/pg_project_context.py:281-290` (an upsert that overwrites
`current_focus`, **with** a trigger bump and `focus_stamp` dating, **without** a
CAS); `repositories/pg_access_log.py:38-113` + `services/decay_flusher.py` +
`config.py:379` (the `access_log` buffer is purged on every flush, 300 s by
default); `services/roadmap_service.py` (the only writer that bumps on unchanged
text); `src/brain_v42/services/brain_service.py` (dream scope, strict equality);
`services/dream_project_scope.py:83-120` (`PROJECT_TOOL_POLICIES` — resolving a
project is per-tool work); `ops/recovery/brain-v42-v4.sql` **and
`ops/recovery/brain-v42-v4-pgrestore.sql`** (`:404-412` `expected_session_indexes`,
a closed list of three indexes checked `:665` and `:687`) +
`tests/unit/test_recovery_contract{,_v2,_v3,_v4}.py`,
`tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` (CTE parity),
`tests/unit/test_recovery_contract_v3.py:164-168,488`
(`SESSION_INDEX_DEFINITION_MD5`),
`tests/integration/db/test_recovery_contract_v4_execution.py:106` (**both** assets
run against a real database) (fingerprints, `table_set` derived from `METADATA`,
head duplicates); `docs/OPERATIONS.md:118`;
`tests/unit/test_documentation_contract.py:25-32,1820-1825`;
`tests/integration/conftest.py:129-155`; `alembic/versions/039_…:17,337-339` (`-x
allow_project_context_trigger_downgrade=yes`); the dream unit's systemd drop-in
`killswitches.conf` (DRY sweep armed and pool at ten, read on 2026-08-18);
production measured 2026-08-18: head `045`; `10/59` `project_contexts` with a NULL
focus, **all at `focus_revision = 0` and never dated**; `access_log` at **0 rows**;
colon mass `red-shrik:agent` 312 / `red-lab:architect` 135 / four remaining
`red-lab:*` 86; `src/brain_v42/mcp/provenance_middleware.py:74-96` (headers only,
never the arguments); `src/brain_v42/maintenance/plan_index_repair_store.py:63` +
`tests/unit/test_plan_index_repair_head_pin.py:45-52`; `alembic/versions/`
(versioned head 045, **no 046**). No commit, no brain write, no DB write, no file
touched outside `docs/design/refonte-projets-sessions/`.*

*2026-08-19 pass — four corrections, two of them about claims introduced by the
previous fix (read-only, no writes):*
1. *`brain_update_project_focus` **is not** the only one that bumps on unchanged text:
   `end`'s CAS (`pg_brain_session.py:713-714`) sets `expected + 1` without comparing
   the text, and the 037 CHECK requires it — it's the normal regime of a session ending
   (D7).*
2. *The `OLD+2` invoked to forbid an application-side bump **does not exist**: the 032
   trigger **assigns**, it doesn't add; both explicit bumps must stay (D7).*
3. *The `AFTER UPDATE OF current_focus` constraint trigger **does not see INSERTs** —
   `pg_project_context.create:51-71` and `get_or_create:273-275`'s INSERT branch write
   a focus at `focus_revision = 0` outside its scope: a named gap, three routes
   proposed, N1 bounded accordingly.*
4. *`ops/recovery/brain-v42-v4.sql:913-918` requires `tgenabled = 'O'` of every
   expected trigger: M-D's trigger, **created disabled** (§5.4), makes the attestation
   red under both asset configurations — window to be dated, regeneration to be placed
   alongside activation (§5.2). The `expected_runtime_user_triggers` list counts
   **thirteen** triggers across five tables (`:533-548`), seven of them on
   `project_contexts`.*
*Also: `d04dc588` reread — "A real checkpoint refreshes heartbeat atomically; a
replay doesn't" ⇒ removing the heartbeat effect would be a third divergence (Q3(b));
suggestion fields renamed `project_uncaptured_since_start(_count)` to match the
predicate; head count corrected (two firm, four conditional). Measurements from
2026-08-19, read-only: head `045`; `10/59` NULL focus, all at revision 0 and never
dated; `access_log` 0 rows; seven `split_part` views; seven user triggers on
`project_contexts`; colon mass 312/135/64/15/5/2 = 533 against `red-shrik` 245.*

*Residue-folding pass, 2026-08-19 (second pass of the day; read-only, no DB write,
no commit). Six corrections, three of them major:*

| What was missing or wrong | What is true | Where |
|---|---|---|
| The v4 attestation was treated as **one** file (`brain-v42-v4.sql`) | There are **two**: `brain-v42-v4-pgrestore.sql` carries the same structures, is run against a real database (`…v4_execution.py:106`) and held in **CTE parity** (`…v4_pgrestore.py:29-33`). "Regenerate `ops/recovery/`" = both | §5.2 *(i)*, §5.1, PLAN §8 |
| Three attestation-breaking mechanisms inventoried | **Four**: `expected_session_indexes` (`v4.sql:404-412`, checked `:665`/`:687`, doubled by `SESSION_INDEX_DEFINITION_MD5`) freezes the CLOSED list of `brain_sessions`'s indexes | §5.2 *(ii)* |
| The word "index" absent from the document | `started_by_actor` is born **without an index** and D5 filters on it on every outermost call; the three real indexes (measured) don't cover it. Decision **to be examined**, cost **to be measured in Phase 0**, not settled here | D1, D5 |
| B8 cited as an established constraint | **Scored on a stale measurement**: spike measured on Claude Code 2.1.220, `claude --version` = **2.1.234**; replay scheduled as a Phase 0 step | §1.2 table, D1 |
| Ambiguity population presented as still to discover | **Partial** measurement already available in `7ffe0e8a` (2026-08-16: `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2); re-measured on 2026-08-19: 24 of the 29 `open` in a project with ≥2 | D6, bound (3) |
| "revisions advance by one step (dossier's CAS 209→210)" | **Corroboration withdrawn**: this CAS is a `brain_session_end`, not `roadmap_service`, and nothing says the focus text actually changed. The proof remains the plpgsql source alone (`032_brain_sessions.py:19-34`) | D7 |
| "18 sweepable ghosts" with no caveat | Re-measured on 2026-08-19: **29 `open`, 21 sweepable >7 days** out of 467 rows | §1.1 |
