# Instruction file — joint PROJECTS + SESSIONS overhaul

**Status: state of play, not a go.** The anchor ticket `d30cf6e5` (2026-08-18) says
explicitly "DO NOT START without explicit operator framing". This file is the
instruction for the workstream for three architects: measured surface, proven pains,
invariants, history, and questions only the operator can settle. All numeric
measurements are dated and **perishable** — re-measure them, never copy them forward.

---

## A. Currently measured surface

### A.1 PROJECTS system — four building blocks, no end-to-end doc

**Block 1 — The format** (`src/brain_v42/models/project_key.py`, single source of truth):

- Canonical regex `^[a-z0-9]+([:-][a-z0-9]+)*$` (kebab-case; `:` accepted as a
  separator on the same footing as `-`).
- Two auto-canonicalized aliases, matched exactly and case-sensitive:
  `brain` and `brain_v42` → `brain-v42` (`_ALIASES`).
- **Write/read asymmetry**: `canonicalize_project_key(value, strict=True)`
  (default, write path) raises `ValueError` with a suggestion on any non-kebab-case
  key; `strict=False` (read path) lets it through as-is — a read with a bad key
  simply returns zero results.
- `None` passes through unchanged (unscoped global knowledge).
- `ProjectKeyCanonicalMixin` applies canonicalization to any Pydantic model
  declaring `project_key`. The session service canonicalizes strictly on `start`
  (`services/brain_session_service.py:101`) and tolerantly on `list` (`:166`).

**Block 2 — The database** (`docs/SCHEMA.md`) — three DISTINCT project surfaces:

1. `projects` (migration 033): registry. PK `project_key VARCHAR(50)`, CHECK regex
   identical to the code, `registry_status` ∈ {claimed, unclaimed, archived},
   `source` ∈ {context, reference, manual}. A context creates a `claimed` entry;
   a plain reference creates `unclaimed`; deleting the context reverts to
   `unclaimed/reference`.
2. `project_aliases` (033): `alias_key VARCHAR(128)` PK → `project_key` FK CASCADE.
   The migration recorded the historical aliases (`brain_v42`→`brain-v42`,
   `auto_discord`→`auto-discord`), normalized the project columns and
   `related_projects` on upgrade, then **triggers** apply the rule to subsequent
   writes. The downgrade does not restore the old spellings.
3. `project_contexts` (the actual operational object): `project_key VARCHAR(50) UNIQUE`,
   `current_focus`, `focus_revision BIGINT` (032, CAS), `focus_updated_at` (040,
   written by `db/focus_stamp` under `IS DISTINCT FROM`, never by a trigger, NULL =
   never measured), `related_projects TEXT[]`, `project_group VARCHAR(50)`, roadmap,
   counters. **After 033, `project_contexts.project_key` is immutable**: renaming
   a project requires an explicit migration, a direct UPDATE fails.

The knowledge tables (`decisions`, `learnings`, `snippets`: `project_key
VARCHAR(50)` nullable; `runbooks`, `adrs`, `indexed_plans`: NOT NULL) carry the key
**with no FK** — consistency rests on the Pydantic boundary plus the 033 triggers
that maintain the registry, the graph identities, and the outbox.

**Block 3 — The "hierarchy" is FLAT.** No parent/child link in the database; the
colon is a naming convention.

**Correction of 2026-08-18 — "a single exception in the whole codebase" was false, and
that undercount propagated.** This file used to state that `db/project_group_scope.py:20-27`
was the only place encoding sub-partition semantics. Recounted by hand with grep,
there are **five in `src/`**:
1. `db/project_group_scope.py:24-26` — `base_key NOT LIKE '%:%' AND project_key LIKE
   base||':%'`;
2. `services/project_group_ticket_service.py:129-137` — inline SQL copy;
3. `services/proposal_service.py:377-383` — inline SQL copy, even though the module
   imports `project_group_scope`;
4. `services/project_group_ticket_service.py:164-167` — **copy in Python**, in the
   same `_lock_participants_scope` method as #2;
5. `repositories/pg_project_context.py:202-213` (`get_keys_by_group`) — a `split_part`
   variant, invisible to a grep on `not_like("%:%")`.
Plus **seven live views** on the database side (measured with:
`select table_name from information_schema.views where table_schema='public' and
view_definition like '%split_part%'`), all coming from migration **036**, born from
two copied CTE bodies (`_RED_KEYS_CTE`, `_BRAIN_RED_KEYS_CTE`) in
`split_part(project_key, ':', 1) <> project_key AND split_part(…) IN red_base`. 024
is not a second live object: 036 replaces its view via `CREATE OR REPLACE`.
**Three distinct formulations of the same predicate, then, not a single exception.**

What remains true, and it is the underlying point: everywhere **else**, it's strict
equality — the dream pipeline filters on `l.project_key = :pk` (spec `dbb7c5ce`, §2 —
"there is no prefix filter anywhere in the pipeline").

**Block 4 — Numbers.** *The ones from spec `dbb7c5ce` (2026-08-08) were re-measured on
2026-08-18 and moved — copying them forward would be exactly the mistake this file
forbids up top.*
- **2026-08-08 (spec)**: corpus of 3,803 artifacts across 54 `project_contexts`, 26
  artifacts at `project_key IS NULL`, zero contexts with a null key; six colon keys in
  their own right; `red-shrik:agent` 280 > `red-shrik` 222; **479 colon artifacts =
  20.2% of the pool's mass**, outside consolidation.
- **2026-08-18 (re-measured)**: corpus of 4,316 artifacts across **59**
  `project_contexts`; `red-shrik:agent` **312** > `red-shrik` **245** (the proposal
  holds); `red-lab:architect` 135, `red-lab:orchestrator` 64, `:reviewer` 15,
  `:sentinel` 5, `:developer` 2 — **533 colon artifacts**.
- **And the remedy started without this file knowing it.** The spec proposed "either
  add the six keys to the pool". The drop-in `killswitches.conf`, read on 2026-08-18,
  carries `BRAIN_DREAM_PROJECT_POOL=…,red-shrik:agent,…,red-lab:architect,…` **since
  the pool opened on 2026-08-10**: two of the six keys have their own night, i.e.
  **447 of the 533 artifacts (84%)**. The residue with no run at all is **86
  artifacts** across four `red-lab:*` keys. What remains fully open for the six,
  however, is **cross** consolidation: since the pipeline filters on strict equality,
  `red-lab`'s night will never see `red-lab:architect`, even if it's in the pool. The
  pool is, moreover, **at its cap of ten**.

### A.2 SESSIONS system — schema (migrations 032 + 037, lifecycle v4 in prod since 2026-07-24)

`brain_sessions`: `project_key` **FK → `project_contexts(project_key)` ON DELETE
RESTRICT** (a session requires an existing context; `start` on an unknown project →
NotFound). `client_key VARCHAR(128)`, `status` ∈ {open, ended, abandoned},
`started_focus` + `started_focus_revision` (snapshot at start), `summary`,
`next_focus`, `captured_knowledge_ids UUID[]` (terminal snapshot, ≤100),
`nothing_to_capture_reason`, `abandonment_reason`, `end_expected_focus_revision`,
`focus_outcome` ∈ {applied, conflict}, `focus_at_end`, `focus_revision_at_end`,
`started_at`, `last_heartbeat_at`, `ended_at`.

- `UNIQUE (project_key, client_key)`: idempotence of the start. Hard consequence:
  **a `client_key` can never be reused after a terminal state**
  (`pg_brain_session.py:112-116`: "use a new client_key").
- **Indexes — measured on 2026-08-19, exactly three** (`pg_indexes` on
  `brain_sessions`): `brain_sessions_pkey` on `id`,
  `uq_brain_sessions_project_client (project_key, client_key)`,
  `idx_brain_sessions_project_status_started (project_key, status, started_at DESC)`.
  None covers an actor — the column doesn't exist yet. This isn't an operational
  footnote: this list is **CLOSED** in the recovery attestation
  (`ops/recovery/brain-v42-v4.sql:404-412`, `expected_session_indexes`, checked at `:665`
  and `:687`, mirrored by `SESSION_INDEX_DEFINITION_MD5` in
  `tests/unit/test_recovery_contract_v3.py:164-168,488`), so that **adding an index
  to `brain_sessions` breaks the attestation** on the same footing as adding a column
  or a trigger. Today's cardinality: 467 rows, 29 `open`.
- CHECK `brain_sessions_terminal_state_valid` (037): a complete state machine in SQL.
  `open` = zero terminal fields; `ended` = non-blank summary + next_focus, non-null
  outcome, arithmetic consistency of the CAS (`applied` ⇒ `focus_revision_at_end =
  end_expected_focus_revision + 1` and `focus_at_end = next_focus`; `conflict` ⇒
  different revisions), and a **strict XOR** between a non-empty ledger and
  `nothing_to_capture_reason`; `abandoned` = reason alone, no end field, empty
  snapshot.
- The Pydantic model (`models/brain_session.py:148-232`) redoes these validations on
  the application side — a double DB/app rail.

`brain_session_artifacts`: **PK `knowledge_id`** = absolute exclusivity (an artifact
belongs to a single session, for life). `knowledge_type` CHECK ∈ {decision, learning,
snippet, runbook, adr, indexed_plan, legacy}. `attributed_knowledge_ids` (a derived
field, not a column) rehydrates the ledger in every result, including for an
abandoned session.

Constants (`models/brain_session.py:13-21`): `MAX_CAPTURED_KNOWLEDGE_IDS = 100`;
`SESSION_STALE_AFTER = 24 h` (a **derived** flag, displayed, changes no status);
`AUTO_STALE_AFTER = 7 d` + `AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"` (the
only server-side path).

### A.3 The seven tools and their exact contracts (`mcp/tools/session_lifecycle_tools.py`, version 4.0)

Argument bounds: `project_key` ≤50; `client_key`/`expected_client_key` ≤128;
`summary`/`next_focus` ≤10,000; `reason` ≤2,000; `knowledge_ids` 1–100; `limit` ≤100.
Every docstring carries "No hook or auto-close may invoke this lifecycle boundary" —
the covenant is written into the tool's contract.

| Tool | Contract | Idempotence / errors |
|---|---|---|
| `brain_session_start(project_key, client_key)` | Strict canonicalization; INSERT `ON CONFLICT DO NOTHING` on (project, client_key); snapshot of focus + revision; best-effort briefing (failure ⇒ placeholder, session stays open) | Replay if the pair exists and is `open`; `ClientKeyConflictError` if terminal |
| `brain_session_resume(session_id, expected_client_key)` | Identity guard (UUID + key pair) BEFORE any mutating read; returns `current_focus`, `current_focus_revision`, briefing, `open_session_count` | Non-open session ⇒ state error |
| `brain_session_capture(session_id, key, knowledge_ids)` | Locks the session FOR UPDATE then the context; `_validate_captures` (`pg_brain_session.py:618-648`): every UUID must exist in ONE of the six `CAPTURE_TABLES` (`:55-62`), **same `project_key`**, **`created_at >= started_at`**; all-or-nothing batch; insert `ON CONFLICT DO NOTHING` + race resolution; refreshes the heartbeat atomically | Exact replay is idempotent, including on a terminated session if all ids already belong to the session; otherwise `StateError` |
| `brain_session_heartbeat(session_id, key)` | Refreshes `last_heartbeat_at` only | Non-open session ⇒ error |
| `brain_session_end(session_id, key, summary, next_focus, expected_focus_revision, nothing_to_capture_reason?)` | Reads the server ledger (no ids on input); XOR ledger/reason; re-validates the captures; focus CAS: current revision ⇒ `applied` + focus written; concurrent ⇒ `conflict`, focus untouched, **session still closes**; freezes `focus_at_end`/`focus_revision_at_end` | Identity/capture/state errors are fail-closed, session stays open; replay of a persisted end is idempotent |
| `brain_session_abandon(session_id, key, reason)` | Closes without touching the focus; preserves the ledger's attributions | Idempotent replay if same reason; otherwise `TerminalConflictError` |
| `brain_session_list(project_key?, status, limit, offset)` | Tolerant canonicalization; open/stale/ended/abandoned/all filters; `stale` = derived filter | Pure read |

### A.4 Peripheral writers

- **Server sweep** (`repositories/pg_brain_session.py:493-560`,
  `maintenance/session_sweep.py`): the only path with no `expected_client_key` guard.
  **A SINGLE statement** UPDATE…WHERE…RETURNING — under READ COMMITTED, the predicate
  is re-evaluated under the row lock, so a heartbeat that commits during the sweep
  pulls its row out (a direct answer to the 2026-08-06 false-death). Shipped closed
  and dry. **STATE CORRECTED — measured on 2026-08-18: DRY is ARMED.** This file used
  to state "never armed in production", copying forward a 2026-08-16 measurement
  (`7ffe0e8a`: zero env, zero `abandonment_reason='auto_stale_7d'`) — the state had
  changed on 2026-08-18 around 20:52. Direct reading of the drop-in
  `brain-v42-dream.service.d/killswitches.conf`, lines 53-63: "SWEEP: armed (DRY) on
  2026-08-18, operator decision — post-crash recovery. 18 sweepable ghost sessions
  measured this day", then `BRAIN_DREAM_SWEEP_ENABLED=true` and
  `BRAIN_DREAM_SWEEP_DRY_RUN=true`. **DRY lists without writing; WET was never
  armed.** The "18" is a quote from the drop-in, not a live measurement:
  **re-measured on 2026-08-19, 29 `open` sessions of which 21 sweepable >7 d and 24
  stale >24 h, out of 467 rows** (`count(*) filter (where status='open')`, the same
  filter on `last_heartbeat_at < now() - interval '7 days'`, likewise at 24 h). Dated
  and **perishable** — replay it, never copy it forward; the ADR and the PLAN quoted
  this 18 with no caveat. A reader of this file alone would have settled question E3
  on a false state.
- **ProvenanceMiddleware** (`mcp/provenance_middleware.py`): reads `x-brain-agent` and
  `x-brain-session` on EVERY tool call, sees the real tool behind
  `brain_call_tool`, with re-entrance (`enter_call`/`is_outermost_call`).
  `access_log.actor` (041) persists the actor. It **observes everything but writes
  nothing** into the lifecycle — that's the core of ticket `7ffe0e8a`.
- **Focus outside a session**: `brain_update_project_focus(project_key, focus,
  expected_focus_revision, feature_status?)` — an atomic CAS batch for focus+roadmap,
  closes no session. `focus_revision` belongs to the **project**, not the session.

---

## B. PROVEN pains

**B1 — The declarative lifecycle does not survive ephemeral subagents.**
Source: ticket `2bd14b24` (red-shrik, 2026-08-06). 39 sessions abandoned in one go
during a manual cleanup (37 `red-writer`, 2 `red-shrik`), all without a summary or a
capture — nearly one per dispatched subagent. "This isn't an incident, it's the
steady-state regime: without cleanup, it happens again in two weeks." **Severity:
critical** — this is the structuring pain of the workstream.

**B2 — `last_heartbeat_at` lies in both directions, the same day.**
Source: ticket `2bd14b24`. False-death: the purge abandoned a LIVE session
(9b6f7e18, mid-workstream) because the heartbeat only moves on an explicit command.
False-life: 39 sessions dead for two weeks looked open. "A signal that's wrong in
both directions isn't to be calibrated, it's to be replaced." **Severity: critical.**
Auto-heartbeat (`7ffe0e8a`) fixes only the first case.

**B3 — Capture and heartbeat only have explicit writers, while the middleware sees everything.**
Source: ticket `7ffe0e8a` + code. 2026-08-16 measurements (perishable): 23 `open`
sessions, 21 stale >24 h, 8 >7 d; since 2026-08-01, **18%** of closed sessions
capture (52/291) and **34%** of artifacts are attributed (226/661). Provenance exists
(X-Brain-Agent, `access_log.actor`) but feeds neither the heartbeat nor the ledger.
**Severity: high** — the very purpose of sessions (traceability of knowledge) is
achieved only a third of the time.

**B4 — The capture ledger rejects by TYPE: tickets are not capturable, and a mixed batch is fail-closed on its worst element.**
Source: anchor ticket `d30cf6e5` (lived on 2026-08-18) + code. `CAPTURE_TABLES`
(`pg_brain_session.py:55-62`) lists six tables; tickets are not among them.
`_validate_captures` (`:618-648`) rejects **the entire batch** with a single
`BrainSessionInputError` the moment one id is invalid. An agent capturing
[learning, ticket] also loses the learning. **Severity: medium**, an ergonomics
issue plus a traceability gap ("ticket" work is never attributable).

**B5 — Rigid capture window: exact same project + `created_at >= started_at`.**
Source: code (`pg_brain_session.py:630-633`). An artifact created under `red-lab:architect`
is not capturable by a `red-lab` session (strict equality); an artifact created
just before `start` (or whose parent session was swept) is orphaned for life —
the exclusive PK forbids any reattribution. **Severity: medium**, worsened by B1
(swept ghosts keep their attributions locked).

**B6 — Focus discipline rests on the agent, nothing server-side.**
Source: ticket `d30cf6e5` (lived the same day). Two parallel sessions on snapshot
rev 209 closed cleanly (CAS applied 209→210), but the rule "one line added,
SHA-256 compared" held only through agent discipline. The server guarantees the
revision arithmetic, **not the content**: a `next_focus` that overwrites everything
still passes the CAS. **Severity: high** — the focus is the only cross-session
memory.

**B7 — No semantic checkpoint; freshness is silence.**
Source: ticket `d04dc588`. Between `start` and `end`, there is no surface to
publish progress/blocker/next step; freshness is derived solely from the
heartbeat's age. 2026-08-02 audit: nothing of the kind in the lifecycle surfaces at
`8c436aa7`; the smallest admissible batch is documentary (spec + product decision +
CAS/replay contract). **Severity: medium.**

**B8 — No client can declare its session: X-Brain-Session is dead.**
Source: ticket `7ffe0e8a` + `docs/upstream/2026-08-06-claude-otlp-session-join.md`
(join impossible). `${CLAUDE_CODE_SESSION_ID}` is expanded from the process env at
MCP config load time → either a literal template or the parent's id;
`normalize_session` → `None` is the NOMINAL case. To be re-measured at every Claude
Code version bump. **Severity: high** for any redesign built on client identity.
**And this measurement is STALE — noted 2026-08-19.** The spike's header reads
"**Measured version: Claude Code 2.1.220**"; `claude --version` returns
**2.1.234** today. Both of its sources themselves declare the measurement
perishable, in the same terms: the spike ("Re-measure at every Claude Code version
bump rather than take this conclusion on faith") and ticket `2bd14b24` ("to be
re-measured at every Claude Code version bump"), with `7ffe0e8a` repeating
"re-measure at every version bump". Fourteen versions have gone by with nobody
replaying the spike, and **no baseline in this workstream measures the session
header**: B8 is therefore a constraint **priced on a stale measurement**, not a
re-verified one. It remains the working hypothesis — the risk is asymmetric, a
revived `X-Brain-Session` would *open* options rather than close them — but any
decision resting on B8 must say it rests on 2.1.220.

**B9 — `client_key` is free-form and unconventioned.**
Source: ticket `2bd14b24` (`codex-task01-schema-20260727`, `codex-desktop`…).
No way to distinguish agent from operator, and the (project, key) uniqueness makes
every post-terminal retry generate a fresh key — mechanical row proliferation.
**Severity: medium.**

**B10 — Projects: the ghost drift already happened once and remains the reference failure mode.**
Source: learnings `7bc821a1` (high) and `367e27ae` (tombstone). 2026-06-27→29:
15 entities under `brain_v42`/`brain`, invisible to hyphen-scoped briefing/search,
**self-perpetuating** (a handoff focus literally taught the wrong key). Fixed by
migrating the 15 entities + canonicalization at the boundary (fix 6e513c9) +
alias/triggers 033. Tombstone `367e27ae` archives the note that taught the mistake.
**Severity: historical (closed), but load-bearing**: any redesign of the format
must preserve the property "the wrong key cannot be persisted".

**B11 — Sub-projects: the heavy half of some projects sits outside any consolidation.**
Source: spec `dbb7c5ce` §2. See the numbers in A.1. Closing the debt means either
adding the six keys to the pool (six more runs), or **introducing a prefix
semantics that exists nowhere** (except `project_group_scope`). "Both are
workstreams, not settings." **Severity: revised to MEDIUM on 2026-08-19** (the
sibling ADR requalified it): the first branch of the remedy is **2/6 executed**
since 2026-08-10 — `red-shrik:agent` and `red-lab:architect` are in the pool, i.e.
447 of the 533 colon artifacts. 86 artifacts with no run at all remain (four
`red-lab:*` keys, pool at its cap of ten) and, entirely open, **cross**
consolidation: a parent in the pool still doesn't see its children (strict
equality). It's that latter half that remains "high" on corpus value; the spec's
number 479, meanwhile, must no longer be cited.

**B12 — Projects system scattered, with no end-to-end doc.**
Source: ticket `d30cf6e5` (measured finding); cross-checked here: the "projects
system" lives across 4 building blocks (code format, 3 tables, colon convention,
dream spec) that no document ties together. This file is the first attempt.
**Severity: medium** (comprehension cost, risk of inconsistency at every
evolution).

---

## C. Invariants to preserve no matter what

1. **The explicitness covenant is an operator choice, not a technical default.**
   `start/list/resume/capture/heartbeat/end/abandon` are explicit commands from the
   user; the sole server-side exception, the `auto_stale_7d` sweep (**shipped**
   closed and dry — but **armed in DRY on 2026-08-18** by operator decision, see A.4:
   do not read this parenthesis as a current state).
   Any proposal that has an agent, a hook, or a client close/open a session is a
   **change of covenant** to be settled by the operator — never slipped quietly into
   a redesign.
   > ⚠️ **AMENDED BY THE OPERATOR ON 2026-08-19 (framing, Q12 = track (a)).** The
   > covenant becomes **two-regime**, by session *nature* declared at `start`:
   > **`operator`** — unchanged to the letter, the seven commands remain explicit;
   > **`agent`** — a tracing session, **auto-closed with no ritual**, with no
   > `heartbeat`, whose liveness comes from server observation (`last_observed_at`).
   > This is not a slip: it's an amendment submitted then signed, and it only holds
   > for the `agent` nature.
   >
   > ⚠️ **AMENDMENT WIDENED BY SESSION 2 THE SAME DAY (ADR §0bis.1): the server no
   > longer only auto-CLOSES, it auto-OPENS.** `start` becomes automatic, keyed on
   > `(project, connection)`. A single explicit and **retroactive** gesture — the
   > *claim* — promotes a tracing session to `operator`. The `auto_stale_7d` sweep is
   > therefore no longer the only other server exception: the automatic opening and
   > closing of tracing sessions are two more, signed off by the operator.
   >
   > **The default FLIPS and becomes `agent`.** An earlier version of this box read
   > "default `operator`, forced by C2", which was correct **as long as `start`
   > stayed explicit**; under automatic opening, that default would spawn a
   > ritual-bearing session per agent call — B1 industrialized. The judgment channel
   > isn't lost for all that: **the claim IS the declared intent to write it**. No
   > claim ⇒ nobody expects a summary ⇒ auto-close destroys nothing.
   >
   > **Central guarantee demanded by the operator**: an `operator` session **is
   > NEVER** closed by the inactivity timeout. Only the 7-day sweep can take it.
   > Tracing sessions are, at **4 h SIGNED on 2026-08-20**, on their own setting,
   > never while a call is in flight (ADR §0bis.3, amended by §0ter.5).
   > **ELIGIBILITY threshold for the nightly sweep, never a closing delay** — real
   > worst-case latency ≈ 28 h.
2. **Fail-closed on endings.** XOR ledger/`nothing_to_capture_reason`; identity,
   capture, or state errors leave the session open.
3. **Identity guard (UUID, `expected_client_key`) before any mutation** — isolation
   between parallel sessions, explicitly NOT an authentication.
4. **Non-blocking CAS on the focus**: a conflict never prevents a valid session from
   closing, and freezes an honest snapshot (`conflict`, `focus_at_end`,
   `focus_revision_at_end`). `focus_revision` belongs to the project.
5. **Exclusivity and immutability of attribution** (PK `knowledge_id`): declared
   provenance, once persisted, cannot be silently overwritten. If the redesign adds
   reattribution, it must be an explicit, logged operation.
6. **Idempotence of replays** (start, exact capture, persisted end, abandon with the
   same reason): agent retries are the norm, not the exception.
7. **Unrepresentable states impossible**: the 037 terminal CHECK + the double
   Pydantic rail. The 037→036 downgrade is fail-closed (refuses to lose open/abandoned
   captures or a `conflict` outcome).
   > ⚠️ **AMENDED BY THE OPERATOR ON 2026-08-19 (framing, Q15 = route (3)).** The
   > *property* is preserved — it is not loosened one notch. What changes is that the
   > state machine gains **one more terminal state**, for the auto-close of `agent`-nature
   > sessions: the current CHECK forbids `ended` without a non-empty `summary` **and**
   > non-empty `next_focus` and requires `captured_knowledge_ids = {}` on `abandoned`
   > (`037_session_lifecycle_v4.py:14-91`), so a ritual-free close today has **no
   > legal state**. This is migration **M-G**, the only one in the workstream that
   > touches the core. It carries: the double Pydantic rail moved **together with**
   > the CHECK and never after, the pin's channel (C10), regenerating **both**
   > `ops/recovery/` v4 assets, and a 037→036 downgrade that must learn the new
   > state or silently lose terminal sessions. **Its content is not yet
   > specified** (ADR §0.4).
8. **Strict canonicalization on write / tolerant on read** for `project_key`,
   aliases in the database, and **immutability of `project_contexts.project_key`**
   (033). The property "ghost drift cannot happen again" must not regress.
9. **The focus holds only non-derivable judgment** (docstring `FocusArg`): measurable
   state is recomputed at briefing time, never copied forward.
10. **HARD migration constraint**: `_REQUIRED_ALEMBIC_HEAD = "045"`
    (`maintenance/plan_index_repair_store.py:63`, guarded by
    `tests/unit/test_plan_index_repair_head_pin.py`) fail-closes the plan-index
    repair in prod at the slightest unapplied head (ticket `c60d023d`: this would be
    the fifth stuck-head incident). **Any new head the redesign proposes must be
    explicitly sequenced with bumping this pin and applying it in production** —
    and a 046 (embedding dimension) is **planned** on this same channel.
    *Correction of 2026-08-18*: "already pending" overstated it — `ls
    alembic/versions/` stops at `045_dream_run_model_width.py`, no 046 exists,
    and `c60d023d` itself qualifies it as "not urgent". So this isn't a gate that
    precedes the redesign, just a second series that must never fly at the same
    time as the first.
    *Second point of the same invariant, not seen until now*: the pin isn't the only
    head guard. `docs/OPERATIONS.md:118` and `tests/unit/test_recovery_contract.py:279`
    also carry the number as a literal, and the `ops/recovery/` attestation
    fingerprints the `brain_sessions` schema, the `brain_session_artifacts` CHECK, the
    **closed** list of `project_contexts` triggers **and the CLOSED list of
    `brain_sessions` indexes** (`expected_session_indexes`, `brain-v42-v4.sql:404-412`,
    checked at `:665` and `:687`) — i.e. exactly the **four** objects a sessions
    redesign touches. *Completed on 2026-08-19: the index list was the fourth, not
    previously catalogued.*
    *Third point, from the same day*: the v4 attestation is **two files**, not one.
    `ops/recovery/brain-v42-v4-pgrestore.sql` — the variant for a database restored via
    `pg_restore`, the one behind the runbook's isolated evidence
    (`docs/PLAN_INDEX_REPAIR_RUNBOOK.md:62,122-123`) — carries the same structures and
    is **live and tested**: `tests/integration/db/test_recovery_contract_v4_execution.py:106`
    executes **both** assets against a real database, and
    `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` enforces **CTE parity**
    between the two. This file, the ADR, and the PLAN named it nowhere
    (`grep -c pgrestore` = 0/0/0): "regenerate `ops/recovery/`" means **both v4
    assets**, or the restore proof is only half-regenerated.
11. **A session requires an existing `project_context`** (FK RESTRICT) — no session
    orphaned from a project.
12. **The sweep stays ONE statement** (re-evaluation under lock): this is the answer
    carved in stone for the false-death; any rewrite must preserve this property.

---

## D. Useful history

- **032**: `brain_sessions` v3 + `focus_revision` + conditioned CAS trigger
  (`IS DISTINCT FROM`). **033**: `projects`/`project_aliases` registry, normalization
  triggers, key immutability, graph/outbox backfill. **037** (prod 2026-07-24, proven
  before the MCP restart + HTTP v4 canaries): lifecycle v4 — persistent provenance,
  focus outcomes, heartbeat; upgrade refuses a UUID attributed to several sessions;
  v3 captures copied as `knowledge_type='legacy'`. **040**: `focus_updated_at`.
  Production **re-measured at `045` on 2026-08-18** (do not copy forward; measure it).
  **A 032 detail not to lose — the ADR and the PLAN had lost it**: that "conditioned
  CAS trigger" has a precise name and effect. `increment_project_focus_revision()` +
  `project_contexts_focus_revision_trigger BEFORE UPDATE OF current_focus ON
  project_contexts FOR EACH ROW`, "`IF NEW.current_focus IS DISTINCT FROM
  OLD.current_focus THEN NEW.focus_revision := OLD.focus_revision + 1`". Consequence:
  **no writer can change the focus text without bumping the revision**, upsert
  `ON CONFLICT DO UPDATE` included. Do not confuse it with **040**, which dates the
  prose (`focus_updated_at`) and which is, itself, written by application code
  (`db/focus_stamp`), never by a trigger.
  **Three clarifications added on 2026-08-19, because the ADR and the PLAN got it
  wrong twice in a row deducing them from memory** — recording them here, at the
  source: (1) the trigger is `BEFORE UPDATE`: it does **not** see INSERTs, so a row
  created with a focus is born at `focus_revision = 0` (column default) with nothing
  firing; (2) it **assigns**, it does not add — a statement that itself sets
  `focus_revision + 1` gets overwritten by that same value, never compounded into
  `OLD+2`; (3) **two writers set the revision explicitly and therefore bump it even
  on UNCHANGED text**, where the trigger stays silent: the CAS in `brain_session_end`
  (`pg_brain_session.py:713-714`, required by CHECK 037: `applied` ⇒
  `focus_revision_at_end = end_expected_focus_revision + 1`) and
  `brain_update_project_focus` (CAS token of a blockers-only batch). These two bumps
  are not redundant with the trigger: they cover the case the trigger does not
  cover.
- **2026-06-27→29**: underscore ghost-project incident (learning `7bc821a1`),
  15 entities migrated, canonicalization at the boundary (6e513c9), CLAUDE.md guard
  (077cbb7), tombstone `367e27ae`.
- **2026-08-06**: the founding evening of the session pains — a live session purged
  (false-death), the 39 ghosts discovered (false-life), 25 manual abandons; tickets
  `2bd14b24` and `7ffe0e8a` opened; operator response recorded: a session exists for
  **knowledge traceability**, the lifecycle must be "automatic, not declarative" —
  with no mechanism chosen.
- **X-Brain-Session spike**: the verdict "JOIN IMPOSSIBLE" comes from the
  2026-08-06 spike, `docs/upstream/2026-08-06-claude-otlp-session-join.md`,
  **measured on Claude Code 2.1.220** — `claude --version` returns **2.1.234** on
  2026-08-19, and the spike demands to be replayed "at every Claude Code version
  bump" (see B8).
  **Correction of 2026-08-18 — false attribution**: this file used to state "ticket
  `2dfbb83d` closed **negative**". Re-read (`brain_ticket_get`), `2dfbb83d` is not the
  spike: it's "Generalize the live workload panel … to all brain clients",
  `status: closed`, closed on 2026-08-16 by a staleness audit **with "POSITIVE PROOF
  OF THE OPPOSITE, at all four layers"** (commit 88bacd5, `metrics/client_activity.py`,
  `POST /v1/client-activity` → 200). Closed **DELIVERED**, then, not negative. The
  mistake came from upstream ticket `7ffe0e8a` and had propagated here then into the
  ADR.
- **2026-08-07/10**: sweep design (D1: the owner = (project, actor), the subagent
  inherits the parent's X-Brain-Agent); sweep shipped closed/dry — **armed in DRY on
  2026-08-18, see A.4**; 9 manual abandons on 2026-08-11.
- **2026-08-10**: multi-project dream pool opened **at ten projects, including TWO
  colon keys** (`red-shrik:agent`, `red-lab:architect`) — something spec `dbb7c5ce`
  still presented as excluded; server scope by (project, phase) armed.
- **2026-08-18 (same day as the anchor ticket)**: REORG switched back to WET and
  sweep armed in DRY, both by operator decision in the `killswitches.conf` drop-in.
- **2026-08-18**: anchor ticket `d30cf6e5`; lived: a mixed capture batch fail-closed
  on the tickets; a clean 209→210 CAS between two parallel sessions, content
  discipline resting on agent-side only; operator announcement "quite a few things I
  don't like", with no detail.

---

## E. Knowledge gaps — for the operator alone to settle

> **2026-08-19 framing sessions: E1, E2, E4, E7 and E10 are settled** (see their
> boxes). An earlier note here said E2 (subagents) "becomes sharper under answer
> (a)": session 2 **settled it**, and by measurement — no header distinguishes a
> subagent from its owner, hence inheritance. What remains open: **E3** (ordering of
> arming the automation), **E5** (sub-projects), **E6** (rename/merge), **E8**
> (focus content guard), and **E9** (reattribution). **The single source for the
> answers is ADR §0 and §0bis.**

1. > ✅ **SETTLED ON 2026-08-19: track (a).** Two session natures — a tracing agent
   > auto-closed with no ritual, an operator with a ritual. The end ritual
   > (`summary` + `next_focus`) **survives for the `operator` nature** and remains
   > the only moment where non-derivable judgment gets written; the `agent` nature
   > has none. Detail,
   > requirements and costs: **ADR §0.1, D11 and §0.4**. This answer opens a new
   > question this file did not ask — the terminal state of a ritual-free session
   > (Q15, settled the same day: a new state, migration M-G).
   >
   > *Original text:*

   **The unsettled question from ticket `2bd14b24`, asked to it and left open**: if
   closing becomes automatic, what becomes of the end ritual (summary + next_focus,
   the only moment where non-derivable judgment gets written)? Three tracks
   presented, none chosen: (a) two session natures — a tracing agent auto-closed
   with no ritual, an operator with a ritual; (b) no ritual at all anymore,
   judgment moved to a dedicated object; (c) auto-end with a server-derived summary
   (caveat: a product of copied-forward measurable state, which the doctrine
   forbids).
2. > ✅ **SETTLED ON 2026-08-19 (session 2) BY MEASUREMENT, not by arbitration:
   > INHERITANCE.** Subagents attach to their owner's session, with no tag at all,
   > because they **share its connection**. This is no longer a design preference but
   > a measured constraint: **none of the three headers distinguishes a subagent from
   > its owner** — `X-Brain-Agent` carries the PROJECT (`${PWD}` reduced to its
   > basename, or the literal `brain-v42` from `.mcp.json`), `X-Brain-Session` is dead
   > (B8), `Mcp-Session-Id` carries the CONNECTION — and none of them will: header
   > configuration is **per MCP server**, not per subagent. Tagging would require an
   > upstream capability that doesn't exist, the same class as B8. The sweep design
   > (D1) was therefore right, and we now know **why**. Detail: ADR §0bis.2.
   >
   > *Original text:*

   **Does a subagent deserve its own session, or does it attach to its parent's?**
   Ticket `2bd14b24` says this choice "conditions everything else". The design
   sweep (D1) leans toward "the subagent's activity IS the operator's", but
   nothing is settled on the session side.
3. **Whether to arm the existing automation** — *question re-asked against the
   measured state, 2026-08-19.* An earlier version of this line read "7-day sweep
   (shipped, never armed)": that's the stale state section A.4 already corrects, and
   leaving it here was the worst possible spot, since it is **this very line** that
   asks the operator to decide. **The sweep has been armed in DRY since 2026-08-18**
   (drop-in `killswitches.conf`: `BRAIN_DREAM_SWEEP_ENABLED=true`,
   `BRAIN_DREAM_SWEEP_DRY_RUN=true`); only **WET** remains to be decided. What
   remains, then: switching the sweep to WET, auto-heartbeat, and auto-capture
   (`7ffe0e8a`, spec ready, explicit covenant change). In what order, and with what
   limits (the ticket lists: N open sessions with no owner — refuse? last active?;
   stdio with no headers — degrade without attributing)?
4. > ✅ **SETTLED ON 2026-08-19 (session 2): YES, capturable, on `from_project`.**
   > Authorship, not destination: the capture answers "what did this session
   > PRODUCE", and the session that writes the ticket is in `from_project` — an
   > exact analogy with the six existing tables. Measured this day: **231 tickets,
   > 187 self** (`from_project = to_project`, where the question has no object)
   > **and 44 cross-project**, where it matters. **A mixed batch stays
   > all-or-nothing** — an uncontested point. Forced consequence: Q14 = route (a),
   > the recovery attestation's `knowledge_sources` widens to include tickets, on
   > **both** v4 assets.
   >
   > *Original text:*

   **Should tickets become capturable** (a 7th ledger type), or is their exclusion
   a design choice to document? And should a mixed batch stay all-or-nothing?
5. **Sub-projects** — *question re-asked against the measured state, 2026-08-19.* It
   used to ask "close the debt on the **479** artifacts some other way? Who
   consolidates `red-shrik:agent`?" against the 2026-08-08 numbers, whereas section
   A.1 re-measured them: **533** colon artifacts, of which **447 (84%)** sit on two
   keys that have been **in the dream pool since 2026-08-10**
   (`red-shrik:agent`, `red-lab:architect`) — the "add the six keys" remedy is
   **2/6 executed**, and "who consolidates `red-shrik:agent`?" already has its
   answer for the nightly half. What's left to settle: (a) introduce a real
   parent/child semantics (prefix or a database link) or generalize
   `project_group` — this is **cross** consolidation, which the pool doesn't give
   (strict equality: `red-lab`'s night doesn't see `red-lab:architect`); (b) bring
   in the **86** artifacts on the four `red-lab:*` keys still outside the pool,
   knowing the pool is **at its cap of ten**; (c) accept the flatness.
6. **Project rename/merge**: the key has been immutable since 033 and "renaming
   requires an explicit migration". Should the redesign ship a tooled rename/merge
   operation (with aliases), or is immutability a deliberate invariant?
7. > ✅ **APPROVED ON 2026-08-19 (session 2), after its trapped half was dissolved.**
   > The `d04dc588` freshness doctrine is picked back up. What changed: the
   > checkpoint **stops being a liveness mechanism** and becomes a **pure judgment
   > object** — under automatic opening, an agent session's liveness comes from
   > `last_observed_at`, and an `operator` session is never closed by inactivity.
   > The checkpoint thus moves back to the right side of the FACT/JUDGMENT divide,
   > and its sole job goes back to being B7. **Form chosen, a mix**: append-only
   > storage `UNIQUE(session_id, seq)` (idempotent replay, C6) but **the ticket's**
   > payload — `progress` + `blocker|null` + `next_step` in ONE call. ADR §0bis.4.
   >
   > *Original text:*

   **Freshness and checkpoint** (`d04dc588`): an explicit product decision is required
   — freshness derived solely from the checkpoint's age, focus drift exposed
   separately and never a cause of staleness. The MVP is specified but gated on
   approval.
8. **Server-side focus discipline** (B6): does it need a server guard (append-only,
   bounded diff, or a structured focus object), at the price of new rigidity on the
   only free judgment channel?
9. **Reattribution**: a swept session carries off its attributions with it (exclusive
   PK). An explicit reattribution right, or is orphaning the price of proof?
10. > ✅ **SUBMITTED AND ANSWERED ON 2026-08-19.** List B **covers it**: no
    > additional irritant, so no B15. **Prioritization, however, is rejected** —
    > the order no longer comes from derived severity but from the
    > **"knowledge traceability"** axis, with B3, B4, B5 leading. Consequence on
    > the PLAN: resequencing to **P0 → P1 → P2 → P4.4 → P3** and promoting Q6 to
    > a Phase 0 exit criterion (ADR §0.3). *A methodological caveat that remains
    > true:* a list derived from tickets captures what got **incidented**, not what
    > chafes day to day; the absence of B15 is an answer, not proof of
    > exhaustiveness.
    >
    > *Original text:*

    **What are the operator's pains?** "Quite a few things I don't like" isn't
    detailed. This file derives the pains from available evidence; list B must be
    submitted to the operator for confirmation, prioritization, and completion —
    there may be un-ticketed irritants that nothing here captures.

---

*Primary sources: tickets `d30cf6e5`, `2bd14b24`, `d04dc588`, `7ffe0e8a`,
`c60d023d`; plan spec `dbb7c5ce`; learnings `7bc821a1`, `367e27ae`; code at the
cited paths (repo state as of 2026-08-18); `docs/SCHEMA.md`; migrations
032/033/036/037/040.*

*Verification pass of 2026-08-19 (read-only, no DB write): head `045`;
seven user triggers on `project_contexts`, including
`project_contexts_focus_revision_trigger` (`pg_get_triggerdef` + `prosrc` re-read);
`10/59` contexts with `current_focus IS NULL`, **all** at `focus_revision = 0` and
`focus_updated_at IS NULL`; `access_log` at 0 rows; seven `public` views containing
`split_part`; colon mass `red-shrik:agent` 312 / `red-lab:architect` 135 /
`red-lab:orchestrator` 64 / `:reviewer` 15 / `:sentinel` 5 / `:developer` 2 = 533, against
`red-shrik` 245 and `red-lab` 184; drop-in `killswitches.conf` (mtime 2026-08-18 20:52):
sweep `ENABLED=true`/`DRY_RUN=true`, pool of ten including two colon keys. Code re-read:
`pg_project_context.py:51-71,202-213,273-281,295-305`; `pg_brain_session.py:713-714`;
`roadmap_service.py:191-196`; `db/focus_stamp.py`; `ops/recovery/brain-v42-v4.sql`
(`:533-556` closed list of thirteen triggers, `:913-918` `tgenabled='O'`, `:1083-1113`
`knowledge_sources` across six tables, **`:404-412` closed list of three indexes checked
at `:665`/`:687`**) **and `ops/recovery/brain-v42-v4-pgrestore.sql`, which carries the
same structures** (`tests/integration/db/test_recovery_contract_v4_execution.py:106`
executes both; `tests/unit/test_recovery_contract_v4_pgrestore.py:29-33` enforces CTE
parity). Three claims in this file were re-asked
against this state: E.3 (sweep), E.5 and B11 (sub-projects), plus the 032 detail in D.*

*Residuals-folding pass, 2026-08-19 (read-only, no DB write, no
commit). Today's measurements, **dated and perishable**:*
- *`select version_num from alembic_version` → **045**.*
- *`brain_sessions`: **29 `open`**, of which **21** at `last_heartbeat_at < now() -
  interval '7 days'` and **24** past 24 h, out of **467** rows — the drop-in's "18
  sweepable ghosts" was dated 2026-08-18 and was picked up with no caveat by ADR
  §1.1 and PLAN §0.*
- *Indexes on `brain_sessions`: exactly **three** (`pg_indexes`), none on an actor
  (A.2).*
- *`open` sessions per project, ≥2: `auto-discord` 8, `brain-v42` 4, `red-arena` 4,
  `datalake-v1`/`red-gift`/`claude-dev-pc`/`red-lab` 2 — **24 of the 29 `open`** sit in
  a project that carries at least two. This is the **ceiling** of the "ambiguous"
  population under the "exactly one" rule: it counts per project, not per (project,
  actor) pair, and can only be refined once `started_by_actor` ships. Ticket
  `7ffe0e8a` already carried the same partial measurement on 2026-08-16
  ("Simultaneous: `auto-discord` 6, `red-arena` 3, `claude-dev-pc`/`red-lab` 2") with
  none of the three documents citing it.*
- *`claude --version` → **2.1.234**, while the spike `docs/upstream/2026-08-06-claude-otlp-session-join.md`
  declares "Measured version: Claude Code 2.1.220" and demands to be replayed at
  every version bump (see B8).*
- *`grep -c pgrestore` across this file's three documents → **0/0/0** before this
  pass, even though `ops/recovery/brain-v42-v4-pgrestore.sql` exists, runs against a
  real database, and is kept in CTE parity with the live variant (invariant 10).*
